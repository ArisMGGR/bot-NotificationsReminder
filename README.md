# Reminder Bot (Telegram & Discord)

Create reminders with a **title** and an **interval** (e.g. *"Check Uni"* every
*5 days*). When a reminder is due you get a notification telling you to check it,
with an **OK** button that restarts that reminder's timer so it fires again after
the same interval.

Notifications arrive through **Telegram**, **Discord**, or both — on your phone
and desktop alike. The bot can run anywhere (your PC, a Raspberry Pi, a server) —
you don't need to keep any window open.

> One project, a few names you'll see: the repository is `bot-NotificationsReminder`,
> the script is `reminder_bot.py`, and the systemd unit is `reminder-bot`. They
> all refer to this bot.

## One-time setup

You need Python 3.10 or newer. From the project folder, as your normal user
(not with `sudo`), run the setup wizard:

```bash
python3 setup_wizard.py
```

It asks for everything the bot needs, one question at a time:

1. **Platforms** — Telegram only, Discord only, or both.
2. **Bot token(s)** — checked online straight away, so a typo is caught now
   rather than when the bot fails to start.
   - *Telegram:* message **@BotFather**, send `/newbot`, and copy the token.
   - *Discord:* at <https://discord.com/developers/applications> create a
     **New Application → Bot → Reset Token** and copy it. The wizard then prints
     an invite link — open it to add the bot to your server. No privileged
     intents are needed.
3. **Who may use the bot** — optional allow-lists (see
   [below](#who-can-use-your-bot)). Press Enter for no limit.
4. **Saves `conf.env`**, readable only by you (mode `600`). An existing file is
   kept as `conf.env.bak`.
5. **Virtual environment** — offers to create `./venv` and install only the
   libraries your chosen platform(s) need.
6. **Background service** — offers to install the bot as a systemd service so it
   runs on its own and starts on boot (see [below](#run-it-in-the-background-systemd-service)).

Run the wizard again any time to change a setting: every question shows the
current value, and pressing Enter keeps it.

<details>
<summary>Setting it up by hand instead</summary>

```bash
cp conf.env.example conf.env
chmod 600 conf.env          # the token is a password — keep it to yourself
python3 -m venv venv
venv/bin/pip install -r requirements-telegram.txt   # and/or requirements-discord.txt
venv/bin/python reminder_bot.py
```

Edit `conf.env` first: set `BOT_PLATFORMS` (`telegram`, `discord` or
`telegram,discord`) and the token(s). A `token.txt` file with just the Telegram
token also works, as long as `TELEGRAM_BOT_TOKEN` in `conf.env` is left empty:
a token set there takes precedence.
</details>

To run the bot in a terminal (rather than as a service):

```bash
venv/bin/python reminder_bot.py
```

Then say hello: on Telegram, open your bot and send `/start`; on Discord, type
`/new` in a server with the bot or in a DM with it. Slash commands can take a
minute to appear the first time.

## Using it

### On Telegram

- `/start` – a short welcome and the list of commands.
- `/new` – the bot asks for a title, then how often (e.g. `5 days`, `30 minutes`,
  `2 hours`, `1 week`). Titles can be up to 120 characters, and intervals up
  to 520 weeks (about ten years).
- `/list` – see your reminders; each has **Reset** and **Delete** buttons, and
  the message has a **Close list** button. Very long lists are trimmed to fit in
  one message, with a note saying how many aren't shown.
- `/help` – a reminder of how it all works.
- `/cancel` – abandon a `/new` you started but don't want to finish.

### On Discord

- `/new` – opens a small form: **what to remind you about** and **how often**.
- `/list` – the channel's reminders, with a **Reset…** dropdown (pick one to
  restart its timer), a **Delete…** dropdown and a **Close list** button. Up to
  25 reminders are shown; a note says how many more there are.
- `/help` – a reminder of how it all works.

Reminders belong to the chat (Telegram) or channel/DM (Discord) they were made
in, and each platform keeps its own — a Telegram chat and a Discord channel
never see each other's reminders.

When a reminder is due, the bot posts it with an **✅ OK** button — press it to
restart the timer.

Keep the bot running for reminders to fire (leave the terminal open, or run it
as a service). Reminders are saved to `reminders.json` next to the script. (An
older `telegram_reminders.json` is moved there automatically on first start.)

## Who can use your bot

**By default, anyone who can reach your bot can use it.** Reminders are
partitioned by chat/channel, so strangers can't see or touch yours — but they
can create their own, which grows your data file and logs on your machine. The
wizard asks about these; you can also set them in `conf.env`.

Reminders already made in a chat or channel that a list now excludes are kept
but not sent (nobody there could press OK). They fire again if you allow that
chat back.

Ids are numbers. A list that is set but holds no valid id at all (a username,
say) blocks everyone rather than no one, and the log names the setting to fix.

### Telegram

Set `TELEGRAM_ALLOWED_CHAT_IDS` to a comma-separated list of chat ids; every
other chat is then ignored:

```
TELEGRAM_ALLOWED_CHAT_IDS=123456789
```

Not sure of your chat id? Set `TELEGRAM_ALLOWED_CHAT_IDS=0` in `conf.env`, start
the bot, and send it `/start`: it will refuse you and log the line

```
Ignoring a Telegram update from chat 123456789 — not in TELEGRAM_ALLOWED_CHAT_IDS.
```

That number is your chat id. Put it in `conf.env` and restart. Group chat ids are
negative — include the minus sign. (The older name `ALLOWED_CHAT_IDS` still works.)

### Discord

Three optional lists, each comma-separated:

```
# Servers
DISCORD_ALLOWED_GUILD_IDS=
# People
DISCORD_ALLOWED_USER_IDS=
# Channels (a DM is a channel too)
DISCORD_ALLOWED_CHANNEL_IDS=
```

Keep comments in `conf.env` on their own lines. A `#` after the `=` can end up
in the value: systemd, which reads the file for the service, never treats it
as a comment.

Every list you fill in must match, and empty lists are ignored. The server list
doesn't apply to DMs, which have no server — so `GUILD_IDS=A` plus `USER_IDS=X`
means "only X, and only in server A or in a DM with the bot". Anyone else gets a
private "This bot isn't enabled here" reply.

To copy an id, turn on **Developer Mode** (Discord Settings → Advanced), then
right-click a server, channel or user → **Copy ID**.

## Run it in the background (systemd service)

So you don't have to keep a terminal open, install the bot as a **systemd
service** on Ubuntu (or any systemd Linux). It then runs in the background,
starts automatically on boot, and restarts itself if it ever crashes.

The setup wizard offers to do this for you at the end. To do it yourself, from
the project folder run:

```bash
./install-service.sh
```

The script fills in your paths, tightens `conf.env` to mode `600` if it isn't
already, checks the libraries for your platforms are installed, installs the
service, (re)starts it and checks it's still running a few seconds later. It uses `./venv` if it finds one, otherwise your
system `python3`. Run it again after changing code; after changing `conf.env`
a restart is enough. If the old `telegram-reminder-bot` service from an earlier
version is installed, the script replaces it.

Once installed you can manage it with the usual `systemctl` commands (the
service is named `reminder-bot`):

```bash
sudo systemctl status reminder-bot     # is it running?
journalctl -u reminder-bot -f          # watch the live logs
sudo systemctl restart reminder-bot    # restart it (e.g. after editing conf.env)
sudo systemctl stop reminder-bot       # stop it (until next boot)
sudo systemctl disable --now reminder-bot   # stop and never start on boot
```

To remove the service again, run the uninstaller from the project folder:

```bash
./uninstall-service.sh
```

It stops the service, stops it starting on boot, and deletes the unit file. It
deliberately leaves your `conf.env`, `venv`, reminders and logs alone, and prints
where they are so you can delete them yourself if you're finished with the bot.
You can still run `venv/bin/python reminder_bot.py` by hand afterwards.

The unit runs sandboxed: no new privileges, a private `/tmp`, and a read-only
view of the filesystem apart from the project folder. If you move the data or
log files elsewhere, add their directory to `ReadWritePaths=` in the unit or the
bot won't be able to write them.

> Prefer to wire it up by hand? `reminder-bot.service` is the unit template;
> replace the `__USER__`, `__WORKDIR__` and `__PYTHON__` placeholders, copy it to
> `/etc/systemd/system/`, then run `sudo systemctl daemon-reload && sudo
> systemctl enable --now reminder-bot`. To undo that by hand: `sudo systemctl
> disable --now reminder-bot && sudo rm /etc/systemd/system/reminder-bot.service
> && sudo systemctl daemon-reload`.

## What happens if the internet (or Telegram/Discord) is down

When a reminder comes due you're notified **once** — the bot doesn't keep
nagging you until you press **OK**. Delivery is still resilient, though:

- If the send fails because the connection is down, the reminder isn't counted
  as delivered. The bot retries every few seconds and delivers it as soon as the
  connection is back — so the single notification is never lost, it just waits
  for the network.
- If the service instead *rejects* the message outright — on Telegram usually
  because you blocked the bot; on Discord because it lost permission to post in
  that channel, the channel was deleted, or your DMs are closed — retrying would
  never succeed, so the bot stops after one attempt and records the reason as
  `FAILED` rather than looping forever. Use **Reset** in `/list` to arm that
  reminder again once the cause is fixed.
- While the service is unreachable, each still-undelivered reminder is also
  written to a local fallback file, **`missed_notifications.log`**, next to the
  script. Because a network fallback (email/SMS) would be down too when the
  internet is off, this local record is the reliable place to see what you
  missed. Each line looks like:
  ```
  2026-07-21 14:03:07  MISSED  reminder #2 "Check Uni" (Telegram chat 12345) — could not reach Telegram: 🌐 Network hiccup ...
  ```

`RE_ALERT_INTERVAL` near the top of `reminderbot/core.py` (default: 5 minutes)
throttles how often that offline log is written during a long outage. The file
itself is capped at `MAX_MISSED_LOG_BYTES` (default: 5 MB); past that, the
current file becomes `missed_notifications.log.1` and a fresh one starts.

If both platforms are enabled and one of them can't start (a revoked token, for
example), the whole bot stops with an error rather than running half-broken; as
a service, systemd keeps restarting it until the problem is fixed, and the error
is in the logs.

## Daily log files

The bot keeps a plain-text log for each day in a **`logs/`** folder next to the
script. A new file is started automatically at midnight, named after the date:

```
logs/2026-08-08.log
logs/2026-08-09.log
```

Each day's file records:

- **Any errors** that happened that day (network hiccups, a message being
  rejected, unexpected problems) — the same friendly one-line messages you see
  in the terminal. When the same message repeats back-to-back, it is written
  once per heartbeat window rather than every time, so a long outage leaves a
  readable trail instead of thousands of identical lines — but it *is* written
  again in each new window, so it never disappears from later days' files.
  Only an unbroken run is collapsed: if two different problems alternate, each
  of their lines is written.
- An **`All good`** heartbeat written every 30 minutes when no error occurred in
  that window, so even a quiet, healthy day leaves a visible pulse in the file:
  ```
  2026-08-08 14:30:00  INFO     All good — no errors in the last 30 minutes.
  ```
  (If errors did occur in a window, they're already logged above and the
  heartbeat just notes how many.)

Ordinary startup/status chatter is kept out, so the daily file stays focused on
what went wrong and confirmation that things are healthy. `HEARTBEAT_INTERVAL`
near the top of `reminderbot/core.py` (default: 30 minutes) controls how often
the "All good" line is written, and `LOG_RETENTION_DAYS` (default: 30) controls
how long old daily files are kept — older ones are deleted at the next midnight
rollover. Set it to `0` to keep everything.

## Project layout

| Path | What it is |
| --- | --- |
| `setup_wizard.py` | The interactive setup (standard library only). |
| `reminder_bot.py` | Starts the bot on the platforms enabled in `conf.env`. |
| `reminderbot/core.py` | Storage, timers, logs and the loop that fires reminders — shared by both platforms. |
| `reminderbot/telegram_bot.py` | Telegram commands, buttons and delivery. |
| `reminderbot/discord_bot.py` | Discord slash commands, form, dropdowns and delivery. |
| `reminderbot/config.py` | Reads `conf.env`. |

## Development

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
python -m pytest          # the test suite — no token or network needed
python -m pyflakes reminder_bot.py setup_wizard.py reminderbot tests
bash -n install-service.sh uninstall-service.sh
```

The tests drive the handlers with small stand-in objects, so they cover the
message formatting (including that every Telegram MarkdownV2 message the bot
builds actually parses), the `/list` size budgets on both platforms, the Discord
allow-list rules, the data file's validation, the setup wizard's full question
flow, and the log de-duplication — without talking to Telegram or Discord. CI
runs the same checks on Python 3.10 and 3.13.

---

## License

[MIT](LICENSE) — do what you like with it, no warranty.
