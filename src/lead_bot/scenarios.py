# lead-bot/src/lead_bot/scenarios.py
"""Plays scripted text conversations through the real worker and checks how each one ended.

    uv run python -m lead_bot.scenarios --offline         # keyword stand-ins: free, checks the flow
    uv run python -m lead_bot.scenarios                   # real Jev and writer: billed
    uv run python -m lead_bot.scenarios --only opt_out    # one category, or one scenario by name

Each scenario in scenarios/texts.toml is one homeowner's conversation. It runs against its own
fresh database and an in-memory calendar, through the same Worker the real bot uses, so what's
tested is what runs: the opening text, the database round trip of every turn, the do-not-call
checks before sending, the conversation logic, and (without --offline) Jev and the writer.

The report (scenarios/report.md) has every conversation, every writer draft with the guard's
verdict, and how long each reply took.
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
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from .booking import CalendarBooking
from .consent import CONSENT_VERSION, ConsentEvidence, consent_text
from .conversation import ConversationServices
from .leads import parse_lead_form
from .settings import BusinessSettings
from .stand_ins import MemoryCalendar, OfflineJev
from .store import Store
from .text_script import is_gsm7, segment_count
from .timing import TextingHours
from .turn_data import TurnOutcome, WriterOutcome
from .worker import Worker

PHOENIX = ZoneInfo("America/Phoenix")
# Every scenario starts at this moment, so the offered times are the same on every run.
PRETEND_NOW = datetime(2026, 10, 5, 10, 0, tzinfo=PHOENIX)
# The homeowner takes this long to answer each text.
REPLY_DELAY = timedelta(seconds=30)
# After the script runs out, time moves on in these steps until this long has passed, so the
# reminders and the hand-off of quiet conversations happen as they would.
AFTERWARDS_STEP = timedelta(hours=1)
AFTERWARDS_FOR = timedelta(days=5)
SCENARIO_SETTINGS = BusinessSettings(
    business_name="Desert Ridge Roofing LLC", timezone=PHOENIX, texting_hours=TextingHours(PHOENIX)
)
TIME_PLACEHOLDERS = ("{first_time}", "{second_time}", "{third_time}")
# A bot text that promises the team will answer a question (the fixed text says it this way).
TEAM_PROMISE = re.compile(r"\bpass\w*\b.{0,40}\bteam\b", re.IGNORECASE)


# ---- the scripts ----


@dataclass(frozen=True)
class Scenario:
    name: str
    category: str
    texts: tuple[str, ...]
    expected_results: tuple[str, ...]
    expected_answers: dict[str, str] = field(default_factory=dict)
    alerts_include: tuple[str, ...] = ()
    never_say: tuple[str, ...] = ()
    # None: not checked. True/False: whether the number must end up on the do-not-call list.
    do_not_call: bool | None = None
    # True for wording only a real language model can follow. The --offline keyword stand-in
    # would fail these for its own reasons, so offline runs skip them.
    needs_real_jev: bool = False
    note: str = ""


def _strings(value: Any, where: str) -> tuple[str, ...]:
    """A TOML value that must be a string or a list of strings, as a tuple."""
    if isinstance(value, str):
        return (value,)
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return tuple(value)
    raise ValueError(f"{where} must be a string or a list of strings")


def load_scenarios(path: Path) -> list[Scenario]:
    """Read and check texts.toml. A mistake in the file stops the run before any paid call."""
    with path.open("rb") as file:
        document = tomllib.load(file)
    allowed_keys = {
        "name",
        "category",
        "texts",
        "result",
        "answers",
        "alerts_include",
        "never_say",
        "do_not_call",
        "needs_real_jev",
        "note",
    }
    scenarios: list[Scenario] = []
    for raw in document.get("scenario", []):
        name = str(raw.get("name", ""))
        unknown_keys = set(raw) - allowed_keys
        if not name or unknown_keys or "texts" not in raw or "result" not in raw:
            raise ValueError(
                f"scenario {name or '(no name)'}: needs name, texts and result; "
                f"unknown keys {sorted(unknown_keys)}"
            )
        answers = raw.get("answers", {})
        if not isinstance(answers, dict) or not all(isinstance(value, str) for value in answers.values()):
            raise ValueError(f"scenario {name}: answers must be a table of strings")
        never_say = _strings(raw.get("never_say", []), f"scenario {name}: never_say")
        for pattern in never_say:
            re.compile(pattern)  # a broken pattern fails here, not halfway through a paid run
        do_not_call = raw.get("do_not_call")
        if do_not_call is not None and not isinstance(do_not_call, bool):
            raise ValueError(f"scenario {name}: do_not_call must be true or false")
        needs_real_jev = raw.get("needs_real_jev", False)
        if not isinstance(needs_real_jev, bool):
            raise ValueError(f"scenario {name}: needs_real_jev must be true or false")
        texts = raw["texts"]
        if not isinstance(texts, list) or not all(isinstance(item, str) for item in texts):
            raise ValueError(f"scenario {name}: texts must be a list of strings (it may be empty)")
        scenarios.append(
            Scenario(
                name=name,
                category=str(raw.get("category", "")),
                texts=tuple(texts),
                expected_results=_strings(raw["result"], f"scenario {name}: result"),
                expected_answers=dict(answers),
                alerts_include=_strings(raw.get("alerts_include", []), f"scenario {name}: alerts_include"),
                never_say=never_say,
                do_not_call=do_not_call,
                needs_real_jev=needs_real_jev,
                note=str(raw.get("note", "")),
            )
        )
    names = [scenario.name for scenario in scenarios]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise ValueError(f"duplicate scenario names: {duplicates}")
    return scenarios


# ---- running one conversation ----


@dataclass(frozen=True)
class Turn:
    """One homeowner text and the bot's texts in reply (none, one, or a few)."""

    homeowner: str
    bot: tuple[str, ...]
    seconds: float  # how long the worker took to answer
    on_do_not_call_after: bool = False


@dataclass
class ScenarioReport:
    scenario: Scenario
    opening: tuple[str, ...]  # the first text, and any reminders sent before the first reply
    turns: list[Turn]
    afterwards: tuple[str, ...]  # texts sent after the script ran out (reminders)
    result: str | None
    answers: dict[str, str]
    alerts: list[str]
    booking: str | None
    on_do_not_call: bool
    writer_outcomes: list[WriterOutcome]
    failures: list[str]
    stopped_early: bool = False  # the bot closed the conversation before the script ran out
    crashed: str | None = None

    @property
    def passed(self) -> bool:
        return not self.failures and self.crashed is None

    @property
    def bot_texts(self) -> list[str]:
        return [*self.opening, *(text for turn in self.turns for text in turn.bot), *self.afterwards]


class CollectingSender:
    """Keeps every text the worker sends, in order, instead of sending it."""

    def __init__(self) -> None:
        self.sent: list[str] = []
        self._total = 0  # never reset: each id must be unique, like a real provider's

    def send(self, phone: str, body: str) -> str:
        self.sent.append(body)
        self._total += 1
        return f"SCENARIO{self._total}"

    def take(self) -> tuple[str, ...]:
        """The texts sent since the last take."""
        texts, self.sent = tuple(self.sent), []
        return texts


class ScenarioClock:
    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now


def _fill_in_times(text: str, store: Store, lead_id: int) -> str:
    """Replace {first_time} etc. with the times the bot actually offered."""
    if not any(placeholder in text for placeholder in TIME_PLACEHOLDERS):
        return text
    conversation = store.conversation(lead_id)
    offered = () if conversation is None else conversation.offered_slots
    for placeholder, slot in zip(TIME_PLACEHOLDERS, offered, strict=False):
        text = text.replace(placeholder, slot.label)
    return text


def run_scenario(scenario: Scenario, services: ConversationServices, work_directory: Path) -> ScenarioReport:
    """Play one scripted conversation. `services` gives Jev and the writer; the calendar is new."""
    settings = SCENARIO_SETTINGS
    clock = ScenarioClock(PRETEND_NOW.astimezone(PHOENIX))
    # Each scenario books on its own empty calendar, so they can run in parallel without taking
    # each other's times. The calendar's clock stays at the start, so the offered times never move.
    booking = CalendarBooking(settings.inspection_rules, MemoryCalendar(), clock=lambda: PRETEND_NOW)
    scenario_services = ConversationServices(
        ask_jev=services.ask_jev, booking=lambda: booking, writer=services.writer
    )
    writer_outcomes: list[WriterOutcome] = []

    def keep_writer_outcomes(outcome: TurnOutcome) -> None:
        writer_outcomes.extend(outcome.writer_outcomes)

    sender = CollectingSender()
    store = Store(work_directory / f"{scenario.name}.db")
    worker = Worker(store, settings, scenario_services, sender, clock=clock, on_turn=keep_writer_outcomes)
    lead = parse_lead_form("Dana Ruiz", "(602) 555-0142", "12 Saguaro Ln, Mesa AZ", "storm_damage")
    consent = ConsentEvidence(
        given=True,
        version=CONSENT_VERSION,
        text_shown=consent_text(settings.business_name),
        ip_address="127.0.0.1",
        forwarded_for=None,
        user_agent="scenario runner",
        source="scenarios.py",
    )
    added = store.add_lead(lead, consent, clock(), settings.texting_hours)
    lead_id = added.lead_id
    report = ScenarioReport(
        scenario=scenario,
        opening=(),
        turns=[],
        afterwards=(),
        result=None,
        answers={},
        alerts=[],
        booking=None,
        on_do_not_call=False,
        writer_outcomes=writer_outcomes,
        failures=[],
    )
    try:
        worker.run_once()
        report.opening = sender.take()
        for line in scenario.texts:
            conversation_over = store.conversation_result(lead_id) not in (None, "booked")
            if conversation_over or store.is_on_do_not_call(lead.phone):
                # The bot has stopped (handed off or opted out); the rest of the script is unused.
                report.stopped_early = True
                break
            clock.now += REPLY_DELAY
            text = _fill_in_times(line, store, lead_id)
            store.record_incoming_text(lead.phone, text, f"SCENARIOIN{len(report.turns) + 1}", clock())
            started = time.monotonic()
            worker.run_once()
            report.turns.append(
                Turn(text, sender.take(), time.monotonic() - started, store.is_on_do_not_call(lead.phone))
            )
        # Then time passes with no more texts, so reminders and quiet hand-offs happen.
        end = clock.now + AFTERWARDS_FOR
        while clock.now < end:
            clock.now += AFTERWARDS_STEP
            worker.run_once()
        report.afterwards = sender.take()
    except Exception as error:  # a crash is reported against this scenario, and the rest still run
        report.crashed = f"{type(error).__name__}: {error}"
    report.result = store.conversation_result(lead_id)
    report.answers = store.lead_answers(lead_id)
    report.alerts = [alert.reason for alert in store.owner_alerts()]
    report.booking = store.booking_label(lead_id)
    report.on_do_not_call = store.is_on_do_not_call(lead.phone)
    report.failures = check_scenario(report)
    return report


def check_scenario(report: ScenarioReport) -> list[str]:
    """Everything wrong with how this conversation went. Empty means it passed."""
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
    if scenario.do_not_call is not None and report.on_do_not_call != scenario.do_not_call:
        failures.append(f"on the do-not-call list: {report.on_do_not_call}, expected {scenario.do_not_call}")
    bot_texts = report.bot_texts
    for pattern in scenario.never_say:
        for text in bot_texts:
            if re.search(pattern, text, re.IGNORECASE):
                failures.append(f"said /{pattern}/: {text!r}")

    # Rules every conversation must keep, whatever the script says.
    if report.result == "opted_out" and not report.on_do_not_call:
        failures.append("opted out but the number isn't on the do-not-call list")
    if report.result == "booked" and report.booking is None:
        failures.append("ended as booked but no booking was recorded")
    if report.result != "booked" and report.booking is not None:
        failures.append(f"a booking ({report.booking}) was made but it ended as {report.result!r}")
    # After an opt-out: at most the one confirmation, in the same turn, then nothing ever again.
    opted_out_at = next((index for index, turn in enumerate(report.turns) if turn.on_do_not_call_after), None)
    if opted_out_at is not None:
        if len(report.turns[opted_out_at].bot) > 1:
            failures.append(f"{len(report.turns[opted_out_at].bot)} texts sent with the opt-out; at most 1")
        later = [text for turn in report.turns[opted_out_at + 1 :] for text in turn.bot]
        if later or report.afterwards:
            failures.append(f"texted after the opt-out: {[*later, *report.afterwards]!r}")
    for outcome in report.writer_outcomes:
        blocked_draft = None if outcome.sent else outcome.draft
        if blocked_draft is not None and any(blocked_draft in text for text in bot_texts):
            failures.append(f"a blocked draft was sent: {blocked_draft!r}")
    for text in bot_texts:
        if not is_gsm7(text):
            # One character outside the basic SMS alphabet makes the whole text cost 2-3x.
            failures.append(f"not plain SMS characters (sent as UCS-2): {text!r}")
    # "I'll pass that to our team" is only true if the owner was told.
    team_promises = [text for text in bot_texts if TEAM_PROMISE.search(text)]
    team_alerts = [alert for alert in report.alerts if alert.startswith("homeowner_question_for_the_team")]
    if len(team_promises) > len(team_alerts):
        failures.append(
            f"promised a team follow-up {len(team_promises)} times with {len(team_alerts)} owner alerts"
        )
    return failures


# ---- the report ----


def _percentiles(values: Sequence[float], unit: str = "s") -> str:
    if not values:
        return "none"
    if len(values) == 1:
        return f"{values[0]:.2f}{unit} (1)"
    deciles = statistics.quantiles(values, n=10, method="inclusive")
    return (
        f"median {statistics.median(values):.2f}{unit}, 90th percentile {deciles[8]:.2f}{unit}, "
        f"max {max(values):.2f}{unit} ({len(values)})"
    )


def write_report(reports: Sequence[ScenarioReport], mode: str, total_seconds: float, path: Path) -> str:
    passed = [report for report in reports if report.passed]
    all_turns = [turn for report in reports for turn in report.turns]
    outcomes = [outcome for report in reports for outcome in report.writer_outcomes]
    sent_drafts = [outcome for outcome in outcomes if outcome.sent]
    writer_turn_texts = {outcome.question for outcome in outcomes}
    script_seconds = [turn.seconds for turn in all_turns if turn.homeowner not in writer_turn_texts]
    writer_seconds = [outcome.seconds for outcome in outcomes]
    segments = [float(segment_count(text)) for report in reports for text in report.bot_texts]

    lines = [
        "# Scenario report",
        "",
        f"Run {datetime.now().astimezone():%Y-%m-%d %H:%M %Z} with {mode}. "
        f"{len(passed)} of {len(reports)} conversations passed, in {total_seconds:.0f}s.",
        "",
        "## Reply time (our side only; Twilio's delivery comes on top)",
        "",
        f"- Scripted replies: {_percentiles(script_seconds)}",
        f"- Writer + guard: {_percentiles(writer_seconds)}",
        f"- SMS segments per bot text: {_percentiles(segments, unit='')}",
        "",
        "## Writer",
        "",
        f"{len(outcomes)} drafts requested, {len(sent_drafts)} sent, "
        f"{len(outcomes) - len(sent_drafts)} replaced by the fixed text.",
        "",
        "| Scenario | Question | Draft | Verdict |",
        "| --- | --- | --- | --- |",
    ]
    for report in reports:
        for outcome in report.writer_outcomes:
            verdict = "sent" if outcome.sent else "blocked: " + ", ".join(outcome.problems)
            draft = (outcome.draft or "(none)").replace("|", "\\|").replace("\n", " ")
            lines.append(f"| {report.scenario.name} | {outcome.question} | {draft} | {verdict} |")

    lines += ["", "## Results by category", ""]
    for category in sorted({report.scenario.category for report in reports}):
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

    lines += ["", "## Conversations", ""]
    for report in reports:
        status = "PASS" if report.passed else "FAIL"
        lines.append(f"### {status} {report.scenario.name} ({report.scenario.category})")
        if report.scenario.note:
            lines.append(f"_{report.scenario.note}_")
        lines.append("")
        for text in report.opening:
            lines.append(f"- BOT: {_one_line(text)}")
        for turn in report.turns:
            lines.append(f"- HOMEOWNER: {turn.homeowner}")
            replies = " / ".join(_one_line(text) for text in turn.bot) or "(no reply)"
            lines.append(f"  - BOT ({turn.seconds:.2f}s): {replies}")
        if report.stopped_early:
            lines.append("- (the bot had stopped; the rest of the script wasn't sent)")
        for text in report.afterwards:
            lines.append(f"- BOT, later: {_one_line(text)}")
        lines.append(
            f"- ended: {report.result}; answers: {report.answers}; booking: {report.booking}; "
            f"do-not-call: {report.on_do_not_call}; alerts: {report.alerts}"
        )
        lines.append("")
    text = "\n".join(lines) + "\n"
    path.write_text(text, encoding="utf-8")
    return text


def _one_line(text: str) -> str:
    return text.replace("\n", " | ")


# ---- running them all ----


def build_services(offline: bool) -> tuple[ConversationServices, str]:
    """Jev and the writer, shared by every scenario. run_scenario adds each one's own calendar."""
    if offline:
        # No writer: off-script questions get the fixed text, as when the writer is down.
        return ConversationServices(ask_jev=OfflineJev(), booking=_no_calendar_yet), (
            "offline keyword stand-ins, no writer"
        )
    from .wiring import DEFAULT_WRITER_MODEL, build_jev, build_writer, require_model_keys

    try:
        require_model_keys()
    except RuntimeError as error:
        raise SystemExit(f"[scenarios] {error} Or pass --offline.") from None
    writer_model = os.environ.get("WRITER_MODEL", "").strip() or DEFAULT_WRITER_MODEL
    services = ConversationServices(ask_jev=build_jev().ask, booking=_no_calendar_yet, writer=build_writer())
    return services, f"real Jev and writer {writer_model}, in-memory calendar"


def _no_calendar_yet() -> CalendarBooking:
    raise RuntimeError("run_scenario replaces this with the scenario's own in-memory calendar")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--file", type=Path, default=Path("scenarios/texts.toml"))
    parser.add_argument("--report", type=Path, default=Path("scenarios/report.md"))
    parser.add_argument("--offline", action="store_true", help="keyword stand-in for Jev, and no writer")
    parser.add_argument("--only", help="run only this category or scenario name")
    parser.add_argument("--parallel", type=int, default=4, help="conversations at once (default 4)")
    arguments = parser.parse_args(argv)

    load_dotenv()
    scenarios = load_scenarios(arguments.file)
    if arguments.only:
        scenarios = [s for s in scenarios if arguments.only in (s.name, s.category)]
        if not scenarios:
            raise SystemExit(f"[scenarios] nothing matches --only {arguments.only}")
    if arguments.offline:
        skipped = [s.name for s in scenarios if s.needs_real_jev]
        scenarios = [s for s in scenarios if not s.needs_real_jev]
        if skipped:
            print(f"[scenarios] skipped offline (need real Jev): {', '.join(skipped)}")
    services, mode = build_services(arguments.offline)
    print(f"[scenarios] {len(scenarios)} conversations, {mode}")

    started = time.monotonic()
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as work_directory:
        with ThreadPoolExecutor(max_workers=max(1, arguments.parallel)) as pool:
            reports = list(pool.map(lambda s: run_scenario(s, services, Path(work_directory)), scenarios))
    total_seconds = time.monotonic() - started

    for report in reports:
        mark = "PASS" if report.passed else "FAIL"
        problems = [*report.failures, *([report.crashed] if report.crashed else [])]
        detail = "" if report.passed else "  " + "; ".join(problems)
        print(f"{mark} {report.scenario.name}{detail}")
    write_report(reports, mode, total_seconds, arguments.report)
    passed_count = sum(report.passed for report in reports)
    print(f"[scenarios] {passed_count}/{len(reports)} passed in {total_seconds:.0f}s")
    print(f"[scenarios] report: {arguments.report}")
    return 0 if passed_count == len(reports) else 1


if __name__ == "__main__":
    sys.exit(main())
