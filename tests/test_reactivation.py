# lead-bot/tests/test_reactivation.py
# Reactivation campaigns: the contact list is checked row by row (every consent column
# required), the import leaves out numbers that mustn't be texted, the worker starts contacts at
# the daily limit spread across the window, each contact is checked again at its turn, and the
# conversation starts with "still interested?" and gets one reminder, with no owner alert when
# nobody replies.
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest
from conftest import DANA_PHONE, HOURS, make_consent, make_lead, phoenix
from test_owner_alerts import RecordingMailer
from text_fakes import FIRST_OFFERED_TIME, SETTINGS, Clock, RecordingSender, ScriptedJev, make_worker, reply

from lead_bot.leads import ReactivationContact, pretty_phone
from lead_bot.owner_alerts import OwnerAlertNotifier
from lead_bot.reactivation import (
    REQUIRED_COLUMNS,
    InvalidContactList,
    import_report,
    main,
    read_contact_list,
    status_report,
)
from lead_bot.store import IMPORTED_CONSENT_VERSION, CampaignExists, Store
from lead_bot.text_script import (
    NOT_INTERESTED_TEXT,
    is_gsm7,
    reactivation_first_text,
)
from lead_bot.timing import max_text_attempts

TODAY = date(2026, 10, 5)
WORDING = "I agree that Desert Ridge Roofing LLC may send me automated texts about my roof."
SOURCE = "https://desertridgeroofing.example/quote"
HEADER = ",".join(REQUIRED_COLUMNS)
# A second homeowner, built so no phone-like string is ever typed whole in a test.
SAM_PHONE = "+1" + "602" + "555" + "0188"


def contact(phone: str = DANA_PHONE, name: str = "Dana Ruiz") -> ReactivationContact:
    return ReactivationContact(
        full_name=name,
        phone=phone,
        address="12 Saguaro Ln, Mesa AZ",
        consented_on=date(2025, 3, 14),
        consent_source=SOURCE,
        consent_wording=WORDING,
    )


def numbered_contacts(count: int) -> list[ReactivationContact]:
    # 555-01xx numbers are reserved for fiction; built from parts so none is typed whole.
    return [
        contact(phone="+1" + "480" + "555" + f"01{number:02d}", name=f"Owner {number}")
        for number in range(count)
    ]


def write_csv(tmp_path: Path, *rows: str, header: str = HEADER) -> Path:
    path = tmp_path / "old-leads.csv"
    path.write_text("\n".join([header, *rows]) + "\n", encoding="utf-8")
    return path


def good_row(phone: str = pretty_phone(DANA_PHONE), consent_date: str = "2025-03-14") -> str:
    return f'Dana Ruiz,{phone},"12 Saguaro Ln, Mesa AZ",{consent_date},{SOURCE},"{WORDING}"'


def first_texts(sender: RecordingSender) -> list[str]:
    return [body for body in sender.bodies if "You asked us about your roof a while back" in body]


# ---- reading the list ----


def test_a_good_list_becomes_contacts(tmp_path: Path) -> None:
    result = read_contact_list(write_csv(tmp_path, good_row()), TODAY)
    assert result.refused == []
    assert result.contacts == [contact()]


def test_headers_match_whatever_their_case_and_an_excel_marker_is_ignored(tmp_path: Path) -> None:
    path = tmp_path / "excel.csv"
    header = " Full_Name ,PHONE,Address,Consent_Date,consent_source,Consent_Wording"
    path.write_text("\ufeff" + header + "\n" + good_row() + "\n", encoding="utf-8")
    assert read_contact_list(path, TODAY).contacts == [contact()]


def test_a_list_without_a_required_column_is_refused_whole(tmp_path: Path) -> None:
    header = "full_name,phone,address,consent_date,consent_source"
    with pytest.raises(InvalidContactList, match="missing the column\\(s\\) consent_wording"):
        read_contact_list(write_csv(tmp_path, "a,b,c,d,e", header=header), TODAY)


@pytest.mark.parametrize("column", REQUIRED_COLUMNS)
def test_a_row_missing_any_value_is_refused_by_line_number(tmp_path: Path, column: str) -> None:
    values = {
        "full_name": "Dana Ruiz",
        "phone": pretty_phone(DANA_PHONE),
        "address": "12 Saguaro Ln",
        "consent_date": "2025-03-14",
        "consent_source": SOURCE,
        "consent_wording": WORDING,
    }
    values[column] = "  "
    row = ",".join(f'"{values[name]}"' for name in REQUIRED_COLUMNS)
    result = read_contact_list(write_csv(tmp_path, row), TODAY)
    assert result.contacts == []
    assert [(refused.line_number, refused.reason) for refused in result.refused] == [(2, f"missing {column}")]


def test_bad_phones_dates_and_repeats_are_refused_and_blank_lines_skipped(tmp_path: Path) -> None:
    path = write_csv(
        tmp_path,
        good_row(),
        good_row(phone="call me"),
        good_row(phone=pretty_phone(SAM_PHONE), consent_date="March 2025"),
        good_row(phone=pretty_phone(SAM_PHONE), consent_date="2026-10-06"),
        "",
        good_row(),
    )
    result = read_contact_list(path, TODAY)
    assert len(result.contacts) == 1
    assert [(refused.line_number, refused.reason) for refused in result.refused] == [
        (3, "phone isn't a 10-digit US number"),
        (4, "consent_date must be a date like 2025-03-14"),
        (5, "consent_date is in the future"),
        (7, "same phone as line 2"),
    ]


# ---- importing ----


def test_an_imported_contact_is_a_waiting_lead_with_its_consent_proof(store: Store) -> None:
    result = store.add_campaign("spring", "old-leads.csv", 50, [contact()], phoenix(2026, 10, 5, 7), HOURS)
    (lead_id,) = result.lead_ids
    assert store.lead_status(lead_id) == "campaign_waiting"
    conversation = store.conversation(lead_id)
    assert conversation is not None and conversation.stage == "still_interested"
    assert store.transcript(lead_id) == []  # nothing is texted at import
    with store._read() as connection:
        consent = connection.execute("SELECT * FROM consents WHERE lead_id = ?", (lead_id,)).fetchone()
        kind = connection.execute("SELECT kind FROM conversations WHERE lead_id = ?", (lead_id,)).fetchone()[
            0
        ]
    assert (consent["given"], consent["version"], consent["text_shown"], consent["source"]) == (
        1,
        IMPORTED_CONSENT_VERSION,
        WORDING,
        SOURCE,
    )
    assert phoenix(2025, 3, 14, 0).timestamp() < consent["consented_at"] < phoenix(2025, 3, 15, 0).timestamp()
    assert kind == "reactivation"


def test_numbers_that_mustn_t_be_texted_are_left_out(store: Store) -> None:
    opted_out, in_conversation, earlier = numbered_contacts(3)
    store.add_to_do_not_call(opted_out.phone, "replied STOP", phoenix(2026, 10, 1, 9))
    store.add_lead(make_lead(phone=in_conversation.phone), make_consent(), phoenix(2026, 10, 1, 9), HOURS)
    store.add_campaign("first", "a.csv", 50, [earlier], phoenix(2026, 10, 2, 9), HOURS)

    result = store.add_campaign(
        "second",
        "b.csv",
        50,
        [opted_out, in_conversation, earlier, contact()],
        phoenix(2026, 10, 5, 7),
        HOURS,
    )

    assert result.added == 1
    assert result.skipped == {
        "on the do-not-call list": [opted_out.phone],
        "already in a conversation": [in_conversation.phone],
        "in an earlier campaign": [earlier.phone],
    }


def test_a_dry_run_reports_the_same_and_saves_nothing(store: Store) -> None:
    store.add_to_do_not_call(SAM_PHONE, "replied STOP", phoenix(2026, 10, 1, 9))
    contacts = [contact(), contact(phone=SAM_PHONE, name="Sam Lee")]

    dry = store.add_campaign("spring", "a.csv", 50, contacts, phoenix(2026, 10, 5, 7), HOURS, save=False)

    assert (dry.campaign_id, dry.added, list(dry.skipped)) == (None, 1, ["on the do-not-call list"])
    assert store.campaign_summaries() == []
    real = store.add_campaign("spring", "a.csv", 50, contacts, phoenix(2026, 10, 5, 7), HOURS)
    assert (real.added, real.skipped) == (dry.added, dry.skipped)


def test_a_campaign_name_is_used_once(store: Store) -> None:
    store.add_campaign("spring", "a.csv", 50, [contact()], phoenix(2026, 10, 5, 7), HOURS)
    with pytest.raises(CampaignExists):
        store.add_campaign("spring", "b.csv", 50, [contact(phone=SAM_PHONE)], phoenix(2026, 10, 5, 7), HOURS)


# ---- the daily limit and spacing ----


def run_day_by_day(store: Store, clock: Clock, sender: RecordingSender, days: int) -> list[int]:
    """Run the worker every 10 minutes for `days` days; how many first texts went each day."""
    worker, _ = make_worker(store, ScriptedJev(), clock, sender=sender)
    per_day: list[int] = []
    for _ in range(days):
        before = len(first_texts(sender))
        for _ in range(24 * 6):
            worker.run_once()
            clock.now += timedelta(minutes=10)
        per_day.append(len(first_texts(sender)) - before)
    return per_day


def test_no_more_than_the_daily_limit_start_each_day(store: Store) -> None:
    store.add_campaign("spring", "a.csv", 3, numbered_contacts(7), phoenix(2026, 10, 4, 20), HOURS)
    clock, sender = Clock(phoenix(2026, 10, 5, 0)), RecordingSender()
    assert run_day_by_day(store, clock, sender, days=3) == [3, 3, 1]


def test_contacts_are_spread_across_the_window_not_sent_at_once(store: Store) -> None:
    # 50 a day over the 8:00-20:30 window: one every 15 minutes.
    store.add_campaign("spring", "a.csv", 50, numbered_contacts(5), phoenix(2026, 10, 5, 7), HOURS)
    clock = Clock(phoenix(2026, 10, 5, 10, 0))
    worker, sender = make_worker(store, ScriptedJev(), clock)
    worker.run_once()
    clock.now += timedelta(minutes=14)
    worker.run_once()
    assert len(first_texts(sender)) == 1
    clock.now += timedelta(minutes=1)
    worker.run_once()
    assert len(first_texts(sender)) == 2


def test_the_daily_limit_holds_even_when_the_spacing_is_bypassed(store: Store) -> None:
    # The spacing alone already keeps a day near the limit, so this clears it by hand before
    # every pass (as a hand-edited database would) to check the limit works on its own.
    store.add_campaign("spring", "a.csv", 3, numbered_contacts(7), phoenix(2026, 10, 4, 20), HOURS)
    clock = Clock(phoenix(2026, 10, 5, 9, 0))
    worker, sender = make_worker(store, ScriptedJev(), clock)
    for _ in range(10):
        with store._write_transaction() as connection:
            connection.execute("UPDATE campaigns SET last_started_at = NULL")
        worker.run_once()
        clock.now += timedelta(minutes=1)
    assert len(first_texts(sender)) == 3


def test_nothing_starts_outside_the_texting_window(store: Store) -> None:
    (lead_id,) = store.add_campaign(
        "spring", "a.csv", 50, [contact()], phoenix(2026, 10, 5, 6), HOURS
    ).lead_ids
    worker, sender = make_worker(store, ScriptedJev(), Clock(phoenix(2026, 10, 5, 21, 0)))
    worker.run_once()
    assert sender.sent == []
    # Not started at all, rather than started and then held by the queue-time window check.
    assert store.lead_status(lead_id) == "campaign_waiting"


# ---- checked again at its turn ----


def test_a_number_put_on_do_not_call_by_hand_after_the_import_is_never_texted(store: Store) -> None:
    # A raw insert, as an owner with a database tool would: no lead status changes with it, so
    # only the check at the contact's turn (and queue-time) stands between it and a text.
    (lead_id,) = store.add_campaign(
        "spring", "a.csv", 50, [contact()], phoenix(2026, 10, 5, 7), HOURS
    ).lead_ids
    with store._write_transaction() as connection:
        connection.execute(
            "INSERT INTO do_not_call (phone, added_at, source) VALUES (?, ?, 'owner')",
            (DANA_PHONE, phoenix(2026, 10, 5, 7).timestamp()),
        )
    worker, sender = make_worker(store, ScriptedJev(), Clock())
    worker.run_once()
    assert sender.sent == []
    assert store.lead_status(lead_id) == "opted_out"
    # Skipped at its turn, not merely stopped later by the queue-time check: this layer works alone.
    with store._read() as connection:
        skipped = connection.execute("SELECT skipped FROM campaign_contacts WHERE lead_id = ?", (lead_id,))
        assert skipped.fetchone()[0] == "on the do-not-call list"


def test_an_opt_out_from_another_conversation_stops_the_waiting_contact(store: Store) -> None:
    (lead_id,) = store.add_campaign(
        "spring", "a.csv", 50, [contact()], phoenix(2026, 10, 5, 7), HOURS
    ).lead_ids
    store.add_to_do_not_call(DANA_PHONE, "replied STOP", phoenix(2026, 10, 5, 8))
    assert store.lead_status(lead_id) == "opted_out"
    worker, sender = make_worker(store, ScriptedJev(), Clock())
    worker.run_once()
    assert sender.sent == []


def test_a_contact_who_filled_in_the_form_since_the_import_gets_one_conversation(store: Store) -> None:
    (lead_id,) = store.add_campaign(
        "spring", "a.csv", 50, [contact()], phoenix(2026, 10, 5, 7), HOURS
    ).lead_ids
    store.add_lead(make_lead(), make_consent(), phoenix(2026, 10, 5, 9), HOURS)
    worker, sender = make_worker(store, ScriptedJev(), Clock())
    worker.run_once()
    assert len(sender.sent) == 1 and first_texts(sender) == []  # only the form lead's first text
    assert store.lead_status(lead_id) == "duplicate"


# ---- the conversation ----


def start_campaign(store: Store, jev: ScriptedJev) -> tuple[Clock, RecordingSender, int]:
    (lead_id,) = store.add_campaign(
        "spring", "a.csv", 50, [contact()], phoenix(2026, 10, 5, 7), HOURS
    ).lead_ids
    clock = Clock()
    worker, sender = make_worker(store, jev, clock)
    worker.run_once()
    return clock, sender, lead_id


def test_the_first_text_says_why_and_asks_if_they_still_want_it(store: Store) -> None:
    _, sender, _ = start_campaign(store, ScriptedJev())
    assert sender.sent == [
        (DANA_PHONE, reactivation_first_text(SETTINGS, "Dana Ruiz", "12 Saguaro Ln, Mesa AZ"))
    ]
    body = sender.bodies[0]
    assert body.startswith("Hi Dana, this is the automated assistant for Desert Ridge Roofing LLC.")
    assert "Would you still like a free roof inspection at 12 Saguaro Ln, Mesa AZ?" in body
    assert body.endswith("Reply HELP for help, STOP to opt out.")
    assert is_gsm7(body)


def test_a_yes_goes_on_to_the_usual_questions_and_books(store: Store) -> None:
    jev = ScriptedJev("yes", "no", "no", "yes", FIRST_OFFERED_TIME)
    clock, sender, lead_id = start_campaign(store, jev)
    worker, _ = make_worker(store, jev, clock, sender=sender)
    for text in ["yes please", "no", "no", "yes", "the first one"]:
        reply(store, worker, clock, DANA_PHONE, text)
    assert sender.bodies[1] == "Great. Is water coming into the house right now?"
    assert store.booking_label(lead_id) is not None
    assert store.lead_answers(lead_id)["address_confirmed"] == "yes"


def test_no_longer_needed_gets_one_polite_text_and_never_another(store: Store) -> None:
    jev = ScriptedJev("no")
    clock, sender, lead_id = start_campaign(store, jev)
    worker, _ = make_worker(store, jev, clock, sender=sender)
    reply(store, worker, clock, DANA_PHONE, "no thanks, already got it fixed")
    assert sender.bodies[-1] == NOT_INTERESTED_TEXT
    assert store.is_on_do_not_call(DANA_PHONE)
    assert store.conversation_result(lead_id) == "not_interested"
    clock.now += timedelta(days=5)
    worker.run_once()
    assert sender.bodies[-1] == NOT_INTERESTED_TEXT  # no reminder after it


def test_a_different_address_is_flagged_and_the_questions_go_on(store: Store) -> None:
    jev = ScriptedJev("different_address")
    clock, sender, lead_id = start_campaign(store, jev)
    worker, _ = make_worker(store, jev, clock, sender=sender)
    reply(store, worker, clock, DANA_PHONE, "yes but we moved to 40 Palm Dr")
    assert sender.bodies[-1].endswith("Is water coming into the house right now?")
    assert [alert.reason for alert in store.owner_alerts()] == ["address_needs_correcting"]


def test_one_reminder_two_days_later_then_no_owner_alert(store: Store) -> None:
    clock, sender, lead_id = start_campaign(store, ScriptedJev())
    worker, _ = make_worker(store, ScriptedJev(), clock, sender=sender)
    for _ in range(7 * 24):
        clock.now += timedelta(hours=1)
        worker.run_once()
    assert len(sender.sent) == max_text_attempts("reactivation") == 2
    assert sender.bodies[1].endswith("This is our last text about it. Reply STOP to opt out.")
    assert store.lead_status(lead_id) == "unreached"
    assert store.owner_alerts() == []


def test_a_reply_after_the_campaign_went_quiet_is_still_answered(store: Store) -> None:
    jev = ScriptedJev("yes")
    clock, sender, lead_id = start_campaign(store, jev)
    worker, _ = make_worker(store, jev, clock, sender=sender)
    for _ in range(7 * 24):
        clock.now += timedelta(hours=1)
        worker.run_once()
    reply(store, worker, clock, DANA_PHONE, "yes actually")
    assert sender.bodies[-1] == "Great. Is water coming into the house right now?"


def test_the_owner_email_says_the_lead_came_from_the_list(store: Store) -> None:
    jev = ScriptedJev("different_address")
    clock, sender, _ = start_campaign(store, jev)
    worker, _ = make_worker(store, jev, clock, sender=sender)
    reply(store, worker, clock, DANA_PHONE, "we moved")
    mailer = RecordingMailer()
    OwnerAlertNotifier(
        store, SETTINGS, mailer, "bot@example.com", "owner@example.com", clock=clock
    ).notify_pending()
    body = mailer.sent[0].get_content()
    assert "From the reactivation list: spring" in body
    assert "On the form" not in body


# ---- reports and the command ----


def test_the_import_report_lists_refusals_skips_and_each_wording(store: Store, tmp_path: Path) -> None:
    contact_list = read_contact_list(write_csv(tmp_path, good_row(), good_row(phone="call me")), TODAY)
    result = store.add_campaign(
        "spring", "a.csv", 50, contact_list.contacts, phoenix(2026, 10, 5, 7), HOURS, save=False
    )
    report = import_report(contact_list, result, 50, dry_run=True)
    assert report.startswith("DRY RUN: nothing was saved.")
    assert "line 3: phone isn't a 10-digit US number" in report
    assert f'1 contact: "{WORDING}"' in report
    assert "take 1 day" in report


def test_the_command_does_a_dry_run_then_imports_then_reports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    database = tmp_path / "leads.db"
    monkeypatch.setenv("LEAD_BOT_DB_PATH", str(database))
    monkeypatch.setenv("BUSINESS_NAME", "Desert Ridge Roofing LLC")
    monkeypatch.setenv("BUSINESS_CALLBACK_PHONE", pretty_phone(SAM_PHONE))
    monkeypatch.setenv("CLIENT_CONFIG_PATH", str(Path(__file__).parent.parent / "client.example.toml"))
    path = write_csv(tmp_path, good_row())

    main(["import", str(path), "--name", "spring", "--dry-run"])
    assert "DRY RUN" in capsys.readouterr().out
    assert Store(database).campaign_summaries() == []

    main(["import", str(path), "--name", "spring", "--daily-limit", "20"])
    assert "Campaign saved" in capsys.readouterr().out
    main(["status"])
    status = capsys.readouterr().out
    assert "spring (20 a day" in status and "waiting for their turn: 1" in status


def test_status_with_no_campaigns() -> None:
    assert status_report([]) == "No campaigns yet."
