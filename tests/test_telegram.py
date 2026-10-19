"""Tests for the Telegram front-end.

Nothing here talks to Telegram: the handlers are driven with small stand-in
objects that record what the bot tried to send, so the message formatting and
the /list budget can be checked without a token or a network.
"""

import asyncio
import datetime
import logging
import os
from types import SimpleNamespace

import pytest

pytest.importorskip("telegram")

from telegram import (  # noqa: E402
    Chat, ForceReply, Message, MessageEntity, Update, User,
)
from telegram.constants import ParseMode  # noqa: E402
from telegram.error import (  # noqa: E402
    BadRequest, ChatMigrated, Conflict, Forbidden, InvalidToken, NetworkError,
)
from telegram.ext import (  # noqa: E402
    CallbackQueryHandler, ConversationHandler, TypeHandler,
)

from helpers import make_reminder, read_store, write_store  # noqa: E402
from reminderbot import config, core  # noqa: E402
from reminderbot import telegram_bot as tg  # noqa: E402


# ---------------------------------------------------------------------------
# Stand-ins
# ---------------------------------------------------------------------------


class FakeMessage:
    """Stands in for a Telegram message; records what the bot replies."""

    def __init__(self):
        self.text = ""
        self.sent = []
        self.is_topic_message = None
        self.message_thread_id = None

    async def reply_text(self, text, **kwargs):
        self.sent.append((text, kwargs))
        return SimpleNamespace(message_id=len(self.sent))


class FakeBot:
    def __init__(self, error=None):
        self.sent = []
        self.deleted = []
        self.error = error

    async def send_message(self, chat_id, text=None, **kwargs):
        if self.error is not None:
            raise self.error
        self.sent.append((text, kwargs))
        return SimpleNamespace(message_id=len(self.sent))

    async def delete_message(self, chat_id, message_id):
        self.deleted.append(message_id)


def make_context():
    return SimpleNamespace(user_data={}, chat_data={}, bot=FakeBot())


def make_update(text="", chat_id=42):
    message = FakeMessage()
    message.text = text
    return SimpleNamespace(
        message=message,
        effective_chat=SimpleNamespace(id=chat_id),
        effective_message=message,
    )


# ---------------------------------------------------------------------------
# MarkdownV2 validation
# ---------------------------------------------------------------------------

# Characters Telegram requires to be backslash-escaped in ordinary MarkdownV2
# text. '*', '_' and '`' are excluded because they are also the markup we emit
# on purpose; those are checked for balance instead.
MUST_ESCAPE = set(r"[]()~>#+-=|{}.!")


def assert_valid_markdown_v2(text):
    """Fail if `text` would be rejected by Telegram's MarkdownV2 parser.

    Walks the string tracking backslash escapes and code spans: any special
    character left bare outside a code span is an error, and the emphasis
    delimiters must pair up.
    """
    in_code = False
    stars = underscores = 0
    i = 0
    while i < len(text):
        char = text[i]
        if char == "\\":
            i += 2  # escaped: the next character is literal
            continue
        if char == "`":
            in_code = not in_code
        elif not in_code:
            assert char not in MUST_ESCAPE, (
                f"unescaped {char!r} at index {i} of {text!r}"
            )
            if char == "*":
                stars += 1
            elif char == "_":
                underscores += 1
        i += 1
    assert not in_code, f"unclosed code span in {text!r}"
    assert stars % 2 == 0, f"unbalanced '*' in {text!r}"
    assert underscores % 2 == 0, f"unbalanced '_' in {text!r}"


def assert_sendable(text, kwargs):
    """Every MarkdownV2 message must parse, and fit inside Telegram's limit."""
    if kwargs.get("parse_mode") == ParseMode.MARKDOWN_V2:
        assert_valid_markdown_v2(text)
    assert tg.tg_len(text) <= 4096, f"message is {tg.tg_len(text)} units long"


def test_tg_len_counts_utf16_code_units():
    assert tg.tg_len("abc") == 3
    assert tg.tg_len("🔔") == 2  # non-BMP: two code units, one Python char
    assert tg.tg_len("🔔" * 10) == 20


# ---------------------------------------------------------------------------
# /list rendering and budget
# ---------------------------------------------------------------------------


def test_list_is_empty_and_valid_with_no_reminders():
    text, keyboard = asyncio.run(tg.build_reminder_list(42))
    assert_valid_markdown_v2(text)
    assert len(keyboard.inline_keyboard) == 1  # just the Close row


def test_list_only_shows_the_calling_chat_on_telegram():
    write_store([
        make_reminder(id=1, chat_id=42),
        make_reminder(id=2, chat_id=99),
        make_reminder(id=3, chat_id=42, platform="discord"),
    ])
    text, keyboard = asyncio.run(tg.build_reminder_list(42))
    assert text.count("Check Uni") == 1
    assert len(keyboard.inline_keyboard) == 2  # one reminder row + Close


def test_list_escapes_titles_that_would_break_markdown():
    write_store([make_reminder(title="a_b *c* [d](e) ~f~ #g! 1.2-3")])
    text, _ = asyncio.run(tg.build_reminder_list(42))
    assert_valid_markdown_v2(text)


def test_list_sorts_nearest_due_first():
    now = core.now()
    write_store([
        make_reminder(id=1, title="later", last_reset=now, interval_seconds=9000),
        make_reminder(id=2, title="sooner", last_reset=now, interval_seconds=10),
    ])
    text, _ = asyncio.run(tg.build_reminder_list(42))
    assert text.index("sooner") < text.index("later")


def test_list_caps_the_number_of_items():
    write_store([
        make_reminder(id=i, title=f"reminder {i}")
        for i in range(1, tg.MAX_LIST_ITEMS + 6)
    ])
    text, keyboard = asyncio.run(tg.build_reminder_list(42))
    assert "and 5 more" in text
    assert len(keyboard.inline_keyboard) == tg.MAX_LIST_ITEMS + 1


@pytest.mark.parametrize(
    "is_topic,thread_id,expected",
    [
        (True, 7, 7),        # a forum topic: answer in that topic
        (None, 7, None),     # a reply chain in an ordinary group: no topic
        (None, None, None),  # a private chat or plain group
    ],
)
def test_list_is_sent_to_the_topic_it_was_asked_in(is_topic, thread_id, expected):
    """F-031: without the thread id, /list in a forum topic landed in General."""
    write_store([make_reminder()])
    update, context = make_update("/list"), make_context()
    update.message.is_topic_message = is_topic
    update.message.message_thread_id = thread_id
    asyncio.run(tg.list_reminders(update, context))
    text, kwargs = context.bot.sent[0]
    assert kwargs["message_thread_id"] == expected
    assert_sendable(text, kwargs)


def test_list_of_emoji_titles_stays_within_telegrams_real_limit():
    """The regression this budget exists for: emoji cost two code units each.

    Counted with len() these titles look small enough to fit; counted the way
    Telegram counts they are not, and the message would be refused outright —
    leaving the user unable to see or delete their own reminders.
    """
    write_store([
        make_reminder(id=i, title="🔔" * core.MAX_TITLE_LEN)
        for i in range(1, tg.MAX_LIST_ITEMS + 1)
    ])
    text, _ = asyncio.run(tg.build_reminder_list(42))
    assert tg.tg_len(text) <= 4096
    assert "more" in text  # it did have to truncate
    assert_valid_markdown_v2(text)


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


def test_help_and_start_messages_are_sendable():
    for handler in (tg.start, tg.help_command):
        update, context = make_update(), make_context()
        asyncio.run(handler(update, context))
        text, kwargs = update.message.sent[0]
        assert_sendable(text, kwargs)


def test_new_flow_creates_a_reminder_and_replies_cleanly():
    context = make_context()

    title_update = make_update("Renew *passport* (soon!)")
    assert asyncio.run(tg.new_title(title_update, context)) == tg.ASK_INTERVAL
    assert_sendable(*title_update.message.sent[0])

    interval_update = make_update("5 days")
    asyncio.run(tg.new_interval(interval_update, context))
    assert_sendable(*interval_update.message.sent[0])

    stored = read_store()
    assert len(stored) == 1
    assert stored[0]["title"] == "Renew *passport* (soon!)"
    assert stored[0]["interval_seconds"] == 5 * 86400
    assert stored[0]["platform"] == "telegram"


def test_new_title_rejects_empty_and_overlong_titles():
    context = make_context()
    for text in ("   ", "x" * (core.MAX_TITLE_LEN + 1)):
        update = make_update(text)
        assert asyncio.run(tg.new_title(update, context)) == tg.ASK_TITLE
    assert not os.path.exists(core.DATA_FILE)


def test_new_interval_reprompts_on_junk():
    context = make_context()
    tg.pending_titles(context)[42] = "Check Uni"
    update = make_update("whenever")
    assert asyncio.run(tg.new_interval(update, context)) == tg.ASK_INTERVAL
    assert_sendable(*update.message.sent[0])


def test_new_interval_refuses_an_interval_too_long_to_store():
    """F-014: stored, this interval stopped every reminder from firing."""
    context = make_context()
    tg.pending_titles(context)[42] = "x"
    update = make_update("1" * 400 + " s")
    assert asyncio.run(tg.new_interval(update, context)) == tg.ASK_INTERVAL
    assert not os.path.exists(core.DATA_FILE)
    assert_sendable(*update.message.sent[0])


def test_every_new_prompt_forces_a_reply():
    """F-017: in a group, privacy mode only lets the bot see replies to its own
    messages, so each /new question must open a reply for the person asking."""
    def forced(update):
        markup = update.message.sent[-1][1].get("reply_markup")
        return isinstance(markup, ForceReply) and markup.selective is True

    context = make_context()
    update = make_update("/new")
    asyncio.run(tg.new_start(update, context))
    assert forced(update), "title question"
    for bad_title in ("   ", "x" * (core.MAX_TITLE_LEN + 1)):
        update = make_update(bad_title)
        asyncio.run(tg.new_title(update, context))
        assert forced(update), "title asked again"
    update = make_update("Check Uni")
    asyncio.run(tg.new_title(update, context))
    assert forced(update), "interval question"
    update = make_update("whenever")
    asyncio.run(tg.new_interval(update, context))
    assert forced(update), "interval asked again"


def test_new_restarts_an_unfinished_new():
    """F-018: without re-entry, /new during a pending /new got no reply at all."""
    app = tg.build_application("123456:" + "A" * 35)
    conv = next(h for h in app.handlers[0] if isinstance(h, ConversationHandler))
    assert conv.allow_reentry is True

    context = make_context()
    tg.pending_titles(context)[42] = "abandoned"
    asyncio.run(tg.new_start(make_update("/new"), context))
    assert 42 not in tg.pending_titles(context)


def test_edited_messages_reach_no_handler():
    """F-025: an edited message's update has no .message, so a handler that
    matched it crashed on update.message (and an edited title would have been
    read as the interval). Only new messages may reach the handlers."""
    app = tg.build_application("123456:" + "A" * 35)
    # CommandHandler.check_update reads the bot's username, normally set by getMe.
    app.bot._bot_user = User(1, "bot", True, username="my_bot")

    def update(text, edited):
        entities = [MessageEntity("bot_command", 0, len(text))] if text.startswith("/") else None
        message = Message(
            1, datetime.datetime.now(), Chat(42, "private"),
            from_user=User(7, "u", False), text=text, entities=entities,
        )
        message.set_bot(app.bot)
        return Update(1, edited_message=message) if edited else Update(1, message=message)

    conv = next(h for h in app.handlers[0] if isinstance(h, ConversationHandler))
    handlers = conv.entry_points + conv.fallbacks + [
        h for state in conv.states.values() for h in state
    ] + [
        h for h in app.handlers[0]
        if not isinstance(h, (ConversationHandler, CallbackQueryHandler))
    ]
    texts = ("/start", "/help", "/list", "/new", "/cancel", "Check Uni")
    for handler in handlers:
        for text in texts:
            assert not handler.check_update(update(text, edited=True)), (handler, text)

    # The same messages, unedited, are still handled.
    for text in texts:
        assert any(h.check_update(update(text, edited=False)) for h in handlers), text


def test_new_flows_in_two_chats_keep_their_own_titles():
    """user_data follows one person into every chat, so a pending title must be
    kept per chat: interleaving /new in a private chat and a group must neither
    swap the titles nor lose the second one."""
    user_data = {}
    private = SimpleNamespace(user_data=user_data, chat_data={})
    group = SimpleNamespace(user_data=user_data, chat_data={})

    asyncio.run(tg.new_title(make_update("private thing", chat_id=1), private))
    asyncio.run(tg.new_title(make_update("group thing", chat_id=-100), group))
    asyncio.run(tg.new_interval(make_update("5 days", chat_id=1), private))
    asyncio.run(tg.new_interval(make_update("2 hours", chat_id=-100), group))

    stored = {r["chat_id"]: r["title"] for r in read_store()}
    assert stored == {1: "private thing", -100: "group thing"}


def test_cancel_only_forgets_this_chats_pending_title():
    context = make_context()
    tg.pending_titles(context).update({42: "here", 7: "elsewhere"})
    asyncio.run(tg.cancel(make_update(chat_id=42), context))
    assert tg.pending_titles(context) == {7: "elsewhere"}


def test_due_reminder_notification_is_sendable():
    """Render the alert exactly as the front-end sends it."""
    text, _ = tg.alert_message(make_reminder(title="Pay the *rent* — flat #3 (2.5% late fee!)"))
    assert_valid_markdown_v2(text)


# ---------------------------------------------------------------------------
# Buttons
# ---------------------------------------------------------------------------


class FakeQuery:
    """Stands in for a callback query; records the edits the bot makes."""

    def __init__(self, data, answer_error=None, message_id=5):
        self.data = data
        self.answer_error = answer_error
        self.message = SimpleNamespace(message_id=message_id)
        self.edits = []

    async def answer(self):
        if self.answer_error is not None:
            raise self.answer_error

    async def edit_message_text(self, text, **kwargs):
        self.edits.append((text, kwargs))

    async def delete_message(self):
        pass


def press(data, chat_id=42, answer_error=None):
    query = FakeQuery(data, answer_error)
    update = SimpleNamespace(callback_query=query, effective_chat=SimpleNamespace(id=chat_id))
    asyncio.run(tg.on_button(update, make_context()))
    return query


def test_ok_restarts_the_timer_with_a_sendable_reply():
    write_store([make_reminder(id=1, title="Pay the *rent* (2.5%!)", last_reset=0.0, alerting=True)])
    query = press("ok:1")
    stored = read_store()[0]
    assert stored["alerting"] is False and stored["last_reset"] > 0
    text, kwargs = query.edits[0]
    assert "timer restarted" in text
    assert kwargs["parse_mode"] == ParseMode.MARKDOWN_V2
    assert_sendable(text, kwargs)


def test_a_press_too_old_to_answer_still_counts():
    """Pressed while the bot was offline: Telegram refuses the late answer,
    but the press itself must not be dropped."""
    write_store([make_reminder(id=1, last_reset=0.0, alerting=True)])
    query = press("ok:1", answer_error=BadRequest("Query is too old"))
    assert read_store()[0]["alerting"] is False
    assert "timer restarted" in query.edits[0][0]


def test_list_buttons_refresh_the_list_with_a_sendable_message():
    write_store([make_reminder(id=1), make_reminder(id=2, title="other")])
    query = press("del:1")
    assert [r["id"] for r in read_store()] == [2]
    assert_sendable(*query.edits[0])


# ---------------------------------------------------------------------------
# Alert delivery maps Telegram errors onto retry / give up
# ---------------------------------------------------------------------------


def make_frontend(error=None):
    frontend = object.__new__(tg.TelegramFrontend)
    frontend.app = SimpleNamespace(bot=FakeBot(error))
    return frontend


def test_send_alert_delivers():
    frontend = make_frontend()
    asyncio.run(frontend.send_alert(make_reminder()))
    assert len(frontend.app.bot.sent) == 1


@pytest.mark.parametrize(
    "error,expected",
    [
        (BadRequest("can't parse"), core.DeliveryRejected),
        (Forbidden("blocked"), core.DeliveryRejected),
        (NetworkError("down"), core.DeliveryRetry),
        (Conflict("two bots"), core.DeliveryRetry),
    ],
)
def test_send_alert_classifies_errors(error, expected):
    with pytest.raises(expected):
        asyncio.run(make_frontend(error).send_alert(make_reminder()))


def test_a_group_that_became_a_supergroup_gets_its_reminders_there(monkeypatch):
    """F-015: the old id refuses messages for good, so retrying it (the old
    behaviour) looped forever, and /list in the new supergroup showed nothing."""
    write_store([
        make_reminder(id=1, chat_id=-4711, last_reset=0.0),
        make_reminder(id=2, chat_id=42),
    ])

    class MigratingBot:
        sent_to = []

        async def send_message(self, chat_id, **kwargs):
            if chat_id == -4711:
                raise ChatMigrated(-100123)
            self.sent_to.append(chat_id)

    frontend = object.__new__(tg.TelegramFrontend)
    frontend.app = SimpleNamespace(bot=MigratingBot())

    class _StopLoop(BaseException):
        pass

    ticks = []

    async def two_ticks(_seconds):
        ticks.append(1)
        if len(ticks) == 2:
            raise _StopLoop

    monkeypatch.setattr(core.asyncio, "sleep", two_ticks)
    with pytest.raises(_StopLoop):
        asyncio.run(core.check_reminders({"telegram": frontend}))

    assert MigratingBot.sent_to == [-100123], "delivered in the supergroup on the next tick"
    stored = {r["id"]: r for r in read_store()}
    assert stored[1]["chat_id"] == -100123 and stored[1]["alerting"] is True
    assert stored[2]["chat_id"] == 42
    assert [r["id"] for r in asyncio.run(core.reminders_for("telegram", -100123))] == [1]
    with open(core.MISSED_LOG_FILE, encoding="utf-8") as f:
        assert "moved to chat -100123" in f.read()


def migration_message(chat_id, to=None, came_from=None):
    return SimpleNamespace(
        chat_id=chat_id, migrate_to_chat_id=to, migrate_from_chat_id=came_from,
    )


@pytest.mark.parametrize(
    "message",
    [
        migration_message(-4711, to=-100123),          # in the old group
        migration_message(-100123, came_from=-4711),   # in the new supergroup
    ],
)
def test_a_reminder_waiting_for_ok_follows_its_group_to_the_supergroup(message):
    """F-065: reminders moved only when an alert to the old id failed, and an
    alert already sent is never sent again, so a reminder waiting for OK was
    left under the dead id: missing from the supergroup's /list, its buttons
    there finding nothing, and never firing again. Either status message
    alone must move it."""
    write_store([
        make_reminder(id=1, chat_id=-4711, last_reset=0.0, alerting=True),
        make_reminder(id=2, chat_id=42),
    ])
    update = SimpleNamespace(effective_message=message)
    asyncio.run(tg.on_migration(update, make_context()))

    assert [r["id"] for r in asyncio.run(core.reminders_for("telegram", -100123))] == [1]
    assert {r["id"]: r["chat_id"] for r in read_store()} == {1: -100123, 2: 42}
    # The other status message, or a later ChatMigrated, finds nothing to move.
    asyncio.run(tg.follow_migration(-4711, -100123))
    assert {r["id"]: r["chat_id"] for r in read_store()} == {1: -100123, 2: 42}
    query = press("ok:1", chat_id=-100123)
    assert "timer restarted" in query.edits[0][0]
    assert read_store()[0]["alerting"] is False


def test_migration_status_messages_reach_the_handler():
    app = tg.build_application("123456:" + "A" * 35)  # builds offline
    [handler] = [
        h for h in app.handlers[0] if getattr(h, "callback", None) is tg.on_migration
    ]
    old_group = Message(
        1, datetime.datetime.now(), Chat(-4711, "group"), migrate_to_chat_id=-100123,
    )
    plain = Message(
        2, datetime.datetime.now(), Chat(-4711, "group"),
        from_user=User(7, "u", False), text="hello",
    )
    assert handler.check_update(Update(1, message=old_group))
    assert not handler.check_update(Update(2, message=plain))


def test_alerts_are_held_for_chats_outside_the_allow_list(monkeypatch):
    """F-019: an allow-listed bot must not keep posting to excluded chats."""
    frontend = make_frontend()
    monkeypatch.setattr(tg, "ALLOWED_CHAT_IDS", {42})
    assert frontend.accepts(make_reminder(chat_id=42)) is True
    assert frontend.accepts(make_reminder(chat_id=99)) is False
    monkeypatch.setattr(tg, "ALLOWED_CHAT_IDS", set())
    assert frontend.accepts(make_reminder(chat_id=99)) is True


def test_describe_error_is_registered_with_the_core():
    assert "Telegram" in core.describe_error(NetworkError("x"))


@pytest.mark.parametrize("error", [BadRequest("Topic_closed"), RuntimeError("boom")])
def test_the_user_hears_about_a_failed_command(monkeypatch, error):
    """F-037: a recognised error (BadRequest) used to leave the user in silence,
    while Discord apologised for the same failure."""
    replies = []

    async def reply_text(self, text, **kwargs):
        replies.append(text)

    monkeypatch.setattr(Message, "reply_text", reply_text)
    message = Message(
        1, datetime.datetime.now(), Chat(42, "supergroup"),
        from_user=User(7, "u", False), text="/list",
    )
    asyncio.run(tg.on_error(Update(1, message=message), SimpleNamespace(error=error)))
    assert len(replies) == 1 and "went wrong" in replies[0]


def test_a_polling_error_replies_to_nobody():
    asyncio.run(tg.on_error(None, SimpleNamespace(error=NetworkError("down"))))


def test_token_txt_works_with_the_example_conf_env(monkeypatch, tmp_path):
    """F-020: the README says token.txt works next to a conf.env made from the
    example; the example's placeholder token used to win over it."""
    example = os.path.join(os.path.dirname(config.CONF_FILE), "conf.env.example")
    for key, value in config.read_conf_file(example).items():
        monkeypatch.setenv(key, value)
    token_file = tmp_path / "token.txt"
    token_file.write_text("123456789:" + "B" * 35 + "\n", encoding="utf-8")
    monkeypatch.setattr(config, "TOKEN_FILE", str(token_file))
    assert tg.get_token() == "123456789:" + "B" * 35


# ---------------------------------------------------------------------------
# Access control
# ---------------------------------------------------------------------------


def test_allowed_chat_ids_parses_a_list(monkeypatch):
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "42, -100123 456 nonsense")
    assert tg.load_allowed_chat_ids() == {42, -100123, 456}


def test_allowed_chat_ids_still_reads_the_old_name(monkeypatch):
    monkeypatch.setenv("ALLOWED_CHAT_IDS", "7")
    assert tg.load_allowed_chat_ids() == {7}


def test_allowed_chat_ids_defaults_to_open():
    assert tg.load_allowed_chat_ids() == set()


def test_guard_is_a_no_op_when_no_allow_list_is_set(monkeypatch):
    monkeypatch.setattr(tg, "ALLOWED_CHAT_IDS", set())
    asyncio.run(tg.restrict_to_allowed_chats(make_update(chat_id=7), make_context()))


def test_guard_stops_updates_from_other_chats(monkeypatch):
    monkeypatch.setattr(tg, "ALLOWED_CHAT_IDS", {42})
    asyncio.run(tg.restrict_to_allowed_chats(make_update(chat_id=42), make_context()))
    with pytest.raises(tg.ApplicationHandlerStop):
        asyncio.run(
            tg.restrict_to_allowed_chats(make_update(chat_id=99), make_context())
        )


def test_an_allow_list_with_no_valid_id_blocks_every_chat(monkeypatch):
    """F-033: "@me" used to leave the list empty, which means open to all."""
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "@me")
    monkeypatch.setattr(tg, "ALLOWED_CHAT_IDS", tg.load_allowed_chat_ids())
    with pytest.raises(tg.ApplicationHandlerStop):
        asyncio.run(tg.restrict_to_allowed_chats(make_update(chat_id=99), make_context()))
    assert make_frontend().accepts(make_reminder(chat_id=99)) is False


def test_log_lines_name_the_allow_list_setting_in_use(monkeypatch):
    """F-063: with only the older ALLOWED_CHAT_IDS set, the log said to edit
    TELEGRAM_ALLOWED_CHAT_IDS; doing that made the new name win and locked out
    every chat the old list allowed."""
    monkeypatch.setenv("ALLOWED_CHAT_IDS", "1")
    monkeypatch.setattr(tg, "ALLOWED_CHAT_IDS", set())
    monkeypatch.setattr(tg, "ALLOWED_CHAT_IDS_SETTING", tg.ALLOWED_CHAT_IDS_SETTING)
    frontend = tg.TelegramFrontend("123456:" + "A" * 35)  # builds offline
    messages = []

    class Capture(logging.Handler):
        def emit(self, record):
            messages.append(record.getMessage())

    handler = Capture()
    tg.log.addHandler(handler)
    try:
        with pytest.raises(tg.ApplicationHandlerStop):
            asyncio.run(tg.restrict_to_allowed_chats(make_update(chat_id=2), make_context()))
        asyncio.run(tg.follow_migration(1, -100123))  # 1 is allowed, its new id isn't
    finally:
        tg.log.removeHandler(handler)

    named = [m for m in messages if "ALLOWED_CHAT_IDS" in m]
    assert len(named) == 2, messages
    assert not any("TELEGRAM_ALLOWED_CHAT_IDS" in m for m in named), named


def test_guard_runs_before_every_other_handler():
    """The guard only protects anything if it is registered ahead of the rest."""
    app = tg.build_application("123456:" + "A" * 35)  # builds offline
    first_group = min(app.handlers)
    assert first_group < 0
    assert min(g for g in app.handlers if g != first_group) >= 0
    guards = [
        h for h in app.handlers[first_group]
        if isinstance(h, TypeHandler) and h.callback is tg.restrict_to_allowed_chats
    ]
    assert guards and guards[0].type is Update


# ---------------------------------------------------------------------------
# Lifecycle: polling that dies must stop the bot
# ---------------------------------------------------------------------------


def polling_frontend():
    frontend = object.__new__(tg.TelegramFrontend)
    frontend.fatal_messages = []
    frontend._fatal = frontend.fatal_messages.append
    frontend._closing = False
    return frontend


def finished_task(error=None):
    async def body():
        if error is not None:
            raise error

    async def run_it():
        task = asyncio.create_task(body(), name=tg.POLLING_TASK_NAME)
        await asyncio.gather(task, return_exceptions=True)
        return task

    return asyncio.run(run_it())


def test_polling_that_dies_stops_the_bot():
    frontend = polling_frontend()
    frontend._on_polling_done(finished_task(InvalidToken("revoked")))
    assert len(frontend.fatal_messages) == 1
    assert "Telegram polling stopped" in frontend.fatal_messages[0]
    assert "token" in frontend.fatal_messages[0]


def test_polling_that_ends_during_shutdown_is_not_an_error():
    frontend = polling_frontend()
    frontend._closing = True
    frontend._on_polling_done(finished_task(InvalidToken("revoked")))
    assert frontend.fatal_messages == []


def test_the_polling_task_is_found_and_watched():
    frontend = polling_frontend()

    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(stop.wait(), name=tg.POLLING_TASK_NAME)
        await asyncio.sleep(0)
        frontend._watch_polling()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.sleep(0)  # let the done-callback run

    asyncio.run(scenario())
    assert frontend.fatal_messages == [
        "Telegram polling stopped: the polling task was cancelled"
    ]


def test_start_watches_polling_until_stop():
    """start() must arm the watch; stop() must disarm it before polling ends."""
    fatal_messages = []

    class FakeUpdater:
        running = False

        async def start_polling(self, error_callback=None):
            self.running = True
            self.task = asyncio.create_task(asyncio.Event().wait(), name=tg.POLLING_TASK_NAME)

        async def stop(self):
            self.running = False
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)

    async def noop():
        pass

    async def scenario(die):
        updater = FakeUpdater()
        frontend = object.__new__(tg.TelegramFrontend)
        frontend._fatal, frontend._closing = None, False
        frontend.app = SimpleNamespace(
            initialize=noop, start=noop, stop=noop, shutdown=noop, running=False,
            updater=updater, bot=SimpleNamespace(username="my_bot"),
        )
        await frontend.start(fatal_messages.append)
        if die:
            updater.task.cancel()
            await asyncio.gather(updater.task, return_exceptions=True)
            await asyncio.sleep(0)
        await frontend.stop()
        await asyncio.sleep(0)

    asyncio.run(scenario(die=False))
    assert fatal_messages == [], "a normal stop is not a failure"
    asyncio.run(scenario(die=True))
    assert len(fatal_messages) == 1


@pytest.mark.parametrize("step", ["initialize", "start", "start_polling"])
def test_a_start_failure_is_reported_and_undone(step):
    """F-036: a NetworkError after getMe (start_polling first clears any webhook)
    escaped run_frontends() as a raw traceback, with the app left running."""
    calls = []

    def stage(name):
        async def run(*args, **kwargs):
            calls.append(name)
            if name == step:
                raise NetworkError("httpx.ConnectError")
            if name == "start":
                app.running = True
            if name == "start_polling":
                updater.running = True
        return run

    updater = SimpleNamespace(running=False, start_polling=stage("start_polling"), stop=stage("updater.stop"))
    app = SimpleNamespace(
        initialize=stage("initialize"), start=stage("start"), stop=stage("stop"),
        shutdown=stage("shutdown"), running=False, updater=updater,
        bot=SimpleNamespace(username="my_bot"),
    )
    frontend = object.__new__(tg.TelegramFrontend)
    frontend._fatal, frontend._closing, frontend.app = None, False, app

    assert asyncio.run(core.run_frontends([frontend])) == 1
    assert calls[-1] == "shutdown"
    assert ("stop" in calls) == (step == "start_polling"), "a started app is stopped"
