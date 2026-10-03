# lead-bot/tests/test_leads.py
# Form parsing, phone numbers, and the rule for what happens to a new lead.
from __future__ import annotations

import pytest

from lead_bot.leads import InvalidLeadForm, decide_intake, normalize_us_phone, parse_lead_form, pretty_phone


@pytest.mark.parametrize(
    "typed",
    ["(602) 555-0142", "602-555-0142", "602.555.0142", "6025550142", "+1 602 555 0142", "1 (602) 555-0142"],
)
def test_common_ways_of_typing_a_us_number(typed: str) -> None:
    assert normalize_us_phone(typed) == "+16025550142"


@pytest.mark.parametrize(
    "typed",
    [
        "",
        "555-0142",  # too short
        "602 555 0142 ext 12",  # letters: refused, not stripped into a different number
        "+44 20 7946 0958",  # not a US number
        "(102) 555-0142",  # area codes never start with 1
        "(602) 155-0142",  # nor do exchanges
        "٦٠٢٥٥٥٠١٤٢",  # Arabic-Indic digits: \d would accept these, [0-9] doesn't
    ],
)
def test_numbers_that_cannot_be_called_are_refused(typed: str) -> None:
    with pytest.raises(InvalidLeadForm):
        normalize_us_phone(typed)


def test_pretty_phone() -> None:
    assert pretty_phone("+16025550142") == "(602) 555-0142"


def test_the_form_is_cleaned_up() -> None:
    lead = parse_lead_form("  Dana   Ruiz ", "602-555-0142", " 12 Saguaro  Ln ", "storm_damage")
    assert (lead.full_name, lead.phone, lead.address, lead.problem) == (
        "Dana Ruiz",
        "+16025550142",
        "12 Saguaro Ln",
        "storm_damage",
    )


@pytest.mark.parametrize(
    ("full_name", "address", "problem", "message"),
    [
        ("   ", "12 Saguaro Ln", "repair", "name"),
        ("Dana", "", "repair", "address"),
        ("Dana", "12 Saguaro Ln", "", "roof"),
        ("Dana", "12 Saguaro Ln", "gutters", "roof"),  # not one of the drop-down values
        ("D" * 101, "12 Saguaro Ln", "repair", "name"),
    ],
)
def test_each_bad_field_gets_its_own_message(
    full_name: str, address: str, problem: str, message: str
) -> None:
    with pytest.raises(InvalidLeadForm, match=message):
        parse_lead_form(full_name, "602-555-0142", address, problem)


# ---- intake rule ----


def test_consent_and_nothing_in_the_way_means_a_call() -> None:
    assert decide_intake(consent_given=True, on_do_not_call=False, already_in_follow_up=False) == "call"


def test_no_consent_means_no_ai_call() -> None:
    assert decide_intake(False, False, False) == "no_call_consent"


def test_do_not_call_beats_a_fresh_tick() -> None:
    # Ticking the box again doesn't silently undo an earlier opt-out; the owner decides.
    assert decide_intake(consent_given=True, on_do_not_call=True, already_in_follow_up=False) == (
        "previously_opted_out"
    )


def test_do_not_call_beats_a_running_follow_up() -> None:
    assert decide_intake(True, True, True) == "previously_opted_out"


def test_a_second_form_for_a_number_already_being_called_starts_nothing() -> None:
    assert decide_intake(True, False, True) == "already_in_follow_up"
