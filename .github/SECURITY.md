# Security

## Reporting a vulnerability

Please report security issues privately, through GitHub's
[private vulnerability reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability)
on this repository, rather than opening a public issue. This is a hobby project
maintained in spare time, so please allow a little patience for a reply.

## Running it safely

- **Your bot tokens are passwords.** Anyone holding one controls your bot and
  can read every message sent to it. Keep it in `conf.env`, keep that file at mode
  `600`, and never commit it — `conf.env` and `token.txt` are both in `.gitignore`.
  `setup_wizard.py` writes it that way (and keeps the previous version as
  `conf.env.bak`, also mode `600` and gitignored).
  If a token leaks, revoke it — `/revoke` in @BotFather for Telegram, **Reset
  Token** in the Discord Developer Portal — and issue a new one; deleting the
  file it leaked from is not enough.
- **The bot is open to everyone by default.** Reminders are partitioned by
  chat/channel, so strangers cannot see or change yours, but they can create
  their own and grow your data file and logs. Set `TELEGRAM_ALLOWED_CHAT_IDS`
  and/or the `DISCORD_ALLOWED_*` lists to lock it down — see the README.
- **Your reminder titles are stored in the clear** in `reminders.json`
  and can appear in `missed_notifications.log`. Both are gitignored. Don't put
  secrets in a reminder title.
- **The systemd unit runs sandboxed** (`NoNewPrivileges`, `ProtectSystem=strict`,
  a private `/tmp`, and write access only to the project folder). If you change
  where data is stored, update `ReadWritePaths=` rather than removing the
  sandboxing.
