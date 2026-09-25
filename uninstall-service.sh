#!/usr/bin/env bash
#
# Remove the Reminder Bot's systemd service — the inverse of
# install-service.sh.
#
# Usage:
#   ./uninstall-service.sh
#
# Stops the service, stops it starting on boot, and deletes the unit file.
#
# Your reminders, logs, conf.env and venv are NOT touched. install-service.sh only ever
# created the unit file, so that is all this removes; the data belongs to you
# and is listed at the end so you can delete it yourself if you want to.
#
# Needs sudo to remove the unit file, and will ask for your password.

set -euo pipefail

SERVICE_NAME="reminder-bot"
UNIT="/etc/systemd/system/${SERVICE_NAME}.service"

# Directory this script sits in = the project directory.
WORKDIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ $# -gt 0 ]]; then
    case "$1" in
        -h|--help)
            sed -n '3,16p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
    esac
    echo "Unknown option: $1 (this script takes no options; see --help)." >&2
    exit 1
fi

# --- Sanity checks ----------------------------------------------------------

if ! command -v systemctl >/dev/null 2>&1; then
    echo "Error: systemctl not found, so there is no systemd service to remove." >&2
    echo "If you started the bot by hand, stop it with Ctrl+C in its terminal." >&2
    exit 1
fi

# Removing a unit from /etc needs root. Skip sudo when we already are root, so
# this still works on a system that doesn't have sudo installed.
if [[ "$(id -u)" -eq 0 ]]; then
    SUDO=""
elif command -v sudo >/dev/null 2>&1; then
    SUDO="sudo"
else
    echo "Error: need root to remove $UNIT, and sudo isn't installed." >&2
    echo "Re-run this script as root." >&2
    exit 1
fi

# --- Work out what's actually there -----------------------------------------

# systemctl exits non-zero for a unit that is inactive, disabled or unknown —
# all normal answers here. Under `set -e` an unguarded query would abort the
# script instead of telling us what we asked, so every one is guarded.
query() {
    systemctl "$1" "$SERVICE_NAME" 2>/dev/null || true
}

IS_ACTIVE="$(query is-active)"
IS_ENABLED="$(query is-enabled)"

if [[ ! -f "$UNIT" && "$IS_ACTIVE" != "active" && "$IS_ENABLED" != "enabled" ]]; then
    echo "Nothing to do: $SERVICE_NAME is not installed as a systemd service."
    echo "(No unit file at $UNIT, and the service isn't running or enabled.)"
    exit 0
fi

echo "Service   : $SERVICE_NAME"
echo "Unit file : $UNIT$([[ -f "$UNIT" ]] || echo "  (missing)")"
echo "State     : ${IS_ACTIVE:-unknown}, ${IS_ENABLED:-not enabled}"
echo

# --- Remove it --------------------------------------------------------------

if [[ "$IS_ACTIVE" == "active" ]]; then
    echo "Stopping the service ..."
    $SUDO systemctl stop "$SERVICE_NAME"
else
    echo "The service isn't running."
fi

if [[ "$IS_ENABLED" == "enabled" || "$IS_ENABLED" == "enabled-runtime" ]]; then
    echo "Disabling it so it no longer starts on boot ..."
    $SUDO systemctl disable "$SERVICE_NAME" >/dev/null
fi

if [[ -f "$UNIT" ]]; then
    echo "Removing $UNIT ..."
    $SUDO rm -f "$UNIT"
fi

echo "Reloading systemd ..."
$SUDO systemctl daemon-reload
# Clears a lingering "failed" entry so the removed service stops showing up in
# `systemctl --failed`. Harmless if there wasn't one.
$SUDO systemctl reset-failed "$SERVICE_NAME" >/dev/null 2>&1 || true

# --- What was deliberately left behind --------------------------------------

echo
echo "Done. The service is gone and the bot will not start on boot."
echo

# Built as a plain string rather than an array: an empty array under `set -u`
# is an error on bash 3.2, which macOS still ships, and this script should not
# fall over on the last line of a successful uninstall.
#
# The same runtime files .gitignore lists, including the numbered ones: a
# reminders.json.corrupt.* file holds reminders the bot couldn't read, and
# missed_notifications.log.1 the older missed-reminder titles. A pattern that
# matches nothing stays literal and fails the -e test.
KEPT=""
for path in \
    "$WORKDIR"/{conf.env,conf.env.bak,conf.env.tmp,token.txt,logs,venv} \
    "$WORKDIR"/{reminders.json,telegram_reminders.json}{,.tmp} \
    "$WORKDIR"/{reminders.json,telegram_reminders.json}.corrupt.* \
    "$WORKDIR"/missed_notifications.log{,.*}; do
    if [[ -e "$path" ]]; then
        KEPT+="    ${path##*/}"$'\n'
    fi
done

if [[ -n "$KEPT" ]]; then
    echo "Your data was left untouched, in $WORKDIR:"
    printf '%s' "$KEPT"
    echo
    echo "Delete those yourself if you're done with the bot — note that conf.env holds"
    echo "your bot token(s), and the reminders.json files hold your reminders."
    echo
fi

echo "You can still run the bot by hand at any time:"
# %q quotes the path, so the line still works when pasted if it has spaces.
printf '    cd %q && venv/bin/python reminder_bot.py\n' "$WORKDIR"
echo "To set the service back up:  ./install-service.sh"
