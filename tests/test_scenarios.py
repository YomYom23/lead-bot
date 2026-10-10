# lead-bot/tests/test_scenarios.py
# The scenario runner: texts.toml loads and is well-formed, a scripted conversation is played and
# judged, and the checks catch what they're meant to catch. Real Jev and the writer are never used.
from __future__ import annotations

from pathlib import Path

import pytest
from text_fakes import FIRST_OFFERED_TIME, GUARD_CHECK, ScriptedJev, StubWriter

from lead_bot.booking import CalendarBooking
from lead_bot.conversation import ConversationServices
from lead_bot.scenarios import Scenario, ScenarioReport, Turn, check_scenario, load_scenarios, run_scenario
from lead_bot.turn_data import WriterOutcome

TEXTS_FILE = Path(__file__).parent.parent / "scenarios" / "texts.toml"


def no_calendar() -> CalendarBooking:
    raise AssertionError("run_scenario must give each conversation its own calendar")


def scenario(texts: tuple[str, ...], result: str = "booked", never_say: tuple[str, ...] = ()) -> Scenario:
    return Scenario(
        name="test", category="test", texts=texts, expected_results=(result,), never_say=never_say
    )


def report_with(**changes: object) -> ScenarioReport:
    """A passing booked report, with the given fields changed."""
    report = ScenarioReport(
        scenario=scenario(("yes",)),
        opening=("Hi Dana, this is the automated assistant for Desert Ridge Roofing LLC.",),
        turns=[],
        afterwards=(),
        result="booked",
        answers={},
        alerts=[],
        booking="Monday October 5 at 12 PM",
        on_do_not_call=False,
        writer_outcomes=[],
        failures=[],
    )
    for name, value in changes.items():
        setattr(report, name, value)
    return report


def test_the_scenario_file_loads_and_covers_every_category() -> None:
    scenarios = load_scenarios(TEXTS_FILE)
    assert len(scenarios) >= 50
    categories = {s.category for s in scenarios}
    assert {"booking", "opt_out", "handoff", "fixed_answer", "writer", "adversarial"} <= categories


def test_a_mistake_in_the_file_stops_the_run_before_any_conversation(tmp_path: Path) -> None:
    broken = tmp_path / "texts.toml"
    broken.write_text('[[scenario]]\nname = "x"\ntexts = ["hi"]\nresult = "booked"\nnever_sya = ["oops"]\n')
    with pytest.raises(ValueError, match="unknown keys"):
        load_scenarios(broken)


def test_a_scripted_conversation_books_and_passes(tmp_path: Path) -> None:
    jev = ScriptedJev("yes", "no", "no", "yes", FIRST_OFFERED_TIME)
    services = ConversationServices(ask_jev=jev, booking=no_calendar)
    texts = ("yes", "no", "no", "yes", "{first_time} please")

    report = run_scenario(scenario(texts), services, tmp_path)

    assert report.passed, (report.failures, report.crashed)
    assert report.result == "booked"
    # The placeholder became the time the bot actually offered first.
    assert report.turns[4].homeowner == f"{report.booking} please"
    # No reminders after a booking.
    assert report.afterwards == ()


def test_a_homeowner_who_never_replies_gets_every_reminder_then_the_owner_is_told(tmp_path: Path) -> None:
    services = ConversationServices(ask_jev=ScriptedJev(), booking=no_calendar)

    report = run_scenario(scenario((), result="unreached"), services, tmp_path)

    assert report.passed, (report.failures, report.crashed)
    assert len(report.opening) == 1
    assert len(report.afterwards) == 3  # the three reminders
    assert "no_reply_to_any_text" in report.alerts


def test_writer_answers_are_kept_for_the_report(tmp_path: Path) -> None:
    jev = ScriptedJev("other_question", GUARD_CHECK)
    services = ConversationServices(
        ask_jev=jev, booking=no_calendar, writer=StubWriter("It takes about an hour.")
    )

    report = run_scenario(scenario(("How long is it?",), result="stopped_replying"), services, tmp_path)

    assert report.passed, (report.failures, report.crashed)
    assert report.turns[0].bot[0].startswith("It takes about an hour.")
    assert [outcome.sent for outcome in report.writer_outcomes] == [True]


def test_never_say_catches_a_forbidden_line() -> None:
    report = report_with(
        scenario=scenario(("hi",), never_say=(r"\$\s*\d",)),
        turns=[Turn("how much?", ("It's about $400.",), 0.1)],
    )
    assert check_scenario(report) == ['said /\\$\\s*\\d/: "It\'s about $400."']


def test_an_opt_out_without_a_do_not_call_entry_fails_whatever_the_script_says() -> None:
    report = report_with(scenario=scenario(("stop",), result="opted_out"), result="opted_out", booking=None)
    assert check_scenario(report) == ["ended as opted_out but the number isn't on the do-not-call list"]


def test_any_text_after_the_opt_out_confirmation_fails() -> None:
    report = report_with(
        scenario=scenario(("stop texting me", "hello?"), result="opted_out"),
        result="opted_out",
        booking=None,
        on_do_not_call=True,
        turns=[
            Turn("stop texting me", ("You're opted out.",), 0.1, on_do_not_call_after=True),
            Turn("hello?", ("Hi again!",), 0.1, on_do_not_call_after=True),
        ],
    )
    assert check_scenario(report) == ["texted after the opt-out: ['Hi again!']"]


def test_a_reminder_after_the_opt_out_fails() -> None:
    report = report_with(
        scenario=scenario(("stop texting me",), result="opted_out"),
        result="opted_out",
        booking=None,
        on_do_not_call=True,
        turns=[Turn("stop texting me", ("You're opted out.",), 0.1, on_do_not_call_after=True)],
        afterwards=("Just checking in!",),
    )
    assert check_scenario(report) == ["texted after the opt-out: ['Just checking in!']"]


def test_a_sent_blocked_draft_fails() -> None:
    draft = "The inspector always calls ahead."
    report = report_with(
        turns=[Turn("will they call?", (draft,), 0.1)],
        writer_outcomes=[WriterOutcome("will they call?", draft, ("unsupported_fact",), 0.5)],
        alerts=["homeowner_question_for_the_team:unsupported_fact"],
    )
    assert check_scenario(report) == [f"a blocked draft was sent: {draft!r}"]


def test_a_text_outside_the_basic_sms_alphabet_fails() -> None:
    report = report_with(turns=[Turn("thanks", ("You\u2019re welcome!",), 0.1)])
    assert check_scenario(report) == ["not plain SMS characters (sent as UCS-2): 'You\u2019re welcome!'"]


def test_a_team_promise_without_an_owner_alert_fails() -> None:
    report = report_with(
        turns=[Turn("do I need to be home?", ("I'll pass that question to our team.",), 0.1)]
    )
    assert check_scenario(report) == ["promised a team follow-up 1 times with 0 owner alerts"]
    report.alerts = ["homeowner_question_for_the_team:not_in_facts"]
    assert check_scenario(report) == []
