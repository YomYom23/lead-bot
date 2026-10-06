# lead-bot/src/lead_bot/text_script.py
"""Every text the bot sends that is fixed in advance, and the facts the writer may use.

The only texts not written here are the writer's answers to off-script questions, and guard.py
checks each of those before it is sent.

What the first text carries, and why. Twilio's toll-free verification guide lists what a
program's opening confirmation must include: the business name, message frequency, "Msg & data
rates may apply", help, and opt-out instructions. The first text has all of them, and says it's
an automated assistant. Every reminder repeats "Reply STOP to opt out."

All fixed texts use only GSM-7 characters (GSM7_CHARACTERS below). One curly apostrophe or emoji
makes the phone network send the whole text as UCS-2, which fits 70 characters per segment
instead of 160, so the same text is billed as two or three. The tests check every fixed text.
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from datetime import time
from typing import Literal

from .leads import pretty_phone
from .settings import BusinessSettings
from .slots import BookingRules, Slot
from .text_questions import Stage
from .timing import MAX_TEXT_ATTEMPTS

# ---- characters and length ----

# The GSM 03.38 basic character set, then its extension table (each extension character
# takes two of the 160).
GSM7_BASIC_CHARACTERS = frozenset(
    "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?"
    "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà"
)
GSM7_EXTENSION_CHARACTERS = frozenset("^{}\\[~]|€")
GSM7_CHARACTERS = GSM7_BASIC_CHARACTERS | GSM7_EXTENSION_CHARACTERS

# Phone keyboards and models produce these. Each has a plain GSM-7 equivalent.
PLAIN_PUNCTUATION = str.maketrans(
    {
        "\u2018": "'",  # ' left single quote
        "\u2019": "'",  # ' right single quote, the iPhone apostrophe
        "\u201c": '"',
        "\u201d": '"',
        "\u2013": "-",  # en dash
        "\u2014": "-",  # em dash
        "\u2026": "...",
        "\u00a0": " ",  # non-breaking space
    }
)


def plain_punctuation(text: str) -> str:
    """Curly quotes, long dashes and the like replaced with their plain GSM-7 versions."""
    return text.translate(PLAIN_PUNCTUATION)


def is_gsm7(text: str) -> bool:
    return all(character in GSM7_CHARACTERS for character in text)


def segment_count(text: str) -> int:
    """How many SMS segments a text is billed as.

    GSM-7: 160 characters in one segment, or 153 per segment when split (the rest of each
    segment holds the joining header). UCS-2: 70 in one, 67 per segment when split.
    """
    if is_gsm7(text):
        length = sum(2 if character in GSM7_EXTENSION_CHARACTERS else 1 for character in text)
        single, per_part = 160, 153
    else:
        length, single, per_part = len(text), 70, 67
    return 1 if length <= single else math.ceil(length / per_part)


# ---- keywords Twilio handles itself ----

# From Twilio's help article "Twilio support for opt-out keywords (SMS STOP filtering)", read
# 2026-10-04:
#   - A text that is exactly one of these words makes Twilio block the number from further
#     texts, send its own "unsubscribed" confirmation, and pass the text on to our webhook. Only
#     single-word texts count: "STOP PLEASE" doesn't trigger it.
#   - On a toll-free number, START and UNSTOP undo it (YES doesn't, so "yes" stays an answer).
#   - HELP and INFO get Twilio's own reply and are NOT passed on to us by default.
TWILIO_STOP_KEYWORDS = frozenset(
    {"stop", "stopall", "unsubscribe", "cancel", "end", "quit", "revoke", "optout"}
)
TWILIO_START_KEYWORDS = frozenset({"start", "unstop"})
TWILIO_HELP_KEYWORDS = frozenset({"help", "info"})

TwilioKeyword = Literal["stop", "start", "help"]


def twilio_keyword(text: str) -> TwilioKeyword | None:
    """Which of Twilio's keywords the whole text is, or None.

    Exact single words only, like Twilio. "Stop." with a full stop may or may not trigger Twilio's
    block (its article doesn't say), so it isn't treated as Twilio's: conversation.py catches it
    as an opt-out in the homeowner's own words and sends our own confirmation.
    """
    word = text.strip().lower()
    if word in TWILIO_STOP_KEYWORDS:
        return "stop"
    if word in TWILIO_START_KEYWORDS:
        return "start"
    if word in TWILIO_HELP_KEYWORDS:
        return "help"
    return None


# ---- the opening texts ----

DISCLOSURES = "Msg frequency varies. Msg & data rates may apply. Reply HELP for help, STOP to opt out."


def first_name(full_name: str) -> str:
    words = plain_punctuation(full_name).split()
    return words[0] if words else "there"


def first_text(settings: BusinessSettings, full_name: str, address: str) -> str:
    return (
        f"Hi {first_name(full_name)}, this is the automated assistant for {settings.business_name}, "
        f"texting about the free roof inspection you requested. {stage_question('address', address, ())} "
        f"{DISCLOSURES}"
    )


def reminder_text(settings: BusinessSettings, full_name: str, address: str, is_last: bool) -> str:
    text = (
        f"Hi {first_name(full_name)}, {settings.business_name} here again about your free roof "
        f"inspection. {stage_question('address', address, ())}"
    )
    if is_last:
        text += (
            " This is our last text about it. You can also call us at "
            f"{pretty_phone(settings.callback_phone)}."
        )
    return text + " Reply STOP to opt out."


def opening_text(settings: BusinessSettings, full_name: str, address: str, attempt: int) -> str:
    """The opening text for this attempt: 1 is the first text, 2 and up are reminders."""
    if not 1 <= attempt <= MAX_TEXT_ATTEMPTS:
        raise ValueError(f"there is no opening text number {attempt}; the last is {MAX_TEXT_ATTEMPTS}")
    if attempt == 1:
        return first_text(settings, full_name, address)
    return reminder_text(settings, full_name, address, is_last=attempt == MAX_TEXT_ATTEMPTS)


# ---- the questions ----


def stage_question(stage: Stage, address: str, offered_slots: Sequence[Slot]) -> str:
    """The question each stage asks. 'booked' asks nothing."""
    match stage:
        case "address":
            return f"Is the inspection for {plain_punctuation(address)}?"
        case "leak":
            return "Is water coming into the house right now?"
        case "insurance":
            return "Are you planning to file an insurance claim for this?"
        case "homeowner":
            return "Are you the owner of the home?"
        case "offer":
            times = "\n".join(f"{number}) {slot.label}" for number, slot in enumerate(offered_slots, start=1))
            # Not "the earliest times": after "none of those work" the bot offers later ones.
            return (
                f"These inspection times are open:\n{times}\n"
                "Reply with the number of the one that works best."
            )
        case "booked":
            return ""


# ---- fixed answers and endings ----

# Fixed answers to the questions the bot must not answer itself.
#   Insurance: in roofing, talking coverage, deductibles or "we'll handle your claim" counts as
#   acting as an unlicensed public adjuster in several states. The bot only points to the insurer.
#   Price: no price list exists to quote from, and a made-up number is the worst thing it could say.
INSURANCE_LINE = (
    "I can't help with insurance questions. Your insurance company is the right one to ask, "
    "and our inspector will document what they find."
)
PRICE_LINE = (
    "I can't give prices by text. The inspector will give you a written estimate after the inspection."
)
QUESTION_FOR_TEAM_LINE = "I'll pass that question to our team so the inspector can answer it."

ADDRESS_WRONG_PREFIX = "Thanks, our team will confirm the address with you."
URGENT_LEAK_PREFIX = "I'm sorry to hear that. I'll flag this as urgent for our team."
SLOT_TAKEN_PREFIX = "Sorry, that time was just taken."
NOT_CAUGHT_PREFIX = "Sorry, I didn't catch that."
CONFUSION_PREFIX = "Sorry for the confusion."
LATER_TEXT = "No problem. Reply whenever it suits you."
WRONG_PERSON_TEXT = "Sorry about that. We won't text this number again."
NOT_HOMEOWNER_TEXT = (
    "Thanks. The inspection needs the owner's OK, so someone from our team will follow up with you."
)
NO_TIME_SUITED_TEXT = "No problem. Someone from our team will contact you to find a time that works."
# Starts the one re-offer after "none of those work". conversation.py looks for it in the
# history to know the re-offer already happened, so it must stay the start of that text.
LATER_TIMES_PREFIX = "Here are some later times."
CHANGE_BOOKING_TEXT = "No problem. Someone from our team will contact you to change it."
CALENDAR_DOWN_TEXT = (
    "I can't reach our scheduling system right now. Someone from our team will contact you to set "
    "up the inspection."
)
CALENDAR_FULL_TEXT = (
    "Our inspection calendar is full right now, so someone from our team will contact you to set up a time."
)
SLOTS_KEPT_FILLING_TEXT = (
    "That time was just taken too. Someone from our team will contact you to set a time."
)


def booked_text(slot_label: str, address: str) -> str:
    return (
        f"You're all set for {slot_label}. The inspector will come to {plain_punctuation(address)}. "
        "If anything changes, just text us here."
    )


def wants_human_text(settings: BusinessSettings) -> str:
    return f"Of course. Someone from {settings.business_name} will contact you shortly."


def not_understood_text(settings: BusinessSettings) -> str:
    return (
        "Sorry, I'm having trouble understanding. "
        f"Someone from {settings.business_name} will contact you shortly."
    )


def technical_trouble_text(settings: BusinessSettings) -> str:
    return (
        "Sorry, I'm having technical trouble. "
        f"Someone from {settings.business_name} will contact you shortly."
    )


def opt_out_confirmation(settings: BusinessSettings) -> str:
    """Sent when someone opts out in their own words. A bare STOP gets Twilio's confirmation instead.

    The FCC allows exactly one text after an opt-out, confirming it, with no marketing in it
    (47 CFR 64.1200(a)(12)). Sent within five minutes it's presumed covered by the original
    consent, so worker.py drops this text if it's older than that when its turn to send comes.
    """
    return f"{settings.business_name}: You're opted out and won't get any more texts from us."


def help_text(settings: BusinessSettings) -> str:
    """What a HELP reply should say. Twilio answers HELP itself; phase 2 sets its reply to this."""
    return (
        f"{settings.business_name}: automated texts about your roof inspection request. For help call "
        f"{pretty_phone(settings.callback_phone)}. Msg frequency varies. Msg & data rates may apply. "
        "Reply STOP to opt out."
    )


# ---- what the writer may say ----

# What the writer may say about the business beyond what conversation_facts() works out from
# settings. PLACEHOLDER, like the inspection hours in settings.py, until a real roofer gives
# theirs. Every sentence here can be texted to a homeowner, so nothing goes in that the business
# hasn't approved.
PLACEHOLDER_BUSINESS_FACTS = (
    "The inspection is free, and agreeing to it doesn't commit the homeowner to buying anything.",
    "The inspector documents what they find on the roof.",
    "After the inspection the homeowner gets a written estimate for any work that's needed.",
    "The company does roof repairs and roof replacements for homes.",
)

WEEKDAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def _clock_time(clock_time: time) -> str:
    """time(8, 0) -> '8 AM', time(17, 30) -> '5:30 PM'. Same style as the slot labels."""
    hour_on_12_hour_clock = clock_time.hour % 12 or 12
    minutes = f":{clock_time.minute:02d}" if clock_time.minute else ""
    return f"{hour_on_12_hour_clock}{minutes} {'AM' if clock_time.hour < 12 else 'PM'}"


def _join_words(words: Sequence[str], last_joiner: str) -> str:
    """['a'] -> 'a', ['a', 'b'] -> 'a and b', ['a', 'b', 'c'] -> 'a, b and c'."""
    if len(words) <= 1:
        return "".join(words)
    return f"{', '.join(words[:-1])} {last_joiner} {words[-1]}"


def inspection_days_text(rules: BookingRules) -> str:
    """'Monday to Friday' for a run of days, 'Monday, Wednesday and Friday' otherwise."""
    weekdays = sorted(rules.open_weekdays)
    names = [WEEKDAY_NAMES[weekday] for weekday in weekdays]
    is_a_run = weekdays == list(range(weekdays[0], weekdays[-1] + 1))
    if is_a_run and len(names) > 2:
        return f"{names[0]} to {names[-1]}"
    return _join_words(names, "and")


def closed_days_text(rules: BookingRules) -> str | None:
    """'Saturday or Sunday': the days with no inspections. None when every day is open."""
    closed = [name for weekday, name in enumerate(WEEKDAY_NAMES) if weekday not in rules.open_weekdays]
    return _join_words(closed, "or") if closed else None


def inspection_hours_text(settings: BusinessSettings) -> str:
    rules = settings.inspection_rules
    return (
        f"We do inspections {inspection_days_text(rules)}, "
        f"{_clock_time(rules.opening_time)} to {_clock_time(rules.closing_time)}."
    )


def later_times_text(settings: BusinessSettings) -> str:
    """Before the re-offered times. The offer question itself follows it."""
    return f"{LATER_TIMES_PREFIX} {inspection_hours_text(settings)}"


def off_topic_text(settings: BusinessSettings) -> str:
    """For flirting and small talk: friendly, no team promise, and no alert to the owner."""
    return (
        f"I'm an automated assistant for {settings.business_name}, so I can only help with your "
        "roof inspection."
    )


def conversation_facts(settings: BusinessSettings, address: str, booked_label: str | None) -> str:
    """Everything the writer is allowed to state in this conversation, one fact per line.

    The guard's unsupported_fact check compares each draft against exactly this text, so a
    fact missing here gets the draft blocked rather than invented.
    """
    rules = settings.inspection_rules
    inspection_minutes = int(rules.appointment_length.total_seconds() // 60)
    booking = (
        f"An inspection is booked for {booked_label}."
        if booked_label is not None
        else "No inspection has been booked yet."
    )
    closed_days = closed_days_text(rules)
    return "\n".join(
        [
            f"Business name: {settings.business_name}.",
            "This assistant is an artificial intelligence that texts for the business.",
            f"The business's phone number: {pretty_phone(settings.callback_phone)}.",
            f"Inspections take about {inspection_minutes} minutes.",
            f"Inspections can be booked {inspection_days_text(rules)}, between "
            f"{_clock_time(rules.opening_time)} and {_clock_time(rules.closing_time)}, "
            f"up to {rules.days_ahead} days ahead.",
            # Said outright: given only "Monday to Friday", the writer answered "do you do
            # Saturdays?" with NOT_IN_FACTS instead of working out the no.
            *([f"There are no inspections on {closed_days}."] if closed_days else []),
            f"The inspection address on file: {address}.",
            booking,
            *PLACEHOLDER_BUSINESS_FACTS,
        ]
    )


# A reply that is just a number: how the offer text asks for a pick. "2", "#2", "2.", "number 2".
PICKED_NUMBER = re.compile(r"^\W*(?:number|option|#)?\s*(\d)\W*$", re.IGNORECASE)


def picked_number(text: str, offered_count: int) -> int | None:
    """The 1-based number picked, if the whole text is a number from 1 to offered_count."""
    match = PICKED_NUMBER.match(text)
    if match is None:
        return None
    number = int(match.group(1))
    return number if 1 <= number <= offered_count else None
