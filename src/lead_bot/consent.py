# lead-bot/src/lead_bot/consent.py
"""The call-consent wording on the lead form, and the evidence kept for each lead.

The FCC ruled in February 2024 that an AI voice is an "artificial voice" under the federal
robocall law. Telemarketing calls with one need the person's prior express written consent
(47 CFR 64.1200(a)(2)). Per (f)(9), that is a signed agreement (an electronic signature counts)
which must:
  - authorize the seller, by name, to call with an artificial voice,
  - name the phone number it covers,
  - disclose that signing isn't a condition of buying anything.
CONSENT_TEMPLATE covers each point; the comments beside it say which.

Have a telemarketing lawyer review the wording before a real client uses it. The research
paper this project follows says the same.

The wording is versioned. Each lead's record stores the exact text they were shown, so a later
change can never alter what an earlier lead agreed to. Change CONSENT_VERSION with any edit to
the wording.
"""

from __future__ import annotations

from dataclasses import dataclass
from string import Template

CONSENT_VERSION = "call-consent-2026-10-02"

CONSENT_TEMPLATE = Template(
    # "$business_name may call me": names the seller.
    # "at the phone number I entered above": the number covered.
    # "artificial (AI-generated) voice" and "automated technology": the call types covered.
    # "not a condition of buying anything": the required disclosure.
    # Transcription and the opt-out are disclosed here too, so nothing on the call surprises them.
    "I agree that $business_name may call me at the phone number I entered above, including "
    "calls placed with automated technology that use an artificial (AI-generated) voice, to "
    "discuss roofing services and schedule an inspection. Calls may be transcribed. Agreeing is "
    "not a condition of buying anything. I can stop the calls at any time by pressing 9 or "
    'saying "stop" during a call.'
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
