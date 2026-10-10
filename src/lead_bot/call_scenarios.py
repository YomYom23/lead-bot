# lead-bot/src/lead_bot/call_scenarios.py
"""Plays scripted phone calls through the real CallSession and checks how each one ended.

    uv run python -m lead_bot.call_scenarios --offline        # keyword stand-ins: free, checks the flow
    uv run python -m lead_bot.call_scenarios                  # real Jev and writer: billed
    uv run python -m lead_bot.call_scenarios --only message   # one category, or one call by name

Each [[call]] in scenarios/calls.toml is one caller. It runs against its own fresh database and
in-memory calendar, through the same CallSession the relay uses, so what's tested is what runs:
the stages, the database writes, the owner alerts, the consent and the confirmation text. Only
Twilio's speech-to-text and voice are missing: each line is what the caller said, as text.

The report (scenarios/call-report.md, git-ignored) has every call and why any failed.
"""

from __future__ import annotations

import argparse
import re
import sys
import tempfile
import time
import tomllib
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from .booking import CalendarBooking
from .call_script import QUESTION_FOR_TEAM_LINE
from .call_session import Action, CallSession, ContinueTurn, Say
from .consent import CONSENT_VERSION, ConsentEvidence, consent_text
from .conversation import ConversationServices
from .leads import parse_lead_form
from .scenarios import PHOENIX, PRETEND_NOW, SCENARIO_SETTINGS, TEAM_PROMISE, build_services
from .stand_ins import MemoryCalendar
from .store import Store

# Who is calling, set up before the call starts:
#   new            a number nobody has heard from
#   known          the number on an earlier form lead (Dana Ruiz, 12 Saguaro Ln), with consent
#   known_booked   the same, already booked for an inspection
#   hidden         a hidden number (Twilio says 'anonymous')
#   do_not_call    a new number that is on the do-not-call list
CALLER_KINDS = ("new", "known", "known_booked", "hidden", "do_not_call")
NEW_CALLER = "+1" + "480" + "555" + "0166"
KNOWN_ADDRESS = "12 Saguaro Ln, Mesa AZ"


@dataclass(frozen=True)
class CallScenario:
    name: str
    category: str
    words: tuple[str, ...]
    expected_results: tuple[str, ...]
    caller: str = "new"
    expected_answers: dict[str, str] = field(default_factory=dict)
    alerts_include: tuple[str, ...] = ()
    never_say: tuple[str, ...] = ()
    # None: not checked. True/False: whether a confirmation text must be queued.
    texted: bool | None = None
    hang_up: bool = False  # the caller hangs up after the last line, without saying goodbye
    needs_real_jev: bool = False
    note: str = ""


def _strings(value: Any, where: str) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return tuple(value)
    raise ValueError(f"{where} must be a string or a list of strings")


def load_call_scenarios(path: Path) -> list[CallScenario]:
    """Read and check calls.toml. A mistake in the file stops the run before any paid call."""
    with path.open("rb") as file:
        document = tomllib.load(file)
    allowed_keys = {
        "name", "category", "words", "result", "caller", "answers", "alerts_include", "never_say",
        "texted", "hang_up", "needs_real_jev", "note",
    }  # fmt: skip
    scenarios: list[CallScenario] = []
    for raw in document.get("call", []):
        name = str(raw.get("name", ""))
        unknown_keys = set(raw) - allowed_keys
        if not name or unknown_keys or "words" not in raw or "result" not in raw:
            raise ValueError(
                f"call {name or '(no name)'}: needs name, words and result; "
                f"unknown keys {sorted(unknown_keys)}"
            )
        caller = str(raw.get("caller", "new"))
        if caller not in CALLER_KINDS:
            raise ValueError(f"call {name}: caller must be one of {', '.join(CALLER_KINDS)}")
        answers = raw.get("answers", {})
        if not isinstance(answers, dict) or not all(isinstance(value, str) for value in answers.values()):
            raise ValueError(f"call {name}: answers must be a table of strings")
        never_say = _strings(raw.get("never_say", []), f"call {name}: never_say")
        for pattern in never_say:
            re.compile(pattern)  # a broken pattern fails here, not halfway through a paid run
        flags: dict[str, bool | None] = {}
        for key in ("texted", "hang_up", "needs_real_jev"):
            value = raw.get(key)
            if value is not None and not isinstance(value, bool):
                raise ValueError(f"call {name}: {key} must be true or false")
            flags[key] = value
        words = raw["words"]
        if not isinstance(words, list) or not all(isinstance(item, str) for item in words):
            raise ValueError(f"call {name}: words must be a list of strings (it may be empty)")
        scenarios.append(
            CallScenario(
                name=name,
                category=str(raw.get("category", "")),
                words=tuple(words),
                expected_results=_strings(raw["result"], f"call {name}: result"),
                caller=caller,
                expected_answers=dict(answers),
                alerts_include=_strings(raw.get("alerts_include", []), f"call {name}: alerts_include"),
                never_say=never_say,
                texted=flags["texted"],
                hang_up=bool(flags["hang_up"]),
                needs_real_jev=bool(flags["needs_real_jev"]),
                note=str(raw.get("note", "")),
            )
        )
    names = [scenario.name for scenario in scenarios]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise ValueError(f"duplicate call names: {duplicates}")
    return scenarios


# ---- running one call ----


@dataclass
class CallReport:
    scenario: CallScenario
    lines: list[tuple[str, str]]  # (speaker, words) for the whole call, from the transcript
    result: str | None
    answers: dict[str, str]
    alerts: list[str]
    booking: str | None
    texts: list[tuple[str, str]]  # (phone, body) queued by the call
    consent_given: bool | None  # the lead's consent row, if any
    said_after_end: list[str]
    failures: list[str]
    crashed: str | None = None

    @property
    def passed(self) -> bool:
        return not self.failures and self.crashed is None

    @property
    def bot_lines(self) -> list[str]:
        return [words for speaker, words in self.lines if speaker == "bot"]


def _set_up_caller(store: Store, kind: str) -> str:
    """Prepare the database for this kind of caller. Returns the number they call from."""
    if kind == "hidden":
        return "anonymous"
    if kind == "do_not_call":
        store.add_to_do_not_call(NEW_CALLER, "replied STOP", PRETEND_NOW)
        return NEW_CALLER
    if kind == "new":
        return NEW_CALLER
    lead = parse_lead_form("Dana Ruiz", "(602) 555-0142", KNOWN_ADDRESS, "storm_damage")
    consent = ConsentEvidence(
        given=True,
        version=CONSENT_VERSION,
        text_shown=consent_text(SCENARIO_SETTINGS.business_name),
        ip_address="127.0.0.1",
        forwarded_for=None,
        user_agent="call scenario runner",
        source="call_scenarios.py",
    )
    lead_id = store.add_lead(lead, consent, PRETEND_NOW, SCENARIO_SETTINGS.texting_hours).lead_id
    if kind == "known_booked":
        # Scenario setup only: a booking made earlier, by text, written as save_turn would.
        with store._write_transaction() as connection:
            connection.execute(
                "INSERT INTO bookings (lead_id, starts_at, slot_label, booked_at) VALUES (?, ?, ?, ?)",
                (lead_id, PRETEND_NOW.timestamp(), "Tuesday October 6 at 8 AM", PRETEND_NOW.timestamp()),
            )
            connection.execute("UPDATE leads SET status = 'booked' WHERE lead_id = ?", (lead_id,))
    return lead.phone


def run_call(scenario: CallScenario, services: ConversationServices, work_directory: Path) -> CallReport:
    """Play one scripted call. `services` gives Jev and the writer; the calendar is new."""
    clock_now = [PRETEND_NOW.astimezone(PHOENIX)]

    def clock() -> datetime:
        return clock_now[0]

    booking = CalendarBooking(SCENARIO_SETTINGS.inspection_rules, MemoryCalendar(), clock=lambda: PRETEND_NOW)
    call_services = ConversationServices(
        ask_jev=services.ask_jev, booking=lambda: booking, writer=services.writer
    )
    store = Store(work_directory / f"call-{scenario.name}.db")
    report = CallReport(scenario, [], None, {}, [], None, [], None, [], [])
    call_id: int | None = None
    try:
        phone = _set_up_caller(store, scenario.caller)
        incoming = store.start_incoming_call(f"CA-{scenario.name}", phone, clock())
        call_id = incoming.call_id
        connected = store.connect_call(incoming.call_id, incoming.relay_token, clock())
        if connected is None:
            raise RuntimeError("the call didn't connect")
        session = CallSession(connected, SCENARIO_SETTINGS, store, call_services, clock)
        for words in scenario.words:
            if session.ended:
                # The bot hung up; whatever the script says next is never heard. Saying it
                # anyway checks that nothing more is said after the end.
                late = session.on_prompt(words)
                report.said_after_end += _spoken(late)
                continue
            actions: list[Action] = session.on_prompt(words)
            while actions and isinstance(actions[-1], ContinueTurn):
                actions = session.continue_turn()
        if not session.ended:
            # A call ends one way or another: a script that just stops is the caller hanging up.
            session.on_hangup()
    except Exception as error:  # a crash is reported against this call, and the rest still run
        report.crashed = f"{type(error).__name__}: {error}"
    if call_id is not None:
        report.lines = [(turn.speaker, turn.text) for turn in store.call_transcript(call_id)]
        report.result = store.call_result(call_id)
        lead_id = store.call_lead_id(call_id)
        if lead_id is not None:
            report.answers = store.lead_answers(lead_id)
            report.booking = store.booking_label(lead_id)
            with store._read() as connection:
                consent = connection.execute(
                    "SELECT given FROM consents WHERE lead_id = ?", (lead_id,)
                ).fetchone()
            report.consent_given = None if consent is None else consent["given"] == 1
        report.alerts = [alert.reason for alert in store.owner_alerts()]
        with store._read() as connection:
            rows = connection.execute("SELECT phone, body FROM texts WHERE direction = 'out'").fetchall()
        report.texts = [(row["phone"], row["body"]) for row in rows]
    report.failures = check_call(report)
    return report


def _spoken(actions: Sequence[Action]) -> list[str]:
    return [action.text for action in actions if isinstance(action, Say)]


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
    if scenario.texted is not None and bool(report.texts) != scenario.texted:
        failures.append(f"confirmation text queued: {bool(report.texts)}, expected {scenario.texted}")
    for pattern in scenario.never_say:
        for line in report.bot_lines:
            if re.search(pattern, line, re.IGNORECASE):
                failures.append(f"said /{pattern}/: {line!r}")

    # Rules every call must keep, whatever the script says.
    if report.result == "booked" and report.booking is None:
        failures.append("ended as booked but no booking was recorded")
    if report.booking is not None and scenario.caller != "known_booked" and report.result != "booked":
        failures.append(f"a booking ({report.booking}) was made but the call ended as {report.result!r}")
    if report.texts and report.consent_given is not True:
        failures.append("a text was queued for a caller who never agreed to texts")
    if report.texts and scenario.caller in ("hidden", "do_not_call"):
        failures.append(f"a text was queued for a {scenario.caller} caller")
    if report.result != "opted_out" and not report.alerts:
        # Every call ends with the owner told: a booking, a message, a hang-up or a hand-off.
        failures.append("the call ended without an owner alert")
    if report.said_after_end:
        failures.append(f"spoke after the call ended: {report.said_after_end!r}")
    # "I'll pass that question to our team" is only true if the owner was told, once per question.
    # A message's "I'll pass that on to the team" is made true by the message alert instead.
    question_promises = [line for line in report.bot_lines if QUESTION_FOR_TEAM_LINE in line]
    team_alerts = [alert for alert in report.alerts if alert.startswith("homeowner_question_for_the_team")]
    if len(question_promises) > len(team_alerts):
        failures.append(
            f"promised to pass {len(question_promises)} questions to the team "
            f"with {len(team_alerts)} owner alerts"
        )
    message_promises = [
        line for line in report.bot_lines if TEAM_PROMISE.search(line) and QUESTION_FOR_TEAM_LINE not in line
    ]
    if message_promises and report.result == "message" and "caller_left_a_message" not in report.alerts:
        failures.append("took a message without an owner alert carrying it")
    return failures


# ---- the report ----


def write_call_report(reports: Sequence[CallReport], mode: str, total_seconds: float, path: Path) -> None:
    passed = [report for report in reports if report.passed]
    lines = [
        "# Call scenario report",
        "",
        f"Run {datetime.now().astimezone():%Y-%m-%d %H:%M %Z} with {mode}. "
        f"{len(passed)} of {len(reports)} calls passed, in {total_seconds:.0f}s.",
        "",
    ]
    failed = [report for report in reports if not report.passed]
    if failed:
        lines += ["## Failures", ""]
        for report in failed:
            reasons = [*report.failures, *([f"crashed: {report.crashed}"] if report.crashed else [])]
            lines.append(f"- **{report.scenario.name}**: " + "; ".join(reasons))
        lines.append("")
    lines += ["## Calls", ""]
    for report in reports:
        lines.append(
            f"### {'PASS' if report.passed else 'FAIL'} {report.scenario.name} ({report.scenario.category})"
        )
        if report.scenario.note:
            lines.append(f"_{report.scenario.note}_")
        lines.append("")
        for speaker, words in report.lines:
            label = {"bot": "BOT", "caller": "CALLER"}.get(speaker)
            lines.append(f"- {label}: {words}" if label else f"- [{words}]")
        lines.append(
            f"- ended: {report.result}; answers: {report.answers}; booking: {report.booking}; "
            f"texted: {bool(report.texts)}; alerts: {report.alerts}"
        )
        lines.append("")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--file", type=Path, default=Path("scenarios/calls.toml"))
    parser.add_argument("--report", type=Path, default=Path("scenarios/call-report.md"))
    parser.add_argument("--offline", action="store_true", help="keyword stand-in for Jev, and no writer")
    parser.add_argument("--only", help="run only this category or call name")
    parser.add_argument("--parallel", type=int, default=4, help="calls at once (default 4)")
    arguments = parser.parse_args(argv)

    load_dotenv()
    scenarios = load_call_scenarios(arguments.file)
    if arguments.only:
        scenarios = [s for s in scenarios if arguments.only in (s.name, s.category)]
        if not scenarios:
            raise SystemExit(f"[call scenarios] nothing matches --only {arguments.only}")
    if arguments.offline:
        skipped = [s.name for s in scenarios if s.needs_real_jev]
        scenarios = [s for s in scenarios if not s.needs_real_jev]
        if skipped:
            print(f"[call scenarios] skipped offline (need real Jev): {', '.join(skipped)}")
    services, mode = build_services(arguments.offline)
    print(f"[call scenarios] {len(scenarios)} calls, {mode}")

    started = time.monotonic()
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as work_directory:
        with ThreadPoolExecutor(max_workers=max(1, arguments.parallel)) as pool:
            reports = list(pool.map(lambda s: run_call(s, services, Path(work_directory)), scenarios))
    total_seconds = time.monotonic() - started

    for report in reports:
        problems = [*report.failures, *([report.crashed] if report.crashed else [])]
        print(
            f"{'PASS' if report.passed else 'FAIL'} {report.scenario.name}"
            + ("" if report.passed else "  " + "; ".join(problems))
        )
    write_call_report(reports, mode, total_seconds, arguments.report)
    passed_count = sum(report.passed for report in reports)
    print(f"[call scenarios] {passed_count}/{len(reports)} passed in {total_seconds:.0f}s")
    print(f"[call scenarios] report: {arguments.report}")
    return 0 if passed_count == len(reports) else 1


if __name__ == "__main__":
    sys.exit(main())
