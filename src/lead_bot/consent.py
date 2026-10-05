# lead-bot/src/lead_bot/consent.py
"""The text-message consent wording on the lead form, and the evidence kept for each lead.

The FCC treats a text as a "call" under the federal robocall law (the TCPA). Texts sent with
automated technology to sell something are treated here as needing the person's prior express
written consent (47 CFR 64.1200(a)(2)), the cautious reading. Per (f)(9), that is a signed
agreement (an electronic signature counts) which must:
  - authorize the seller, by name, to send the messages,
  - name the phone number it covers,
  - disclose that signing isn't a condition of buying anything.

Twilio's toll-free verification reviews the opt-in too. Its opt-in guide expects the form to say
what the texts are about, that frequency varies, that message and data rates may apply, and how
to get help or opt out (HELP and STOP). CONSENT_TEMPLATE covers each point; the comments beside
it say which.

Have a telemarketing lawyer review the wording before a real client uses it. The research paper
this project follows says the same.

The wording is versioned. Each lead's record stores the exact text they were shown, so a later
change can never alter what an earlier lead agreed to. Change CONSENT_VERSION with any edit to
the wording.
"""

from __future__ import annotations

from dataclasses import dataclass
from string import Template

CONSENT_VERSION = "text-consent-2026-10-04"

CONSENT_TEMPLATE = Template(
    # "$business_name may send": names the seller.
    # "to the mobile number I entered above": the number covered.
    # "automated text messages, including messages written with the help of AI": what kind.
    # "about my roof inspection request and scheduling": what the texts are for.
    # "not a condition of buying anything": the required disclosure.
    # Frequency, rates, HELP and STOP: what Twilio's opt-in review looks for.
    "I agree that $business_name may send automated text messages, including messages written "
    "with the help of AI, to the mobile number I entered above about my roof inspection request "
    "and scheduling. Agreeing is not a condition of buying anything. Message frequency varies. "
    "Msg & data rates may apply. Reply HELP for help or STOP to opt out at any time."
)


def consent_text(business_name: str) -> str:
    return CONSENT_TEMPLATE.substitute(business_name=business_name)


@dataclass(frozen=True)
class ConsentEvidence:
    """What to keep so the consent can be proven later: who, what they saw, when, from where.

    recorded_at is added by the store, in the same transaction that creates the lead.
    """

    given: bool  # True only if the box was ticked on the current version of the form
    version: str
    text_shown: str
    ip_address: str
    forwarded_for: str | None  # set by a proxy in front of the server, if there is one
    user_agent: str
    source: str  # the URL the form was submitted to
