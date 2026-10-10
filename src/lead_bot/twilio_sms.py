# lead-bot/src/lead_bot/twilio_sms.py
"""Everything that talks to Twilio: its settings, checking that a webhook really came from
Twilio, and sending a text.

The server (server.py) uses `request_is_from_twilio` on the two webhooks Twilio calls:
  - incoming texts from homeowners  -> POST /twilio/incoming
  - delivery reports for our texts  -> POST /twilio/status
The worker (worker.py --send) uses `TwilioSender` to send what's queued.

Signatures are checked with Twilio's own validator from its Python SDK, as Twilio's docs ask:
Twilio adds webhook parameters without notice, and the SDK signs whatever set arrives.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from twilio.base.exceptions import TwilioRestException
from twilio.http.http_client import TwilioHttpClient
from twilio.request_validator import RequestValidator
from twilio.rest import Client

from .leads import InvalidLeadForm, normalize_us_phone
from .worker import SenderCredentialsRejected, SendRefused

logger = logging.getLogger(__name__)

TWILIO_VARIABLES = ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_PHONE_NUMBER")

# The paths Twilio calls on our server. Both go into the Twilio Console / the send request, and
# the signature check must use the same public URL Twilio used.
INCOMING_PATH = "/twilio/incoming"
STATUS_PATH = "/twilio/status"
# "A call comes in" on the number: a call the business's line forwarded (call_session.py).
VOICE_PATH = "/twilio/voice/incoming"

# A send that hangs is given up after this long. The text is then marked 'unknown' (it may have
# gone), so a long wait gains nothing.
SEND_TIMEOUT_SECONDS = 10.0

# Twilio error codes, from its error dictionary (twilio.com/docs/api/errors).
RECIPIENT_OPTED_OUT = 21610  # the person texted STOP to our number; Twilio blocks every send
# 401/403 (error 20003, authentication failed): every send would fail the same way, so the
# worker stops instead of marking each queued text failed.
CREDENTIAL_ERROR_STATUSES = (401, 403)


def status_callback_url(public_base_url: str) -> str | None:
    """Where Twilio reports each text's delivery, or None when the server isn't public https.

    Twilio can't reach 127.0.0.1, and a plain-http callback would send the signature in the clear.
    """
    if not public_base_url.startswith("https://"):
        return None
    return public_base_url.rstrip("/") + STATUS_PATH


@dataclass(frozen=True)
class TwilioSettings:
    account_sid: str
    auth_token: str
    phone_number: str  # the number texts are sent from, E.164

    @classmethod
    def from_environment(cls) -> TwilioSettings | None:
        """The Twilio settings, or None when none are set (the dry-run phase).

        All three or none: a half-filled .env stops here with every missing name, rather than
        failing on the first text.
        """
        values = {name: os.environ.get(name, "").strip() for name in TWILIO_VARIABLES}
        missing = [name for name, value in values.items() if not value]
        if len(missing) == len(TWILIO_VARIABLES):
            return None
        if missing:
            raise RuntimeError(f"Set all three Twilio values in .env, or none. Missing: {', '.join(missing)}")
        try:
            phone_number = normalize_us_phone(values["TWILIO_PHONE_NUMBER"])
        except InvalidLeadForm:
            raise RuntimeError(
                "TWILIO_PHONE_NUMBER must be a 10-digit US number, like +18665550100"
            ) from None
        if not values["TWILIO_ACCOUNT_SID"].startswith("AC"):
            # An API key SID (SK...) looks similar but can't be used with the auth token.
            raise RuntimeError("TWILIO_ACCOUNT_SID must be the Account SID, which starts with AC")
        return cls(values["TWILIO_ACCOUNT_SID"], values["TWILIO_AUTH_TOKEN"], phone_number)


def request_is_from_twilio(
    auth_token: str, signed_url: str, form: Mapping[str, Any], signature: str | None
) -> bool:
    """Whether the X-Twilio-Signature header matches this request.

    `signed_url` must be the public URL Twilio called (PUBLIC_BASE_URL plus the path), not the
    URL this server sees: behind Tailscale Funnel or a proxy, the server sees http://127.0.0.1,
    and Twilio signed the https:// address. `form` is the posted form as Starlette gives it,
    which the validator reads with getlist().
    """
    if not signature or not auth_token:
        # An empty token would make the expected signature computable by anyone.
        return False
    return bool(RequestValidator(auth_token).validate(signed_url, form, signature))


class TwilioSender:
    """Sends one text through Twilio's Messages API. Implements worker.Sender."""

    def __init__(
        self,
        settings: TwilioSettings,
        status_callback_url: str | None,
        http_client: Any = None,  # tests pass a fake; Twilio's own client otherwise
    ) -> None:
        self._from = settings.phone_number
        self._status_callback_url = status_callback_url
        self._client = Client(
            settings.account_sid,
            settings.auth_token,
            # max_retries=0: a retried request could send the same text twice. A failed send is
            # marked 'unknown' and left for the owner instead (worker.send_next_queued).
            http_client=http_client
            if http_client is not None
            else TwilioHttpClient(timeout=SEND_TIMEOUT_SECONDS, max_retries=0),
        )

    def send(self, phone: str, body: str) -> str:
        """Send the text and return Twilio's MessageSid.

        Raises SendRefused when Twilio certainly didn't take it, SenderCredentialsRejected when
        our credentials are wrong, and lets anything else through: then it isn't known whether
        the text went, and the worker marks it 'unknown' rather than risk sending it twice.
        """
        options: dict[str, Any] = {"to": phone, "from_": self._from, "body": body}
        if self._status_callback_url is not None:
            options["status_callback"] = self._status_callback_url
        try:
            message = self._client.messages.create(**options)
        except TwilioRestException as error:
            if error.status in CREDENTIAL_ERROR_STATUSES:
                raise SenderCredentialsRejected(f"Twilio error {error.code}: {error.msg}") from error
            if 400 <= error.status < 500:
                # A 4xx means Twilio rejected the request and created no message: not sent.
                raise SendRefused(
                    f"Twilio error {error.code}: {error.msg}",
                    recipient_opted_out=error.code == RECIPIENT_OPTED_OUT,
                ) from error
            raise  # a 5xx: Twilio may have created the message before failing
        return str(message.sid)
