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


def read_conf_file(path=CONF_FILE):
    """Parse a KEY=value file into an ordered dict. Missing file -> {}.

    Blank lines and # comments are skipped; one layer of matching quotes
    around a value is removed. In an unquoted value, whitespace followed by
    "#" starts a comment, as in python-dotenv: the bot reads conf.env with
    dotenv, so reading "123  # servers" as anything but "123" here would make
    setup_wizard.py offer a different value from the one the bot uses.
    """
    values = {}
    if not os.path.exists(path):
        return values
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
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
    parser above so conf.env still works without the extra package.
    """
    try:
        from dotenv import load_dotenv  # type: ignore
        load_dotenv(path)
        return
    except ImportError:
        pass
    for key, value in read_conf_file(path).items():
        os.environ.setdefault(key, value)


def parse_id_list(raw, name="the id list"):
    """Turn "42, -100123 456" into {42, -100123, 456}, warning about junk."""
    ids = set()
    for part in (raw or "").replace(",", " ").split():
        try:
            ids.add(int(part))
        except ValueError:
            log.warning("Ignoring %r in %s — ids are numbers.", part, name)
    return ids


def id_list_from_env(*names):
    """Read the first of `names` that is set in the environment as an id set.

    An empty set means "no restriction". A list that is set but holds no
    valid id at all ("@me", a username) is the opposite of that intent, so it
    comes back as {DENY_ALL}, which no id matches: a typo in an allow-list
    locks the bot down instead of silently opening it to everyone.
    """
    for name in names:
        raw = os.environ.get(name, "").strip()
        if raw:
            ids = parse_id_list(raw, name)
            if not ids:
                log.error(
                    "%s is set but holds no valid id, so nobody is allowed "
                    "until it is fixed. Ids are numbers; see the README.", name,
                )
                return {DENY_ALL}
            return ids
    return set()


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
