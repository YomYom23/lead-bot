# lead-bot/src/lead_bot/call_questions.py
"""The questions Jev is asked about each thing a caller says on the phone.

Every turn asks the same three yes/no hazard questions, plus one pick-one question, "answer",
whose options depend on where the call is (its stage). The options always include the same five
escape hatches (insurance question, price question, other question, repeat, unclear), so an
off-script reply is recognised at any stage instead of being forced into yes or no.

One Jev request per turn, with every question in it: on a phone call each extra round trip is
silence the caller hears.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal

from typesafe_sdk import Choice, Noul

from .jev import Question
from .slots import Slot

# Where a call can be. In order for a new caller with a roof to inspect:
#   reason -> name -> address -> address_confirm -> leak -> insurance -> homeowner -> offer
#   -> text_consent -> anything_else
# address_check replaces address for a caller already on file ("Is this about the roof at ...?").
# message is for a caller who wants something else: what they say is passed to the team.
CallStage = Literal[
    "reason",
    "name",
    "message",
    "address_check",
    "address",
    "address_confirm",
    "leak",
    "insurance",
    "homeowner",
    "offer",
    "text_consent",
    "anything_else",
]

HAZARD_QUESTIONS: dict[str, Question] = {
    # Caught by keywords first (call_session.is_explicit_opt_out); this catches other wordings.
    "opt_out": Noul(
        instructions="In their latest words, the caller asks the business never to call, text or "
        "contact them again, or to be taken off a list."
    ),
    # Not an end to the call: the bot takes a message so a person calls them back.
    "wants_human": Noul(
        instructions="In their latest words, the caller asks to speak with a real person, a human, "
        "or someone from the company, or asks for someone to call them back."
    ),
    "abusive": Noul(instructions="The caller's latest words are abusive, threatening, or sexually explicit."),
}

# The escape hatches, offered at every stage alongside that stage's real answers.
OFF_SCRIPT_OPTIONS: dict[str, str] = {
    "insurance_question": "Asks what insurance will cover or pay, about a deductible, or about filing "
    "or handling an insurance claim",
    "price_question": "Asks what something costs",
    "other_question": "Asks some other question instead of answering",
    "repeat": "Asks the assistant to repeat itself, or says they didn't hear or understand",
    "unclear": "Says something that fits none of the other options",
}

STAGE_OPTIONS: dict[CallStage, dict[str, str]] = {
    "reason": {
        "roof": "Wants a roof inspection, an estimate, or help with a roof problem such as a leak, "
        "storm or hail damage, missing shingles, or a repair",
        "message": "Is calling about something else, such as a job already under way, a bill, a "
        "delivery, or selling something",
    },
    "name": {"gave_name": "Says their name"},
    "address_check": {
        "yes": "Confirms the call is about the roof at that address",
        "no": "Says it's about a different address",
    },
    "address": {"gave_address": "Says the street address of the property"},
    "address_confirm": {
        "yes": "Confirms the address was heard right",
        "no": "Says the address is wrong, or corrects part of it",
    },
    "leak": {
        "yes": "Says water is coming into the house now",
        "no": "Says no water is coming in right now",
        "unsure": "Says they don't know",
    },
    "insurance": {
        # "Probably" counts as yes: the answer only routes the lead, so leaning one way is enough.
        "yes": "Plans to file an insurance claim, probably will, or already has",
        "no": "Isn't filing an insurance claim",
        "unsure": "Hasn't decided or doesn't know",
    },
    "homeowner": {
        "yes": "Owns the home",
        "no": "Doesn't own it, for example a renter, a relative, or a property manager",
    },
    "text_consent": {
        "yes": "Agrees to get a text with the details",
        "no": "Doesn't want a text",
    },
    "anything_else": {
        "no": "Has nothing else: says no, thanks, or goodbye",
        "yes_has_more": "Says yes or that they have a question, but hasn't asked it yet",
    },
}

# Option ids that mean something other than "this slot": a slot id equal to one of these would
# make "the caller picked slot 'none'" look like "the caller picked nothing".
NO_SLOT_OPTION = "none"
RESERVED_OPTION_IDS = frozenset({NO_SLOT_OPTION, *OFF_SCRIPT_OPTIONS})


def build_call_questions(stage: CallStage, offered_slots: Sequence[Slot]) -> dict[str, Question]:
    """All the questions for one turn, keyed by the name each answer comes back under."""
    if stage == "offer":
        clashing_ids = RESERVED_OPTION_IDS & {slot.id for slot in offered_slots}
        if clashing_ids:
            raise ValueError(f"reserved option ids used as slot ids: {sorted(clashing_ids)}")
        # The options ARE the offered times, so Jev can only return a time that was offered,
        # never one it made up. A question about other days ("do you do Saturdays?") must not
        # land on NO_SLOT_OPTION, which hands the caller to a person; it's other_question.
        stage_options = {
            **{slot.id: slot.label for slot in offered_slots},
            NO_SLOT_OPTION: "Turns down all of these times, or says they want a different day or time. "
            "Not a question about which days or times are possible",
        }
    elif stage == "message":
        raise ValueError("the message stage asks Jev nothing: whatever the caller says is the message")
    else:
        stage_options = STAGE_OPTIONS[stage]
    return {
        **HAZARD_QUESTIONS,
        "answer": Choice(
            instructions="Which option best describes how the caller's latest words respond to "
            "what the assistant just said.",
            criteria={**stage_options, **OFF_SCRIPT_OPTIONS},
        ),
    }
