# lead-bot/tests/test_text_script.py
# The fixed texts and the small parsers beside them: the first text's required parts, SMS
# alphabet and segment counts, Twilio's keywords, and picking a time by number.
from __future__ import annotations

import pytest
from text_fakes import SETTINGS

from lead_bot.conversation import is_explicit_opt_out
from lead_bot.leads import pretty_phone
from lead_bot.text_script import (
    help_text,
    is_gsm7,
    opening_text,
    opt_out_confirmation,
    picked_number,
    plain_punctuation,
    segment_count,
    twilio_keyword,
)
from lead_bot.timing import MAX_TEXT_ATTEMPTS

ADDRESS = "12 Saguaro Ln, Mesa AZ"


# ---- the opening texts ----


def test_the_first_text_identifies_the_business_the_bot_and_the_way_out() -> None:
    text = opening_text(SETTINGS, "Dana Ruiz", ADDRESS, attempt=1)
    assert "Desert Ridge Roofing LLC" in text
    assert "automated assistant" in text  # it says it's a bot
    assert ADDRESS in text
    assert "Msg & data rates may apply" in text
    assert "STOP to opt out" in text


@pytest.mark.parametrize("attempt", range(1, MAX_TEXT_ATTEMPTS + 1))
def test_every_opening_text_names_the_business_and_is_plain_sms(attempt: int) -> None:
    text = opening_text(SETTINGS, "Dana Ruiz", ADDRESS, attempt)
    assert "Desert Ridge Roofing" in text
    assert is_gsm7(text), text
    assert segment_count(text) <= 3


def test_reminders_say_how_to_stop_too() -> None:
    for attempt in range(2, MAX_TEXT_ATTEMPTS + 1):
        assert "STOP" in opening_text(SETTINGS, "Dana Ruiz", ADDRESS, attempt)


def test_there_is_no_opening_text_past_the_last_attempt() -> None:
    with pytest.raises(ValueError):
        opening_text(SETTINGS, "Dana Ruiz", ADDRESS, MAX_TEXT_ATTEMPTS + 1)


def test_the_opt_out_confirmation_says_no_more_texts_and_nothing_else() -> None:
    text = opt_out_confirmation(SETTINGS)
    assert "Desert Ridge Roofing LLC" in text
    assert "any more texts" in text
    assert "?" not in text  # a question would invite more conversation after an opt-out
    assert segment_count(text) == 1


def test_the_help_text_gives_the_phone_number_and_the_way_out() -> None:
    text = help_text(SETTINGS)
    assert pretty_phone(SETTINGS.callback_phone) in text
    assert "STOP" in text
    assert segment_count(text) <= 2  # HELP is rare, so two segments is fine; this catches bloat


def test_only_the_last_reminder_gives_the_phone_number() -> None:
    phone = pretty_phone(SETTINGS.callback_phone)
    texts = [
        opening_text(SETTINGS, "Dana Ruiz", ADDRESS, attempt) for attempt in range(1, MAX_TEXT_ATTEMPTS + 1)
    ]
    assert [phone in text for text in texts] == [False] * (MAX_TEXT_ATTEMPTS - 1) + [True]


# ---- the SMS alphabet ----


def test_curly_punctuation_becomes_plain() -> None:
    assert plain_punctuation("It\u2019s \u201cfine\u201d \u2014 really\u2026") == 'It\'s "fine" - really...'


def test_one_curly_apostrophe_turns_a_one_segment_text_into_three() -> None:
    plain = "x" * 159 + "'"
    assert segment_count(plain) == 1
    assert segment_count(plain.replace("'", "\u2019")) == 3  # 160 UCS-2 characters, 67 per segment


def test_gsm7_extension_characters_count_twice() -> None:
    assert segment_count("x" * 159 + "[") == 2  # 159 + 2 = 161


# ---- Twilio's keywords ----


@pytest.mark.parametrize(
    ("text", "keyword"),
    [
        ("STOP", "stop"),
        ("  stop ", "stop"),
        ("Unsubscribe", "stop"),
        ("START", "start"),
        ("HELP", "help"),
        ("stop please", None),
        ("Stop.", None),  # not certainly Twilio's: the bot confirms this one itself
        ("yes", None),
    ],
)
def test_only_an_exact_single_keyword_is_left_to_twilio(text: str, keyword: str | None) -> None:
    assert twilio_keyword(text) == keyword


# ---- opt-outs in the homeowner's own words ----


@pytest.mark.parametrize(
    "text",
    [
        "Stop.",
        "stop texting me",
        "Please stop texting me",
        "STOP PLEASE",
        "unsubscribe me",
        "remove me from your list",
        "don't text me again",
        "take me off your list",
    ],
)
def test_a_clear_opt_out_is_caught_without_a_model(text: str) -> None:
    assert is_explicit_opt_out(text)


@pytest.mark.parametrize(
    "text",
    [
        "yes and it won't stop dripping",
        "the leak stopped after the rain",
        "can you stop by tuesday?",
        "no",
        "I'm not interested in a new roof, just a repair",
    ],
)
def test_stop_in_an_ordinary_answer_is_not_an_opt_out(text: str) -> None:
    assert not is_explicit_opt_out(text)


# ---- picking a time by number ----


@pytest.mark.parametrize(
    ("text", "number"),
    [("2", 2), (" 2 ", 2), ("#2", 2), ("2.", 2), ("number 3", 3), ("Option 1", 1), ("4", None), ("0", None)],
)
def test_a_bare_number_picks_that_offered_time(text: str, number: int | None) -> None:
    assert picked_number(text, offered_count=3) == number


def test_a_number_inside_a_sentence_is_left_to_jev() -> None:
    assert picked_number("2 kids will be home", offered_count=3) is None
