"""Small helpers shared by the test modules."""

import json

from reminderbot import core


def write_store(entries):
    with open(core.DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(entries, f)


def read_store():
    """The reminders in the data file.

    The bot saves {"next_id": ..., "reminders": [...]}; write_store() above
    writes the older bare list, which stays as it is until the bot next saves.
    """
    with open(core.DATA_FILE, encoding="utf-8") as f:
        data = json.load(f)
    return data["reminders"] if isinstance(data, dict) else data


def read_next_id():
    with open(core.DATA_FILE, encoding="utf-8") as f:
        return json.load(f)["next_id"]


def make_reminder(**overrides):
    reminder = {
        "id": 1,
        "platform": "telegram",
        "chat_id": 42,
        "title": "Check Uni",
        "amount": 5,
        "unit": "days",
        "interval_seconds": 5 * 86400,
        "last_reset": core.now(),
        "alerting": False,
    }
    reminder.update(overrides)
    return reminder
