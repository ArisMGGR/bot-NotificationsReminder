# Contributing

Thanks for taking a look. This is a small bot — a shared core plus one module
per chat platform; the bar is simply that a change is understandable a year
from now.

## Getting set up

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
```

You do **not** need a Telegram or Discord token to work on it — the tests run
entirely offline.

## Before you open a pull request

Run what CI runs:

```bash
python -m pytest -q
python -m pyflakes reminder_bot.py setup_wizard.py reminderbot tests
python -m compileall -q reminder_bot.py setup_wizard.py reminderbot
bash -n install-service.sh uninstall-service.sh
```

CI runs these on Python 3.10 and 3.13. 3.10 is the supported floor.

## House style

- Comments explain *why*, not *what*. If a line looks odd, say what breaks
  without it — most of the comments in this codebase exist because something
  once went wrong there.
- Platform code stays in its own module: `reminderbot/core.py` must never
  import `telegram` or `discord`, and `setup_wizard.py` / `reminderbot/config.py`
  must stay standard-library only (the wizard runs before any library is
  installed).
- Telegram messages sent with `parse_mode=ParseMode.MARKDOWN_V2` go through
  Telegram's MarkdownV2 parser. In those, escape anything you interpolate with
  `esc()`, and escape literal `.`, `!`, `(`, `)` and friends in the surrounding
  text. The test suite checks this for you; if `assert_valid_markdown_v2`
  fails, Telegram would have rejected the message. Messages sent without a
  `parse_mode` (`/start`, `/cancel`, the `/new` error replies, the error
  handler) are plain text: don't escape anything there, or the backslashes
  show up in the chat.
  On Discord, escape interpolated text with the `esc()` in `discord_bot.py`.
- Anything on the event loop that reads or writes the reminders file must hold
  `store_lock`, and anything that mutates a reminder after an `await` should go
  through `update_reminder()`. Startup code that runs before the loop exists
  (`migrate_legacy_data_file()`) is the one exception: nothing else can touch
  the file yet.
- New behaviour needs a test. The existing ones show the pattern: drive the
  handler with the stand-in objects in `tests/`, don't reach for the network.

## Reporting a security issue

Please see [SECURITY.md](SECURITY.md) — don't open a public issue for it.
