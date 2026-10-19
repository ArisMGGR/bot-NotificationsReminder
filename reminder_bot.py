"""Reminder Bot — recurring reminders on Telegram, Discord, or both.

Chat with your bot to create recurring reminders. When a reminder is due, the
bot sends you a message with an "OK" button. Pressing OK restarts that
reminder's timer, so it fires again after the same interval.

Setup (one time):
    python3 setup_wizard.py

The wizard asks which platform(s) to use, checks your bot token(s), writes
conf.env, creates a virtual environment in ./venv with the libraries you
need, and can install the bot as a background service.

Run it by hand:
    venv/bin/python reminder_bot.py

Which platforms run is set by BOT_PLATFORMS in conf.env ("telegram",
"discord" or "telegram,discord"). Each platform keeps its own reminders:
a Telegram chat and a Discord channel never see each other's.
"""

import asyncio
import os
import shlex
import subprocess
import sys

# python-telegram-bot 20.x still installs on Python 3.9, so a stale interpreter
# fails later with a confusing library error instead of an obvious one. Say it
# here, before the first third-party import.
if sys.version_info < (3, 10):
    raise SystemExit(
        "This bot needs Python 3.10 or newer (you have "
        f"{sys.version_info.major}.{sys.version_info.minor})."
    )

from reminderbot import config  # noqa: E402

MISSING_LIBRARY = (
    "ERROR: {platform} is enabled but its library isn't installed ({error}).\n"
    "Run setup_wizard.py again, or install it into the bot's Python:\n"
    "    {python} -m pip install -r requirements-{platform}.txt\n"
)


def shell_quote(text):
    """Quote a path for a command the user will copy and paste.

    The bot's Python lives in the project folder's venv, so a folder named
    "My Bots" would otherwise split the pasted command at the space.
    """
    if os.name == "nt":
        return subprocess.list2cmdline([text])
    return shlex.quote(text)


def pip_python():
    """The bot's Python as the MISSING_LIBRARY hint should name it.

    The hint names requirements-<platform>.txt relative to the project
    folder, so it is pasted there. A venv inside the project is then named
    relative to it, which needs no quotes in any shell. Any other path is
    quoted; on Windows a quoted path also needs PowerShell's call operator
    "&", since Windows PowerShell 5.1 (the default shell) rejects
    "C:\\My Bots\\python.exe" -m pip with "Unexpected token '-m'".
    """
    python = sys.executable
    try:
        relative = os.path.relpath(python, config.HERE)
    except ValueError:  # Windows: on another drive
        relative = os.pardir
    if relative != os.pardir and not relative.startswith(os.pardir + os.sep):
        python = relative
    quoted = shell_quote(python)
    if os.name == "nt" and quoted != python:
        return f"& {quoted}"
    return quoted


def build_frontends(platforms):
    """Create a front-end for each enabled platform, or explain what's missing."""
    frontends = []
    problems = []

    if "telegram" in platforms:
        try:
            from reminderbot import telegram_bot
        except ImportError as e:
            problems.append(MISSING_LIBRARY.format(
                platform="telegram", error=e, python=pip_python(),
            ))
        else:
            try:
                token = telegram_bot.get_token()
            except config.TokenFileError as e:
                problems.append(f"ERROR: {e}\n")
            else:
                if token:
                    frontends.append(telegram_bot.TelegramFrontend(token))
                else:
                    problems.append(
                        "ERROR: Telegram is enabled but no bot token was found.\n"
                        "Get one from @BotFather in Telegram (send /newbot), then run\n"
                        "setup_wizard.py, or set TELEGRAM_BOT_TOKEN in conf.env.\n"
                    )

    if "discord" in platforms:
        try:
            from reminderbot import discord_bot
        except ImportError as e:
            problems.append(MISSING_LIBRARY.format(
                platform="discord", error=e, python=pip_python(),
            ))
        else:
            token = discord_bot.get_token()
            if token:
                frontends.append(discord_bot.DiscordFrontend(token))
            else:
                problems.append(
                    "ERROR: Discord is enabled but no bot token was found.\n"
                    "Create a bot at https://discord.com/developers/applications,\n"
                    "then run setup_wizard.py, or set DISCORD_BOT_TOKEN in conf.env.\n"
                )

    return frontends, problems


def main():
    try:
        config.load_conf_file()
    except config.ConfFileError as e:
        print(f"ERROR: {e}\nOr run python3 setup_wizard.py to write a new one.\n")
        return 1
    platforms = config.enabled_platforms()
    if not platforms:
        print(
            "ERROR: No platform is configured.\n\n"
            "Run the setup wizard to choose Telegram and/or Discord and enter\n"
            "your bot token(s):\n"
            "    python3 setup_wizard.py\n"
        )
        return 1

    frontends, problems = build_frontends(platforms)
    if problems:
        print("\n".join(problems))
        return 1

    from reminderbot import core

    core.migrate_legacy_data_file()
    try:
        return asyncio.run(core.run_frontends(frontends))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
