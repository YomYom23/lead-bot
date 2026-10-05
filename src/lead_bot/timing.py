# lead-bot/src/lead_bot/timing.py
"""When the bot may start a text: the texting window and the reminder schedule.

The FCC treats a text message as a "call" under the federal robocall law, so the quiet-hours rule
for telemarketing (47 CFR 64.1200(c)(1): nothing before 8 a.m. or after 9 p.m. in the recipient's
local time) is applied to every text the bot STARTS: the first text and each reminder. Every one
passes through next_allowed_time when it's scheduled, and the worker checks is_allowed again right
before it sends one.

A reply to a text the homeowner just sent is not held for the window: they wrote first and are
waiting for the answer. That reading goes on the list for the lawyer, as do state rules that are
stricter than the federal one.

Version 1 uses the business's timezone for the lead's local time: a roofer serves one area, so its
leads live in its timezone. A lead in another timezone would need their own (from the address)
before this is safe for a business near a timezone line.

Every datetime here is timezone-aware. Naive datetimes are refused, because Python's astimezone()
treats a naive datetime as the computer's own local time, which is silently wrong on a server in
another timezone.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

# The federal limits. 8:00 itself is allowed (the rule bans calls "before the hour of 8 a.m."),
# and 21:00 is not.
LEGAL_EARLIEST_TEXT = time(8, 0)
LEGAL_LATEST_TEXT = time(21, 0)

# The default last start time is 30 minutes inside the legal limit. A text due at 8:59 p.m. can
# go out at 9:00:05 after a slow worker pass or a retry, and lawsuits over messages that arrived a
# few minutes late are common. The margin absorbs that.
DEFAULT_LATEST_TEXT = time(20, 30)

# Placeholder reminder schedule. If the homeowner hasn't replied, remind them 2 hours, 1 day and
# 3 days after the first text, then stop. Each delay is measured from the first text, so a late
# reminder doesn't push every later one back.
REMINDER_DELAYS = (timedelta(hours=2), timedelta(days=1), timedelta(days=3))
MAX_TEXT_ATTEMPTS = 1 + len(REMINDER_DELAYS)

# Two opening texts are never closer together than this. If the worker was down and planned
# reminder times have passed, they'd otherwise all come due at once and arrive back to back.
MINIMUM_GAP_BETWEEN_TEXTS = timedelta(hours=1)


@dataclass(frozen=True)
class TextingHours:
    timezone: ZoneInfo
    earliest: time = LEGAL_EARLIEST_TEXT  # the first moment a text may be started
    latest: time = DEFAULT_LATEST_TEXT  # texts must start BEFORE this; it is not included

    def __post_init__(self) -> None:
        # A narrower window than the law allows is fine (a business may not want 8 a.m. texts).
        # A wider one is refused here, so no setting can ever produce an illegal send time.
        if self.earliest < LEGAL_EARLIEST_TEXT or self.latest > LEGAL_LATEST_TEXT:
            raise ValueError(
                f"texting hours {self.earliest}-{self.latest} go outside the legal 8:00-21:00 window"
            )
        if self.earliest >= self.latest:
            raise ValueError(f"texting hours start ({self.earliest}) must be before the end ({self.latest})")


def utc_now() -> datetime:
    """The real clock. Everything else takes `now` as an argument so tests can fix the time."""
    return datetime.now(UTC)


def is_allowed(moment: datetime, hours: TextingHours) -> bool:
    """True if the bot may start a text at this moment."""
    _require_timezone(moment)
    local_time = moment.astimezone(hours.timezone).time()
    return hours.earliest <= local_time < hours.latest


def next_allowed_time(moment: datetime, hours: TextingHours) -> datetime:
    """The moment itself if a text may start then, otherwise the next opening of the window.

    Always returned in UTC, which is how the store saves times.
    """
    _require_timezone(moment)
    if is_allowed(moment, hours):
        return moment.astimezone(UTC)
    local_moment = moment.astimezone(hours.timezone)
    # Built from the local date plus a local clock time, so a daylight-saving change can't move
    # the opening off 8:00. Adding hours to a UTC time would drift by one hour twice a year.
    opening_today = datetime.combine(local_moment.date(), hours.earliest, tzinfo=hours.timezone)
    if local_moment < opening_today:
        return opening_today.astimezone(UTC)
    opening_tomorrow = datetime.combine(
        local_moment.date() + timedelta(days=1), hours.earliest, tzinfo=hours.timezone
    )
    return opening_tomorrow.astimezone(UTC)


def reminder_time(
    first_text_sent: datetime, texts_sent: int, now: datetime, hours: TextingHours
) -> datetime | None:
    """When to send the next reminder after `texts_sent` opening texts got no reply, or None to stop."""
    if texts_sent < 1:
        raise ValueError("a reminder only exists after at least one text")
    if texts_sent >= MAX_TEXT_ATTEMPTS:
        return None
    planned = first_text_sent + REMINDER_DELAYS[texts_sent - 1]
    earliest_permitted = now + MINIMUM_GAP_BETWEEN_TEXTS
    return next_allowed_time(max(planned, earliest_permitted), hours)


def clock_label(moment: datetime, timezone: ZoneInfo) -> str:
    """'8:00 AM' style. Built by hand because strftime's no-padding flag (%-I) fails on Windows."""
    local_moment = moment.astimezone(timezone)
    hour_on_clock = local_moment.hour % 12 or 12  # 0 and 12 both show as 12
    period = "AM" if local_moment.hour < 12 else "PM"
    return f"{hour_on_clock}:{local_moment.minute:02d} {period}"


def _require_timezone(moment: datetime) -> None:
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError(f"{moment!r} has no timezone; use datetime.now(UTC) or attach a ZoneInfo")
