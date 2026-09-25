"""The Telegram front-end: commands, buttons and alert delivery.

Chat with the bot to create recurring reminders:
    /new
    -> "What should I remind you about?"  ->  Check Uni
    -> "How often?"                        ->  5 days
    ... 5 days later the bot messages you: "Check Uni - you have to check it!"
        with an [OK] button that restarts the 5-day timer.

By default anyone who finds the bot on Telegram can use it — they get their own
reminders in their own chat, not yours. Set TELEGRAM_ALLOWED_CHAT_IDS (a
comma-separated list of chat ids, in conf.env) to serve only those chats.
"""

import asyncio
import os

from telegram import ForceReply, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import (
    BadRequest,
    ChatMigrated,
    Conflict,
    Forbidden,
    InvalidToken,
    NetworkError,
    RetryAfter,
    TelegramError,
    TimedOut,
)
from telegram.ext import (
    ApplicationBuilder,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    TypeHandler,
    filters,
)
from telegram.helpers import escape_markdown

from . import config, core
from .core import MAX_TITLE_LEN, format_duration, log

PLATFORM = "telegram"

# Telegram rejects any message longer than 4096 characters, counted in UTF-16
# code units — so every emoji costs 2. These keep the single /list message
# comfortably inside that limit, so a long title can never make /list
# undeliverable and lock the user out of their own reminders. The list budget
# is measured with tg_len() rather than len(), or an emoji-heavy list would
# pass the check and still be refused.
MAX_LIST_ITEMS = 30
MAX_LIST_UNITS = 3800

# Chat ids allowed to use this bot, populated from TELEGRAM_ALLOWED_CHAT_IDS
# (or the older name ALLOWED_CHAT_IDS) at startup. Empty means "no
# restriction": anyone who finds the bot can create reminders in their chat.
ALLOWED_CHAT_IDS = set()

# Conversation states for the /new flow.
ASK_TITLE, ASK_INTERVAL = range(2)

# python-telegram-bot names the task that runs getUpdates like this. Watched
# so the bot stops when polling dies for good, instead of running on deaf.
POLLING_TASK_NAME = "Updater:start_polling:polling_task"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def tg_len(text):
    """Length of `text` the way Telegram measures it: in UTF-16 code units.

    Telegram's 4096-character message limit counts code units, not Python
    characters, so anything outside the BMP — every emoji — costs 2. Budgeting
    with len() lets an emoji-heavy /list pass the check and still be rejected.
    """
    return len(text.encode("utf-16-le")) // 2


def esc(text):
    """Escape user-supplied text for MarkdownV2."""
    return escape_markdown(str(text), version=2)


def alert_message(reminder):
    """The text and OK button of a due-reminder alert."""
    text = f"🔔 *{esc(reminder['title'])}*\n\nYou have to check it\\!"
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("✅ OK (restart timer)", callback_data=f"ok:{reminder['id']}")]]
    )
    return text, keyboard


def answer_prompt():
    """Reply markup for a /new question: opens a reply to the bot's message.

    In a group, a bot in privacy mode (Telegram's default) only receives
    commands and replies to its own messages, so a plain "Check Uni" typed
    into the group would never reach /new. selective=True limits the forced
    reply to the person running /new; the others in the group aren't prompted.
    """
    return ForceReply(selective=True)


def pending_titles(context):
    """The titles of unfinished /new flows, keyed by chat id.

    user_data follows a person across every chat they share with the bot, so a
    single "title" slot would let a /new in a group pick up the title typed in
    a private chat, and finishing one flow would wipe the other's.
    """
    return context.user_data.setdefault("pending_titles", {})


# ---------------------------------------------------------------------------
# Command / conversation handlers
# ---------------------------------------------------------------------------


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Hi! I'm your reminder bot.\n\n"
        "• /new – create a reminder\n"
        "• /list – see your reminders\n"
        "• /help – how it works\n\n"
        "When a reminder is due I'll message you with an OK button. "
        "Press it to restart that reminder's timer."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "*How I work*\n\n"
        "1\\. Send /new and I'll ask for a *title* and *how often*\\.\n"
        "2\\. When the time is up I send you the reminder with an *OK* button\\.\n"
        "3\\. Press *OK* and the timer restarts for the same interval\\.\n\n"
        "Interval examples: `5 days`, `30 minutes`, `2 hours`, `1 week`, "
        "`45 seconds`\\.\n\n"
        "Other commands:\n"
        "• /list – all reminders in one message, with Reset / Delete buttons "
        "per reminder and a Close list button\n"
        "• /cancel – stop creating a reminder",
        parse_mode=ParseMode.MARKDOWN_V2,
    )


async def new_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # /new also restarts an unfinished /new (allow_reentry), so drop its title.
    pending_titles(context).pop(update.effective_chat.id, None)
    await update.message.reply_text(
        "📝 What should I remind you about? (Send the title, or /cancel)",
        reply_markup=answer_prompt(),
    )
    return ASK_TITLE


async def new_title(update: Update, context: ContextTypes.DEFAULT_TYPE):
    title = update.message.text.strip()
    if not title:
        await update.message.reply_text(
            "That title is empty. Send me a few words to remind you about, "
            "or /cancel.",
            reply_markup=answer_prompt(),
        )
        return ASK_TITLE
    if len(title) > MAX_TITLE_LEN:
        await update.message.reply_text(
            f"That title is a bit long ({len(title)} characters). "
            f"Keep it under {MAX_TITLE_LEN} and send it again, or /cancel.",
            reply_markup=answer_prompt(),
        )
        return ASK_TITLE

    pending_titles(context)[update.effective_chat.id] = title
    await update.message.reply_text(
        "⏱ How often should I remind you?\n"
        "Examples: `5 days`, `30 minutes`, `2 hours`, `1 week`",
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=answer_prompt(),
    )
    return ASK_INTERVAL


async def new_interval(update: Update, context: ContextTypes.DEFAULT_TYPE):
    parsed = core.parse_interval(update.message.text)
    if parsed is None:
        await update.message.reply_text(
            "I didn't get that\\. Try something like `5 days` or `30 minutes` "
            f"\\(at most {core.MAX_INTERVAL_WEEKS} weeks\\)\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=answer_prompt(),
        )
        return ASK_INTERVAL  # stay in this state and ask again

    chat_id = update.effective_chat.id
    title = pending_titles(context).pop(chat_id, None)
    if title is None:
        await update.message.reply_text(
            "I've lost track of the title. Send /new to start again."
        )
        return ConversationHandler.END

    amount, unit_word, seconds = parsed
    reminder = await core.add_reminder(
        PLATFORM, chat_id, title, amount, unit_word, seconds,
    )

    await update.message.reply_text(
        f"✅ Done\\! I'll remind you about *{esc(reminder['title'])}* every "
        f"{amount} {esc(unit_word)} \\(next {esc(format_duration(seconds))}\\)\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
    )
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pending_titles(context).pop(update.effective_chat.id, None)
    await update.message.reply_text("Cancelled.")
    return ConversationHandler.END


async def build_reminder_list(chat_id):
    """Build the single /list message: (text, inline keyboard).

    All reminders go into one message, numbered. The keyboard has one
    Reset/Delete row per reminder (matched by number) and a Close row.

    Telegram caps a message at 4096 UTF-16 code units, so the list stops before
    it reaches that: an over-long list would be rejected outright, leaving the
    user unable to see or delete anything. Whatever doesn't fit is counted in
    a closing line rather than silently dropped. The budget is measured in the
    same units Telegram uses — see tg_len() — because a list of emoji titles
    can sit well under 3800 *characters* and still exceed the real limit.
    """
    close_row = [InlineKeyboardButton("✖ Close list", callback_data="close:0")]

    reminders = await core.reminders_for(PLATFORM, chat_id)  # closest to firing first
    if not reminders:
        return (
            "You have no reminders yet\\. Send /new to add one\\.",
            InlineKeyboardMarkup([close_row]),
        )

    lines = ["📋 *Your reminders*", ""]
    rows = []
    used = sum(tg_len(line) + 1 for line in lines)
    for i, r in enumerate(reminders, start=1):
        entry = (
            f"{i}\\. *{esc(r['title'])}* — "
            f"every {r['amount']} {esc(r['unit'])}, {esc(core.status_text(r))}"
        )
        too_many = i > MAX_LIST_ITEMS
        too_long = used + tg_len(entry) + 1 > MAX_LIST_UNITS
        if too_many or too_long:
            shown = i - 1
            lines.append("")
            lines.append(
                f"_…and {len(reminders) - shown} more\\. Delete a few to see "
                f"the rest\\._"
            )
            break
        lines.append(entry)
        used += tg_len(entry) + 1
        rows.append(
            [
                InlineKeyboardButton(f"🔄 Reset {i}", callback_data=f"reset:{r['id']}"),
                InlineKeyboardButton(f"🗑 Delete {i}", callback_data=f"del:{r['id']}"),
            ]
        )
    rows.append(close_row)
    return "\n".join(lines), InlineKeyboardMarkup(rows)


async def list_reminders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id

    # Replace the previously sent list so the chat only ever shows one.
    old_id = context.chat_data.pop("list_message_id", None)
    if old_id is not None:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=old_id)
        except TelegramError:
            pass  # already gone, or too old for Telegram to delete

    # In a forum group, answer in the topic /list was sent from, as reply_text()
    # does for every other command. Without a thread id the list lands in the
    # General topic, and fails outright if General is closed. Only a topic
    # message's thread id is usable: in an ordinary group it just names a
    # reply chain.
    message = update.effective_message
    thread_id = message.message_thread_id if message and message.is_topic_message else None

    text, keyboard = await build_reminder_list(chat_id)
    msg = await context.bot.send_message(
        chat_id, text, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=keyboard,
        message_thread_id=thread_id,
    )
    context.chat_data["list_message_id"] = msg.message_id


async def refresh_list_message(query, chat_id, context: ContextTypes.DEFAULT_TYPE):
    """Rewrite the pressed /list message in place with fresh data."""
    text, keyboard = await build_reminder_list(chat_id)
    try:
        await query.edit_message_text(
            text, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=keyboard
        )
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            raise
    # The edited message is now the current list (covers lists that survived
    # a bot restart, since the tracked id lives in memory).
    if query.message is not None:
        context.chat_data["list_message_id"] = query.message.message_id


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle inline button presses: OK / Reset / Delete / Close."""
    query = update.callback_query
    try:
        await query.answer()
    except TelegramError:
        # Answering only stops the button's loading spinner. A press made while
        # the bot was offline arrives too old to answer ("Query is too old"),
        # and letting that abort the handler would silently drop the press.
        pass
    action, _, id_str = query.data.partition(":")

    if action == "close":
        context.chat_data.pop("list_message_id", None)
        try:
            await query.delete_message()
        except TelegramError:
            # Too old for Telegram to delete: blank it out instead.
            await query.edit_message_text("List closed. Send /list to reopen.")
        return

    if action not in ("ok", "reset", "del"):
        return  # not a button we issued
    try:
        reminder_id = int(id_str)
    except ValueError:
        return

    chat_id = update.effective_chat.id
    reminder = await core.act_on_reminder(
        PLATFORM, chat_id, reminder_id, "del" if action == "del" else "reset",
    )

    if action == "ok":
        # OK on a due-reminder alert: restart the timer in place.
        if reminder is None:
            await query.edit_message_text("This reminder no longer exists.")
            return
        left = format_duration(reminder["interval_seconds"])
        await query.edit_message_text(
            f"✅ *{esc(reminder['title'])}* — timer restarted \\(next {esc(left)}\\)\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
    else:
        # Buttons inside the /list message: the change is already applied, so
        # update that same message and order and times stay current.
        await refresh_list_message(query, chat_id, context)


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


def describe_error(error: Exception) -> str:
    """Turn a Telegram exception into one short, human-readable line.

    Returns an explanation of what happened and whether you need to act,
    instead of the multi-page traceback the library produces by default, or
    "" for anything that isn't a Telegram error.
    """
    if isinstance(error, Conflict):
        return (
            "⚠️  Another copy of this bot is running with the same token "
            "(getUpdates conflict). Stop the other instance — only one can poll at a time."
        )
    if isinstance(error, InvalidToken):
        return (
            "❌ Telegram rejected the bot token. Check TELEGRAM_BOT_TOKEN in your "
            "conf.env file (get a fresh token from @BotFather if needed, or run "
            "setup_wizard.py again)."
        )
    if isinstance(error, Forbidden):
        return (
            "🚫 Telegram refused a message (Forbidden). Usually the user blocked the "
            "bot or never sent it /start."
        )
    if isinstance(error, RetryAfter):
        return (
            f"⏳ Telegram is rate-limiting us — asked to wait {error.retry_after}s. "
            "The library retries automatically; no action needed."
        )
    if isinstance(error, TimedOut):
        return (
            "🌐 Request to Telegram timed out. Temporary network slowness — "
            "the bot keeps retrying automatically; no action needed."
        )
    if isinstance(error, BadRequest):
        # Must be checked before NetworkError (BadRequest subclasses it).
        return (
            f"⚠️  Telegram rejected a request we sent (BadRequest: {error}). "
            "Often caused by unsupported characters/formatting in a message."
        )
    if isinstance(error, NetworkError):
        # Covers httpx.ReadError, ConnectError, etc. wrapped by the library:
        # the connection to Telegram dropped or the internet blipped.
        detail = str(error).strip().rstrip(":") or error.__class__.__name__
        return (
            f"🌐 Network hiccup while talking to Telegram ({detail}) - "
            "the bot reconnects automatically"
        )
    return ""  # not ours, or unknown -> caller should log the full traceback


core.ERROR_DESCRIBERS.append(describe_error)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    """Central error handler, registered with the application.

    Without this, python-telegram-bot prints 'No error handlers are
    registered' followed by a raw traceback for every hiccup.
    """
    friendly = describe_error(context.error)
    if friendly:
        log.warning(friendly)
    else:
        # Something we don't recognise: keep the full traceback (it's the only
        # way to debug it), but say clearly where it came from.
        log.error("Unexpected error while handling a Telegram update:", exc_info=context.error)

    # If a user's command triggered it, let them know instead of going silent —
    # a recognised error (say, a BadRequest from /list) as much as an unknown
    # one, as the Discord front-end does. Polling errors have no update.
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "😵 Sorry, something went wrong handling that. Please try again."
            )
        except TelegramError:
            # Any Telegram failure here — unreachable (NetworkError) or a
            # refusal such as Forbidden when the user has blocked the bot.
            # Letting it escape would raise inside the error handler itself
            # and produce the raw traceback this module exists to avoid.
            pass  # the log line above is already the record


# ---------------------------------------------------------------------------
# Access control
# ---------------------------------------------------------------------------


def load_allowed_chat_ids():
    """Read the optional Telegram chat allow-list from the environment.

    Left unset (the default), the bot answers anybody who finds it on Telegram.
    Reminders are partitioned by chat, so strangers can't see yours — but they
    can grow your data file and logs. Set TELEGRAM_ALLOWED_CHAT_IDS to a
    comma-separated list of chat ids to serve only those chats. The older
    name ALLOWED_CHAT_IDS is still read if the new one isn't set.
    """
    return config.id_list_from_env("TELEGRAM_ALLOWED_CHAT_IDS", "ALLOWED_CHAT_IDS")


async def restrict_to_allowed_chats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Drop updates from chats outside the allow-list, before any handler runs.

    Registered in group -1, so raising ApplicationHandlerStop here keeps the
    update from reaching the commands, the /new conversation, or the buttons.
    """
    if not ALLOWED_CHAT_IDS:
        return  # no allow-list configured: open to all, as documented
    chat = update.effective_chat
    if chat is None or chat.id not in ALLOWED_CHAT_IDS:
        log.warning(
            "Ignoring a Telegram update from chat %s — not in TELEGRAM_ALLOWED_CHAT_IDS.",
            chat.id if chat else "unknown",
        )
        raise ApplicationHandlerStop


# ---------------------------------------------------------------------------
# Front-end lifecycle
# ---------------------------------------------------------------------------


def get_token():
    """TELEGRAM_BOT_TOKEN from the environment/conf.env, else token.txt."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token and os.path.exists(config.TOKEN_FILE):
        with open(config.TOKEN_FILE, "r", encoding="utf-8") as f:
            token = f.read().strip()
    return token


def build_application(token):
    app = ApplicationBuilder().token(token).build()

    # New messages only. By default these handlers also match an *edited*
    # message, whose update has no .message, so every handler reading
    # update.message would crash — and an edited title would be taken as the
    # answer to the next question.
    new_messages = filters.UpdateType.MESSAGE
    answer = filters.TEXT & ~filters.COMMAND & new_messages

    conv = ConversationHandler(
        entry_points=[CommandHandler("new", new_start, filters=new_messages)],
        states={
            ASK_TITLE: [MessageHandler(answer, new_title)],
            ASK_INTERVAL: [MessageHandler(answer, new_interval)],
        },
        fallbacks=[CommandHandler("cancel", cancel, filters=new_messages)],
        # Without this, /new sent while an earlier /new is still waiting for
        # its answers matches no handler and gets no reply at all, for as long
        # as the bot runs. (conversation_timeout would need PTB's optional
        # JobQueue extra, so re-entry is the fix used here.)
        allow_reentry=True,
    )

    # Group -1 runs before everything else, so the allow-list is enforced once
    # rather than being repeated in every handler.
    app.add_handler(TypeHandler(Update, restrict_to_allowed_chats), group=-1)

    app.add_handler(CommandHandler("start", start, filters=new_messages))
    app.add_handler(CommandHandler("help", help_command, filters=new_messages))
    app.add_handler(CommandHandler("list", list_reminders, filters=new_messages))
    app.add_handler(conv)
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_error_handler(on_error)
    return app


class TelegramFrontend:
    """Runs the Telegram bot alongside the others; see core.run_frontends()."""

    platform = PLATFORM

    def __init__(self, token):
        ALLOWED_CHAT_IDS.clear()
        ALLOWED_CHAT_IDS.update(load_allowed_chat_ids())
        if ALLOWED_CHAT_IDS:
            log.info(
                "Telegram: serving %d allow-listed chat(s); all other chats are ignored.",
                config.id_count(ALLOWED_CHAT_IDS),
            )
        self.app = build_application(token)
        self._fatal = None
        self._closing = False

    async def start(self, fatal):
        self._fatal = fatal
        try:
            await self.app.initialize()  # checks the token with getMe
            await self.app.start()
            # Polling errors (a getUpdates Conflict, a dropped connection) go to
            # the same friendly error handler as everything else. Starting it
            # first clears any webhook, and the library doesn't retry that by
            # default, so a network blip right after getMe fails here too.
            await self.app.updater.start_polling(
                error_callback=lambda exc: self.app.create_task(
                    self.app.process_error(update=None, error=exc)
                ),
            )
        except TelegramError as e:
            # Undo whatever did start, then report it the way run_frontends()
            # expects, instead of ending the bot in a raw traceback.
            await self.stop()
            if isinstance(e, InvalidToken):
                raise core.FrontendStartError(describe_error(e)) from e
            raise core.FrontendStartError(
                f"Telegram failed to start: {describe_error(e) or e}"
            ) from e
        self._watch_polling()
        me = self.app.bot.username
        log.info("Telegram: connected as @%s.", me)

    def _watch_polling(self):
        """Stop the whole bot if polling ends while we're still meant to run.

        The library retries network trouble forever, but gives up for good on
        an InvalidToken (the token was revoked while running) or an unexpected
        exception. Nothing else would notice: the process would stay up with
        Telegram deaf, and systemd would never restart it. The Discord
        front-end does the same through fatal().
        """
        task = next(
            (t for t in asyncio.all_tasks() if t.get_name() == POLLING_TASK_NAME),
            None,
        )
        if task is None:
            log.warning(
                "Telegram: couldn't find the polling task to watch, so the bot "
                "won't stop by itself if polling dies (library version change?)."
            )
            return
        task.add_done_callback(self._on_polling_done)

    def _on_polling_done(self, task):
        if self._closing or self._fatal is None:
            return
        if task.cancelled():
            reason = "the polling task was cancelled"
        elif task.exception() is not None:
            error = task.exception()
            reason = describe_error(error) or repr(error)
        else:
            reason = "polling ended unexpectedly"
        self._fatal(f"Telegram polling stopped: {reason}")

    async def stop(self):
        self._closing = True
        if self.app.updater and self.app.updater.running:
            await self.app.updater.stop()
        if self.app.running:
            await self.app.stop()
        await self.app.shutdown()

    def accepts(self, reminder):
        """Whether the allow-list still lets this reminder's chat use the bot."""
        return not ALLOWED_CHAT_IDS or reminder["chat_id"] in ALLOWED_CHAT_IDS

    async def send_alert(self, reminder):
        text, keyboard = alert_message(reminder)
        try:
            await self.app.bot.send_message(
                chat_id=reminder["chat_id"],
                text=text,
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=keyboard,
            )
        except ChatMigrated as e:
            # The group became a supergroup under a new id. Must come before
            # the TelegramError catch below, which would retry the dead id forever.
            await self._follow_migration(reminder["chat_id"], e.new_chat_id)
            raise core.DeliveryRetry(
                e, f"the group moved to chat {e.new_chat_id}; retrying there",
            ) from e
        except (BadRequest, Forbidden) as e:
            # Telegram refused the message itself — a malformed request, or
            # the user blocked the bot. Retrying would loop forever.
            raise core.DeliveryRejected(e) from e
        except (TimedOut, RetryAfter, NetworkError) as e:
            # Couldn't reach Telegram: retry on the next tick.
            raise core.DeliveryRetry(e) from e
        except TelegramError as e:
            # Anything else the library raises (Conflict, InvalidToken): not
            # this reminder's fault, so leave it due and retry. The cause is
            # spelled out rather than reusing the outage wording, which would
            # send the operator hunting a network fault that isn't there.
            raise core.DeliveryRetry(e, "the Telegram API call failed") from e

    async def _follow_migration(self, old_chat_id, new_chat_id):
        """Move a migrated group's reminders to its new id; the next tick delivers."""
        moved = await core.move_chat(PLATFORM, old_chat_id, new_chat_id)
        log.warning(
            "Telegram group %s became supergroup %s; moved its %d reminder(s) there.",
            old_chat_id, new_chat_id, moved,
        )
        if (
            ALLOWED_CHAT_IDS
            and old_chat_id in ALLOWED_CHAT_IDS
            and new_chat_id not in ALLOWED_CHAT_IDS
        ):
            log.warning(
                "Add %s to TELEGRAM_ALLOWED_CHAT_IDS in conf.env and restart: the "
                "group's new id isn't allowed, so its reminders are held until it is.",
                new_chat_id,
            )
