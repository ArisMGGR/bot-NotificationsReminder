#!/usr/bin/env bash
#
# Remove the Reminder Bot's systemd service — the inverse of
# install-service.sh.
#
# Usage:
#   ./uninstall-service.sh
#
# Stops the service, stops it starting on boot, and deletes the unit file.
# The old telegram-reminder-bot service from earlier versions goes the same way.
#
# Your reminders, logs, conf.env and venv are NOT touched. install-service.sh only ever
# created the unit file, so that is all this removes; the data belongs to you
# and is listed at the end so you can delete it yourself if you want to.
#
# Needs sudo to remove the unit file, and will ask for your password.

set -euo pipefail

SERVICE_NAME="reminder-bot"

# The name the service had before the bot supported Discord. install-service.sh
# retires it, but a machine that never re-ran the installer may still have it,
# running with the same token.
LEGACY_SERVICE_NAME="telegram-reminder-bot"

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

# --- Work out what's actually there -----------------------------------------

unit_file() {
    echo "/etc/systemd/system/$1.service"
}

# systemctl exits non-zero for a unit that is inactive, disabled or unknown —
# all normal answers here. Under `set -e` an unguarded query would abort the
# script instead of telling us what we asked, so every one is guarded.
query() {
    systemctl "$1" "$2" 2>/dev/null || true
}

installed() {
    [[ -f "$(unit_file "$1")" \
        || "$(query is-active "$1")" == "active" \
        || "$(query is-enabled "$1")" == "enabled" ]]
}

# Space-separated; neither name contains a space.
FOUND=""
for name in "$SERVICE_NAME" "$LEGACY_SERVICE_NAME"; do
    if installed "$name"; then
        FOUND+="$name "
    fi
done

if [[ -z "$FOUND" ]]; then
    echo "Nothing to do: $SERVICE_NAME is not installed as a systemd service."
    echo "(No unit file at $(unit_file "$SERVICE_NAME"), and the service isn't running or enabled.)"
    exit 0
fi

# Removing a unit from /etc needs root. Checked only once there is something to
# remove, so "Nothing to do" above never turns into a demand for root. Skip
# sudo when we already are root, so this works without sudo installed.
if [[ "$(id -u)" -eq 0 ]]; then
    SUDO=""
elif command -v sudo >/dev/null 2>&1; then
    SUDO="sudo"
else
    echo "Error: need root to remove the service (${FOUND% }), and sudo isn't installed." >&2
    echo "Re-run this script as root." >&2
    exit 1
fi

# --- Remove it --------------------------------------------------------------

remove_service() {
    local name="$1" unit is_active is_enabled
    unit="$(unit_file "$name")"
    is_active="$(query is-active "$name")"
    is_enabled="$(query is-enabled "$name")"

    echo "Service   : $name"
    echo "Unit file : $unit$([[ -f "$unit" ]] || echo "  (missing)")"
    echo "State     : ${is_active:-unknown}, ${is_enabled:-not enabled}"

    if [[ "$is_active" == "active" ]]; then
        echo "Stopping the service ..."
        $SUDO systemctl stop "$name"
    else
        echo "The service isn't running."
    fi

    if [[ "$is_enabled" == "enabled" || "$is_enabled" == "enabled-runtime" ]]; then
        echo "Disabling it so it no longer starts on boot ..."
        $SUDO systemctl disable "$name" >/dev/null
    fi

    if [[ -f "$unit" ]]; then
        echo "Removing $unit ..."
        $SUDO rm -f "$unit"
    fi
    echo
}

for name in $FOUND; do
    remove_service "$name"
done

echo "Reloading systemd ..."
$SUDO systemctl daemon-reload
# Clears a lingering "failed" entry so a removed service stops showing up in
# `systemctl --failed`. Harmless if there wasn't one.
for name in $FOUND; do
    $SUDO systemctl reset-failed "$name" >/dev/null 2>&1 || true
done

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

# Name the Python install-service.sh would have picked, in its order: a service
# installed without ./venv ran on the system python3, and "venv/bin/python"
# would then be a command that doesn't exist.
if [[ -x "$WORKDIR/venv/bin/python" ]]; then
    RUN_PYTHON="venv/bin/python"
elif [[ -x "$WORKDIR/.venv/bin/python" ]]; then
    RUN_PYTHON=".venv/bin/python"
else
    RUN_PYTHON="python3"
fi
echo "You can still run the bot by hand at any time:"
# %q quotes the path, so the line still works when pasted if it has spaces.
printf '    cd %q && %s reminder_bot.py\n' "$WORKDIR" "$RUN_PYTHON"
echo "To set the service back up:  ./install-service.sh"
