"""Tests for setup_wizard.py.

The prompts are driven by a scripted list of answers and the network,
venv and service steps are replaced with stand-ins, so the whole flow can be
checked without typing, a token, or touching the real conf.env.
"""

import io
import os
import urllib.error

import pytest

import setup_wizard as sw
from reminderbot.config import read_conf_file

TG_TOKEN = "123456789:" + "A" * 35
DC_TOKEN = "M" * 24 + "." + "abcdef" + "." + "z" * 27

# Kept before the autouse fixture below swaps them for offline stand-ins.
REAL_CHECK_TELEGRAM = sw.check_telegram_token
REAL_CHECK_DISCORD = sw.check_discord_token


@pytest.fixture(autouse=True)
def isolated_wizard(tmp_path, monkeypatch):
    monkeypatch.setattr(sw, "CONF_FILE", str(tmp_path / "conf.env"))
    monkeypatch.setattr(sw, "BACKUP_FILE", str(tmp_path / "conf.env.bak"))
    monkeypatch.setattr(sw, "TOKEN_FILE", str(tmp_path / "token.txt"))
    monkeypatch.setattr(sw, "VENV_DIR", str(tmp_path / "venv"))
    monkeypatch.setattr(sw, "check_telegram_token", lambda t: (True, "@my_bot"))
    monkeypatch.setattr(sw, "check_discord_token", lambda t: (True, ("MyBot", "111")))
    if hasattr(os, "geteuid"):
        monkeypatch.setattr(sw.os, "geteuid", lambda: 1000)


def script(monkeypatch, answers):
    """Feed `answers` to input(), one per prompt; fail if the wizard asks more."""
    remaining = list(answers)
    prompts = []

    def fake_input(prompt=""):
        prompts.append(prompt)
        if not remaining:
            raise AssertionError(f"unexpected extra prompt: {prompt!r}")
        return remaining.pop(0)

    monkeypatch.setattr("builtins.input", fake_input)
    return prompts


def skip_install_steps(monkeypatch):
    calls = {}
    monkeypatch.setattr(sw, "setup_venv", lambda platforms: calls.setdefault("venv", platforms))
    monkeypatch.setattr(
        sw, "offer_service", lambda python, platforms: calls.setdefault("service", python),
    )
    return calls


# ---------------------------------------------------------------------------
# Small pieces
# ---------------------------------------------------------------------------


def test_parse_ids():
    assert sw.parse_ids("42, -7 42", allow_negative=True) == ([42, -7], [])
    assert sw.parse_ids("42 -7 abc 0", allow_negative=False) == ([42], ["-7", "abc", "0"])


def test_telegram_ids_accept_the_readmes_zero(monkeypatch):
    """README: set TELEGRAM_ALLOWED_CHAT_IDS=0 to learn your chat id. Re-running
    the wizard afterwards must keep that on Enter, not re-prompt forever."""
    assert sw.parse_ids("0", allow_negative=True) == ([0], [])
    script(monkeypatch, [""])  # one Enter; a second prompt fails the test
    assert sw.ask_ids("Allowed chat ids", "0", allow_negative=True) == "0"


def test_run_command_hint_quotes_a_path_with_spaces(monkeypatch):
    monkeypatch.setattr(sw, "HERE", "/home/pi/My Bots/bot")
    lines = sw.run_command_hint(None)
    if os.name == "nt":
        assert lines[0] == 'cd "/home/pi/My Bots/bot"'
    else:
        assert lines == ["cd '/home/pi/My Bots/bot' && python3 reminder_bot.py"]


def test_windows_run_hint_has_no_ampersands(monkeypatch):
    """F-052: Windows PowerShell 5.1 rejects "&&", so the pasted hint failed."""
    monkeypatch.setattr(sw, "HERE", r"C:\Users\pi\My Bots\bot")
    monkeypatch.setattr(sw.os, "name", "nt")
    lines = sw.run_command_hint(None)
    assert lines == [r'cd "C:\Users\pi\My Bots\bot"', "python3 reminder_bot.py"]
    assert not any("&&" in line for line in lines)


def test_a_superscript_digit_is_asked_again_not_a_crash(monkeypatch):
    """F-046: "²".isdigit() is True but int("²") raises, which ended the wizard."""
    prompts = script(monkeypatch, ["²", "2"])
    assert sw.ask_choice("Where?", [("telegram", "T"), ("discord", "D")], "telegram") == "discord"
    assert len(prompts) == 2


def test_mask_hides_most_of_a_token():
    masked = sw.mask(TG_TOKEN)
    assert masked.startswith("1234") and masked.endswith("AAAA")
    assert TG_TOKEN not in masked
    assert sw.mask("short") == "*****"


def test_token_shape_checks():
    assert sw.looks_like_telegram_token(TG_TOKEN)
    assert not sw.looks_like_telegram_token("hello")
    assert sw.looks_like_discord_token(DC_TOKEN)
    assert not sw.looks_like_discord_token(TG_TOKEN)


def test_default_platforms_from_existing_settings():
    assert sw.default_platforms({"BOT_PLATFORMS": "discord"}) == "discord"
    assert sw.default_platforms({"TELEGRAM_BOT_TOKEN": "x", "DISCORD_BOT_TOKEN": "y"}) == "telegram,discord"
    assert sw.default_platforms({}) == ""


@pytest.mark.parametrize(
    "bot_platforms",
    ["telgram", "Discord", "discord, myspace", "telegram,discord", "", "  "],
)
def test_default_platforms_agrees_with_the_bot(monkeypatch, bot_platforms):
    """F-067: a BOT_PLATFORMS naming no platform fell back to the tokens here,
    so the wizard offered Telegram as current while the bot started nothing."""
    from reminderbot import config

    existing = {"BOT_PLATFORMS": bot_platforms, "TELEGRAM_BOT_TOKEN": TG_TOKEN}
    monkeypatch.setattr(config, "TOKEN_FILE", sw.TOKEN_FILE)
    for key, value in existing.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    assert sw.default_platforms(existing) == ",".join(config.enabled_platforms())


def test_a_bot_platforms_naming_no_platform_is_pointed_out(monkeypatch, capsys):
    with open(sw.CONF_FILE, "w", encoding="utf-8") as f:
        f.write(f"BOT_PLATFORMS=telgram\nTELEGRAM_BOT_TOKEN={TG_TOKEN}\n")
    skip_install_steps(monkeypatch)
    script(monkeypatch, ["", "", "", ""])  # platform, token, ids, save
    assert sw.main() == 0
    assert "names no platform, so the bot starts none" in capsys.readouterr().out
    assert read_conf_file(sw.CONF_FILE)["BOT_PLATFORMS"] == "telegram"


def test_old_allowed_chat_ids_name_is_offered_as_the_default():
    assert sw.current_value({"ALLOWED_CHAT_IDS": "42"}, "TELEGRAM_ALLOWED_CHAT_IDS") == "42"


def test_render_conf_round_trips_and_keeps_extra_settings(tmp_path):
    settings = {
        "BOT_PLATFORMS": "telegram,discord",
        "TELEGRAM_BOT_TOKEN": TG_TOKEN,
        "TELEGRAM_ALLOWED_CHAT_IDS": "42,-7",
        "DISCORD_BOT_TOKEN": DC_TOKEN,
        "DISCORD_ALLOWED_GUILD_IDS": "",
    }
    path = tmp_path / "out.env"
    path.write_text(sw.render_conf(settings, {"MY_OWN": "keep me"}), encoding="utf-8")
    parsed = read_conf_file(str(path))
    assert parsed == {**settings, "MY_OWN": "keep me"}


KEPT_VALUES = {
    "GREETING": "  hi  ",
    "NOTE": "a # b",
    "QUOTE": "it's",
    "SAY": " it's # fine ",
    "QUOTED": '"x"',
    "PLAIN": "keep me",
    "GLUED": "a#b",
    "EMPTY": "",
}


def test_render_conf_keeps_values_that_needed_quotes(tmp_path):
    """F-038: a value read from quotes was written back bare, so its edge
    spaces or its " # ..." part were lost the next time conf.env was read."""
    path = tmp_path / "out.env"
    path.write_text(sw.render_conf({"BOT_PLATFORMS": "telegram"}, KEPT_VALUES), encoding="utf-8")
    parsed = read_conf_file(str(path))
    assert {k: parsed[k] for k in KEPT_VALUES} == KEPT_VALUES
    dotenv = pytest.importorskip("dotenv")
    parsed = dotenv.dotenv_values(str(path))
    assert {k: parsed[k] for k in KEPT_VALUES} == KEPT_VALUES, "the bot reads it the same"


def test_unmanaged_drops_managed_and_legacy_keys():
    existing = {"BOT_PLATFORMS": "telegram", "ALLOWED_CHAT_IDS": "1", "CUSTOM": "x"}
    assert sw.unmanaged(existing) == {"CUSTOM": "x"}


def test_write_conf_keeps_a_backup_and_is_private():
    with open(sw.CONF_FILE, "w", encoding="utf-8") as f:
        f.write("OLD=1\n")
    sw.write_conf("NEW=1\n")
    with open(sw.BACKUP_FILE, encoding="utf-8") as f:
        assert f.read() == "OLD=1\n"
    with open(sw.CONF_FILE, encoding="utf-8") as f:
        assert f.read() == "NEW=1\n"
    if os.name == "posix":
        assert os.stat(sw.CONF_FILE).st_mode & 0o777 == 0o600
        assert os.stat(sw.BACKUP_FILE).st_mode & 0o777 == 0o600


# ---------------------------------------------------------------------------
# Online checks
# ---------------------------------------------------------------------------


def test_real_telegram_check(monkeypatch):
    def rejected(url, headers=None, timeout=10):
        raise urllib.error.HTTPError(url, 401, "Unauthorized", {}, None)

    def offline(url, headers=None, timeout=10):
        raise urllib.error.URLError("no route to host")

    monkeypatch.setattr(sw, "http_get_json", rejected)
    assert REAL_CHECK_TELEGRAM(TG_TOKEN)[0] is False
    monkeypatch.setattr(sw, "http_get_json", offline)
    assert REAL_CHECK_TELEGRAM(TG_TOKEN)[0] is None
    monkeypatch.setattr(
        sw, "http_get_json",
        lambda url, headers=None, timeout=10: {"ok": True, "result": {"username": "my_bot"}},
    )
    assert REAL_CHECK_TELEGRAM(TG_TOKEN) == (True, "@my_bot")


def test_real_discord_check(monkeypatch):
    def fake(url, headers=None, timeout=10):
        assert headers["Authorization"] == f"Bot {DC_TOKEN}"
        if url.endswith("/users/@me"):
            return {"id": "222", "username": "MyBot"}
        return {"id": "111"}

    monkeypatch.setattr(sw, "http_get_json", fake)
    assert REAL_CHECK_DISCORD(DC_TOKEN) == (True, ("MyBot", "111"))

    def rejected(url, headers=None, timeout=10):
        raise urllib.error.HTTPError(url, 401, "Unauthorized", {}, None)

    monkeypatch.setattr(sw, "http_get_json", rejected)
    assert REAL_CHECK_DISCORD(DC_TOKEN)[0] is False


@pytest.mark.parametrize(
    "check,token",
    [
        (REAL_CHECK_TELEGRAM, "123456789:AAAA " + "B" * 31),   # a space: InvalidURL
        (REAL_CHECK_TELEGRAM, "123456789:ABC-DEF…"),          # the example, pasted
        (REAL_CHECK_DISCORD, "MTIz….Gh1b2c.abc…"),
        (REAL_CHECK_DISCORD, DC_TOKEN + " x"),
    ],
)
def test_a_token_no_service_could_accept_is_refused_offline(monkeypatch, check, token):
    """F-053: a space ended the wizard in an InvalidURL traceback, and a "…"
    was reported as "couldn't reach", whose next question keeps the token."""
    monkeypatch.setattr(
        sw, "http_get_json", lambda *a, **k: pytest.fail("sent a token no service accepts"),
    )
    ok, reason = check(token)
    assert ok is False
    assert "character" in reason


# ---------------------------------------------------------------------------
# The whole flow
# ---------------------------------------------------------------------------


def test_fresh_setup_for_both_platforms(monkeypatch):
    calls = skip_install_steps(monkeypatch)
    script(monkeypatch, [
        "3",                 # both platforms
        TG_TOKEN,            # Telegram token
        "42, -100",          # allowed chat ids
        DC_TOKEN,            # Discord token
        "1",                 # allowed servers
        "",                  # allowed users: no limit
        "",                  # allowed channels: no limit
        "y",                 # save
    ])
    assert sw.main() == 0
    conf = read_conf_file(sw.CONF_FILE)
    assert conf["BOT_PLATFORMS"] == "telegram,discord"
    assert conf["TELEGRAM_BOT_TOKEN"] == TG_TOKEN
    assert conf["TELEGRAM_ALLOWED_CHAT_IDS"] == "42,-100"
    assert conf["DISCORD_BOT_TOKEN"] == DC_TOKEN
    assert conf["DISCORD_ALLOWED_GUILD_IDS"] == "1"
    assert conf["DISCORD_ALLOWED_USER_IDS"] == ""
    assert calls["venv"] == ["telegram", "discord"]


def test_output_without_utf8_does_not_end_the_wizard(monkeypatch):
    """F-062: on Windows, output that isn't a console window (Git Bash, a pipe)
    uses the ANSI code page, which has no "→" or "✓", and printing the first
    one ended the wizard before anything was saved."""
    monkeypatch.setattr(sw.sys, "stdout", io.TextIOWrapper(io.BytesIO(), encoding="cp1252"))
    skip_install_steps(monkeypatch)
    script(monkeypatch, ["3", TG_TOKEN, "", DC_TOKEN, "", "", "", "y"])
    assert sw.main() == 0
    assert read_conf_file(sw.CONF_FILE)["BOT_PLATFORMS"] == "telegram,discord"


def test_rerun_keeps_existing_values_on_enter(monkeypatch):
    with open(sw.CONF_FILE, "w", encoding="utf-8") as f:
        f.write(f"TELEGRAM_BOT_TOKEN={TG_TOKEN}\nALLOWED_CHAT_IDS=42\nCUSTOM=1\n")
    skip_install_steps(monkeypatch)
    prompts = script(monkeypatch, ["", "", "", ""])  # platform, token, ids, save
    assert sw.main() == 0
    assert "[1]" in prompts[0], "Telegram should be the default choice"
    conf = read_conf_file(sw.CONF_FILE)
    assert conf["BOT_PLATFORMS"] == "telegram"
    assert conf["TELEGRAM_BOT_TOKEN"] == TG_TOKEN
    assert conf["TELEGRAM_ALLOWED_CHAT_IDS"] == "42"
    assert "ALLOWED_CHAT_IDS" not in conf, "the old name is replaced"
    assert conf["CUSTOM"] == "1"
    assert os.path.exists(sw.BACKUP_FILE)


def test_enter_keeps_an_id_list_written_with_an_inline_comment(monkeypatch):
    """F-026: the kept value used to include "# servers", so Enter was refused
    and typing "none" to get past it dropped the server list."""
    with open(sw.CONF_FILE, "w", encoding="utf-8") as f:
        f.write(
            f"BOT_PLATFORMS=discord\nDISCORD_BOT_TOKEN={DC_TOKEN}\n"
            "DISCORD_ALLOWED_GUILD_IDS=123   # servers\n"
        )
    skip_install_steps(monkeypatch)
    script(monkeypatch, ["", "", "", "", "", ""])  # platform, token, 3 lists, save
    assert sw.main() == 0
    assert read_conf_file(sw.CONF_FILE)["DISCORD_ALLOWED_GUILD_IDS"] == "123"


def test_switching_platforms_keeps_the_other_token(monkeypatch):
    with open(sw.CONF_FILE, "w", encoding="utf-8") as f:
        f.write(f"BOT_PLATFORMS=telegram\nTELEGRAM_BOT_TOKEN={TG_TOKEN}\n")
    skip_install_steps(monkeypatch)
    script(monkeypatch, ["2", DC_TOKEN, "", "", "", "y"])
    assert sw.main() == 0
    conf = read_conf_file(sw.CONF_FILE)
    assert conf["BOT_PLATFORMS"] == "discord"
    assert conf["TELEGRAM_BOT_TOKEN"] == TG_TOKEN


def test_bad_ids_and_rejected_tokens_are_asked_again(monkeypatch):
    results = iter([(False, "Telegram says this token is not valid"), (True, "@my_bot")])
    monkeypatch.setattr(sw, "check_telegram_token", lambda t: next(results))
    skip_install_steps(monkeypatch)
    script(monkeypatch, ["1", TG_TOKEN, TG_TOKEN, "abc", "42", "y"])
    assert sw.main() == 0
    assert read_conf_file(sw.CONF_FILE)["TELEGRAM_ALLOWED_CHAT_IDS"] == "42"


def test_unreachable_service_can_keep_the_token(monkeypatch):
    monkeypatch.setattr(sw, "check_telegram_token", lambda t: (None, "offline"))
    skip_install_steps(monkeypatch)
    script(monkeypatch, ["1", TG_TOKEN, "", "", "y"])  # keep anyway = Enter
    assert sw.main() == 0
    assert read_conf_file(sw.CONF_FILE)["TELEGRAM_BOT_TOKEN"] == TG_TOKEN


def test_a_token_txt_saved_with_a_bom_is_offered_without_it(monkeypatch):
    """F-044: Notepad's byte-order mark stayed glued to the token."""
    with open(sw.TOKEN_FILE, "wb") as f:
        f.write(b"\xef\xbb\xbf" + TG_TOKEN.encode())
    skip_install_steps(monkeypatch)
    script(monkeypatch, ["1", "", "", "y"])  # platform, keep token, ids, save
    assert sw.main() == 0
    assert read_conf_file(sw.CONF_FILE)["TELEGRAM_BOT_TOKEN"] == TG_TOKEN


def test_an_unreadable_token_txt_is_explained_not_a_crash(monkeypatch, capsys):
    """F-044: a token.txt that isn't UTF-8 raised UnicodeDecodeError here."""
    with open(sw.TOKEN_FILE, "wb") as f:
        f.write(b"\x80\x81 not text")
    skip_install_steps(monkeypatch)
    script(monkeypatch, ["1", TG_TOKEN, "", "y"])  # no token to keep: one is asked for
    assert sw.main() == 0
    assert "isn't UTF-8 text" in capsys.readouterr().out
    assert read_conf_file(sw.CONF_FILE)["TELEGRAM_BOT_TOKEN"] == TG_TOKEN


def test_an_unreadable_conf_env_is_explained_not_a_crash(monkeypatch, capsys):
    """F-047: a conf.env saved as "ANSI" raised UnicodeDecodeError here."""
    old = "# Reminder bot — settings\nBOT_PLATFORMS=discord\n".encode("cp1252")
    with open(sw.CONF_FILE, "wb") as f:
        f.write(old)
    skip_install_steps(monkeypatch)
    script(monkeypatch, ["1", TG_TOKEN, "", "y"])  # nothing to keep: asked afresh
    assert sw.main() == 0
    assert "isn't UTF-8 text" in capsys.readouterr().out
    assert read_conf_file(sw.CONF_FILE)["BOT_PLATFORMS"] == "telegram"
    with open(sw.BACKUP_FILE, "rb") as f:
        assert f.read() == old, "the unreadable file is kept as conf.env.bak"


def test_declining_to_save_writes_nothing(monkeypatch):
    calls = skip_install_steps(monkeypatch)
    script(monkeypatch, ["1", TG_TOKEN, "", "n"])
    assert sw.main() == 1
    assert not os.path.exists(sw.CONF_FILE)
    assert calls == {}


def test_ctrl_c_cancels_cleanly(monkeypatch):
    def interrupt(prompt=""):
        raise KeyboardInterrupt

    monkeypatch.setattr("builtins.input", interrupt)
    assert sw.main() == 1
    assert not os.path.exists(sw.CONF_FILE)


# ---------------------------------------------------------------------------
# Virtual environment
# ---------------------------------------------------------------------------


def test_venv_is_created_and_only_the_chosen_libraries_installed(monkeypatch):
    commands = []

    def fake_run(command, *args, **kwargs):
        commands.append(command)
        if command[1:3] == ["-m", "venv"]:
            python = sw.venv_python(sw.VENV_DIR)
            os.makedirs(os.path.dirname(python), exist_ok=True)
            open(python, "w").close()
        return type("Result", (), {"returncode": 0})()

    monkeypatch.setattr(sw.subprocess, "run", fake_run)
    script(monkeypatch, ["y"])
    python = sw.setup_venv(["discord"])
    assert python == sw.venv_python(sw.VENV_DIR)
    install = commands[-1]
    assert any(p.endswith("requirements-discord.txt") for p in install)
    assert not any(p.endswith("requirements-telegram.txt") for p in install)
    assert os.path.basename(sw.VENV_DIR) == "venv"


def test_declining_the_venv_runs_nothing(monkeypatch):
    monkeypatch.setattr(sw.subprocess, "run", lambda *a, **k: pytest.fail("ran a command"))
    script(monkeypatch, ["n"])
    assert sw.setup_venv(["telegram"]) is None


def test_declining_the_venv_points_back_to_it(monkeypatch, capsys):
    """F-058: the advice was a system-wide pip install, which Debian 12 and
    Ubuntu 23.04+ refuse ("externally-managed-environment"). The manual
    commands now create ./venv and install into it."""
    monkeypatch.setattr(sw.subprocess, "run", lambda *a, **k: pytest.fail("ran a command"))
    script(monkeypatch, ["n"])
    sw.setup_venv(["telegram"])
    out = capsys.readouterr().out
    shown = sw.shell_quote(os.path.relpath(sw.venv_python(), sw.HERE))
    assert "let it create ./venv" in out
    assert f"{sw.shell_quote(sw.sys.executable)} -m venv " in out
    assert f"{shown} -m pip install -r requirements-telegram.txt" in out
    assert "python3 -m pip" not in out


def test_failed_venv_creation_cleans_up(monkeypatch):
    def fake_run(command, *args, **kwargs):
        os.makedirs(sw.VENV_DIR, exist_ok=True)  # half-made folder
        return type("Result", (), {"returncode": 1})()

    monkeypatch.setattr(sw.subprocess, "run", fake_run)
    script(monkeypatch, ["y"])
    assert sw.setup_venv(["telegram"]) is None
    assert not os.path.exists(sw.VENV_DIR)


def service_message(monkeypatch, capsys):
    """What offer_service() says on a systemd Linux when the libraries are missing."""
    monkeypatch.setattr(sw.sys, "platform", "linux")
    monkeypatch.setattr(sw.shutil, "which", lambda name: "/usr/bin/" + name)
    script(monkeypatch, ["n"])  # don't install the service
    sw.offer_service(None, ["telegram"])
    return capsys.readouterr().out


def test_service_hint_names_the_venv_when_its_libraries_failed(monkeypatch, capsys):
    """F-045: a failed pip install into ./venv was reported as "the service would
    use the system Python", but install-service.sh runs ./venv whenever it exists."""
    python = sw.venv_python(sw.VENV_DIR)
    os.makedirs(os.path.dirname(python))
    open(python, "w").close()
    out = service_message(monkeypatch, capsys)
    assert "system Python" not in out
    assert "-m pip install -r requirements-telegram.txt" in out


def test_service_hint_without_a_venv_still_names_the_system_python(monkeypatch, capsys):
    assert "system Python" in service_message(monkeypatch, capsys)


def test_run_hint_names_a_venv_whose_install_failed(monkeypatch, capsys, tmp_path):
    """F-061: after a failed pip install the wizard said to install into ./venv,
    then to start the bot with python3, which doesn't have the libraries."""
    monkeypatch.setattr(sw, "HERE", str(tmp_path))
    python = sw.venv_python(sw.VENV_DIR)
    os.makedirs(os.path.dirname(python))
    open(python, "w").close()
    out = service_message(monkeypatch, capsys)
    assert f"{sw.shell_quote(os.path.relpath(python, sw.HERE))} reminder_bot.py" in out
    assert "python3 reminder_bot.py" not in out


def make_existing_venv():
    python = sw.venv_python(sw.VENV_DIR)
    os.makedirs(os.path.dirname(python))
    open(python, "w").close()
    return python


def test_recreating_the_venv_replaces_it(monkeypatch):
    old_file = os.path.join(sw.VENV_DIR, "old.txt")
    make_existing_venv()
    open(old_file, "w").close()

    def fake_run(command, *args, **kwargs):
        if command[1:3] == ["-m", "venv"]:
            make_existing_venv()
        return type("Result", (), {"returncode": 0})()

    monkeypatch.setattr(sw.subprocess, "run", fake_run)
    script(monkeypatch, ["2"])  # Delete it and create a fresh one
    assert sw.setup_venv(["telegram"]) == sw.venv_python(sw.VENV_DIR)
    assert not os.path.exists(old_file)


def test_a_venv_that_cant_be_deleted_is_explained_not_a_crash(monkeypatch, capsys):
    """F-056: on Windows a running bot keeps files in ./venv open, and the
    rmtree error ended the wizard in a traceback."""
    make_existing_venv()

    def in_use(path, *args, **kwargs):
        raise PermissionError(32, "The process cannot access the file")

    monkeypatch.setattr(sw.shutil, "rmtree", in_use)
    monkeypatch.setattr(sw.subprocess, "run", lambda *a, **k: pytest.fail("ran a command"))
    script(monkeypatch, ["2"])
    assert sw.setup_venv(["telegram"]) is None
    assert "stop the bot" in capsys.readouterr().out


def test_failed_venv_creation_leaves_an_existing_folder_alone(monkeypatch):
    """F-034: the clean-up used to delete a ./venv that was there before."""
    os.makedirs(sw.VENV_DIR)
    notes = os.path.join(sw.VENV_DIR, "my-notes.txt")
    with open(notes, "w", encoding="utf-8") as f:
        f.write("mine")
    monkeypatch.setattr(
        sw.subprocess, "run", lambda *a, **k: type("Result", (), {"returncode": 1})(),
    )
    assert sw.create_venv() is False
    assert os.path.exists(notes)
