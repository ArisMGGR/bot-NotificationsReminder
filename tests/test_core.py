"""Tests for the platform-independent core: storage, timing, logs, the loop.

Nothing here needs a chat library, a token or a network.

Run with:  python -m pytest
"""

import asyncio
import json
import logging
import os

import pytest

from helpers import make_reminder, read_next_id, read_store, write_store
from reminderbot import core


# ---------------------------------------------------------------------------
# parse_interval / format_duration
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("5 days", (5, "days", 5 * 86400)),
        ("  30 MINUTES ", (30, "minutes", 1800)),
        ("2h", (2, "h", 7200)),
        ("1 week", (1, "week", 604800)),
        ("45 seconds", (45, "seconds", 45)),
    ],
)
def test_parse_interval_accepts_valid_forms(text, expected):
    assert core.parse_interval(text) == expected


@pytest.mark.parametrize(
    "text",
    ["", "days", "5", "0 days", "-3 days", "5 fortnights", "5 days please",
     "²  days", "²days", "5.5 days", "abc"],
)
def test_parse_interval_rejects_junk(text):
    assert core.parse_interval(text) is None


def test_parse_interval_caps_the_interval():
    """F-014: a few hundred digits used to overflow due_at() and stop the loop."""
    assert core.parse_interval(f"{core.MAX_INTERVAL_WEEKS} weeks")[2] == core.MAX_INTERVAL_SECONDS
    assert core.parse_interval(f"{core.MAX_INTERVAL_WEEKS + 1} weeks") is None
    assert core.parse_interval(f"{core.MAX_INTERVAL_SECONDS + 1}s") is None
    assert core.parse_interval("1" * 400 + " s") is None
    assert core.parse_interval("9" * 4000 + " weeks") is None


def test_format_duration_marks_overdue():
    assert core.format_duration(45) == "in 45s"
    assert core.format_duration(3600) == "in 1h 0m"
    assert core.format_duration(-90).startswith("overdue by")


def test_status_text_prefixes_only_upcoming_reminders():
    upcoming = make_reminder(last_reset=core.now(), interval_seconds=3600)
    overdue = make_reminder(last_reset=core.now() - 7200, interval_seconds=3600)
    assert core.status_text(upcoming).startswith("next in")
    assert core.status_text(overdue).startswith("overdue by")


# ---------------------------------------------------------------------------
# Store validation
# ---------------------------------------------------------------------------


def test_load_reminders_returns_empty_when_missing():
    assert core.load_reminders() == []


def test_load_reminders_round_trips():
    write_store([make_reminder()])
    assert core.load_reminders()[0]["title"] == "Check Uni"


def test_load_reminders_reads_old_entries_as_telegram():
    """Files from before Discord support have no "platform" field."""
    old = make_reminder()
    del old["platform"]
    write_store([old])
    assert core.load_reminders()[0]["platform"] == "telegram"


@pytest.mark.parametrize(
    "payload",
    [
        [1, 2, 3],                                        # not dicts at all
        [{"id": 1}],                                      # missing everything else
        [{**make_reminder(), "chat_id": "42"}],           # right key, wrong type
        [{**make_reminder(), "id": True}],                # bool sneaking in as int
        [{**make_reminder(), "platform": "myspace"}],     # unknown platform
        [make_reminder(id=1), make_reminder(id=1, chat_id=99)],  # shared id
        [make_reminder(interval_seconds=10 ** 400)],      # due_at() would overflow
        [make_reminder(last_reset=float("inf"))],         # JSON Infinity
        {"reminders": [make_reminder()]},                 # store without next_id
        {"next_id": 0, "reminders": [make_reminder()]},   # counter out of range
        {"next_id": 2, "reminders": "nope"},              # reminders not a list
    ],
)
def test_load_reminders_quarantines_malformed_entries(payload):
    write_store(payload)
    assert core.load_reminders() == []
    assert not os.path.exists(core.DATA_FILE)
    quarantined = [
        n for n in os.listdir(os.path.dirname(core.DATA_FILE))
        if ".corrupt." in n
    ]
    assert quarantined, "the damaged file must be preserved, not deleted"


def test_load_reminders_quarantines_invalid_json():
    with open(core.DATA_FILE, "w", encoding="utf-8") as f:
        f.write("{not json")
    assert core.load_reminders() == []


def test_load_reminders_quarantines_text_that_isnt_utf8():
    """A hand edit saved as latin-1 must be moved aside, not crash every call."""
    raw = json.dumps([make_reminder(title="café")], ensure_ascii=False)
    with open(core.DATA_FILE, "wb") as f:
        f.write(raw.encode("latin-1"))
    assert core.load_reminders() == []
    assert not os.path.exists(core.DATA_FILE)
    assert any(".corrupt." in n for n in os.listdir(os.path.dirname(core.DATA_FILE)))


def test_apply_reminder_change_skips_the_write_when_nothing_changed():
    write_store([make_reminder()])
    before = os.stat(core.DATA_FILE).st_mtime_ns

    assert core._apply_reminder_change(1, lambda r: False) is True
    assert os.stat(core.DATA_FILE).st_mtime_ns == before, "no-op must not rewrite"

    def bump(reminder):
        reminder["alerting"] = True
        return True

    assert core._apply_reminder_change(1, bump) is True
    assert read_store()[0]["alerting"] is True


def test_apply_reminder_change_reports_a_vanished_reminder():
    write_store([])
    assert core._apply_reminder_change(99, lambda r: True) is False


# ---------------------------------------------------------------------------
# Migration from telegram_reminders.json
# ---------------------------------------------------------------------------


def test_legacy_data_file_is_moved_once():
    with open(core.LEGACY_DATA_FILE, "w", encoding="utf-8") as f:
        f.write("[]")
    core.migrate_legacy_data_file()
    assert os.path.exists(core.DATA_FILE)
    assert not os.path.exists(core.LEGACY_DATA_FILE)


def test_legacy_data_file_never_overwrites_newer_reminders():
    write_store([make_reminder(title="new")])
    with open(core.LEGACY_DATA_FILE, "w", encoding="utf-8") as f:
        f.write("[]")
    core.migrate_legacy_data_file()
    assert read_store()[0]["title"] == "new"
    assert os.path.exists(core.LEGACY_DATA_FILE)


# ---------------------------------------------------------------------------
# Shared store operations keep platforms and chats apart
# ---------------------------------------------------------------------------


def test_reminders_for_filters_by_platform_and_chat_and_sorts():
    now = core.now()
    write_store([
        make_reminder(id=1, title="later", interval_seconds=9000, last_reset=now),
        make_reminder(id=2, title="sooner", interval_seconds=10, last_reset=now),
        make_reminder(id=3, platform="discord", title="discord one"),
        make_reminder(id=4, chat_id=99, title="other chat"),
    ])
    titles = [r["title"] for r in asyncio.run(core.reminders_for("telegram", 42))]
    assert titles == ["sooner", "later"]
    assert [r["id"] for r in asyncio.run(core.reminders_for("discord", 42))] == [3]


def test_reminders_for_waits_for_the_store_lock(monkeypatch):
    """Reads go through store_lock too (CONTRIBUTING.md), so a list can't be
    built halfway through someone else's read-modify-write."""
    monkeypatch.setattr(core, "store_lock", asyncio.Lock())  # bound to this test's loop
    write_store([make_reminder()])

    async def scenario():
        async with core.store_lock:
            task = asyncio.create_task(core.reminders_for("telegram", 42))
            await asyncio.sleep(0.05)
            assert not task.done(), "read the store while another holder had the lock"
        return await task

    assert len(asyncio.run(scenario())) == 1


def test_add_reminder_records_the_platform():
    reminder = asyncio.run(core.add_reminder("discord", 7, "Water plants", 2, "days", 172800))
    assert reminder["id"] == 1
    stored = read_store()
    assert stored[0]["platform"] == "discord"
    assert stored[0]["chat_id"] == 7


def test_act_on_reminder_cannot_touch_another_platforms_reminder():
    """Same chat id, different platform: a button there must not reach it."""
    write_store([make_reminder(id=1, platform="telegram", chat_id=42)])
    assert asyncio.run(core.act_on_reminder("discord", 42, 1, "del")) is None
    assert len(read_store()) == 1


def test_deleted_ids_are_never_handed_out_again():
    """F-016: alerts and lists keep their buttons, so an id must stay retired."""
    first = asyncio.run(core.add_reminder("telegram", 42, "Water plants", 1, "days", 86400))
    asyncio.run(core.act_on_reminder("telegram", 42, first["id"], "del"))
    second = asyncio.run(core.add_reminder("telegram", 42, "Pay rent", 1, "days", 86400))
    assert second["id"] != first["id"]

    # The old alert's OK now finds nothing, instead of resetting "Pay rent".
    assert asyncio.run(core.act_on_reminder("telegram", 42, first["id"], "reset")) is None
    assert asyncio.run(core.act_on_reminder("telegram", 42, first["id"], "del")) is None
    assert [r["title"] for r in read_store()] == ["Pay rent"]


def test_a_bare_list_store_counts_on_from_its_highest_id():
    write_store([make_reminder(id=7)])
    added = asyncio.run(core.add_reminder("telegram", 42, "x", 1, "days", 86400))
    assert added["id"] == 8
    assert read_next_id() == 9  # saved in the new format


def test_next_id_never_falls_below_a_hand_added_entry():
    with open(core.DATA_FILE, "w", encoding="utf-8") as f:
        json.dump({"next_id": 2, "reminders": [make_reminder(id=5)]}, f)
    assert asyncio.run(core.add_reminder("telegram", 42, "x", 1, "days", 86400))["id"] == 6


def test_move_chat_moves_only_that_chat_on_that_platform():
    write_store([
        make_reminder(id=1, chat_id=-4711),
        make_reminder(id=2, chat_id=-4711),
        make_reminder(id=3, chat_id=42),
        make_reminder(id=4, chat_id=-4711, platform="discord"),
    ])
    assert asyncio.run(core.move_chat("telegram", -4711, -100123)) == 2
    assert {r["id"]: r["chat_id"] for r in read_store()} == {
        1: -100123, 2: -100123, 3: 42, 4: -4711,
    }


def test_act_on_reminder_resets_and_deletes():
    write_store([make_reminder(id=1, last_reset=0.0, alerting=True)])
    reminder = asyncio.run(core.act_on_reminder("telegram", 42, 1, "reset"))
    assert reminder["alerting"] is False
    assert read_store()[0]["last_reset"] > 0

    asyncio.run(core.act_on_reminder("telegram", 42, 1, "del"))
    assert read_store() == []


# ---------------------------------------------------------------------------
# Mutation helpers report whether they changed anything
# ---------------------------------------------------------------------------


def test_mark_delivered_stands_down_if_the_user_acted_mid_send():
    reminder = make_reminder(last_reset=100.0)
    assert core.mark_delivered(100.0)(reminder) is True
    assert reminder["alerting"] is True

    reminder = make_reminder(last_reset=200.0, alerting=False)
    assert core.mark_delivered(100.0)(reminder) is False
    assert reminder["alerting"] is False


def test_record_missed_is_throttled_and_reports_no_change():
    reminder = make_reminder()
    error = ConnectionError("connection dropped")

    assert core.record_missed(reminder, error) is True
    assert core.record_missed(reminder, error) is False  # inside RE_ALERT_INTERVAL

    with open(core.MISSED_LOG_FILE, encoding="utf-8") as f:
        lines = f.readlines()
    assert len(lines) == 1
    assert "could not reach Telegram" in lines[0]


def test_record_missed_names_the_right_platform():
    core.record_missed(make_reminder(platform="discord"), ConnectionError("x"))
    with open(core.MISSED_LOG_FILE, encoding="utf-8") as f:
        line = f.read()
    assert "could not reach Discord" in line
    assert "Discord chat 42" in line


def test_record_missed_uses_the_caller_supplied_cause():
    reminder = make_reminder()
    core.record_missed(reminder, RuntimeError("x"), "the Telegram API call failed")
    with open(core.MISSED_LOG_FILE, encoding="utf-8") as f:
        line = f.read()
    assert "the Telegram API call failed" in line
    assert "could not reach Telegram" not in line


def test_record_rejected_stands_the_reminder_down():
    reminder = make_reminder(last_reset=100.0)
    assert core.record_rejected(reminder, PermissionError("blocked"), 100.0) is True
    assert reminder["alerting"] is True
    with open(core.MISSED_LOG_FILE, encoding="utf-8") as f:
        assert "FAILED" in f.read()


FORGED_TITLE = (
    'x"\n2026-09-25 12:00:00  MISSED  reminder #1 "Pay rent" '
    "(Telegram chat 42)\r\x1b[2J "
)


def test_a_title_cannot_forge_log_entries():
    """F-032: anyone may create a reminder, and a title holding a line break
    used to start a separate, genuine-looking entry in the logs."""
    records = []

    class Capture(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    handler = Capture()
    core.log.addHandler(handler)
    try:
        core.record_rejected(make_reminder(title=FORGED_TITLE), PermissionError("x"), 0.0)
        core.record_missed(make_reminder(title=FORGED_TITLE), ConnectionError("down"))
    finally:
        core.log.removeHandler(handler)

    # Universal newlines turn a stray "\r" into a line break too, so this
    # counts every kind of break a reader of the file would see.
    with open(core.MISSED_LOG_FILE, encoding="utf-8") as f:
        text = f.read()
    assert text.count("\n") == 2, "one line per entry"
    assert len(text.splitlines()) == 2, "no other line breaks"
    assert "\x1b" not in text
    assert len(records) == 2
    for message in records:
        assert message.splitlines() == [message]
        assert "\x1b" not in message


def test_loggable_keeps_ordinary_text():
    assert core.loggable("Café 🔔 — 2.5% (tab\there)") == "Café 🔔 — 2.5% (tab here)"


def test_rotate_missed_log_caps_the_file(monkeypatch):
    monkeypatch.setattr(core, "MAX_MISSED_LOG_BYTES", 64)
    with open(core.MISSED_LOG_FILE, "w", encoding="utf-8") as f:
        f.write("x" * 200)
    core.write_missed_line(make_reminder(), ConnectionError("x"), "MISSED", "note")
    assert os.path.exists(core.MISSED_LOG_FILE + ".1")
    assert os.path.getsize(core.MISSED_LOG_FILE) < 200


def test_describe_error_asks_every_registered_describer(monkeypatch):
    monkeypatch.setattr(core, "ERROR_DESCRIBERS", [
        lambda e: "",
        lambda e: "second one knows" if isinstance(e, KeyError) else "",
    ])
    assert core.describe_error(KeyError("x")) == "second one knows"
    assert core.describe_error(ValueError("x")) == ""


# ---------------------------------------------------------------------------
# needs_alert
# ---------------------------------------------------------------------------


def test_needs_alert_fires_once_when_due():
    fresh = make_reminder(last_reset=core.now())
    assert core.needs_alert(fresh) is False

    overdue = make_reminder(last_reset=core.now() - 10 * 86400)
    assert core.needs_alert(overdue) is True

    overdue["alerting"] = True
    assert core.needs_alert(overdue) is False


# ---------------------------------------------------------------------------
# The reminder loop, driven with stand-in senders
# ---------------------------------------------------------------------------


class _StopLoop(BaseException):
    """Raised from the patched sleep to end check_reminders after one tick."""


class FakeSender:
    def __init__(self, error=None, accepted=True):
        self.error = error
        self.accepted = accepted
        self.sent = []

    def accepts(self, reminder):
        return self.accepted

    async def send_alert(self, reminder):
        self.sent.append(reminder["id"])
        if self.error is not None:
            raise self.error


def run_one_tick(monkeypatch, senders):
    async def stop(_seconds):
        raise _StopLoop

    monkeypatch.setattr(core.asyncio, "sleep", stop)
    with pytest.raises(_StopLoop):
        asyncio.run(core.check_reminders(senders))


def overdue(**overrides):
    return make_reminder(last_reset=core.now() - 10 * 86400, **overrides)


def test_loop_delivers_each_platform_through_its_own_sender(monkeypatch):
    write_store([overdue(id=1), overdue(id=2, platform="discord")])
    telegram, discord = FakeSender(), FakeSender()
    run_one_tick(monkeypatch, {"telegram": telegram, "discord": discord})
    assert telegram.sent == [1]
    assert discord.sent == [2]
    assert all(r["alerting"] for r in read_store())


def test_loop_leaves_reminders_of_a_stopped_platform_alone(monkeypatch):
    write_store([overdue(id=1, platform="discord")])
    telegram = FakeSender()
    run_one_tick(monkeypatch, {"telegram": telegram})
    assert telegram.sent == []
    assert read_store()[0]["alerting"] is False


def test_loop_holds_reminders_the_allow_list_now_excludes(monkeypatch):
    """F-019: nobody in an excluded chat could press OK, so don't post there."""
    write_store([overdue(id=1)])
    sender = FakeSender(accepted=False)
    run_one_tick(monkeypatch, {"telegram": sender})
    assert sender.sent == []
    assert read_store()[0]["alerting"] is False, "held, so it fires if allowed back"


def test_loop_stands_down_a_rejected_reminder(monkeypatch):
    write_store([overdue(id=1)])
    sender = FakeSender(core.DeliveryRejected(PermissionError("blocked")))
    run_one_tick(monkeypatch, {"telegram": sender})
    assert read_store()[0]["alerting"] is True
    with open(core.MISSED_LOG_FILE, encoding="utf-8") as f:
        assert "FAILED" in f.read()


def test_loop_keeps_retrying_an_undelivered_reminder(monkeypatch):
    write_store([overdue(id=1)])
    sender = FakeSender(core.DeliveryRetry(ConnectionError("down")))
    run_one_tick(monkeypatch, {"telegram": sender})
    stored = read_store()[0]
    assert stored["alerting"] is False, "must stay due so the next tick retries"
    assert "last_missed_log" in stored
    with open(core.MISSED_LOG_FILE, encoding="utf-8") as f:
        assert "MISSED" in f.read()


def test_loop_survives_an_unexpected_sender_error(monkeypatch):
    write_store([overdue(id=1), overdue(id=2)])
    sender = FakeSender(RuntimeError("boom"))
    run_one_tick(monkeypatch, {"telegram": sender})
    assert sender.sent == [1, 2], "one bad reminder must not skip the rest"
    assert not any(r["alerting"] for r in read_store())


# ---------------------------------------------------------------------------
# Running the front-ends
# ---------------------------------------------------------------------------


class FakeFrontend:
    def __init__(self, platform, start_error=None, die=False):
        self.platform = platform
        self.start_error = start_error
        self.die = die
        self.stopped = False

    async def start(self, fatal):
        if self.start_error:
            raise self.start_error
        if self.die:
            asyncio.get_running_loop().call_soon(fatal, "connection lost")

    async def stop(self):
        self.stopped = True

    def accepts(self, reminder):
        return True

    async def send_alert(self, reminder):
        pass


def test_run_frontends_reports_a_start_failure_and_stops_the_others():
    first = FakeFrontend("telegram")
    second = FakeFrontend("discord", start_error=core.FrontendStartError("bad token"))
    assert asyncio.run(core.run_frontends([first, second])) == 1
    assert first.stopped is True


def test_run_frontends_exits_with_an_error_when_a_frontend_dies():
    frontend = FakeFrontend("discord", die=True)
    assert asyncio.run(core.run_frontends([frontend])) == 1
    assert frontend.stopped is True


# ---------------------------------------------------------------------------
# Log de-duplication
# ---------------------------------------------------------------------------


def make_warning(message):
    return logging.LogRecord(
        "test", logging.WARNING, __file__, 1, message, None, None
    )


def test_dedup_collapses_repeats_inside_the_window():
    dedup = core.DedupWarningFilter(reset_after=1800)
    assert dedup.filter(make_warning("Bad Gateway")) is True
    assert dedup.filter(make_warning("Bad Gateway")) is False
    assert dedup.filter(make_warning("something else")) is True


def test_dedup_window_expires_so_the_error_reappears(monkeypatch):
    """The regression: suppression must expire.

    Before the fix, a warning repeating every few seconds was written once and
    then never again for the life of the process — so the log file rolled over
    at midnight and the new day's file held no trace of the problem, while the
    heartbeat kept citing errors "above" that were not there. An hour of the
    same error on a 30-minute window should now produce a handful of lines:
    more than one, but nowhere near one per occurrence.
    """
    clock = [1000.0]
    monkeypatch.setattr(core.time, "time", lambda: clock[0])

    dedup = core.DedupWarningFilter(reset_after=1800)
    written = 1 if dedup.filter(make_warning("Bad Gateway")) else 0

    # One hour of the identical warning, arriving every five seconds.
    for _ in range(720):
        clock[0] += 5
        if dedup.filter(make_warning("Bad Gateway")):
            written += 1

    assert written >= 2, "a recurring error must reappear in a later window"
    assert written <= 5, "but it must not be written on every repeat"


def test_dedup_does_not_extend_the_window_on_suppressed_repeats(monkeypatch):
    """Suppression is measured from the last line *written*, not the last seen.

    Timing it from the last suppressed repeat would hold the window open
    indefinitely during a persistent outage — exactly the failure above.
    """
    clock = [0.0]
    monkeypatch.setattr(core.time, "time", lambda: clock[0])
    dedup = core.DedupWarningFilter(reset_after=100)

    assert dedup.filter(make_warning("Bad Gateway")) is True
    for _ in range(9):
        clock[0] += 10  # nine repeats inside the 100s window
        assert dedup.filter(make_warning("Bad Gateway")) is False
    clock[0] += 10  # now 100s since the line we wrote
    assert dedup.filter(make_warning("Bad Gateway")) is True


def test_dedup_resets_explicitly():
    dedup = core.DedupWarningFilter(reset_after=1800)
    dedup.filter(make_warning("Bad Gateway"))
    assert dedup.filter(make_warning("Bad Gateway")) is False
    dedup.reset()
    assert dedup.filter(make_warning("Bad Gateway")) is True


def test_heartbeat_status_line_reopens_the_window():
    """"see entries above" must be true: after a heartbeat, a still-recurring
    error is written again instead of being silently suppressed."""
    daily = core.DailyLogFilter()
    assert daily.filter(make_warning("Bad Gateway")) is True
    assert daily.filter(make_warning("Bad Gateway")) is False
    assert daily.error_count == 2  # both counted, only one written

    status = logging.LogRecord("test", logging.INFO, __file__, 1, "All good", None, None)
    status.daily_status = True
    assert daily.filter(status) is True

    assert daily.filter(make_warning("Bad Gateway")) is True


def test_daily_filter_keeps_ordinary_info_out_of_the_file():
    daily = core.DailyLogFilter()
    info = logging.LogRecord("test", logging.INFO, __file__, 1, "chatter", None, None)
    assert daily.filter(info) is False
    assert daily.error_count == 0


def test_daily_handler_prunes_old_log_files(monkeypatch):
    monkeypatch.setattr(core, "LOG_RETENTION_DAYS", 7)
    handler = core.DailyFileHandler(core.LOG_DIR)
    os.makedirs(core.LOG_DIR, exist_ok=True)

    stale = os.path.join(core.LOG_DIR, "1999-01-01.log")
    keep = os.path.join(core.LOG_DIR, "keep-me.txt")
    for path in (stale, keep):
        with open(path, "w", encoding="utf-8") as f:
            f.write("old\n")
    old = core.time.time() - 30 * 86400
    os.utime(stale, (old, old))
    os.utime(keep, (old, old))

    handler.emit(make_warning("triggers the rollover"))
    handler.close()

    assert not os.path.exists(stale)
    assert os.path.exists(keep), "only .log files are pruned"
