"""Reading conf.env and the settings in it.

Standard library only, on purpose: setup_wizard.py imports this with the
system Python before any virtual environment or third-party library exists.
"""

import logging
import os
import re

# The project folder: one level up from this package.
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONF_FILE = os.path.join(HERE, "conf.env")
TOKEN_FILE = os.path.join(HERE, "token.txt")

# Every chat platform the bot can serve, in the order they are offered.
PLATFORMS = ("telegram", "discord")

log = logging.getLogger("reminder_bot")


class ConfFileError(Exception):
    """conf.env exists but can't be read as text. The message says how to fix it."""


def conf_not_utf8(path):
    # An editor saving as "ANSI" (Windows-1252) turns the example's "—" into a
    # byte UTF-8 can't decode, which used to end the bot and the wizard in a
    # raw UnicodeDecodeError traceback.
    return ConfFileError(
        f"{path} isn't UTF-8 text, so the settings in it can't be read. "
        "Save it again as UTF-8."
    )


def read_conf_file(path=CONF_FILE):
    """Parse a KEY=value file into an ordered dict. Missing file -> {}.

    Blank lines and # comments are skipped; one layer of matching quotes
    around a value is removed. In an unquoted value, whitespace followed by
    "#" starts a comment, as in python-dotenv: the bot reads conf.env with
    dotenv, so reading "123  # servers" as anything but "123" here would make
    setup_wizard.py offer a different value from the one the bot uses.

    Raises ConfFileError if the file isn't UTF-8.
    """
    values = {}
    if not os.path.exists(path):
        return values
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except UnicodeDecodeError:
        raise conf_not_utf8(path) from None
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        if not key:
            # "=value": nothing to set, and os.environ rejects an empty
            # name, so load_conf_file() would crash the bot on it.
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        elif value[:1] not in "\"'":
            value = re.sub(r"\s+#.*", "", value)
        values[key] = value
    return values


def load_conf_file(path=CONF_FILE):
    """Load conf.env into os.environ. Existing variables are never overwritten.

    Uses python-dotenv if it's installed; otherwise falls back to the small
    parser above so conf.env still works without the extra package. Raises
    ConfFileError if the file isn't UTF-8, whichever reader is used.
    """
    try:
        from dotenv import load_dotenv  # type: ignore
    except ImportError:
        load_dotenv = None
    if load_dotenv is not None:
        try:
            load_dotenv(path)
        except UnicodeDecodeError:
            raise conf_not_utf8(path) from None
        return
    for key, value in read_conf_file(path).items():
        os.environ.setdefault(key, value)


class TokenFileError(Exception):
    """token.txt exists but holds no usable token. The message says how to fix it."""


def read_token_file(path=TOKEN_FILE):
    """The Telegram token saved in token.txt, or "" if there is no such file.

    Editors on Windows add a byte-order mark (Notepad's UTF-8, PowerShell 5's
    Out-File, which also writes UTF-16). Read as plain UTF-8, the mark would
    stay glued to the token, which Telegram then rejects as invalid, and a
    UTF-16 file would end the bot and the wizard in a UnicodeDecodeError.

    Raises TokenFileError if the file can't be decoded, or holds anything
    besides the token: a second line ("# my bot") used to become part of the
    token, and the bot then stopped with a misleading "Network hiccup".
    """
    if not os.path.exists(path):
        return ""
    with open(path, "rb") as f:
        raw = f.read()
    encoding = "utf-16" if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else "utf-8-sig"
    try:
        text = raw.decode(encoding)
    except UnicodeDecodeError:
        raise TokenFileError(
            f"{path} isn't UTF-8 text, so the Telegram token in it can't be "
            "read. Save it again as UTF-8, or set TELEGRAM_BOT_TOKEN in conf.env."
        ) from None
    token = text.strip()
    if len(token.split()) > 1:
        raise TokenFileError(
            f"{path} holds more than the Telegram token (a second line, or "
            "spaces). Leave only the token in it, or set TELEGRAM_BOT_TOKEN "
            "in conf.env."
        )
    return token


def parse_id_list(raw, name="the id list"):
    """Turn "42, -100123 456" into {42, -100123, 456}, warning about junk."""
    ids = set()
    for part in (raw or "").replace(",", " ").split():
        try:
            ids.add(int(part))
        except ValueError:
            log.warning("Ignoring %r in %s — ids are numbers.", part, name)
    return ids


def first_set(*names):
    """The first of `names` set to something other than blanks, or None."""
    return next((name for name in names if os.environ.get(name, "").strip()), None)


def id_list_from_env(*names):
    """Read the first of `names` that is set in the environment as an id set.

    An empty set means "no restriction". A list that is set but holds no
    valid id at all ("@me", a username) is the opposite of that intent, so it
    comes back as {DENY_ALL}, which no id matches: a typo in an allow-list
    locks the bot down instead of silently opening it to everyone.
    """
    name = first_set(*names)
    if name is None:
        return set()
    ids = parse_id_list(os.environ[name], name)
    if not ids:
        log.error(
            "%s is set but holds no valid id, so nobody is allowed "
            "until it is fixed. Ids are numbers; see the README.", name,
        )
        return {DENY_ALL}
    return ids


class _DenyAll:
    """The one member of an allow-list that matched no valid id."""

    def __repr__(self):
        return "DENY_ALL"


DENY_ALL = _DenyAll()


def id_count(ids):
    """How many real ids an allow-list holds (DENY_ALL isn't one)."""
    return len(ids - {DENY_ALL})


def enabled_platforms():
    """Which platforms to run, from BOT_PLATFORMS or, failing that, the tokens.

    BOT_PLATFORMS is what setup_wizard.py writes ("telegram", "discord" or
    "telegram,discord"). An older conf.env without it keeps working: any
    platform with a token configured is started.
    """
    raw = os.environ.get("BOT_PLATFORMS", "").strip().lower()
    if raw:
        chosen = []
        for part in raw.replace(",", " ").split():
            if part not in PLATFORMS:
                log.warning(
                    "Ignoring %r in BOT_PLATFORMS — choose from %s.",
                    part, ", ".join(PLATFORMS),
                )
            elif part not in chosen:
                chosen.append(part)
        return chosen

    chosen = []
    if os.environ.get("TELEGRAM_BOT_TOKEN") or os.path.exists(TOKEN_FILE):
        chosen.append("telegram")
    if os.environ.get("DISCORD_BOT_TOKEN"):
        chosen.append("discord")
    return chosen
