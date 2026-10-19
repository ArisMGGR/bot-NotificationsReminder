"""The platform-independent heart of the reminder bot.

Storage, timing, logging, the missed-notifications log and the background
loop that fires due reminders all live here. The Telegram and Discord
front-ends (telegram_bot.py, discord_bot.py) only deal with their own chat
API: they turn commands into calls on this module, and they deliver alerts
when check_reminders() asks them to.

Every reminder records which platform it belongs to ("telegram" or
"discord") and the chat/channel id there, so the two never see each other's
reminders.
"""

import asyncio
import json
import logging
import math
import os
import signal
import time
import unicodedata

from .config import HERE

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DATA_FILE = os.path.join(HERE, "reminders.json")

# Where reminders lived before the bot supported Discord. Moved to DATA_FILE
# on first start (see migrate_legacy_data_file).
LEGACY_DATA_FILE = os.path.join(HERE, "telegram_reminders.json")

# Where missed reminders are recorded when the chat service can't be reached.
# When the internet is down, any network-based fallback (email/SMS) is down
# too, so the only reliable fallback is a local file on the machine running
# the bot.
MISSED_LOG_FILE = os.path.join(HERE, "missed_notifications.log")

# The missed-notifications log is append-only, so cap it: once it passes this
# size the current file becomes "<name>.1" and a fresh one is started. Keeps a
# long-running bot from filling the disk with a file nobody ever truncates.
MAX_MISSED_LOG_BYTES = 5 * 1024 * 1024  # 5 MB

# Folder holding one plain-text log file per day (logs/YYYY-MM-DD.log). Each
# day's file collects any errors that happened, plus a periodic "All good" line
# so a healthy, quiet day still leaves a visible heartbeat in the file.
LOG_DIR = os.path.join(HERE, "logs")

# How many days of daily log files to keep. Older ones are deleted when the log
# rolls over at midnight, so `logs/` doesn't grow without bound. Set to 0 to
# keep everything.
LOG_RETENTION_DAYS = 30

# How often (seconds) to write a status line to the daily log. Every 30 minutes
# the bot records "All good" when no error occurred in that window.
HEARTBEAT_INTERVAL = 1800  # 30 minutes

# How often (seconds) the bot checks whether any reminder is due.
CHECK_INTERVAL = 5

# How often, at most, a still-undelivered reminder is written to the local
# missed-notifications log during an outage. A reminder is sent only once when
# it comes due (you're notified a single time, not nagged), but if that send
# fails because the chat service is unreachable it is retried every tick until
# it goes through; this interval just throttles the offline log so an outage
# doesn't spam the file.
RE_ALERT_INTERVAL = 300  # 5 minutes

# Longest reminder title accepted, on every platform. Keeps the titles echoed
# back in alerts and lists well inside each service's message limits.
MAX_TITLE_LEN = 120

# Longest interval accepted: 520 weeks, about ten years. Without a cap, a
# Telegram message holding a few hundred digits makes due_at() overflow a float,
# and the loop then fails on every tick for every chat on both platforms.
MAX_INTERVAL_WEEKS = 520
MAX_INTERVAL_SECONDS = MAX_INTERVAL_WEEKS * 604800

# The platforms a reminder can belong to, and how to name them in messages.
PLATFORM_LABELS = {"telegram": "Telegram", "discord": "Discord"}

# Reminders saved before Discord support carry no "platform" field; they were
# all Telegram reminders.
DEFAULT_PLATFORM = "telegram"

# Supported time units and how many seconds each represents. Both singular and
# plural forms are accepted when the user types an interval.
UNITS = {
    "second": 1, "seconds": 1, "sec": 1, "secs": 1, "s": 1,
    "minute": 60, "minutes": 60, "min": 60, "mins": 60, "m": 60,
    "hour": 3600, "hours": 3600, "hr": 3600, "hrs": 3600, "h": 3600,
    "day": 86400, "days": 86400, "d": 86400,
    "week": 604800, "weeks": 604800, "w": 604800,
}

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
# One readable line per event instead of raw tracebacks. The noisy HTTP
# libraries (httpx/httpcore) are silenced unless something is actually wrong.

LOG_FORMAT = "%(asctime)s  %(levelname)-8s %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


class DailyFileHandler(logging.Handler):
    """Write log records to a fresh text file each day: logs/YYYY-MM-DD.log.

    Unlike TimedRotatingFileHandler (which keeps one active file and renames
    it at rotation), this always writes straight to a file named after today's
    date, so each day's log is a self-contained, easy-to-find .log. It reopens
    the file automatically the first time it's used on a new day, so the bot
    can run for weeks and still split cleanly at midnight.
    """

    def __init__(self, directory):
        super().__init__()
        self.directory = directory
        self._current_date = None
        self._stream = None

    def _ensure_stream(self):
        today = time.strftime("%Y-%m-%d")
        if today != self._current_date:
            if self._stream is not None:
                self._stream.close()
            os.makedirs(self.directory, exist_ok=True)
            path = os.path.join(self.directory, f"{today}.log")
            self._stream = open(path, "a", encoding="utf-8")
            self._current_date = today
            self._prune_old_logs()
        return self._stream

    def _prune_old_logs(self):
        """Delete daily files older than LOG_RETENTION_DAYS.

        Runs once per rollover, inside emit(), so it must never raise or log:
        a failure here is not worth losing the log line that triggered it.
        """
        if LOG_RETENTION_DAYS <= 0:
            return
        cutoff = time.time() - LOG_RETENTION_DAYS * 86400
        try:
            names = os.listdir(self.directory)
        except OSError:
            return
        for name in names:
            if not name.endswith(".log"):
                continue
            path = os.path.join(self.directory, name)
            try:
                if os.path.getmtime(path) < cutoff:
                    os.remove(path)
            except OSError:
                pass

    def emit(self, record):
        try:
            stream = self._ensure_stream()
            stream.write(self.format(record) + "\n")
            stream.flush()
        except Exception:
            self.handleError(record)

    def close(self):
        if self._stream is not None:
            self._stream.close()
            self._stream = None
        # Forget the date too, so a straggling record logged after shutdown
        # (a library's __del__, say) reopens the file instead of crashing.
        self._current_date = None
        super().close()


class DedupWarningFilter(logging.Filter):
    """Collapse a run of identical warnings down to one log line per window.

    When the same problem recurs back-to-back (e.g. Telegram returning
    'Bad Gateway' every few seconds during an outage), printing every one
    just floods the log with duplicates. So the first occurrence is logged
    and repeats of that exact message are dropped; the tally the heartbeat
    reports still counts them all, so the summary line remains the honest
    record of how many happened. A different warning message resets the run
    and prints fresh.

    Suppression expires after ``reset_after`` seconds, measured from the last
    line actually written — not from the last one suppressed. Without that
    expiry an error recurring every few seconds would be written once and then
    never again for the life of the process: the log file would roll over at
    midnight and the new day's file would contain no trace of it, while the
    heartbeat kept reporting errors "above" that were not there.
    """

    def __init__(self, reset_after):
        super().__init__()
        self.reset_after = reset_after
        self._last_warning = None
        self._last_written = 0.0

    def reset(self):
        """Start a fresh window, so the next warning is written even if it repeats."""
        self._last_warning = None

    def filter(self, record):
        if record.levelno < logging.WARNING:
            self._last_warning = None  # a non-warning breaks the run
            return True
        # The attached exception counts too: "Unexpected error while handling
        # a Telegram update:" is the same text for every unknown bug, and
        # keying on the text alone dropped a second, different bug's traceback.
        error = record.exc_info[1] if record.exc_info else None
        message = record.getMessage()
        if error is not None:
            message = (message, type(error), str(error))
        # Deliberately compares against the last *written* line: advancing the
        # timestamp on every suppressed repeat would keep the window open
        # forever during a persistent outage.
        if (
            message == self._last_warning
            and time.time() - self._last_written < self.reset_after
        ):
            return False  # duplicate inside the current window — suppress
        self._last_warning = message
        self._last_written = time.time()
        return True


class DailyLogFilter(logging.Filter):
    """Decide what reaches the daily file, and keep a running error tally.

    The daily file is meant to hold the two things the user cares about:
      * anything that went wrong (WARNING and above), and
      * the periodic "All good" heartbeat (tagged with ``daily_status``).
    Ordinary INFO chatter (startup banner, HTTP noise) is kept out. Every
    genuine problem that passes through is counted so the heartbeat can tell
    whether the last window was clean.

    Counting happens before de-duplication, so a run of identical warnings is
    still tallied in full even though only the first line per window is
    written.
    """

    def __init__(self):
        super().__init__()
        self.error_count = 0
        self._dedup = DedupWarningFilter(HEARTBEAT_INTERVAL)

    def filter(self, record):
        if getattr(record, "daily_status", False):
            # The heartbeat closes one reporting window and opens the next, so
            # anything it says happened "above" is written above: a problem
            # still recurring in the new window gets a fresh line.
            self._dedup.reset()
            return True
        if record.levelno >= logging.WARNING:
            self.error_count += 1
            return self._dedup.filter(record)
        return False


logging.basicConfig(
    format=LOG_FORMAT,
    datefmt=DATE_FORMAT,
    level=logging.INFO,
)
for noisy in ("httpx", "httpcore", "telegram.ext.Updater", "discord.http"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

# Collapse repeated identical warnings on the console too, so a persistent
# outage doesn't scroll the same line past forever. Each handler keeps its own
# dedup state, so this never interferes with the daily file's counting above.
for _console_handler in logging.getLogger().handlers:
    _console_handler.addFilter(DedupWarningFilter(HEARTBEAT_INTERVAL))

# Attach the daily file to the root logger so warnings/errors from every part
# of the app (and the chat libraries) are captured, alongside the console.
daily_log_filter = DailyLogFilter()
_daily_handler = DailyFileHandler(LOG_DIR)
_daily_handler.setLevel(logging.INFO)  # let INFO status lines through...
_daily_handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT))
_daily_handler.addFilter(daily_log_filter)  # ...but the filter keeps out INFO noise
logging.getLogger().addHandler(_daily_handler)

log = logging.getLogger("reminder_bot")


def log_status(message):
    """Record a status line to the daily log (bypasses the WARNING threshold)."""
    log.info(message, extra={"daily_status": True})


def now() -> float:
    return time.time()


def loggable(text):
    """`text` with every control character and line break turned into a space.

    Reminder titles are typed by whoever uses the bot, which by default is
    anyone. Written raw, a title holding a newline would start what looks like
    a separate, genuine entry in the missed-notifications or daily log, and an
    escape sequence would act on the terminal of whoever reads the file.
    """
    return "".join(
        " " if unicodedata.category(char) in ("Cc", "Zl", "Zp") else char
        for char in str(text)
    )


def platform_label(reminder_or_platform):
    """"Telegram" / "Discord", from a reminder dict or a platform name."""
    if isinstance(reminder_or_platform, dict):
        reminder_or_platform = reminder_or_platform.get("platform", DEFAULT_PLATFORM)
    return PLATFORM_LABELS.get(reminder_or_platform, str(reminder_or_platform))


# ---------------------------------------------------------------------------
# Friendly error descriptions
# ---------------------------------------------------------------------------

# Each front-end registers a function here that turns its library's exceptions
# into one readable line (or "" for errors it doesn't recognise). Kept as a
# registry so this module never has to import telegram or discord itself.
ERROR_DESCRIBERS = []


def describe_error(error: Exception) -> str:
    """Turn an exception into one short, human-readable line, or "" if unknown."""
    for describer in ERROR_DESCRIBERS:
        text = describer(error)
        if text:
            return text
    return ""


# ---------------------------------------------------------------------------
# Reminder storage (a simple JSON file)
# ---------------------------------------------------------------------------


# Every read-modify-write of the reminders file goes through this lock. The
# background loop and the command/button handlers all run on the same event
# loop, so without it an `await` in the middle of a sequence lets another
# coroutine save its own version of the list and have it silently overwritten
# (a deleted reminder coming back, a new one disappearing).
store_lock = asyncio.Lock()


# Every reminder must carry these, with these types, or the rest of the module
# (needs_alert, due_at, the list builders) blows up on a KeyError or TypeError
# every few seconds with no way for the user to recover.
REQUIRED_FIELDS = {
    "id": int,
    "chat_id": int,
    "title": str,
    "amount": int,
    "unit": str,
    "interval_seconds": int,
    "last_reset": (int, float),
}


def valid_reminder(entry):
    """Whether one entry from the data file is shaped like a reminder."""
    if not isinstance(entry, dict):
        return False
    for key, types in REQUIRED_FIELDS.items():
        value = entry.get(key)
        # bool is a subclass of int, so isinstance() alone would wave through
        # {"id": true} and leave the breakage for later.
        if isinstance(value, bool) or not isinstance(value, types):
            return False
    # The due time must be a real number. A huge interval (saved before
    # parse_interval had a cap) overflows the float addition, and JSON's
    # Infinity/NaN get through the type check above; either way every due_at()
    # call would raise, stalling the loop for every chat.
    try:
        if not math.isfinite(entry["last_reset"] + entry["interval_seconds"]):
            return False
    except OverflowError:
        return False
    # Optional, for files written before Discord support; see DEFAULT_PLATFORM.
    return entry.get("platform", DEFAULT_PLATFORM) in PLATFORM_LABELS


def _valid_next_id(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def load_store():
    """Read the reminders file as {"next_id": int, "reminders": [...]}.

    Never invents an empty store over readable data. A file we merely failed
    to *read* (permissions, a full disk) raises, so the caller reports the
    problem instead of continuing with a phantom empty list that the next save
    would write over the top of. A file whose *contents* are unusable is moved
    aside first, so the damaged data is preserved for recovery rather than
    silently destroyed.

    "Unusable" covers the entries too, not just the top-level shape: valid JSON
    holding the wrong thing (a list of numbers, entries missing chat_id) would
    otherwise be accepted here and crash every caller afterwards. It also
    covers text that isn't UTF-8 (a hand edit saved in another encoding), a
    number too long for Python to convert, and two entries sharing an id: every lookup is by id, so a Delete in one chat
    would take the other chat's reminder with it.

    "next_id" is the id the next new reminder gets. It only ever grows, so a
    deleted reminder's id is never handed out again. Alerts and /list messages
    keep their buttons after the reminder is gone, and a reused id would let
    an old OK or Delete act on a newer reminder. Files written before this
    field existed are a bare list; they start counting after their highest id.
    """
    if not os.path.exists(DATA_FILE):
        return {"next_id": 1, "reminders": []}
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except UnicodeDecodeError as e:
        quarantine_data_file(f"it isn't UTF-8 text ({e})")
        return {"next_id": 1, "reminders": []}
    except ValueError as e:
        # JSONDecodeError, and also the plain ValueError json raises for an
        # integer longer than Python's int conversion limit (4300 digits):
        # caught too, or the file would stay put and fail every load.
        quarantine_data_file(f"it can't be read as JSON ({e})")
        return {"next_id": 1, "reminders": []}
    if isinstance(data, list):
        reminders, stored_next_id = data, 1
    elif (
        isinstance(data, dict)
        and isinstance(data.get("reminders"), list)
        and _valid_next_id(data.get("next_id"))
    ):
        reminders, stored_next_id = data["reminders"], data["next_id"]
    else:
        quarantine_data_file(
            f"it holds a {type(data).__name__}, not a reminders list with a next_id"
        )
        return {"next_id": 1, "reminders": []}
    bad = sum(1 for entry in reminders if not valid_reminder(entry))
    if bad:
        quarantine_data_file(
            f"{bad} of {len(reminders)} entries aren't valid reminders "
            "(missing or wrongly typed fields)"
        )
        return {"next_id": 1, "reminders": []}
    duplicates = len(reminders) - len({entry["id"] for entry in reminders})
    if duplicates:
        quarantine_data_file(f"{duplicates} entries reuse another entry's id")
        return {"next_id": 1, "reminders": []}
    for entry in reminders:
        entry.setdefault("platform", DEFAULT_PLATFORM)
    # A hand edit may add an entry above the stored counter; never go below it.
    highest = max((entry["id"] for entry in reminders), default=0)
    return {"next_id": max(stored_next_id, highest + 1), "reminders": reminders}


def load_reminders():
    """Just the reminders from the file; see load_store()."""
    return load_store()["reminders"]


def quarantine_data_file(reason):
    """Rename an unusable reminders file out of the way, loudly."""
    backup = f"{DATA_FILE}.corrupt.{time.strftime('%Y%m%d-%H%M%S')}"
    try:
        os.replace(DATA_FILE, backup)
    except OSError as e:
        log.error(
            "Could not read %s (%s) and could not move it aside either (%s). "
            "Starting with an empty list.", DATA_FILE, reason, e,
        )
        return
    log.error(
        "Could not read %s (%s). Moved it to %s and started with an empty "
        "list — your reminders are still in that file.", DATA_FILE, reason, backup,
    )


def save_store(store):
    """Write the reminders file (the dict load_store() returns) atomically.

    Serialises to a temporary file in the same directory and renames it into
    place, so a crash or a full disk mid-write leaves the previous file intact
    instead of a half-written one that can't be parsed.
    """
    tmp_path = f"{DATA_FILE}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(store, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, DATA_FILE)


def migrate_legacy_data_file():
    """Move telegram_reminders.json to reminders.json, once.

    Only when the new file doesn't exist yet, so it can never overwrite
    reminders created since. The old entries have no "platform" field and are
    read as Telegram reminders.

    Runs without store_lock: reminder_bot.py calls it before the event loop
    starts, when nothing else can touch the file.
    """
    if os.path.exists(DATA_FILE) or not os.path.exists(LEGACY_DATA_FILE):
        return
    os.replace(LEGACY_DATA_FILE, DATA_FILE)
    log.info(
        "Moved your reminders from %s to %s.",
        os.path.basename(LEGACY_DATA_FILE), os.path.basename(DATA_FILE),
    )


def _apply_reminder_change(reminder_id, mutate):
    """The blocking half of update_reminder(): read, mutate, maybe write.

    `mutate` returns True when it actually changed something worth persisting.
    A no-op (a throttled missed-log entry, a send the user already acted on)
    skips the write entirely — otherwise an outage would re-serialise and
    fsync the whole file every CHECK_INTERVAL seconds, per reminder, with
    byte-identical contents.
    """
    store = load_store()
    target = next((r for r in store["reminders"] if r["id"] == reminder_id), None)
    if target is None:
        return False
    if mutate(target):
        save_store(store)
    return True


async def update_reminder(reminder_id, mutate):
    """Re-read the store under the lock, apply `mutate` to one reminder, save.

    Used by the background loop after an `await` on the network: the list it
    started from may be stale by now, so the change is applied to freshly read
    data instead of writing a whole remembered list back over newer edits.
    Returns False if the reminder disappeared while we were busy.

    The read-modify-write runs in a worker thread: it fsyncs, and `mutate` may
    append to the missed-notifications log, so keeping it on the event loop
    would stall every other handler for the duration of a disk write.
    """
    async with store_lock:
        return await asyncio.to_thread(_apply_reminder_change, reminder_id, mutate)


async def reminders_for(platform, chat_id):
    """One chat's reminders on one platform, closest to firing first.

    Takes store_lock like every other reader: load_reminders() can move a
    damaged file aside, and that must not race a locked read-modify-write.
    """
    async with store_lock:
        stored = load_reminders()
    reminders = [
        r for r in stored
        if r["platform"] == platform and r["chat_id"] == chat_id
    ]
    reminders.sort(key=due_at)
    return reminders


async def add_reminder(platform, chat_id, title, amount, unit_word, seconds):
    """Create and save a reminder whose first alert is `seconds` from now."""
    async with store_lock:
        store = load_store()
        reminder = {
            "id": store["next_id"],
            "platform": platform,
            "chat_id": chat_id,
            "title": title,
            "amount": amount,
            "unit": unit_word,
            "interval_seconds": seconds,
            "last_reset": now(),
            "alerting": False,
        }
        store["reminders"].append(reminder)
        store["next_id"] += 1
        await asyncio.to_thread(save_store, store)
    return reminder


async def act_on_reminder(platform, chat_id, reminder_id, action):
    """Apply a button press: "reset" restarts the timer, "del" deletes.

    Reminder ids are global, so the platform and chat are matched too: a
    button press may only ever act on a reminder belonging to the chat it
    came from. Returns the reminder acted on, or None if there is no such
    reminder in that chat (already deleted, or someone else's).
    """
    async with store_lock:
        store = load_store()
        reminder = next(
            (
                r for r in store["reminders"]
                if r["id"] == reminder_id
                and r["platform"] == platform
                and r["chat_id"] == chat_id
            ),
            None,
        )
        if reminder is None:
            return None
        if action == "del":
            store["reminders"] = [r for r in store["reminders"] if r["id"] != reminder_id]
        else:
            reminder["last_reset"] = now()
            reminder["alerting"] = False
        await asyncio.to_thread(save_store, store)
    return reminder


async def move_chat(platform, old_chat_id, new_chat_id):
    """Move every reminder of one chat to a new chat id. Returns how many moved.

    Telegram gives a group a new id when it becomes a supergroup, and the old
    id stops accepting messages. Without the move, the group's reminders would
    be retried against the dead id forever, and the group, now under its new
    id, could no longer see them in /list.
    """
    async with store_lock:
        store = load_store()
        moved = 0
        for r in store["reminders"]:
            if r["platform"] == platform and r["chat_id"] == old_chat_id:
                r["chat_id"] = new_chat_id
                moved += 1
        if moved:
            await asyncio.to_thread(save_store, store)
    return moved


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def parse_interval(text):
    """Parse something like '5 days' or '30 minutes' or '2h'.

    Returns (amount, unit_word, seconds), or None if it can't be parsed or is
    longer than MAX_INTERVAL_SECONDS.
    """
    parts = text.strip().lower().split()
    if len(parts) == 1:
        # Handle a glued form like "30m" or "2h".
        token = parts[0]
        i = 0
        while i < len(token) and (token[i].isdigit()):
            i += 1
        if i == 0 or i == len(token):
            return None
        parts = [token[:i], token[i:]]

    if len(parts) != 2:
        return None
    amount_str, unit_word = parts
    # isdigit() is True for characters int() rejects (superscripts like "²"),
    # so require plain ASCII digits rather than letting int() raise.
    if not (amount_str.isascii() and amount_str.isdigit()):
        return None
    # Any amount this long is over the cap in every unit. Rejecting it before
    # int() also avoids converting thousands of digits.
    if len(amount_str) > len(str(MAX_INTERVAL_SECONDS)):
        return None
    amount = int(amount_str)
    if amount <= 0 or unit_word not in UNITS:
        return None
    seconds = amount * UNITS[unit_word]
    if seconds > MAX_INTERVAL_SECONDS:
        return None
    return amount, unit_word, seconds


def format_duration(seconds):
    overdue = seconds < 0
    seconds = int(abs(seconds))
    if seconds < 60:
        text = f"{seconds}s"
    elif seconds < 3600:
        text = f"{seconds // 60}m {seconds % 60}s"
    elif seconds < 86400:
        text = f"{seconds // 3600}h {(seconds % 3600) // 60}m"
    else:
        text = f"{seconds // 86400}d {(seconds % 86400) // 3600}h"
    return f"overdue by {text}" if overdue else f"in {text}"


def status_text(reminder):
    """"next in 3d 3h" or "overdue by 5m", for the reminder lists."""
    delta = due_at(reminder) - now()
    left = format_duration(delta)
    # "overdue by ..." already reads as a full status, so only an upcoming
    # reminder gets the "next" prefix ("next in 3d 3h").
    return left if delta < 0 else f"next {left}"


def due_at(reminder):
    return reminder["last_reset"] + reminder["interval_seconds"]


def needs_alert(reminder):
    """Whether a reminder should be sent right now.

    Fires exactly once, the first time a reminder comes due, so you're notified
    a single time rather than nagged repeatedly. It stays quiet afterwards until
    you press OK (which resets the timer for the next cycle).

    "alerting" is only set once a send actually succeeds, so a reminder that
    couldn't be delivered (service/internet down) is still retried on the next
    tick until it goes through — that's delivery retry, not a duplicate alert.
    """
    if now() < due_at(reminder):
        return False
    return not reminder.get("alerting")  # send once when it first comes due


# ---------------------------------------------------------------------------
# Undelivered reminders
# ---------------------------------------------------------------------------


class DeliveryError(Exception):
    """A front-end couldn't deliver an alert. Wraps the library's exception.

    `note` optionally states the cause for the missed-notifications log, when
    the default wording ("could not reach Telegram") would be misleading.
    """

    def __init__(self, error, note=None):
        super().__init__(str(error))
        self.error = error
        self.note = note


class DeliveryRejected(DeliveryError):
    """The service refused the message itself; retrying cannot help."""


class DeliveryRetry(DeliveryError):
    """Not delivered this time (outage, API hiccup); try again next tick."""


class FrontendStartError(Exception):
    """A front-end could not start. The message is shown to the operator."""


def rotate_missed_log():
    """Start a fresh missed-notifications log once the current one gets large.

    The file is append-only and nothing else ever truncates it, so without this
    a long outage on a long-lived bot grows it without limit. One generation is
    kept as "<name>.1"; best-effort, and never fatal to the write that follows.
    """
    try:
        if os.path.getsize(MISSED_LOG_FILE) < MAX_MISSED_LOG_BYTES:
            return
        os.replace(MISSED_LOG_FILE, f"{MISSED_LOG_FILE}.1")
    except OSError:
        pass


def write_missed_line(reminder, error, tag, note):
    """Append one line about an undelivered reminder to the local log file.

    Returns True only if the line actually reached the file, so a caller that
    throttles on "we logged this already" never suppresses the next entry on
    the strength of a write that failed.
    """
    rotate_missed_log()
    detail = describe_error(error) or f"{error.__class__.__name__}: {error}"
    # One entry, one line: the title (and an error's own text) may hold line
    # breaks, which would otherwise forge extra entries. See loggable().
    line = loggable(
        f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {tag}  "
        f"reminder #{reminder['id']} \"{reminder['title']}\" "
        f"({platform_label(reminder)} chat {reminder['chat_id']}) — {note}: {detail}"
    ) + "\n"
    try:
        with open(MISSED_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line)
    except OSError as e:
        log.error("Could not write missed-notification log %s: %s", MISSED_LOG_FILE, e)
        return False
    return True


def record_missed(reminder, error, note=None):
    """Record a reminder we couldn't deliver, and keep it queued for retry.

    The send will be retried on the next tick, so this is throttled to at most
    one entry per reminder per RE_ALERT_INTERVAL — a long outage leaves a
    readable trail rather than thousands of identical lines. The throttle
    timestamp is only advanced once the line is safely on disk.

    `note` states the cause, because not every retryable failure is an outage:
    a token or getUpdates conflict also lands here, and writing "could not
    reach Telegram" for those would send the operator hunting a network fault
    that doesn't exist. Returns True when the reminder was actually changed
    (and so needs saving).
    """
    if note is None:
        note = f"could not reach {platform_label(reminder)}"
    if now() - reminder.get("last_missed_log", 0) < RE_ALERT_INTERVAL:
        return False
    if not write_missed_line(reminder, error, "MISSED", note):
        return False
    reminder["last_missed_log"] = now()
    log.warning(
        "Couldn't deliver reminder #%s (%s). Saved it to %s; will keep "
        "retrying until it goes through.",
        reminder["id"], loggable(reminder["title"]), MISSED_LOG_FILE,
    )
    return True


def user_acted_since(reminder, last_reset_when_sent):
    """Did the user reset this reminder while we were busy sending it?

    A press of OK or Reset sets a fresh ``last_reset``. That is newer intent
    than the alert we were in the middle of, so the send we just finished must
    not mark the reminder as alerting: doing so would leave it flagged past its
    next due time and silence it until the user reset it again by hand.
    """
    return reminder["last_reset"] != last_reset_when_sent


def mark_delivered(last_reset_when_sent):
    """Build the mutation that records a successful send."""
    def mutate(reminder):
        if user_acted_since(reminder, last_reset_when_sent):
            return False
        reminder["alerting"] = True
        return True
    return mutate


def record_rejected(reminder, error, last_reset_when_sent):
    """Record a reminder the service refused outright, and stop retrying it.

    Unlike an outage, retrying changes nothing here: the message is malformed,
    or the user has blocked the bot. Marking it alerting stops the loop from
    re-sending it every few seconds forever; the reminder stays in the list and
    its timer can be restarted from /list with Reset. Returns True when the
    reminder was actually changed (and so needs saving).
    """
    label = platform_label(reminder)
    write_missed_line(reminder, error, "FAILED", f"{label} refused the message")
    log.error(
        "%s refused reminder #%s (%s), so it was not delivered and will "
        "not be retried — use Reset in /list once the cause is fixed. %s",
        label, reminder["id"], loggable(reminder["title"]),
        loggable(describe_error(error) or f"{error.__class__.__name__}: {error}"),
    )
    if user_acted_since(reminder, last_reset_when_sent):
        # The user reset it mid-send, so it is no longer due and won't be
        # retried anyway. Nothing to persist.
        return False
    reminder["alerting"] = True
    return True


async def save_delivered(reminder):
    """Record that `reminder` was sent. Returns False if the file can't be written.

    The alert has already gone out, so a failed write must not end the tick
    (the other due reminders still need sending) or pass unexplained; the
    caller keeps the reminder from being sent again meanwhile.
    """
    try:
        await update_reminder(reminder["id"], mark_delivered(reminder["last_reset"]))
    except OSError as e:
        log.error(
            "Sent reminder #%s (%s) but couldn't save that it was sent: %s. It "
            "won't be sent again while the bot runs. Fix the cause (often a full "
            "or read-only disk) so it can be saved.",
            reminder["id"], loggable(reminder["title"]), e,
        )
        return False
    return True


# ---------------------------------------------------------------------------
# Background loops
# ---------------------------------------------------------------------------


async def check_reminders(senders):
    """Runs forever: every few seconds, send any reminder that is due.

    `senders` maps a platform name to the front-end that delivers its alerts
    (anything with an async ``send_alert(reminder)`` and an ``accepts(reminder)``).
    Reminders belonging to a platform that isn't running are left alone until
    it is. So are reminders whose chat the platform's allow-list now excludes
    (``accepts`` returns False): that chat can no longer press OK, so an alert
    there could never be acknowledged. They are held, not deleted, and fire
    again if the chat is allowed back.

    Each due reminder is sent once, so the user is notified a single time and
    isn't nagged until they press OK. If a send fails because the service is
    unreachable, the reminder is recorded to a local log as a fallback and
    retried on the next tick, so a dropped message or an internet outage never
    loses the reminder. If the service instead rejects the message outright,
    retrying could never succeed, so the reminder is stood down after one
    attempt rather than looping forever.
    """
    # Reminders that were sent but whose "alerting" flag couldn't be saved
    # (a full or read-only disk): id -> last_reset at the time of the send.
    # The file still says they are due, so without this every tick would
    # send the same alert again, every CHECK_INTERVAL seconds.
    unsaved = {}
    while True:
        try:
            async with store_lock:
                due = [
                    r for r in load_reminders()
                    if r["platform"] in senders
                    and needs_alert(r)
                    and senders[r["platform"]].accepts(r)
                ]
            for stale in due:
                # Re-read immediately before sending. Sending an earlier
                # reminder in this same tick awaited on the network, and the
                # user may have pressed OK, Reset or Delete meanwhile — so the
                # snapshot taken above can be several seconds out of date.
                # (A press *during* our own send is a narrower window still,
                # and is handled after the fact by mark_delivered().)
                async with store_lock:
                    r = next(
                        (
                            x for x in load_reminders()
                            if x["id"] == stale["id"]
                        ),
                        None,
                    )
                if r is None or not needs_alert(r):
                    unsaved.pop(stale["id"], None)
                    continue
                if unsaved.get(r["id"]) == r["last_reset"]:
                    # Already delivered; only the record of it is missing.
                    # Try to save that again instead of re-sending.
                    if await save_delivered(r):
                        del unsaved[r["id"]]
                    continue
                try:
                    await senders[r["platform"]].send_alert(r)
                except DeliveryRejected as send_error:
                    # The service refused the message itself — malformed, or
                    # the user blocked the bot. Retrying is pointless and
                    # would loop forever, so record it and stand the reminder
                    # down. Handled per reminder so one failure doesn't skip
                    # the others.
                    await update_reminder(
                        r["id"],
                        lambda t, e=send_error.error, w=r["last_reset"]:
                            record_rejected(t, e, w),
                    )
                except DeliveryRetry as send_error:
                    # Couldn't get through this time. Record it locally as a
                    # fallback and leave the reminder unacknowledged so the
                    # next tick retries it.
                    await update_reminder(
                        r["id"],
                        lambda t, e=send_error: record_missed(t, e.error, e.note),
                    )
                except Exception as send_error:
                    # Something we don't recognise at all. Keep the reminder due
                    # and log it in full, but carry on with the rest of this
                    # tick so one bad reminder can't hold up the others.
                    log.error(
                        "Unexpected error sending reminder #%s:",
                        r["id"], exc_info=send_error,
                    )
                else:
                    # Delivered: mark it sent so it isn't sent again until the
                    # user presses OK (which resets the timer for the next
                    # cycle). Re-read under the lock rather than writing the
                    # list we started from — a /new, Delete or Reset may have
                    # landed while we were waiting on the network.
                    if await save_delivered(r):
                        unsaved.pop(r["id"], None)
                    else:
                        unsaved[r["id"]] = r["last_reset"]
        except Exception as e:  # keep the loop alive no matter what
            friendly = describe_error(e)
            if friendly:
                log.warning("While sending a due reminder: %s", friendly)
            else:
                log.error("Unexpected error in the reminder loop:", exc_info=e)
        await asyncio.sleep(CHECK_INTERVAL)


async def heartbeat():
    """Every HEARTBEAT_INTERVAL, write a status line to the daily log.

    If no error was logged during the window, it records "All good" so a quiet,
    healthy day still leaves a visible pulse in that day's file. If errors did
    occur, they're already written above, so it just notes how many.
    """
    last_count = daily_log_filter.error_count
    minutes = HEARTBEAT_INTERVAL // 60
    while True:
        await asyncio.sleep(HEARTBEAT_INTERVAL)
        errors = daily_log_filter.error_count - last_count
        last_count = daily_log_filter.error_count
        if errors == 0:
            log_status(f"All good — no errors in the last {minutes} minutes.")
        else:
            log_status(
                f"{errors} error(s) logged in the last {minutes} minutes "
                "(see entries above)."
            )


async def run_frontends(frontends):
    """Start every front-end, run the shared loops, and stop cleanly.

    Each front-end has a ``platform`` name, ``async start(fatal)``,
    ``async stop()``, ``async send_alert(reminder)`` and ``accepts(reminder)``
    (see check_reminders). A front-end calls
    ``fatal(message)`` if it dies after starting (e.g. Discord drops the
    connection for good); that stops the whole bot with exit code 1 so systemd
    restarts it, rather than leaving one platform silently dead.

    Returns the process exit code.
    """
    stop = asyncio.Event()
    failures = []

    def fatal(message):
        failures.append(message)
        log.error(message)
        stop.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):
            pass  # Windows: Ctrl+C arrives as KeyboardInterrupt instead

    started = []
    tasks = []
    try:
        for frontend in frontends:
            try:
                await frontend.start(fatal)
            except FrontendStartError as e:
                log.error("%s", e)
                return 1
            started.append(frontend)

        senders = {frontend.platform: frontend for frontend in started}
        tasks = [
            asyncio.create_task(check_reminders(senders)),
            asyncio.create_task(heartbeat()),
        ]
        log.info(
            "Reminder bot is running on %s. Press Ctrl+C to stop.",
            " and ".join(platform_label(f.platform) for f in started),
        )
        await stop.wait()
    finally:
        for task in tasks:
            task.cancel()
        for frontend in reversed(started):
            try:
                await frontend.stop()
            except Exception as e:
                log.warning(
                    "Error while stopping %s: %s", platform_label(frontend.platform), e,
                )
    return 1 if failures else 0
