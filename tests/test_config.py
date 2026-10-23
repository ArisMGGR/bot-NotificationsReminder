"""Tests for reading conf.env and the settings in it."""

import os
import shlex
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


TOKEN = "123456789:" + "B" * 35


@pytest.mark.parametrize(
    "raw",
    [
        TOKEN.encode() + b"\n",
        b"\xef\xbb\xbf" + TOKEN.encode() + b"\r\n",  # Notepad's UTF-8
        (TOKEN + "\r\n").encode("utf-16"),          # PowerShell 5's Out-File
    ],
)
def test_read_token_file_drops_the_byte_order_mark(tmp_path, raw):
    """F-044: the mark stayed glued to the token, or UTF-16 crashed startup."""
    path = tmp_path / "token.txt"
    path.write_bytes(raw)
    assert config.read_token_file(str(path)) == TOKEN


def test_read_token_file_explains_text_it_cannot_decode(tmp_path):
    path = tmp_path / "token.txt"
    path.write_bytes(b"\x80\x81 not text")
    with pytest.raises(config.TokenFileError):
        config.read_token_file(str(path))


@pytest.mark.parametrize("text", [TOKEN + "\n# my bot\n", TOKEN + " my bot\n"])
def test_read_token_file_refuses_more_than_the_token(tmp_path, text):
    """F-064: a second line became part of the token, and the bot stopped with
    a "Network hiccup" instead of saying what was wrong with the file."""
    path = tmp_path / "token.txt"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(config.TokenFileError, match="more than the Telegram token"):
        config.read_token_file(str(path))


def test_a_token_txt_with_a_second_line_stops_the_bot_with_a_reason(tmp_path, monkeypatch):
    pytest.importorskip("telegram")
    import reminder_bot

    path = tmp_path / "token.txt"
    path.write_text(TOKEN + "\n# my bot\n", encoding="utf-8")
    monkeypatch.setattr(config, "TOKEN_FILE", str(path))
    frontends, problems = reminder_bot.build_frontends(["telegram"])
    assert frontends == []
    assert len(problems) == 1 and "more than the Telegram token" in problems[0]


def test_read_token_file_missing_is_empty(tmp_path):
    assert config.read_token_file(str(tmp_path / "token.txt")) == ""


def test_an_unreadable_token_txt_stops_the_bot_with_a_reason(tmp_path, monkeypatch):
    """F-044: startup ended in a UnicodeDecodeError traceback."""
    pytest.importorskip("telegram")
    import reminder_bot

    path = tmp_path / "token.txt"
    path.write_bytes(b"\x80\x81 not text")
    monkeypatch.setattr(config, "TOKEN_FILE", str(path))
    frontends, problems = reminder_bot.build_frontends(["telegram"])
    assert frontends == []
    assert len(problems) == 1 and "isn't UTF-8 text" in problems[0]


def test_the_missing_library_hint_quotes_a_path_with_spaces(monkeypatch):
    """F-054: pasted, the unquoted hint split "My Bots" and ran ".../My"."""
    import reminder_bot
    import reminderbot

    python = "/home/pi/My Bots/bot/venv/bin/python"
    monkeypatch.setattr(reminder_bot.sys, "executable", python)
    # As if the library were missing: `from reminderbot import telegram_bot`
    # then raises ImportError.
    monkeypatch.setitem(sys.modules, "reminderbot.telegram_bot", None)
    monkeypatch.delattr(reminderbot, "telegram_bot", raising=False)
    frontends, problems = reminder_bot.build_frontends(["telegram"])
    assert frontends == []
    hint = next(line.strip() for line in problems[0].splitlines() if "-m pip install" in line)
    if os.name == "nt":
        # PowerShell 5.1 can't run a quoted path without its call operator.
        assert hint.startswith(f'& "{python}" -m pip')
    else:
        assert shlex.split(hint)[0] == python


def test_the_missing_library_hint_names_the_projects_venv_relatively(monkeypatch, tmp_path):
    """F-054: relative to the project folder, where the hint is pasted, the
    venv's Python needs no quotes, so it works in bash, cmd and PowerShell."""
    import reminder_bot
    import reminderbot

    here = tmp_path / "My Bots"
    scripts = ("Scripts", "python.exe") if os.name == "nt" else ("bin", "python")
    monkeypatch.setattr(config, "HERE", str(here))
    monkeypatch.setattr(reminder_bot.sys, "executable", os.path.join(str(here), "venv", *scripts))
    monkeypatch.setitem(sys.modules, "reminderbot.discord_bot", None)
    monkeypatch.delattr(reminderbot, "discord_bot", raising=False)
    frontends, problems = reminder_bot.build_frontends(["discord"])
    hint = next(line.strip() for line in problems[0].splitlines() if "-m pip install" in line)
    assert hint == f"{os.path.join('venv', *scripts)} -m pip install -r requirements-discord.txt"


# conf.env.example's "—" as an editor saving "ANSI" (Windows-1252) writes it.
ANSI_CONF = "# Reminder bot — settings\nBOT_PLATFORMS=telegram\n".encode("cp1252")


def test_read_conf_file_explains_text_it_cannot_decode(tmp_path):
    """F-047: an ANSI-saved conf.env raised a raw UnicodeDecodeError."""
    path = tmp_path / "conf.env"
    path.write_bytes(ANSI_CONF)
    with pytest.raises(config.ConfFileError):
        config.read_conf_file(str(path))


@pytest.mark.parametrize("with_dotenv", [True, False])
def test_load_conf_file_explains_text_it_cannot_decode(tmp_path, monkeypatch, with_dotenv):
    """The bot reads conf.env with dotenv when it's installed, else the parser above."""
    if with_dotenv:
        pytest.importorskip("dotenv")
    else:
        monkeypatch.setitem(sys.modules, "dotenv", None)  # import dotenv -> ImportError
    monkeypatch.setattr(os, "environ", {})  # keep the real one untouched
    path = tmp_path / "conf.env"
    path.write_bytes(ANSI_CONF)
    with pytest.raises(config.ConfFileError):
        config.load_conf_file(str(path))


def test_an_unreadable_conf_env_stops_the_bot_with_a_reason(tmp_path, monkeypatch, capsys):
    """F-047: startup ended in a UnicodeDecodeError traceback."""
    import reminder_bot

    path = tmp_path / "conf.env"
    path.write_bytes(ANSI_CONF)
    real_load = config.load_conf_file
    monkeypatch.setattr(config, "load_conf_file", lambda: real_load(str(path)))
    assert reminder_bot.main() == 1
    assert "isn't UTF-8 text" in capsys.readouterr().out


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
