#!/usr/bin/env python3
"""Interactive setup for the reminder bot.

Usage (from the project folder, as your normal user — not with sudo):
    python3 setup_wizard.py

It walks you through everything the bot needs:
  1. which platform(s) to run on: Telegram, Discord, or both
  2. the bot token for each, checked online so typos are caught now
  3. the optional allow-lists that limit who can use the bot
  4. writing all of that to conf.env (mode 600; the old file is kept as
     conf.env.bak)
  5. creating a virtual environment in ./venv and installing only the
     libraries the chosen platform(s) need
  6. installing the bot as a background service that starts on boot

Run it again at any time to change a setting: every question shows the
current value, and pressing Enter keeps it.

Only the Python standard library is used, so this runs before the virtual
environment or any bot library exists.
"""

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from reminderbot.config import CONF_FILE, PLATFORMS, TOKEN_FILE, read_conf_file  # noqa: E402

BACKUP_FILE = CONF_FILE + ".bak"
VENV_DIR = os.path.join(HERE, "venv")
MIN_PYTHON = (3, 10)
SERVICE_NAME = "reminder-bot"

PLATFORM_LABELS = {"telegram": "Telegram", "discord": "Discord"}

# What the bot needs in a Discord server: View Channels, Send Messages,
# Send Messages in Threads and Read Message History.
DISCORD_PERMISSIONS = 1024 | 2048 | 274877906944 | 65536

# Discord asks API clients to identify themselves like this.
PROJECT_URL = "https://github.com/ArisMGGR/bot-NotificationsReminder"
DISCORD_USER_AGENT = f"DiscordBot ({PROJECT_URL}, 1.0) reminder-bot-setup"

# The settings this wizard manages, in the order they are written.
MANAGED_KEYS = (
    "BOT_PLATFORMS",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_ALLOWED_CHAT_IDS",
    "DISCORD_BOT_TOKEN",
    "DISCORD_ALLOWED_GUILD_IDS",
    "DISCORD_ALLOWED_USER_IDS",
    "DISCORD_ALLOWED_CHANNEL_IDS",
)

# Older conf.env names this wizard reads as defaults and replaces on write.
LEGACY_KEYS = {"ALLOWED_CHAT_IDS": "TELEGRAM_ALLOWED_CHAT_IDS"}

CLEAR_WORDS = ("none", "-", "clear")


class Cancelled(Exception):
    """The user pressed Ctrl+C or Ctrl+D at a prompt."""


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------


def ask(question, default=""):
    """Ask for a line of text. Enter returns `default`."""
    try:
        answer = input(question).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        raise Cancelled
    return answer if answer else default


def ask_yes_no(question, default=True):
    hint = "[Y/n]" if default else "[y/N]"
    while True:
        answer = ask(f"{question} {hint} ").lower()
        if not answer:
            return default
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False
        print("  Please answer y or n.")


def ask_choice(question, choices, default):
    """Pick one of `choices` ([(key, description), ...]) by number or key."""
    print(question)
    for number, (key, description) in enumerate(choices, start=1):
        marker = "  (default)" if key == default else ""
        print(f"  {number}) {description}{marker}")
    keys = [key for key, _ in choices]
    default_number = keys.index(default) + 1 if default in keys else None
    prompt = f"Choose 1-{len(choices)}" + (f" [{default_number}]" if default_number else "") + ": "
    while True:
        answer = ask(prompt).lower()
        if not answer and default in keys:
            return default
        if answer.isdigit() and 1 <= int(answer) <= len(choices):
            return keys[int(answer) - 1]
        if answer in keys:
            return answer
        print(f"  Please type a number from 1 to {len(choices)}.")


def heading(text):
    print()
    print(text)
    print("-" * len(text))


# ---------------------------------------------------------------------------
# Values
# ---------------------------------------------------------------------------


def mask(token):
    """Show enough of a token to recognise it, not enough to use it."""
    if not token:
        return ""
    if len(token) <= 12:
        return "*" * len(token)
    return f"{token[:4]}…{token[-4:]}"


def looks_like_telegram_token(token):
    return re.fullmatch(r"\d{5,}:[A-Za-z0-9_-]{30,}", token) is not None


def looks_like_discord_token(token):
    return re.fullmatch(r"[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{20,}", token) is not None


def parse_ids(text, allow_negative):
    """"42, 99 7" -> ([42, 99, 7], []). Returns (ids, bad_parts), ids de-duplicated.

    With allow_negative (Telegram) any whole number is accepted: group chat ids
    are negative, and the README's find-your-chat-id trick sets the list to 0,
    which the bot reads fine — so rejecting it here would make Enter-to-keep
    loop forever. Otherwise (Discord) ids must be positive.
    """
    ids, bad = [], []
    for part in text.replace(",", " ").split():
        try:
            value = int(part)
        except ValueError:
            bad.append(part)
            continue
        if value <= 0 and not allow_negative:
            bad.append(part)
        elif value not in ids:
            ids.append(value)
    return ids, bad


def format_ids(ids):
    return ",".join(str(i) for i in ids)


def default_platforms(existing):
    """The platform choice to offer as the default, from an existing conf.env."""
    raw = existing.get("BOT_PLATFORMS", "").lower().replace(",", " ").split()
    chosen = [p for p in PLATFORMS if p in raw]
    if not chosen:
        if existing.get("TELEGRAM_BOT_TOKEN") or os.path.exists(TOKEN_FILE):
            chosen.append("telegram")
        if existing.get("DISCORD_BOT_TOKEN"):
            chosen.append("discord")
    return ",".join(chosen)


def current_value(existing, key):
    """An existing setting, falling back to its older name."""
    if existing.get(key):
        return existing[key]
    for old, new in LEGACY_KEYS.items():
        if new == key and existing.get(old):
            return existing[old]
    return ""


# ---------------------------------------------------------------------------
# Online token checks
# ---------------------------------------------------------------------------


def http_get_json(url, headers=None, timeout=10):
    request = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def check_telegram_token(token):
    """Ask Telegram who this token belongs to.

    Returns (True, "@botname"), (False, reason) if Telegram rejected it, or
    (None, reason) if Telegram couldn't be reached to check.
    """
    try:
        data = http_get_json(f"https://api.telegram.org/bot{token}/getMe")
    except urllib.error.HTTPError as e:
        if e.code in (401, 404):
            return False, "Telegram says this token is not valid"
        return None, f"Telegram answered with HTTP {e.code}"
    except (urllib.error.URLError, OSError, ValueError) as e:
        return None, str(getattr(e, "reason", e))
    if not data.get("ok"):
        return False, data.get("description", "Telegram rejected the token")
    return True, "@" + data.get("result", {}).get("username", "?")


def check_discord_token(token):
    """Ask Discord who this token belongs to.

    Returns (True, (bot name, application id)), (False, reason) if Discord
    rejected it, or (None, reason) if Discord couldn't be reached to check.
    """
    headers = {"Authorization": f"Bot {token}", "User-Agent": DISCORD_USER_AGENT}
    try:
        user = http_get_json("https://discord.com/api/v10/users/@me", headers)
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return False, "Discord says this token is not valid"
        return None, f"Discord answered with HTTP {e.code}"
    except (urllib.error.URLError, OSError, ValueError) as e:
        return None, str(getattr(e, "reason", e))
    app_id = user.get("id", "")
    try:
        app_id = http_get_json("https://discord.com/api/v10/oauth2/applications/@me", headers).get("id", app_id)
    except (urllib.error.URLError, OSError, ValueError):
        pass  # the bot user's id is the application id for every modern bot
    return True, (user.get("username", "?"), app_id)


def discord_invite_url(app_id):
    return (
        f"https://discord.com/oauth2/authorize?client_id={app_id}"
        f"&scope=bot+applications.commands&permissions={DISCORD_PERMISSIONS}"
    )


# ---------------------------------------------------------------------------
# Questions
# ---------------------------------------------------------------------------


def choose_platforms(existing):
    heading("Platforms")
    choice = ask_choice(
        "Where should the bot run?",
        [
            ("telegram", "Telegram only"),
            ("discord", "Discord only"),
            ("telegram,discord", "Both Telegram and Discord (each keeps its own reminders)"),
        ],
        default_platforms(existing) or "telegram",
    )
    return choice.split(",")


def ask_token(platform, current, looks_right, check, example):
    """Ask for a token until it is one we can use. Returns (token, check_info)."""
    label = PLATFORM_LABELS[platform]
    while True:
        if current:
            token = ask(f"{label} bot token [{mask(current)} — Enter to keep]: ", current)
        else:
            token = ask(f"{label} bot token: ")
        if not token:
            print(f"  A token is required to run on {label}.")
            continue

        if not looks_right(token):
            print(f"  That doesn't look like a {label} bot token (they look like {example}).")
            if not ask_yes_no("  Use it anyway?", default=False):
                current = ""
                continue

        print(f"  Checking the token with {label}…")
        ok, info = check(token)
        if ok:
            return token, info
        if ok is False:
            print(f"  ✗ {info}. Please paste it again.")
            current = ""
            continue
        print(f"  ! Couldn't reach {label} to check it ({info}).")
        if ask_yes_no("  Keep this token anyway?", default=True):
            return token, None
        current = ""


def ask_ids(question, current, allow_negative):
    """Ask for an optional id list. Enter keeps `current`; "none" clears it."""
    while True:
        if current:
            hint = f"[{current.replace(',', ', ')} — Enter to keep, \"none\" for no limit]"
        else:
            hint = "[Enter for no limit]"
        answer = ask(f"{question} {hint}: ", current)
        if answer.lower() in CLEAR_WORDS:
            return ""
        ids, bad = parse_ids(answer, allow_negative)
        if bad:
            print(f"  These aren't valid ids: {', '.join(bad)}. Ids are whole numbers.")
            continue
        return format_ids(ids)


def configure_telegram(existing, settings):
    heading("Telegram")
    print("Get a token from @BotFather in Telegram: send /newbot and follow the prompts.")
    current = current_value(existing, "TELEGRAM_BOT_TOKEN")
    if not current and os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE, "r", encoding="utf-8") as f:
            current = f.read().strip()
    token, info = ask_token(
        "telegram", current, looks_like_telegram_token, check_telegram_token,
        "123456789:ABC-DEF…",
    )
    if info:
        print(f"  ✓ Token works — your bot is {info}.")
    settings["TELEGRAM_BOT_TOKEN"] = token

    print()
    print("Who may use the bot? By default anyone who finds it can create their own")
    print("reminders (they never see yours). To allow only certain chats, list their")
    print("chat ids: send your bot a message, then open")
    print("  https://api.telegram.org/bot<token>/getUpdates")
    print("and look for \"chat\":{\"id\":…}. Group ids are negative.")
    print(f"If the bot is already running, stop it first (sudo systemctl stop {SERVICE_NAME}):")
    print("it would take your message before that page could show it.")
    settings["TELEGRAM_ALLOWED_CHAT_IDS"] = ask_ids(
        "Allowed chat ids", current_value(existing, "TELEGRAM_ALLOWED_CHAT_IDS"),
        allow_negative=True,
    )


def configure_discord(existing, settings):
    heading("Discord")
    print("Create a bot at https://discord.com/developers/applications:")
    print("  New Application → Bot → Reset Token, and copy the token.")
    print("No privileged intents are needed.")
    token, info = ask_token(
        "discord", current_value(existing, "DISCORD_BOT_TOKEN"),
        looks_like_discord_token, check_discord_token, "MTIz….Gh1b2c.abc…",
    )
    if info:
        name, app_id = info
        print(f"  ✓ Token works — your bot is {name}.")
        print("  Invite it to your server with this link:")
        print(f"    {discord_invite_url(app_id)}")
    settings["DISCORD_BOT_TOKEN"] = token

    print()
    print("Who may use the bot? Leave all three empty to allow everyone who can see it.")
    print("Every list you fill in must match (DMs skip the server list).")
    print("To copy an id: Discord Settings → Advanced → Developer Mode on, then")
    print("right-click a server, channel or user → Copy ID.")
    settings["DISCORD_ALLOWED_GUILD_IDS"] = ask_ids(
        "Allowed server ids", current_value(existing, "DISCORD_ALLOWED_GUILD_IDS"),
        allow_negative=False,
    )
    settings["DISCORD_ALLOWED_USER_IDS"] = ask_ids(
        "Allowed user ids", current_value(existing, "DISCORD_ALLOWED_USER_IDS"),
        allow_negative=False,
    )
    settings["DISCORD_ALLOWED_CHANNEL_IDS"] = ask_ids(
        "Allowed channel ids", current_value(existing, "DISCORD_ALLOWED_CHANNEL_IDS"),
        allow_negative=False,
    )


# ---------------------------------------------------------------------------
# conf.env
# ---------------------------------------------------------------------------

COMMENTS = {
    "BOT_PLATFORMS": "# Which chat platforms to run: telegram, discord, or telegram,discord",
    "TELEGRAM_BOT_TOKEN": "\n# --- Telegram ---\n# The token from @BotFather. It's a password: keep this file private.",
    "TELEGRAM_ALLOWED_CHAT_IDS": "# Optional: comma-separated chat ids allowed to use the bot. Empty = anyone.",
    "DISCORD_BOT_TOKEN": "\n# --- Discord ---\n# The bot token from the Developer Portal. It's a password: keep this file private.",
    "DISCORD_ALLOWED_GUILD_IDS": (
        "# Optional allow-lists (comma-separated ids). Empty = no limit. Every list\n"
        "# that is set must match; the server list doesn't apply to DMs."
    ),
}


def conf_value(value):
    """`value` as it must be written so it reads back unchanged.

    read_conf_file() hands back values with their quotes removed, so writing
    them out bare would lose what the quotes protected: edge spaces are
    stripped, and " # ..." becomes a comment. Such values are quoted again.
    Single quotes are literal to python-dotenv and systemd alike; double
    quotes are used only when the value holds a single quote and nothing
    those readers would treat as an escape.
    """
    needs_quotes = (
        value != value.strip()
        or re.search(r"\s#", value)
        or value[:1] in ("'", '"')
    )
    if not needs_quotes:
        return value
    if "'" not in value:
        return f"'{value}'"
    if '"' not in value and "\\" not in value:
        return f'"{value}"'
    return value  # holds both quote kinds: no quoting reads back unchanged


def render_conf(settings, extras):
    """The text of conf.env for `settings`, keeping unrelated `extras` as-is."""
    lines = [
        "# Reminder bot settings — written by setup_wizard.py.",
        "# Run `python3 setup_wizard.py` again to change them, or edit by hand.",
        "",
    ]
    for key in MANAGED_KEYS:
        if key not in settings:
            continue
        if key in COMMENTS:
            lines.append(COMMENTS[key])
        lines.append(f"{key}={conf_value(settings[key])}")
    if extras:
        lines.append("")
        lines.append("# --- Other settings (kept from your previous conf.env) ---")
        for key, value in extras.items():
            lines.append(f"{key}={conf_value(value)}")
    return "\n".join(lines) + "\n"


def write_conf(text):
    """Write conf.env readable only by you, keeping the previous one as .bak."""
    if os.path.exists(CONF_FILE):
        shutil.copy2(CONF_FILE, BACKUP_FILE)
        os.chmod(BACKUP_FILE, 0o600)
    tmp = CONF_FILE + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.chmod(tmp, 0o600)
    os.replace(tmp, CONF_FILE)


def unmanaged(existing):
    """Settings in an existing conf.env that this wizard doesn't manage."""
    return {
        key: value for key, value in existing.items()
        if key not in MANAGED_KEYS and key not in LEGACY_KEYS
    }


def summarise(settings):
    heading("Summary")
    platforms = settings["BOT_PLATFORMS"].split(",")
    print(f"  Platforms       : {', '.join(PLATFORM_LABELS[p] for p in platforms)}")
    for key in MANAGED_KEYS[1:]:
        if key not in settings:
            continue
        value = settings[key]
        shown = mask(value) if key.endswith("_TOKEN") else (value.replace(",", ", ") or "(no limit)")
        print(f"  {key:<28}: {shown}")


# ---------------------------------------------------------------------------
# Virtual environment
# ---------------------------------------------------------------------------


def venv_python(venv_dir=None):
    venv_dir = venv_dir or VENV_DIR
    if os.name == "nt":
        return os.path.join(venv_dir, "Scripts", "python.exe")
    return os.path.join(venv_dir, "bin", "python")


def requirement_files(platforms):
    return [os.path.join(HERE, f"requirements-{p}.txt") for p in platforms]


def create_venv():
    """Create ./venv. Returns True on success."""
    print(f"Creating a virtual environment in {VENV_DIR} …")
    existed = os.path.exists(VENV_DIR)
    result = subprocess.run([sys.executable, "-m", "venv", VENV_DIR])
    if result.returncode == 0 and os.path.exists(venv_python()):
        return True
    print("  ✗ Couldn't create the virtual environment.")
    if existed:
        # Not ours to delete: the folder was there before this run (a broken
        # or hand-made venv), and whatever is in it may matter to the user.
        print(f"  {VENV_DIR} already existed, so it was left as it was.")
    else:
        # A failed run can leave a half-made folder behind that we just created.
        shutil.rmtree(VENV_DIR, ignore_errors=True)
    if sys.platform.startswith("linux"):
        version = f"{sys.version_info.major}.{sys.version_info.minor}"
        print("  On Debian/Ubuntu/Raspberry Pi OS this usually means the venv module is")
        print("  missing. Install it and run the wizard again:")
        print(f"    sudo apt install python3-venv   (or python{version}-venv)")
    return False


def install_requirements(python, platforms):
    """pip install the chosen platforms' libraries. Returns True on success."""
    print("Installing the libraries (this can take a minute) …")
    subprocess.run([python, "-m", "pip", "install", "--quiet", "--upgrade", "pip"])
    command = [python, "-m", "pip", "install", "--upgrade"]
    for path in requirement_files(platforms):
        command += ["-r", path]
    result = subprocess.run(command)
    if result.returncode != 0:
        print("  ✗ Installing the libraries failed (see the messages above).")
        return False
    print("  ✓ Libraries installed.")
    return True


def setup_venv(platforms):
    """Offer to create/update ./venv. Returns the Python the bot should use, or None."""
    heading("Virtual environment")
    print("A virtual environment keeps the bot's libraries in ./venv, separate from")
    print("the rest of the system.")
    python = venv_python()

    if os.path.exists(python):
        action = ask_choice(
            "A virtual environment already exists in ./venv.",
            [
                ("update", "Keep it and install/update the libraries"),
                ("recreate", "Delete it and create a fresh one"),
                ("skip", "Leave it as it is"),
            ],
            "update",
        )
        if action == "skip":
            return python
        if action == "recreate":
            shutil.rmtree(VENV_DIR)
            if not create_venv():
                return None
    else:
        if not ask_yes_no("Create a virtual environment in ./venv and install the libraries?"):
            files = " ".join(f"-r requirements-{p}.txt" for p in platforms)
            print("  Skipped. Install the libraries yourself before starting the bot:")
            print(f"    python3 -m pip install {files}")
            return None
        if not create_venv():
            return None

    return python if install_requirements(python, platforms) else None


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


def shell_quote(text):
    """Quote a path for a command the user will copy and paste."""
    if os.name == "nt":
        return subprocess.list2cmdline([text])
    return shlex.quote(text)


def run_command_hint(python):
    shown = os.path.relpath(python, HERE) if python else "python3"
    return f"cd {shell_quote(HERE)} && {shell_quote(shown)} reminder_bot.py"


def offer_service(python):
    heading("Background service")
    can_install = sys.platform.startswith("linux") and shutil.which("systemctl")
    if not can_install:
        print("systemd isn't available here, so the bot can't be installed as a service.")
        print("Start it by hand with:")
        print(f"    {run_command_hint(python)}")
        return

    if python is None:
        print("The libraries aren't installed in ./venv, so the service would use the")
        print("system Python. Make sure the libraries are installed there first.")

    if ask_yes_no(
        "Install (or update) the bot as a service that runs in the background and\n"
        "starts on boot? This asks for your sudo password."
    ):
        result = subprocess.run(["bash", os.path.join(HERE, "install-service.sh")])
        if result.returncode != 0:
            print("  ✗ The service install didn't finish (see the messages above).")
            print("  Fix the problem and run ./install-service.sh again.")
        return

    print("Skipped. You can:")
    print(f"  • start the bot by hand:   {run_command_hint(python)}")
    print("  • install the service later: ./install-service.sh")
    print("If the bot already runs as a service, restart it to pick up the new settings:")
    print(f"    sudo systemctl restart {SERVICE_NAME}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def run():
    print("Reminder bot setup")
    print("==================")
    print("Answer a few questions; press Enter to accept the value in [brackets].")
    print("Press Ctrl+C at any time to stop without saving.")

    if sys.version_info < MIN_PYTHON:
        print(
            f"\n✗ The bot needs Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} or newer, and this is "
            f"{sys.version_info.major}.{sys.version_info.minor}. Install a newer Python and run "
            "this wizard with it."
        )
        return 1

    if hasattr(os, "geteuid") and os.geteuid() == 0:
        print("\n! You're running this as root. Your settings and ./venv would then be")
        print("  owned by root. Run it as your normal user instead; it asks for sudo")
        print("  itself when it installs the service.")
        if not ask_yes_no("Continue as root anyway?", default=False):
            return 1

    existing = read_conf_file(CONF_FILE)
    if existing:
        print("\nFound your existing conf.env — press Enter at a question to keep its value.")

    platforms = choose_platforms(existing)
    settings = {"BOT_PLATFORMS": ",".join(platforms)}
    if "telegram" in platforms:
        configure_telegram(existing, settings)
    if "discord" in platforms:
        configure_discord(existing, settings)

    # Keep the token of a platform that was switched off, so switching it back
    # on later doesn't mean finding the token again.
    for key in MANAGED_KEYS:
        if key not in settings and current_value(existing, key):
            settings[key] = current_value(existing, key)

    summarise(settings)
    if not ask_yes_no("\nSave these settings to conf.env?"):
        print("Nothing was saved.")
        return 1
    write_conf(render_conf(settings, unmanaged(existing)))
    backup_note = " (previous version kept in conf.env.bak)" if os.path.exists(BACKUP_FILE) else ""
    print(f"  ✓ Saved conf.env, readable only by you{backup_note}.")

    python = setup_venv(platforms)
    offer_service(python)

    print("\nAll done.")
    if "telegram" in platforms:
        print("  Telegram: open your bot and send /start.")
    if "discord" in platforms:
        print("  Discord: in a server with the bot (or a DM with it), type /new.")
        print("  Slash commands can take a minute to appear the first time.")
    return 0


def main():
    try:
        return run()
    except Cancelled:
        print("Setup cancelled.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
