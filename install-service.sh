#!/usr/bin/env bash
#
# Install the Reminder Bot as a systemd service so it runs in the background
# and starts automatically on boot — no terminal needed.
#
# Usage:
#   ./install-service.sh
#
# setup_wizard.py offers to run this for you at the end. Run it from the
# project folder; it needs sudo to install the service file, and it will ask
# for your password when required. Running it again updates the service and
# restarts the bot, so new settings in conf.env take effect.

set -euo pipefail

# Answer --help before doing anything: everything below uses sudo, and asking
# for help must never install and restart the service.
if [[ $# -gt 0 ]]; then
    case "$1" in
        -h|--help)
            sed -n '3,12p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
    esac
    echo "Unknown option: $1 (this script takes no options; see --help)." >&2
    exit 1
fi

SERVICE_NAME="reminder-bot"
TEMPLATE="reminder-bot.service"

# The name the service had before the bot supported Discord. If it is still
# installed it is removed below, or both would run with the same token.
LEGACY_SERVICE_NAME="telegram-reminder-bot"

# --- Figure out where everything lives -------------------------------------

# Directory this script sits in = the project directory.
WORKDIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$WORKDIR"

# Checked before anything is changed, so a machine that can't take the
# service stops here with a reason rather than halfway through the install.
if ! command -v systemctl >/dev/null 2>&1; then
    echo "Error: systemctl not found, so the bot can't be installed as a systemd service." >&2
    echo "Start it by hand instead:  venv/bin/python reminder_bot.py" >&2
    exit 1
fi

# Installing a unit into /etc needs root. Skip sudo when we already are root,
# so this still works on a system that doesn't have sudo installed.
if [[ "$(id -u)" -eq 0 ]]; then
    SUDO=""
elif command -v sudo >/dev/null 2>&1; then
    SUDO="sudo"
else
    echo "Error: need root to install the service, and sudo isn't installed." >&2
    echo "Re-run this script as root." >&2
    exit 1
fi

# The user the bot should run as (the person invoking the script, not root).
RUN_USER="${SUDO_USER:-$(id -un)}"

# Pick a Python interpreter: prefer the virtualenv setup_wizard.py creates,
# then an older .venv, else system python3.
if [[ -x "$WORKDIR/venv/bin/python" ]]; then
    PYTHON="$WORKDIR/venv/bin/python"
elif [[ -x "$WORKDIR/.venv/bin/python" ]]; then
    PYTHON="$WORKDIR/.venv/bin/python"
else
    # "|| true": under set -e a failed lookup would end the script here
    # without a word; say why instead.
    PYTHON="$(command -v python3 || true)"
    if [[ -z "$PYTHON" ]]; then
        echo "Error: no ./venv and no python3 on the PATH, so there is no Python to run the bot with." >&2
        echo "Install Python 3.10 or newer, then run python3 setup_wizard.py to create ./venv." >&2
        exit 1
    fi
fi

echo "Project directory : $WORKDIR"
echo "Run as user       : $RUN_USER"
echo "Python interpreter: $PYTHON"
echo

# --- Sanity checks ----------------------------------------------------------

if [[ ! -f "$WORKDIR/reminder_bot.py" ]]; then
    echo "Error: reminder_bot.py not found in $WORKDIR" >&2
    echo "Run this script from inside the project folder." >&2
    exit 1
fi

if [[ ! -f "$WORKDIR/conf.env" ]]; then
    echo "Error: no conf.env file found in $WORKDIR" >&2
    echo "Create one first:  python3 setup_wizard.py" >&2
    exit 1
fi

# The conf.env file holds the bot token(s), and systemd reads it as root before
# dropping to $RUN_USER, so nobody else needs to be able to read it.
ENV_MODE="$(stat -c '%a' "$WORKDIR/conf.env" 2>/dev/null || echo "")"
if [[ "$ENV_MODE" != "600" ]]; then
    echo "Tightening permissions on conf.env (was ${ENV_MODE:-unknown}) so only you can read your token."
    chmod 600 "$WORKDIR/conf.env"
fi

# Check the libraries for each enabled platform are installed. Ask the bot's
# own config code which platforms those are, so this check can never disagree
# with what the bot will actually start (case, the token-based fallback for a
# conf.env without BOT_PLATFORMS, ...). Under systemd only conf.env counts, so
# the same variables exported in this shell are ignored.
PLATFORMS="$(env -u BOT_PLATFORMS -u TELEGRAM_BOT_TOKEN -u DISCORD_BOT_TOKEN \
    "$PYTHON" -c 'from reminderbot import config; config.load_conf_file(); print(" ".join(config.enabled_platforms()))')" \
    || PLATFORMS=""
if [[ -z "$PLATFORMS" ]]; then
    echo "Warning: conf.env doesn't enable any platform, so the bot will exit at start." >&2
    echo "Set BOT_PLATFORMS and the token(s), e.g. with: python3 setup_wizard.py" >&2
    echo
fi
for platform in $PLATFORMS; do
    case "$platform" in
        telegram) module="telegram" ;;
        discord)  module="discord" ;;
        *) continue ;;
    esac
    if ! "$PYTHON" -c "import $module" >/dev/null 2>&1; then
        echo "Warning: the $platform library doesn't seem installed for $PYTHON." >&2
        echo "Install it first, e.g.:" >&2
        echo "    $PYTHON -m pip install -r requirements-$platform.txt" >&2
        echo "(or run python3 setup_wizard.py and let it create ./venv)" >&2
        echo
    fi
done

# --- Build the unit file from the template ----------------------------------

# The template double-quotes the paths systemd would otherwise split at spaces
# (ExecStart=, ReadWritePaths=). A few characters can't be carried through that
# quoting safely, so refuse them up front rather than write a broken unit.
for path in "$WORKDIR" "$PYTHON"; do
    if [[ "$path" == *[\"\\\$]* || "$path" == *$'\n'* ]]; then
        echo "Error: systemd can't use this path as it is: $path" >&2
        echo "Move the project (or Python) to a path without \", \\, \$ or line breaks." >&2
        exit 1
    fi
done

TMP_UNIT="$(mktemp)"
trap 'rm -f "$TMP_UNIT"' EXIT

# systemd reads "%" in these settings as the start of a specifier (%h, %n, ...),
# so a literal one has to be doubled.
systemd_escape() {
    printf '%s' "$1" | sed -e 's/%/%%/g'
}

# Escape the replacement text: an unescaped "&" in a path would expand to the
# whole match, and an unescaped "|" would end the s/// expression outright.
sed_escape() {
    printf '%s' "$1" | sed -e 's/[\\&|]/\\&/g'
}

sed \
    -e "s|__USER__|$(sed_escape "$RUN_USER")|g" \
    -e "s|__WORKDIR__|$(sed_escape "$(systemd_escape "$WORKDIR")")|g" \
    -e "s|__PYTHON__|$(sed_escape "$(systemd_escape "$PYTHON")")|g" \
    "$TEMPLATE" > "$TMP_UNIT"

# --- Retire the old Telegram-only service -----------------------------------

LEGACY_UNIT="/etc/systemd/system/${LEGACY_SERVICE_NAME}.service"
if [[ -f "$LEGACY_UNIT" ]]; then
    echo "Found the old $LEGACY_SERVICE_NAME service — replacing it with $SERVICE_NAME ..."
    $SUDO systemctl disable --now "$LEGACY_SERVICE_NAME" >/dev/null 2>&1 || true
    $SUDO rm -f "$LEGACY_UNIT"
    $SUDO systemctl reset-failed "$LEGACY_SERVICE_NAME" >/dev/null 2>&1 || true
fi

# --- Install and start ------------------------------------------------------

DEST="/etc/systemd/system/${SERVICE_NAME}.service"

echo "Installing service to $DEST ..."
$SUDO cp "$TMP_UNIT" "$DEST"
$SUDO chmod 644 "$DEST"

echo "Reloading systemd and enabling the service ..."
$SUDO systemctl daemon-reload
$SUDO systemctl enable "$SERVICE_NAME" >/dev/null
# restart (not just start) so a re-run picks up new settings or code.
$SUDO systemctl restart "$SERVICE_NAME"

# Type=simple counts the service as started the moment it is forked, so the
# restart above succeeds even when the unit can't run at all (a bad path, a
# sandbox setup failure). Give it a few seconds, then check it is still up.
# A bot that fails later than that (a rejected token on a slow machine) can
# still slip past; its reason is then in the journal.
echo "Checking the service stays up ..."
sleep 5
if ! systemctl is-active --quiet "$SERVICE_NAME"; then
    echo >&2
    echo "Error: the service is installed but isn't running." >&2
    echo "See why with:  journalctl -u $SERVICE_NAME -n 50 --no-pager" >&2
    exit 1
fi

echo
echo "Done. The bot is now running in the background and will start on boot."
echo
echo "Handy commands:"
echo "  Status : sudo systemctl status $SERVICE_NAME"
echo "  Logs   : journalctl -u $SERVICE_NAME -f"
echo "  Stop   : sudo systemctl stop $SERVICE_NAME"
echo "  Start  : sudo systemctl start $SERVICE_NAME"
echo "  Restart: sudo systemctl restart $SERVICE_NAME   (after editing conf.env)"
echo "  Disable: sudo systemctl disable --now $SERVICE_NAME"
echo "  Remove : ./uninstall-service.sh"
