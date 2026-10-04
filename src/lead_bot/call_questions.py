# lead-bot/src/lead_bot/call_questions.py
"""The questions Jev is asked about each thing the homeowner says on a call.

Every turn asks the same four yes/no hazard questions, plus one pick-one question, "answer",
whose options depend on where the call is (its stage). The options always include the same
five escape hatches (insurance question, price question, other question, repeat, unclear), so
an off-script reply is recognised at any stage instead of being forced into yes or no.

One Jev call per turn, with every question in it: on a phone call each extra round trip is
silence the homeowner hears.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal

from typesafe_sdk import Choice, Noul

from .jev import Question
from .slots import Slot

# Where a call can be, in order. "offer" is when inspection times have been read out.
Stage = Literal["permission", "address", "leak", "insurance", "homeowner", "offer", "anything_else"]

HAZARD_QUESTIONS: dict[str, Question] = {
    # Opt-out is also caught by keywords before Jev is asked (call_session.py); this catches the
    # other ways of saying it, like "I'm not interested, quit bugging me".
    "opt_out": Noul(
        instructions="In their latest words, the homeowner asks to stop the calls, not to be called "
        "again, to be taken off the call list, or to stop this call."
    ),
    "wants_human": Noul(
        instructions="In their latest words, the homeowner asks to speak with a real person, a human, "
        "or someone from the company."
    ),
    "wrong_person": Noul(
        instructions="In their latest words, the person says this is a wrong number, or that they "
        "never asked for a roof inspection."
    ),
    "abusive": Noul(
        instructions="The homeowner's latest words are abusive, threatening, or sexually explicit."
    ),
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

STAGE_OPTIONS: dict[Stage, dict[str, str]] = {
    "permission": {
        "yes": "Agrees to talk now, or goes straight to answering",
        "no_time": "Says it's a bad time or they're busy, or asks to be called later",
    },
    "address": {
        "yes": "Confirms the inspection is for that address",
        "no": "Says it's a different address, or corrects it",
    },
    "leak": {
        "yes": "Says water is coming into the house now",
        "no": "Says no water is coming in right now",
        "unsure": "Says they don't know",
    },
    "insurance": {
        # "Probably" counts as yes: the answer only routes the lead, so leaning one way is enough.
        # Without it, "probably, yes" fell between yes and unsure and was heard as unclear.
        "yes": "Plans to file an insurance claim, probably will, or already has",
        "no": "Isn't filing an insurance claim",
        "unsure": "Hasn't decided or doesn't know",
    },
    "homeowner": {
        "yes": "Owns the home",
        "no": "Doesn't own it, for example a renter, a relative, or a property manager",
    },
    "anything_else": {
        "no": "Has nothing else: says no, thanks, or goodbye",
        "yes_has_more": "Says yes or that they have a question, but hasn't asked it yet",
    },
}

# Option ids that mean something other than "this slot": a slot id equal to one of these would
# make "the homeowner picked slot 'none'" look like "the homeowner picked nothing".
NO_SLOT_OPTION = "none"
RESERVED_OPTION_IDS = frozenset({NO_SLOT_OPTION, *OFF_SCRIPT_OPTIONS})


def build_call_questions(stage: Stage, offered_slots: Sequence[Slot]) -> dict[str, Question]:
    """All the questions for one turn, keyed by the name each answer comes back under."""
    if stage == "offer":
        clashing_ids = RESERVED_OPTION_IDS & {slot.id for slot in offered_slots}
        if clashing_ids:
            raise ValueError(f"reserved option ids used as slot ids: {sorted(clashing_ids)}")
        # The options ARE the offered times, so Jev can only return a time that was offered,
        # never one it made up.
        # A question about other days ("do you do Saturdays?") must not land here: this option
        # ends the call, and the phase 3 scenario run showed it hanging up on that question.
        # Questions go to other_question, which the writer answers before re-offering.
        stage_options = {
            **{slot.id: slot.label for slot in offered_slots},
            NO_SLOT_OPTION: "Turns down all of these times, or says they want a different day or time. "
            "Not a question about which days or times are possible",
        }
    else:
        stage_options = STAGE_OPTIONS[stage]
    return {
        **HAZARD_QUESTIONS,
        "answer": Choice(
            instructions="Which option best describes how the homeowner's latest words respond to "
            "what the assistant just said.",
            criteria={**stage_options, **OFF_SCRIPT_OPTIONS},
        ),
    }
