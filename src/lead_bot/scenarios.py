# lead-bot/src/lead_bot/scenarios.py
"""Plays scripted phone calls through the call logic and reports how each one went.

    uv run python -m lead_bot.scenarios                      # real Jev and the real writer
    uv run python -m lead_bot.scenarios --offline            # keyword stand-ins, no keys
    uv run python -m lead_bot.scenarios --only writer        # one category, or one scenario name

The scripts live in scenarios/calls.toml (the format is described at the top of that file). Each
call runs through CallSession, the same object relay.py drives on a live call, against a fresh
database. What's real: Jev, the writer and the guard (unless --offline), the call logic, the
store. What's not: Twilio (each line is handed straight to the session), and the calendar, which
is always in memory. Fifty test calls must not put fifty events on the business's Google
Calendar, and phase 2's live run already proved the Google booking path.

The report (scenarios/report.md by default) has, for each call: pass or fail with the reasons,
the full transcript with how long each reply took, and every draft the writer produced, spoken
or blocked, with the guard's verdict. Read the blocked and spoken drafts by eye: the checks in
calls.toml only catch the mistakes someone thought to write a pattern for.
"""

from __future__ import annotations

import argparse
import os
import re
import statistics
import sys
import tempfile
import time
import tomllib
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from .booking import CalendarBooking
from .call_session import CallServices, CallSession, ContinueTurn, EndCall, Say, WriterOutcome
from .consent import CONSENT_VERSION, ConsentEvidence, consent_text
from .jev import Jev
from .leads import parse_lead_form
from .settings import BusinessSettings
from .simulate import MemoryCalendar, OfflineJev
from .store import Store
from .timing import CallingHours
from .wiring import DEFAULT_WRITER_MODEL, JEV_RETRIES, JEV_TIMEOUT_SECONDS, WRITER_TIMEOUT_SECONDS
from .writer import OpenAIWriter

PHOENIX = ZoneInfo("America/Phoenix")
# Every scenario call happens at this moment, so the offered times are the same on every run:
# Monday 12 PM (2 hours' notice), then Tuesday and Wednesday at 8 AM.
PRETEND_NOW = datetime(2026, 10, 5, 10, 0, tzinfo=PHOENIX)
SCENARIO_SETTINGS = BusinessSettings(
    business_name="Desert Ridge Roofing LLC", timezone=PHOENIX, calling_hours=CallingHours(PHOENIX)
)
TIME_PLACEHOLDERS = ("{first_time}", "{second_time}", "{third_time}")
KEYPRESS = re.compile(r"^[0-9*#]$")
# A bot line that promises the team will answer a question (the fixed line says it this way).
TEAM_PROMISE = re.compile(r"\bpass\w*\b.{0,40}\bteam\b", re.IGNORECASE)


# ---- the scripts ----


@dataclass(frozen=True)
class Scenario:
    name: str
    category: str
    says: tuple[str, ...]
    expected_results: tuple[str, ...]
    expected_answers: dict[str, str] = field(default_factory=dict)
    alerts_include: tuple[str, ...] = ()
    never_say: tuple[str, ...] = ()
    note: str = ""


def _strings(value: Any, where: str) -> tuple[str, ...]:
    """A TOML value that must be a string or a list of strings, as a tuple."""
    if isinstance(value, str):
        return (value,)
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return tuple(value)
    raise ValueError(f"{where} must be a string or a list of strings")


def load_scenarios(path: Path) -> list[Scenario]:
    """Read and check calls.toml. A mistake in the file stops the run before any paid call."""
    with path.open("rb") as file:
        document = tomllib.load(file)
    allowed_keys = {"name", "category", "says", "result", "answers", "alerts_include", "never_say", "note"}
    scenarios: list[Scenario] = []
    for raw in document.get("scenario", []):
        name = str(raw.get("name", ""))
        unknown_keys = set(raw) - allowed_keys
        if not name or unknown_keys:
            raise ValueError(
                f"scenario {name or '(no name)'}: missing name or unknown keys {sorted(unknown_keys)}"
            )
        answers = raw.get("answers", {})
        if not isinstance(answers, dict) or not all(isinstance(value, str) for value in answers.values()):
            raise ValueError(f"scenario {name}: answers must be a table of strings")
        never_say = _strings(raw.get("never_say", []), f"scenario {name}: never_say")
        for pattern in never_say:
            re.compile(pattern)  # a broken pattern fails here, not halfway through a paid run
        scenarios.append(
            Scenario(
                name=name,
                category=str(raw.get("category", "")),
                says=_strings(raw["says"], f"scenario {name}: says"),
                expected_results=_strings(raw["result"], f"scenario {name}: result"),
                expected_answers=dict(answers),
                alerts_include=_strings(raw.get("alerts_include", []), f"scenario {name}: alerts_include"),
                never_say=never_say,
                note=str(raw.get("note", "")),
            )
        )
    names = [scenario.name for scenario in scenarios]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise ValueError(f"duplicate scenario names: {duplicates}")
    return scenarios


# ---- running one call ----


@dataclass(frozen=True)
class Turn:
    """One exchange: what the homeowner said or pressed, and everything the bot said back.

    seconds is how long the bot took to start answering, which is the silence the homeowner
    hears (Twilio's speech-to-text and text-to-speech time comes on top on a real call).
    answer_seconds is set when the bot said "Let me check." first: the time from then until
    the writer's checked answer (or the fallback line) was ready.
    """

    homeowner: str
    bot: tuple[str, ...]
    seconds: float
    answer_seconds: float | None = None


@dataclass
class CallReport:
    scenario: Scenario
    turns: list[Turn]
    result: str | None
    answers: dict[str, str]
    alerts: list[str]
    booking: str | None
    on_do_not_call: bool
    writer_outcomes: list[WriterOutcome]
    failures: list[str]
    crashed: str | None = None

    @property
    def passed(self) -> bool:
        return not self.failures and self.crashed is None


def _fill_in_times(line: str, session: CallSession) -> str:
    labels = [slot.label for slot in session.offered_slots]
    for position, placeholder in enumerate(TIME_PLACEHOLDERS):
        if placeholder in line:
            # Nothing offered yet means the call went off course earlier; the line is still
            # said, so the transcript shows what happened.
            label = labels[position] if position < len(labels) else "(no time was offered)"
            line = line.replace(placeholder, label)
    return line


def run_call(scenario: Scenario, services: CallServices, work_directory: Path) -> CallReport:
    store = Store(work_directory / f"{scenario.name}.db")
    lead = parse_lead_form("Dana Ruiz", "602-555-0142", "12 Saguaro Ln, Mesa AZ", "storm_damage")
    consent = ConsentEvidence(
        given=True,
        version=CONSENT_VERSION,
        text_shown=consent_text(SCENARIO_SETTINGS.business_name),
        ip_address="127.0.0.1",
        forwarded_for=None,
        user_agent="lead_bot.scenarios",
        source="scenario runner",
    )
    added = store.add_lead(lead, consent, PRETEND_NOW, SCENARIO_SETTINGS.calling_hours)
    due_call = store.claim_due_call(PRETEND_NOW)
    if due_call is None:
        raise RuntimeError("the scenario lead had no call due; PRETEND_NOW must be inside calling hours")
    store.mark_call_placed(due_call.call_id, f"SCENARIO-{scenario.name}", PRETEND_NOW)
    connected = store.connect_call(due_call.call_id, due_call.relay_token, PRETEND_NOW)
    if connected is None:
        raise RuntimeError("the scenario call was refused on connect")
    # Each call gets its own empty calendar, so calls never see each other's bookings.
    booking = CalendarBooking(SCENARIO_SETTINGS.inspection_rules, MemoryCalendar(), clock=lambda: PRETEND_NOW)
    call_services = replace(services, booking=lambda: booking)
    session = CallSession(connected, SCENARIO_SETTINGS, store, call_services, clock=lambda: PRETEND_NOW)

    turns: list[Turn] = []
    crashed: str | None = None
    try:
        for raw_line in scenario.says:
            if session.ended:
                break
            line = _fill_in_times(raw_line, session)
            started = time.monotonic()
            actions = session.on_keypress(line) if KEYPRESS.match(line) else session.on_prompt(line)
            seconds = time.monotonic() - started
            answer_seconds: float | None = None
            if any(isinstance(action, ContinueTurn) for action in actions):
                answer_started = time.monotonic()
                actions = [*actions, *session.continue_turn()]
                answer_seconds = time.monotonic() - answer_started
            spoken = tuple(action.text for action in actions if isinstance(action, Say))
            turns.append(Turn(line, spoken, seconds, answer_seconds))
            if any(isinstance(action, EndCall) for action in actions):
                break
        if not session.ended:
            session.on_hangup()  # the script ran out: the homeowner hangs up
    except Exception as error:  # noqa: BLE001 - one broken call must not stop the other 49
        crashed = f"{type(error).__name__}: {error}"

    report = CallReport(
        scenario=scenario,
        turns=turns,
        result=store.call_result(connected.call_id),
        answers=store.lead_answers(added.lead_id),
        alerts=[alert.reason for alert in store.owner_alerts() if alert.lead_id == added.lead_id],
        booking=store.booking_label(added.lead_id),
        on_do_not_call=store.is_on_do_not_call(lead.phone),
        writer_outcomes=list(session.writer_outcomes),
        failures=[],
        crashed=crashed,
    )
    report.failures = check_call(report)
    return report


def check_call(report: CallReport) -> list[str]:
    """Everything wrong with how this call went. Empty means it passed."""
    scenario = report.scenario
    failures: list[str] = []
    if report.result not in scenario.expected_results:
        failures.append(f"ended as {report.result!r}, expected {' or '.join(scenario.expected_results)}")
    for question, expected in scenario.expected_answers.items():
        if report.answers.get(question) != expected:
            failures.append(f"answer {question}={report.answers.get(question)!r}, expected {expected!r}")
    for wanted in scenario.alerts_include:
        if not any(alert.startswith(wanted) for alert in report.alerts):
            failures.append(f"no owner alert {wanted!r} (got {report.alerts})")
    bot_lines = [line for turn in report.turns for line in turn.bot]
    for pattern in scenario.never_say:
        for line in bot_lines:
            if re.search(pattern, line, re.IGNORECASE):
                failures.append(f"said /{pattern}/: {line!r}")

    # Rules every call must keep, whatever the script says.
    if report.result == "opted_out" and not report.on_do_not_call:
        failures.append("opted out but the number isn't on the do-not-call list")
    if report.result == "booked" and report.booking is None:
        failures.append("ended as booked but no booking was recorded")
    if report.result != "booked" and report.booking is not None:
        failures.append(f"a booking ({report.booking}) was made but the call ended as {report.result!r}")
    for outcome in report.writer_outcomes:
        blocked_draft = None if outcome.spoken else outcome.draft
        if blocked_draft is not None and any(blocked_draft in line for line in bot_lines):
            failures.append(f"a blocked draft was spoken: {blocked_draft!r}")
    # "I'll pass that to our team" is only true if the owner was told. The first live run had
    # the writer saying it in its own words six times with no alert behind any of them.
    team_promises = [line for line in bot_lines if TEAM_PROMISE.search(line)]
    team_alerts = [alert for alert in report.alerts if alert.startswith("homeowner_question_for_the_team")]
    if len(team_promises) > len(team_alerts):
        failures.append(
            f"promised a team follow-up {len(team_promises)} times with {len(team_alerts)} owner alerts"
        )
    return failures


# ---- the report ----


def _percentiles(values: Sequence[float]) -> str:
    if not values:
        return "none"
    if len(values) == 1:
        return f"{values[0]:.2f}s (1 turn)"
    deciles = statistics.quantiles(values, n=10, method="inclusive")
    return (
        f"median {statistics.median(values):.2f}s, 90th percentile {deciles[8]:.2f}s, "
        f"max {max(values):.2f}s ({len(values)} turns)"
    )


def write_report(reports: Sequence[CallReport], mode: str, total_seconds: float, path: Path) -> str:
    passed = [report for report in reports if report.passed]
    all_turns = [turn for report in reports for turn in report.turns]
    # A turn with answer_seconds said "Let me check." first; its two parts are timed separately.
    script_turns = [turn.seconds for turn in all_turns if turn.answer_seconds is None]
    filler_turns = [turn.seconds for turn in all_turns if turn.answer_seconds is not None]
    writer_answers = [turn.answer_seconds for turn in all_turns if turn.answer_seconds is not None]
    outcomes = [outcome for report in reports for outcome in report.writer_outcomes]
    spoken_drafts = [outcome for outcome in outcomes if outcome.spoken]

    lines = [
        "# Scenario report",
        "",
        f"Run {datetime.now().astimezone():%Y-%m-%d %H:%M %Z} with {mode}. "
        f"{len(passed)} of {len(reports)} calls passed, in {total_seconds:.0f}s.",
        "",
        "## Latency (our side only; Twilio's speech time comes on top)",
        "",
        f"- Scripted replies: {_percentiles(script_turns)}",
        f"- \"Let me check.\" after an off-script question: {_percentiles(filler_turns)}",
        f"- Writer + guard after \"Let me check.\": {_percentiles(writer_answers)}",
        "",
        "## Writer",
        "",
        f"{len(outcomes)} drafts requested, {len(spoken_drafts)} spoken, "
        f"{len(outcomes) - len(spoken_drafts)} replaced by the fixed line.",
        "",
        "| Scenario | Question | Draft | Verdict |",
        "| --- | --- | --- | --- |",
    ]
    for report in reports:
        for outcome in report.writer_outcomes:
            verdict = "spoken" if outcome.spoken else "blocked: " + ", ".join(outcome.problems)
            draft = (outcome.draft or "(none)").replace("|", "\\|").replace("\n", " ")
            lines.append(f"| {report.scenario.name} | {outcome.question} | {draft} | {verdict} |")

    lines += ["", "## Results by category", ""]
    categories = sorted({report.scenario.category for report in reports})
    for category in categories:
        in_category = [report for report in reports if report.scenario.category == category]
        lines.append(
            f"- {category}: {sum(report.passed for report in in_category)} of {len(in_category)} passed"
        )

    failed = [report for report in reports if not report.passed]
    if failed:
        lines += ["", "## Failures", ""]
        for report in failed:
            reasons = [*report.failures, *([f"crashed: {report.crashed}"] if report.crashed else [])]
            lines.append(f"- **{report.scenario.name}**: " + "; ".join(reasons))

    lines += ["", "## Calls", ""]
    for report in reports:
        status = "PASS" if report.passed else "FAIL"
        lines.append(f"### {status} {report.scenario.name} ({report.scenario.category})")
        if report.scenario.note:
            lines.append(f"_{report.scenario.note}_")
        lines.append("")
        for turn in report.turns:
            lines.append(f"- HOMEOWNER: {turn.homeowner}")
            timing = f"{turn.seconds:.2f}s"
            if turn.answer_seconds is not None:
                timing += f", then {turn.answer_seconds:.2f}s"
            lines.append(f"  - BOT ({timing}): {' / '.join(turn.bot) or '(nothing)'}")
        lines.append(
            f"- ended: {report.result}; answers: {report.answers}; booking: {report.booking}; "
            f"alerts: {report.alerts}"
        )
        lines.append("")
    text = "\n".join(lines) + "\n"
    path.write_text(text, encoding="utf-8")
    return text


# ---- running them all ----


def build_services(offline: bool, writer_model: str) -> tuple[CallServices, str]:
    """Jev and the writer, shared by every call. run_call adds each call's own calendar."""
    if offline:
        # No writer: off-script questions get the fixed line, as on a call with the writer down.
        offline_services = CallServices(ask_jev=OfflineJev(), booking=_no_calendar_yet)
        return offline_services, "offline keyword stand-ins, no writer"
    required = ("TYPESAFE_API_KEY", "OPENAI_API_KEY")
    missing = [name for name in required if not os.environ.get(name, "").strip()]
    if missing:
        raise SystemExit(f"[scenarios] {' and '.join(missing)} must be set (in .env), or pass --offline")
    jev = Jev(timeout_seconds=JEV_TIMEOUT_SECONDS, max_retries=JEV_RETRIES)
    writer = OpenAIWriter(model=writer_model, timeout_seconds=WRITER_TIMEOUT_SECONDS)
    services = CallServices(ask_jev=jev.ask, booking=_no_calendar_yet, writer=writer)
    return services, f"real Jev and writer {writer_model}, in-memory calendar"


def _no_calendar_yet() -> CalendarBooking:
    raise RuntimeError("run_call replaces this with the call's own in-memory calendar")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--file", type=Path, default=Path("scenarios/calls.toml"))
    parser.add_argument("--report", type=Path, default=Path("scenarios/report.md"))
    parser.add_argument("--offline", action="store_true", help="keyword stand-in for Jev, and no writer")
    parser.add_argument("--only", help="run only this category or scenario name")
    parser.add_argument("--parallel", type=int, default=4, help="calls at once (default 4)")
    arguments = parser.parse_args(argv)

    load_dotenv()
    scenarios = load_scenarios(arguments.file)
    if arguments.only:
        scenarios = [s for s in scenarios if arguments.only in (s.name, s.category)]
        if not scenarios:
            raise SystemExit(f"[scenarios] nothing matches --only {arguments.only}")
    writer_model = os.environ.get("WRITER_MODEL", "").strip() or DEFAULT_WRITER_MODEL
    services, mode = build_services(arguments.offline, writer_model)
    print(f"[scenarios] {len(scenarios)} calls, {mode}")

    started = time.monotonic()
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as work_directory:
        with ThreadPoolExecutor(max_workers=max(1, arguments.parallel)) as pool:
            reports = list(pool.map(lambda s: run_call(s, services, Path(work_directory)), scenarios))
    total_seconds = time.monotonic() - started

    for report in reports:
        mark = "PASS" if report.passed else "FAIL"
        problems = [*report.failures, *([report.crashed] if report.crashed else [])]
        detail = "" if report.passed else "  " + "; ".join(problems)
        print(f"{mark} {report.scenario.name}{detail}")
    write_report(reports, mode, total_seconds, arguments.report)
    passed_count = sum(report.passed for report in reports)
    print(
        f"[scenarios] {passed_count}/{len(reports)} passed in {total_seconds:.0f}s; "
        f"report: {arguments.report}"
    )
    return 0 if passed_count == len(reports) else 1


if __name__ == "__main__":
    sys.exit(main())
