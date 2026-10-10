# lead-bot/src/lead_bot/turn_data.py
"""The data one homeowner text is handled with, and the changes handling it produces.

store.py loads a ConversationState and saves a TurnOutcome; conversation.py turns one into the
other. They're defined here, apart from both, so neither module has to import the other.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from .slots import Slot
from .text_questions import Stage

# following_up    -> texts are scheduled; the homeowner hasn't replied yet
# in_conversation -> the homeowner has replied and the bot is working through the questions
# booked          -> an inspection was booked (the bot still answers their texts)
# handed_off      -> a person at the business takes over (they asked, or the bot couldn't help)
# unreached       -> no reply to any text; the owner was alerted
# owner_alerted   -> no automated texts at all (no consent, or opted out before); owner alerted
# duplicate       -> the number already had a conversation open when this form arrived
# opted_out       -> the number went on the do-not-call list
# campaign_waiting -> an old lead from a reactivation list, waiting for its turn under the
#                     campaign's daily limit (Store.start_campaign_contacts)
LeadStatus = Literal[
    "following_up",
    "in_conversation",
    "booked",
    "handed_off",
    "unreached",
    "owner_alerted",
    "duplicate",
    "opted_out",
    "campaign_waiting",
]


@dataclass(frozen=True)
class ConversationState:
    """A conversation as saved between texts: the lead, where it is, and what's known so far."""

    lead_id: int
    phone: str
    full_name: str
    address: str
    problem: str
    stage: Stage
    offered_slots: tuple[Slot, ...]
    unclear_in_a_row: int
    slot_taken_count: int
    booked_label: str | None
    answers: Mapping[str, str]


@dataclass(frozen=True)
class WriterOutcome:
    """What happened to one off-script question. The scenario report counts these."""

    question: str
    draft: str | None  # None when the writer failed before producing one
    problems: tuple[str, ...]  # failed guard checks, or "writer_failed" / "guard_unavailable"
    seconds: float  # writer plus guard

    @property
    def sent(self) -> bool:
        return self.draft is not None and not self.problems


@dataclass(frozen=True)
class NewBooking:
    starts_at: datetime
    label: str


@dataclass
class TurnOutcome:
    """Everything one homeowner text changes.

    Store.save_turn applies all of it in one transaction, so a crash can't leave a booking
    without its confirmation text, or a reply sent from a stage that was never saved.
    """

    stage: Stage
    offered_slots: tuple[Slot, ...]
    unclear_in_a_row: int
    slot_taken_count: int
    replies: list[str] = field(default_factory=list)
    # A text confirming we'll stop texting (an opt-out in their own words, or a wrong number).
    # It is the one text sent after the number goes on the do-not-call list.
    stop_confirmation: str | None = None
    # Set to put the number on the do-not-call list; the text says why.
    do_not_contact_source: str | None = None
    answers: dict[str, str] = field(default_factory=dict)
    owner_alerts: list[str] = field(default_factory=list)
    # What the bot decided but didn't text, e.g. a writer draft the guard blocked.
    notes: list[str] = field(default_factory=list)
    booking: NewBooking | None = None
    close_conversation: bool = False  # the bot stops answering; a person takes it from here
    result: str | None = None  # how the bot's part ended, once it has
    lead_status: LeadStatus | None = None
    writer_outcomes: list[WriterOutcome] = field(default_factory=list)  # for reports; not saved
