# lead-bot/src/lead_bot/text_questions.py
"""The questions Jev is asked about each text the homeowner sends.

Every text gets the same four yes/no hazard questions, plus one pick-one question, "answer",
whose options depend on where the conversation is (its stage). The options always include the
same escape hatches (insurance question, price question, other question, repeat, later,
unclear), so an off-script text is recognised at any stage instead of being forced into yes or no.

One Jev request per text, with every question in it.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal

from typesafe_sdk import Choice, Noul

from .jev import Question
from .slots import Slot

# Where a conversation can be, in order. The first text asks the address question, so a new
# conversation starts at "address". "offer" is when inspection times have been texted. "booked"
# is after the booking: the bot still answers questions, but asks nothing more.
Stage = Literal["address", "leak", "insurance", "homeowner", "offer", "booked"]
STAGES: tuple[Stage, ...] = ("address", "leak", "insurance", "homeowner", "offer", "booked")

HAZARD_QUESTIONS: dict[str, Question] = {
    # STOP and the clear ways of saying it are caught by keywords before Jev is asked
    # (conversation.is_explicit_opt_out). This catches the rest, like "not interested, quit texting me".
    "opt_out": Noul(
        instructions="In their latest text, the homeowner asks to stop the texts, not to be contacted "
        "again, or to be taken off the list, or says they are not interested."
    ),
    "wants_human": Noul(
        instructions="In their latest text, the homeowner asks to talk to a real person, a human, or "
        "someone from the company, or asks the company to phone them instead of texting. Asking "
        "for the company's phone number is not this: that is a question the bot can answer."
    ),
    "wrong_person": Noul(
        instructions="In their latest text, the person says this is a wrong number, or that they "
        "never asked for a roof inspection."
    ),
    "abusive": Noul(
        instructions="The homeowner's latest text is abusive, threatening, or sexually explicit."
    ),
}

# The escape hatches, offered at every stage alongside that stage's real answers.
OFF_SCRIPT_OPTIONS: dict[str, str] = {
    "insurance_question": "Asks what insurance will cover or pay, about a deductible, or about filing "
    "or handling an insurance claim",
    "price_question": "Asks what something costs",
    "other_question": "Asks some other question instead of answering",
    # Flirting and small talk get a fixed redirect, not a writer answer passed to the team.
    # Sexually explicit or threatening texts are the abusive hazard, not this.
    "off_topic": "Flirts, jokes, makes small talk, or asks the assistant something personal that has "
    "nothing to do with the roof, the inspection, or the company. Asking which company this is or "
    "whether this is an automated assistant is other_question, not this",
    "repeat": "Asks what the assistant meant, or says they don't understand the question",
    "later": "Says they're busy right now and will answer later",
    "unclear": "Says something that fits none of the other options",
}

STAGE_OPTIONS: dict[Stage, dict[str, str]] = {
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
        # Without it, "probably, yes" fell between yes and unsure and was read as unclear.
        "yes": "Plans to file an insurance claim, probably will, or already has",
        "no": "Isn't filing an insurance claim",
        "unsure": "Hasn't decided or doesn't know",
    },
    "homeowner": {
        "yes": "Owns the home",
        "no": "Doesn't own it, for example a renter, a relative, or a property manager",
    },
    "booked": {
        "thanks": "Says thanks, okay, or goodbye, or otherwise needs nothing more",
        "change_booking": "Asks to change, move, or cancel the inspection",
    },
}

# Option ids that mean something other than "this slot": a slot id equal to one of these would
# make "the homeowner picked slot 'none'" look like "the homeowner picked nothing".
NO_SLOT_OPTION = "none"
RESERVED_OPTION_IDS = frozenset({NO_SLOT_OPTION, *OFF_SCRIPT_OPTIONS})


def build_questions(stage: Stage, offered_slots: Sequence[Slot]) -> dict[str, Question]:
    """All the questions for one text, keyed by the name each answer comes back under."""
    if stage == "offer":
        clashing_ids = RESERVED_OPTION_IDS & {slot.id for slot in offered_slots}
        if clashing_ids:
            raise ValueError(f"reserved option ids used as slot ids: {sorted(clashing_ids)}")
        # The options ARE the offered times, so Jev can only return a time that was offered,
        # never one it made up. They carry the numbers the offer text showed, so "the 2nd one"
        # can be matched too. (A reply that is just a number never reaches Jev.)
        # A question about other days ("do you do Saturdays?") is other_question, answered from
        # the facts. Asking for a time that wasn't offered is NO_SLOT_OPTION: the bot then
        # states its days and hours and offers later times once (conversation._no_time_suited).
        stage_options = {
            **{slot.id: f"{number}) {slot.label}" for number, slot in enumerate(offered_slots, start=1)},
            NO_SLOT_OPTION: "Turns down all of these times, or asks for a different day or time, like "
            "'Saturday at noon'. Not a question about which days or times are possible",
        }
    else:
        stage_options = STAGE_OPTIONS[stage]
    return {
        **HAZARD_QUESTIONS,
        "answer": Choice(
            instructions="Which option best describes how the homeowner's latest text responds to "
            "what the assistant last texted.",
            criteria={**stage_options, **OFF_SCRIPT_OPTIONS},
        ),
    }
