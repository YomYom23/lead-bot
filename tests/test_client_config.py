# lead-bot/tests/test_client_config.py
"""client.toml loading: the example file loads, and each kind of mistake stops startup by name."""

from __future__ import annotations

from datetime import time, timedelta
from pathlib import Path

import pytest
from conftest import PHOENIX

from lead_bot.client_config import (
    EXAMPLE_CLIENT_CONFIG_PATH,
    InvalidClientConfig,
    load_client_config,
)

GOOD_BUSINESS = '[business]\nfacts = ["The inspection is free."]\n'
GOOD_INSPECTION = """[inspection]
open_weekdays = [0, 1, 2, 3, 4]
opening_time = 08:00:00
closing_time = 17:00:00
appointment_minutes = 60
gap_after_appointment_minutes = 30
minimum_notice_minutes = 120
days_ahead = 14
"""


def write_config(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "client.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_example_file_loads_into_the_placeholder_hours() -> None:
    config = load_client_config(EXAMPLE_CLIENT_CONFIG_PATH)
    rules = config.inspection_hours.booking_rules(PHOENIX)
    assert rules.open_weekdays == frozenset({0, 1, 2, 3, 4})
    assert (rules.opening_time, rules.closing_time) == (time(8, 0), time(17, 0))
    assert rules.appointment_length == timedelta(hours=1)
    assert rules.gap_after_appointment == timedelta(minutes=30)
    assert rules.minimum_notice == timedelta(hours=2)
    assert rules.days_ahead == 14
    assert rules.timezone == PHOENIX
    assert len(config.business_facts) == 4


def test_missing_file_names_the_example_to_copy(tmp_path: Path) -> None:
    with pytest.raises(InvalidClientConfig, match="client.example.toml"):
        load_client_config(tmp_path / "client.toml")


def test_python_code_in_the_file_is_rejected_as_invalid_toml(tmp_path: Path) -> None:
    path = write_config(tmp_path, GOOD_BUSINESS + "\ndef rules(timezone):\n    return 1\n")
    with pytest.raises(InvalidClientConfig, match="isn't valid TOML"):
        load_client_config(path)


@pytest.mark.parametrize(
    ("replace", "with_text", "message"),
    [
        ("days_ahead = 14\n", "", "missing days_ahead"),
        ("opening_time = 08:00:00", 'opening_time = "08:00"', "without quotes"),
        ("closing_time = 17:00:00", "closing_time = 07:00:00", "after opening_time"),
        ("[0, 1, 2, 3, 4]", "[0, 7]", "open_weekdays"),
        ("[0, 1, 2, 3, 4]", "[true]", "open_weekdays"),
        ("appointment_minutes = 60", "appointment_minutes = 0", "appointment_minutes"),
    ],
)
def test_bad_inspection_values_stop_startup_naming_the_key(
    tmp_path: Path, replace: str, with_text: str, message: str
) -> None:
    assert replace in GOOD_INSPECTION  # the edit must actually change the file
    path = write_config(tmp_path, GOOD_BUSINESS + GOOD_INSPECTION.replace(replace, with_text))
    with pytest.raises(InvalidClientConfig, match=message):
        load_client_config(path)


@pytest.mark.parametrize("facts_line", ["facts = []", 'facts = ["ok", ""]', 'facts = "one string"'])
def test_bad_business_facts_are_rejected(tmp_path: Path, facts_line: str) -> None:
    path = write_config(tmp_path, f"[business]\n{facts_line}\n" + GOOD_INSPECTION)
    with pytest.raises(InvalidClientConfig, match=r"\[business\]"):
        load_client_config(path)


def test_missing_section_is_named(tmp_path: Path) -> None:
    path = write_config(tmp_path, GOOD_BUSINESS)
    with pytest.raises(InvalidClientConfig, match=r"needs a \[inspection\] section"):
        load_client_config(path)
