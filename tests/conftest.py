"""Shared fixtures: every test runs against throwaway files in tmp_path."""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from reminderbot import core  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_files(tmp_path, monkeypatch):
    """Keep every test off the real data file, logs and missed-notification log."""
    monkeypatch.setattr(core, "DATA_FILE", str(tmp_path / "reminders.json"))
    monkeypatch.setattr(core, "LEGACY_DATA_FILE", str(tmp_path / "telegram_reminders.json"))
    monkeypatch.setattr(core, "MISSED_LOG_FILE", str(tmp_path / "missed.log"))
    monkeypatch.setattr(core, "LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setattr(core._daily_handler, "directory", str(tmp_path / "logs"))
    for name in (
        "BOT_PLATFORMS", "TELEGRAM_BOT_TOKEN", "DISCORD_BOT_TOKEN",
        "ALLOWED_CHAT_IDS", "TELEGRAM_ALLOWED_CHAT_IDS",
        "DISCORD_ALLOWED_GUILD_IDS", "DISCORD_ALLOWED_USER_IDS",
        "DISCORD_ALLOWED_CHANNEL_IDS",
    ):
        monkeypatch.delenv(name, raising=False)
    yield
