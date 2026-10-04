# lead-bot/tests/test_scenarios.py
# The scenario runner: calls.toml loads and is well-formed, a scripted call is played and judged,
# and the checks catch what they're meant to catch. Real Jev and the writer are never used here.
from __future__ import annotations

from pathlib import Path

import pytest
from call_fakes import FIRST_OFFERED_TIME, GUARD_CHECK, ScriptedJev, StubWriter

from lead_bot.booking import CalendarBooking
from lead_bot.call_session import CallServices
from lead_bot.scenarios import CallReport, Scenario, Turn, check_call, load_scenarios, run_call

CALLS_FILE = Path(__file__).parent.parent / "scenarios" / "calls.toml"


def no_calendar() -> CalendarBooking:
    raise AssertionError("run_call must give each call its own calendar")


def scenario(says: tuple[str, ...], result: str = "booked", never_say: tuple[str, ...] = ()) -> Scenario:
    return Scenario(name="test", category="test", says=says, expected_results=(result,), never_say=never_say)


def test_the_scenario_file_loads_and_has_about_fifty_calls() -> None:
    scenarios = load_scenarios(CALLS_FILE)
    assert len(scenarios) >= 50
    categories = {s.category for s in scenarios}
    assert {"booking", "opt_out", "handoff", "fixed_answer", "writer", "adversarial"} <= categories


def test_a_mistake_in_the_file_stops_the_run_before_any_call(tmp_path: Path) -> None:
    broken = tmp_path / "calls.toml"
    broken.write_text('[[scenario]]\nname = "x"\nsays = ["hi"]\nresult = "booked"\nnever_sya = ["oops"]\n')
    with pytest.raises(ValueError, match="unknown keys"):
        load_scenarios(broken)


def test_a_scripted_call_books_and_passes(tmp_path: Path) -> None:
    jev = ScriptedJev("yes", "yes", "no", "no", "yes", FIRST_OFFERED_TIME, "no")
    services = CallServices(ask_jev=jev, booking=no_calendar)
    says = ("sure", "yes", "no", "no", "yes", "{first_time} please", "no")

    report = run_call(scenario(says), services, tmp_path)

    assert report.passed, report.failures
    assert report.result == "booked"
    # The placeholder became the time the bot actually offered first.
    assert report.turns[5].homeowner == f"{report.booking} please"


def test_the_writer_turn_is_timed_in_two_parts(tmp_path: Path) -> None:
    jev = ScriptedJev("other_question", GUARD_CHECK, ("unclear", {"opt_out": 0.9}))
    services = CallServices(ask_jev=jev, booking=no_calendar, writer=StubWriter("It takes about an hour."))

    report = run_call(scenario(("How long is it?", "bye"), result="opted_out"), services, tmp_path)

    asking = report.turns[0]
    assert asking.bot[0] == "Let me check."
    assert asking.bot[1].startswith("It takes about an hour.")
    assert asking.answer_seconds is not None
    assert [outcome.spoken for outcome in report.writer_outcomes] == [True]


def test_never_say_catches_a_forbidden_line() -> None:
    report = CallReport(
        scenario=scenario(("hi",), never_say=(r"\$\s*\d",)),
        turns=[Turn("how much?", ("It's about $400.",), 0.1)],
        result="booked",
        answers={},
        alerts=[],
        booking="Monday October 5 at 12 PM",
        on_do_not_call=False,
        writer_outcomes=[],
        failures=[],
    )
    assert check_call(report) == ["said /\\$\\s*\\d/: \"It's about $400.\""]


def test_an_opt_out_without_a_do_not_call_entry_fails_whatever_the_script_says() -> None:
    report = CallReport(
        scenario=scenario(("9",), result="opted_out"),
        turns=[],
        result="opted_out",
        answers={},
        alerts=[],
        booking=None,
        on_do_not_call=False,
        writer_outcomes=[],
        failures=[],
    )
    assert check_call(report) == ["opted out but the number isn't on the do-not-call list"]


def test_a_team_promise_without_an_owner_alert_fails() -> None:
    report = CallReport(
        scenario=scenario(("hi",)),
        turns=[Turn("do I need to be home?", ("I'll pass that question to the team.",), 0.1)],
        result="booked",
        answers={},
        alerts=[],
        booking="Monday October 5 at 12:30 PM",
        on_do_not_call=False,
        writer_outcomes=[],
        failures=[],
    )
    assert check_call(report) == ["promised a team follow-up 1 times with 0 owner alerts"]
    report.alerts = ["homeowner_question_for_the_team:not_in_facts"]
    assert check_call(report) == []
