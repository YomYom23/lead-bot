# lead-bot/src/lead_bot/leads.py
"""What a lead is, how the form's fields become one, and what happens to a new lead.

The form handler (server.py) passes the raw fields to parse_lead_form. That returns a NewLead,
or raises InvalidLeadForm with a message to show the homeowner. decide_intake then picks one
of four outcomes, and Store.add_lead (store.py) applies it in a single database transaction.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

ProblemType = Literal["active_leak", "storm_damage", "repair", "replacement", "not_sure"]

# The form's drop-down, in this order. Also the full list of values the form may send: anything
# else is refused rather than stored.
PROBLEM_LABELS: dict[ProblemType, str] = {
    "active_leak": "Roof is leaking now",
    "storm_damage": "Storm or hail damage",
    "repair": "Repair",
    "replacement": "Full replacement",
    "not_sure": "Not sure",
}

MAX_NAME_LENGTH = 100
MAX_ADDRESS_LENGTH = 200

# Characters people type in a phone number. Anything else (letters, "ext. 12") makes the number
# invalid instead of being dropped, because dropping characters can leave a different number
# that still looks valid, and the bot would call a stranger. [0-9] rather than \d: in Python, \d
# also matches non-ASCII digits such as Arabic-Indic numerals.
PHONE_CHARACTERS = re.compile(r"[0-9 ()+.\-]+")

# What happens to a new lead:
#   call                  -> the AI calls them (they consented, and nothing stops it)
#   previously_opted_out  -> no AI call; the owner is alerted to decide whether to call personally
#   no_call_consent       -> no AI call; the owner is alerted to call personally
#   already_in_follow_up  -> the same number is already being called; this lead is just recorded
IntakeDecision = Literal["call", "previously_opted_out", "no_call_consent", "already_in_follow_up"]


class InvalidLeadForm(ValueError):
    """A form field the homeowner needs to fix. The message is shown to them as-is."""


@dataclass(frozen=True)
class NewLead:
    full_name: str
    phone: str  # E.164, e.g. +16025550142: the format Twilio requires
    address: str
    problem: ProblemType


def parse_lead_form(full_name: str, phone: str, address: str, problem: str) -> NewLead:
    """Turn raw form fields into a NewLead, or raise InvalidLeadForm saying what to fix."""
    cleaned_name = " ".join(full_name.split())  # trims, and folds runs of spaces into one
    cleaned_address = " ".join(address.split())
    if not cleaned_name:
        raise InvalidLeadForm("Please enter your name.")
    if len(cleaned_name) > MAX_NAME_LENGTH:
        raise InvalidLeadForm(f"Please keep your name under {MAX_NAME_LENGTH} characters.")
    if not cleaned_address:
        raise InvalidLeadForm("Please enter the property address.")
    if len(cleaned_address) > MAX_ADDRESS_LENGTH:
        raise InvalidLeadForm(f"Please keep the address under {MAX_ADDRESS_LENGTH} characters.")
    return NewLead(
        full_name=cleaned_name,
        phone=normalize_us_phone(phone),
        address=cleaned_address,
        problem=_problem_from_form(problem),
    )


def normalize_us_phone(raw: str) -> str:
    """'(602) 555-0142' -> '+16025550142'. Raises InvalidLeadForm for anything else."""
    text = raw.strip()
    invalid = InvalidLeadForm("Please enter a 10-digit US phone number, like (602) 555-0142.")
    if not PHONE_CHARACTERS.fullmatch(text):
        raise invalid
    digits = re.sub(r"[^0-9]", "", text)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]  # the +1 country code, typed or not
    if len(digits) != 10:
        raise invalid
    # North American numbering: neither the area code nor the exchange starts with 0 or 1.
    if digits[0] in "01" or digits[3] in "01":
        raise invalid
    return "+1" + digits


def pretty_phone(e164_phone: str) -> str:
    """'+16025550142' -> '(602) 555-0142', for pages the homeowner reads."""
    digits = e164_phone.removeprefix("+1")
    return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"


def decide_intake(consent_given: bool, on_do_not_call: bool, already_in_follow_up: bool) -> IntakeDecision:
    """Pure rule for a new lead. The order of the checks is the policy:

    1. A number on the do-not-call list is never called by the AI again, even if this form has
       the box ticked. Whether new consent overrides an earlier opt-out is a question for a
       lawyer; until then the owner decides and calls personally.
    2. A number already being called gets no second sequence: a homeowner who submits twice
       would otherwise get every call twice.
    3. Without the ticked consent box there is no AI call at all.
    """
    if on_do_not_call:
        return "previously_opted_out"
    if already_in_follow_up:
        return "already_in_follow_up"
    if not consent_given:
        return "no_call_consent"
    return "call"


def _problem_from_form(value: str) -> ProblemType:
    # A loop rather than `value in PROBLEM_LABELS` plus a cast: this way the type checker can
    # see that only one of the five known values is ever returned.
    for known_problem in PROBLEM_LABELS:
        if known_problem == value:
            return known_problem
    raise InvalidLeadForm("Please choose what's going on with the roof.")
