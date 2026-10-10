# lead-bot/tests/test_call_scenarios.py
# The call scenario runner: calls.toml loads, every call the offline stand-in can follow passes,
# and the rules checked on every call catch what they're meant to.
from __future__ import annotations

from pathlib import Path

import pytest

from lead_bot.call_scenarios import CallReport, CallScenario, check_call, load_call_scenarios, run_call
from lead_bot.scenarios import build_services

CALLS_FILE = Path(__file__).parent.parent / "scenarios" / "calls.toml"


def test_every_call_the_offline_stand_in_can_follow_passes(tmp_path: Path) -> None:
    services, _ = build_services(offline=True)
    scenarios = [scenario for scenario in load_call_scenarios(CALLS_FILE) if not scenario.needs_real_jev]
    assert len(scenarios) >= 20
    failures = {
        report.scenario.name: [*report.failures, *([report.crashed] if report.crashed else [])]
        for report in (run_call(scenario, services, tmp_path) for scenario in scenarios)
        if not report.passed
    }
    assert failures == {}


def report_with(scenario: CallScenario, **changes: object) -> CallReport:
    report = CallReport(
        scenario=scenario,
        lines=[("bot", "Thanks for calling.")],
        result="booked",
        answers={},
        alerts=["inspection_booked"],
        booking="Monday October 12 at 8 AM",
        texts=[],
        consent_given=None,
        said_after_end=[],
        failures=[],
    )
    for name, value in changes.items():
        setattr(report, name, value)
    return report


def booked_call(caller: str = "new") -> CallScenario:
    return CallScenario(name="x", category="x", words=(), expected_results=("booked",), caller=caller)


def test_a_text_without_consent_fails_whatever_the_script_says() -> None:
    report = report_with(booked_call(), texts=[("+1", "details")], consent_given=False)
    assert "a text was queued for a caller who never agreed to texts" in check_call(report)


@pytest.mark.parametrize("caller", ["hidden", "do_not_call"])
def test_a_text_to_a_hidden_or_opted_out_caller_fails(caller: str) -> None:
    report = report_with(booked_call(caller), texts=[("+1", "details")], consent_given=True)
    assert f"a text was queued for a {caller} caller" in check_call(report)


def test_a_call_that_ends_without_telling_the_owner_fails() -> None:
    report = report_with(booked_call(), alerts=[])
    assert "the call ended without an owner alert" in check_call(report)


def test_speaking_after_the_end_fails() -> None:
    report = report_with(booked_call(), said_after_end=["Hello?"])
    assert check_call(report) == ["spoke after the call ended: ['Hello?']"]


def test_booked_without_a_booking_fails() -> None:
    report = report_with(booked_call(), booking=None)
    assert "ended as booked but no booking was recorded" in check_call(report)
