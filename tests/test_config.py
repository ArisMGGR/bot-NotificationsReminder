"""Tests for reading conf.env and the settings in it."""

import os
import sys

import pytest

from reminderbot import config


def test_read_conf_file_handles_comments_quotes_and_export(tmp_path):
    path = tmp_path / "conf.env"
    path.write_text(
        "# a comment\n"
        "\n"
        "BOT_PLATFORMS=telegram,discord\n"
        "TELEGRAM_BOT_TOKEN=\"123:abc\"\n"
        "export DISCORD_BOT_TOKEN='x.y.z'\n"
        "not a setting\n",
        encoding="utf-8",
    )
    assert config.read_conf_file(str(path)) == {
        "BOT_PLATFORMS": "telegram,discord",
        "TELEGRAM_BOT_TOKEN": "123:abc",
        "DISCORD_BOT_TOKEN": "x.y.z",
    }


INLINE_COMMENT_LINES = (
    "DISCORD_ALLOWED_GUILD_IDS=123   # servers\n"
    "TELEGRAM_BOT_TOKEN=123:abc\t# my bot\n"
    "QUOTED=\"a # b\"\n"
    "GLUED=a#b\n"
    "EMPTY=\n"
)


def test_read_conf_file_drops_inline_comments(tmp_path):
    """F-026: setup_wizard.py offered "123   # servers" as the value to keep,
    then rejected it on Enter; the bot (via dotenv) reads 123."""
    path = tmp_path / "conf.env"
    path.write_text(INLINE_COMMENT_LINES, encoding="utf-8")
    assert config.read_conf_file(str(path)) == {
        "DISCORD_ALLOWED_GUILD_IDS": "123",
        "TELEGRAM_BOT_TOKEN": "123:abc",
        "QUOTED": "a # b",
        "GLUED": "a#b",
        "EMPTY": "",
    }


def test_read_conf_file_agrees_with_dotenv(tmp_path):
    """The wizard reads conf.env with read_conf_file, the bot with dotenv."""
    dotenv = pytest.importorskip("dotenv")
    path = tmp_path / "conf.env"
    path.write_text(INLINE_COMMENT_LINES, encoding="utf-8")
    assert config.read_conf_file(str(path)) == dict(dotenv.dotenv_values(str(path)))


def test_read_conf_file_missing_is_empty(tmp_path):
    assert config.read_conf_file(str(tmp_path / "nope.env")) == {}


def test_parse_id_list_keeps_numbers_and_skips_junk():
    assert config.parse_id_list("42, -100123 456 nonsense") == {42, -100123, 456}
    assert config.parse_id_list("") == set()


def test_id_list_from_env_prefers_the_first_name(monkeypatch):
    monkeypatch.setenv("ALLOWED_CHAT_IDS", "1")
    assert config.id_list_from_env("TELEGRAM_ALLOWED_CHAT_IDS", "ALLOWED_CHAT_IDS") == {1}
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "2")
    assert config.id_list_from_env("TELEGRAM_ALLOWED_CHAT_IDS", "ALLOWED_CHAT_IDS") == {2}


@pytest.mark.parametrize("raw", ["@me", "−100123456", "aris#1234, me"])
def test_an_id_list_with_no_valid_id_denies_everyone(monkeypatch, raw):
    """F-033: an allow-list typo used to read as "no limit" and open the bot."""
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", raw)
    monkeypatch.setenv("ALLOWED_CHAT_IDS", "7")  # a set list never falls back
    ids = config.id_list_from_env("TELEGRAM_ALLOWED_CHAT_IDS", "ALLOWED_CHAT_IDS")
    assert ids, "an empty set would mean no restriction"
    assert not ids & {0, 7, 42, -100123456, None}
    assert config.id_count(ids) == 0


def test_read_conf_file_skips_an_empty_key(tmp_path):
    path = tmp_path / "conf.env"
    path.write_text("=oops\n = spaced\nBOT_PLATFORMS=telegram\n", encoding="utf-8")
    assert config.read_conf_file(str(path)) == {"BOT_PLATFORMS": "telegram"}


def test_fallback_loader_survives_an_empty_key(tmp_path, monkeypatch):
    """F-039: without python-dotenv, "=oops" made os.environ raise at startup."""
    path = tmp_path / "conf.env"
    path.write_text("=oops\nBOT_PLATFORMS=telegram\n", encoding="utf-8")
    monkeypatch.setitem(sys.modules, "dotenv", None)  # import dotenv -> ImportError
    environ = {}
    monkeypatch.setattr(os, "environ", environ)  # keep the real one untouched
    config.load_conf_file(str(path))
    assert environ == {"BOT_PLATFORMS": "telegram"}


def test_enabled_platforms_from_bot_platforms(monkeypatch):
    monkeypatch.setenv("BOT_PLATFORMS", "Discord, telegram, discord, myspace")
    assert config.enabled_platforms() == ["discord", "telegram"]


def test_enabled_platforms_inferred_from_tokens(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "TOKEN_FILE", str(tmp_path / "token.txt"))
    assert config.enabled_platforms() == []
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    assert config.enabled_platforms() == ["telegram"]
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "x.y.z")
    assert config.enabled_platforms() == ["telegram", "discord"]
