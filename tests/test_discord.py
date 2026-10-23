"""Tests for the Discord front-end.

Nothing here talks to Discord: interactions are small stand-ins that record
what the bot answered, so the list layout, the /new form handling, the
buttons and the allow-lists can be checked without a token or a network.
"""

import asyncio
import logging
import socket
from types import SimpleNamespace

import pytest

discord = pytest.importorskip("discord")

from helpers import make_reminder, read_store, write_store  # noqa: E402
from reminderbot import core  # noqa: E402
from reminderbot import discord_bot as dc  # noqa: E402

CHANNEL = 555


@pytest.fixture(autouse=True)
def open_access(monkeypatch):
    monkeypatch.setattr(dc, "ACCESS", dc.DiscordAccess())


def dreminder(**overrides):
    overrides.setdefault("platform", "discord")
    overrides.setdefault("chat_id", CHANNEL)
    return make_reminder(**overrides)


# ---------------------------------------------------------------------------
# Stand-ins
# ---------------------------------------------------------------------------


class FakeResponse:
    def __init__(self):
        self.sent = []
        self.edits = []
        self.modals = []
        self.deferred = False

    async def send_message(self, content=None, **kwargs):
        self.sent.append((content, kwargs))

    async def edit_message(self, **kwargs):
        self.edits.append(kwargs)

    async def send_modal(self, modal):
        self.modals.append(modal)

    async def defer(self):
        self.deferred = True

    def is_done(self):
        return bool(self.sent or self.edits or self.deferred)


class FakePartialMessage:
    def __init__(self, client, message_id):
        self.client = client
        self.message_id = message_id

    async def delete(self):
        self.client.deleted.append(self.message_id)


class FakeChannel:
    def __init__(self, client, channel_id):
        self.client = client
        self.channel_id = channel_id

    def get_partial_message(self, message_id):
        return FakePartialMessage(self.client, message_id)

    async def send(self, content=None, **kwargs):
        if self.client.send_error is not None:
            raise self.client.send_error
        self.client.sent.append((self.channel_id, content, kwargs))


class FakeClient:
    def __init__(self, send_error=None, channel_guilds=None):
        self.list_messages = {}
        self.deleted = []
        self.sent = []
        self.send_error = send_error
        # channel id -> guild id, standing in for discord.py's channel cache
        self.channel_guilds = channel_guilds or {}

    def get_partial_messageable(self, channel_id):
        return FakeChannel(self, channel_id)

    def get_channel(self, channel_id):
        if channel_id not in self.channel_guilds:
            return None
        return SimpleNamespace(guild=SimpleNamespace(id=self.channel_guilds[channel_id]))


class FakeInteraction:
    def __init__(self, channel_id=CHANNEL, guild_id=1, user_id=10, message_id=None, client=None):
        self.channel_id = channel_id
        self.guild_id = guild_id
        self.user = SimpleNamespace(id=user_id)
        self.message = SimpleNamespace(id=message_id) if message_id else None
        self.client = client or FakeClient()
        self.response = FakeResponse()
        self.original_deleted = False
        self.original_edits = []

    async def original_response(self):
        return SimpleNamespace(id=9001)

    async def delete_original_response(self):
        self.original_deleted = True

    async def edit_original_response(self, **kwargs):
        self.original_edits.append(kwargs)


def run(coro):
    return asyncio.run(coro)


async def build_list(channel_id=CHANNEL):
    # discord.py views need a running event loop to be created.
    return await dc.build_reminder_list(channel_id)


# ---------------------------------------------------------------------------
# Access control
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "access,guild,channel,user,allowed",
    [
        (dc.DiscordAccess(), 1, 2, 3, True),                           # no lists
        (dc.DiscordAccess(users={3}), 1, 2, 3, True),
        (dc.DiscordAccess(users={4}), 1, 2, 3, False),
        (dc.DiscordAccess(guilds={1}), 1, 2, 3, True),
        (dc.DiscordAccess(guilds={9}), 1, 2, 3, False),
        (dc.DiscordAccess(guilds={9}), None, 2, 3, True),              # DM skips servers
        (dc.DiscordAccess(channels={2}), None, 2, 3, True),
        (dc.DiscordAccess(channels={8}), None, 2, 3, False),           # DM channel not listed
        (dc.DiscordAccess(guilds={1}, users={3}), 1, 2, 3, True),      # all must match
        (dc.DiscordAccess(guilds={1}, users={4}), 1, 2, 3, False),
        (dc.DiscordAccess(guilds={1}, channels={2}, users={3}), 1, 7, 3, False),
    ],
)
def test_access_rules(access, guild, channel, user, allowed):
    assert access.allows(guild, channel, user) is allowed


def test_access_from_env(monkeypatch):
    monkeypatch.setenv("DISCORD_ALLOWED_GUILD_IDS", "1,2")
    monkeypatch.setenv("DISCORD_ALLOWED_USER_IDS", "3")
    access = dc.DiscordAccess.from_env()
    assert access.guilds == {1, 2}
    assert access.users == {3}
    assert access.channels == set()
    assert access.is_restricted()


def test_an_allow_list_with_no_valid_id_refuses_everyone(monkeypatch):
    """F-033: a username in the list used to leave it empty: no limit at all."""
    monkeypatch.setenv("DISCORD_ALLOWED_USER_IDS", "aris#1234")
    access = dc.DiscordAccess.from_env()
    assert access.is_restricted()
    assert access.allows(1, 2, 999) is False
    assert access.allows(None, 2, None) is False, "not even an interaction without a user"


def test_guard_refuses_politely(monkeypatch):
    monkeypatch.setattr(dc, "ACCESS", dc.DiscordAccess(users={999}))
    interaction = FakeInteraction(user_id=10)
    assert run(dc.guard(interaction)) is False
    text, kwargs = interaction.response.sent[0]
    assert text == dc.NOT_ALLOWED_TEXT
    assert kwargs["ephemeral"] is True


def test_guard_lets_allowed_users_through(monkeypatch):
    monkeypatch.setattr(dc, "ACCESS", dc.DiscordAccess(users={10}))
    interaction = FakeInteraction(user_id=10)
    assert run(dc.guard(interaction)) is True
    assert interaction.response.sent == []


@pytest.mark.parametrize(
    "owner",
    [dc.GuardedTree, dc.OkButton, dc.ListSelect, dc.CloseButton, dc.NewReminderModal],
)
def test_every_entry_point_is_guarded(monkeypatch, owner):
    """Slash commands, buttons, dropdowns and the form must all apply the
    allow-lists — the rules above protect nothing unless they are wired in."""
    monkeypatch.setattr(dc, "ACCESS", dc.DiscordAccess(users={999}))
    interaction = FakeInteraction(user_id=10)
    assert run(owner.interaction_check(None, interaction)) is False
    assert interaction.response.sent[0][0] == dc.NOT_ALLOWED_TEXT


def test_the_client_uses_the_guarded_command_tree():
    async def build():
        client = dc.ReminderClient()
        try:
            return type(client.tree)
        finally:
            await client.close()

    assert run(build()) is dc.GuardedTree


# ---------------------------------------------------------------------------
# /list
# ---------------------------------------------------------------------------


def test_empty_list_has_only_a_close_button():
    text, view = run(build_list())
    assert "no reminders yet" in text
    assert [type(item) for item in view.children] == [dc.CloseButton]


def test_list_shows_only_this_channels_discord_reminders():
    write_store([
        dreminder(id=1, title="mine"),
        dreminder(id=2, title="other channel", chat_id=777),
        make_reminder(id=3, title="telegram one", chat_id=CHANNEL),
    ])
    text, view = run(build_list())
    assert "mine" in text
    assert "other channel" not in text
    assert "telegram one" not in text
    reset, delete, close = view.children
    assert isinstance(reset, dc.ListSelect) and reset.action == "reset"
    assert isinstance(delete, dc.ListSelect) and delete.action == "del"
    assert isinstance(close, dc.CloseButton)
    assert [o.value for o in reset.item.options] == ["1"]


def test_list_escapes_markdown_in_titles():
    write_store([dreminder(title="**bold** _it_ `code`")])
    text, _ = run(build_list())
    assert "\\*\\*bold\\*\\*" in text


def test_list_caps_items_at_the_dropdown_limit():
    write_store([dreminder(id=i, title=f"r{i}") for i in range(1, dc.MAX_LIST_ITEMS + 4)])
    text, view = run(build_list())
    assert "and 3 more" in text
    assert len(view.children[0].item.options) == dc.MAX_LIST_ITEMS


def test_list_of_long_titles_stays_under_discords_limit():
    write_store([
        dreminder(id=i, title="🔔" * core.MAX_TITLE_LEN) for i in range(1, dc.MAX_LIST_ITEMS + 1)
    ])
    text, view = run(build_list())
    assert len(text) <= 2000
    assert "more" in text
    for option in view.children[0].item.options:
        assert len(option.label) <= dc.MAX_OPTION_LEN


def test_list_command_replaces_the_previous_list():
    client = FakeClient()
    client.list_messages[CHANNEL] = 1234
    interaction = FakeInteraction(client=client)
    run(dc.handle_list(interaction))
    assert client.deleted == [1234]
    assert client.list_messages[CHANNEL] == 9001
    assert len(interaction.response.sent) == 1


def test_reset_from_the_list_restarts_the_timer_and_refreshes():
    write_store([dreminder(id=1, last_reset=0.0, alerting=True)])
    interaction = FakeInteraction(message_id=77)
    run(dc.handle_list_action(interaction, "reset", 1))
    stored = read_store()[0]
    assert stored["alerting"] is False and stored["last_reset"] > 0
    assert "Your reminders" in interaction.response.edits[0]["content"]
    assert interaction.client.list_messages[CHANNEL] == 77


def test_delete_from_the_list_removes_it():
    write_store([dreminder(id=1)])
    interaction = FakeInteraction(message_id=77)
    run(dc.handle_list_action(interaction, "del", 1))
    assert read_store() == []
    assert "no reminders yet" in interaction.response.edits[0]["content"]


def test_list_buttons_cannot_reach_another_channel():
    write_store([dreminder(id=1, chat_id=777)])
    run(dc.handle_list_action(FakeInteraction(message_id=77), "del", 1))
    assert len(read_store()) == 1


def test_close_deletes_the_list():
    client = FakeClient()
    client.list_messages[CHANNEL] = 5
    interaction = FakeInteraction(client=client)
    run(dc.handle_close(interaction))
    assert interaction.original_deleted is True
    assert CHANNEL not in client.list_messages


# ---------------------------------------------------------------------------
# /new form
# ---------------------------------------------------------------------------


def test_new_form_creates_a_discord_reminder_in_this_channel():
    interaction = FakeInteraction()
    run(dc.handle_new_submit(interaction, "  Water plants ", "3 days"))
    stored = read_store()
    assert stored[0]["platform"] == "discord"
    assert stored[0]["chat_id"] == CHANNEL
    assert stored[0]["title"] == "Water plants"
    assert stored[0]["interval_seconds"] == 3 * 86400
    text, kwargs = interaction.response.sent[0]
    assert "Water plants" in text
    assert not kwargs.get("ephemeral")


@pytest.mark.parametrize(
    "title,interval",
    [("", "5 days"), ("x" * (core.MAX_TITLE_LEN + 1), "5 days"), ("ok", "whenever"),
     ("ok", f"{core.MAX_INTERVAL_WEEKS + 1} weeks")],
)
def test_new_form_rejects_bad_input_privately(title, interval):
    interaction = FakeInteraction()
    run(dc.handle_new_submit(interaction, title, interval))
    _, kwargs = interaction.response.sent[0]
    assert kwargs["ephemeral"] is True
    assert core.load_reminders() == []


def test_a_rejected_interval_is_echoed_escaped():
    """F-055: the raw answer's backtick paired with the one before /new and
    swallowed the instructions into a code span."""
    interaction = FakeInteraction()
    answer = "`every **day**"
    run(dc.handle_new_submit(interaction, "ok", answer))
    text, _ = interaction.response.sent[0]
    assert dc.esc(answer) in text
    assert answer not in text


def test_new_command_opens_the_form():
    interaction = FakeInteraction()
    run(dc.new_command.callback(interaction))
    assert isinstance(interaction.response.modals[0], dc.NewReminderModal)


def test_help_fits_in_a_message():
    interaction = FakeInteraction()
    run(dc.handle_help(interaction))
    assert len(interaction.response.sent[0][0]) <= 2000


# ---------------------------------------------------------------------------
# Alerts and the OK button
# ---------------------------------------------------------------------------


def test_ok_restarts_the_timer_and_removes_the_button():
    write_store([dreminder(id=1, last_reset=0.0, alerting=True)])
    interaction = FakeInteraction()
    run(dc.handle_ok(interaction, 1))
    assert read_store()[0]["alerting"] is False
    edit = interaction.response.edits[0]
    assert "timer restarted" in edit["content"]
    assert edit["view"] is None


def test_ok_on_a_deleted_reminder_says_so():
    interaction = FakeInteraction()
    run(dc.handle_ok(interaction, 1))
    assert "no longer exists" in interaction.response.edits[0]["content"]


def test_custom_ids_round_trip_through_the_templates():
    """Buttons must still work after a restart: the id alone rebuilds them."""
    async def check():
        ok = dc.OkButton(42)
        match = dc.OkButton.__discord_ui_compiled_template__.fullmatch(ok.item.custom_id)
        rebuilt = await dc.OkButton.from_custom_id(None, None, match)
        assert rebuilt.reminder_id == 42

        select = dc.ListSelect("del", [("1. x", 3, "next in 1d")])
        match = dc.ListSelect.__discord_ui_compiled_template__.fullmatch(select.item.custom_id)
        assert (await dc.ListSelect.from_custom_id(None, None, match)).action == "del"

    run(check())


@pytest.mark.parametrize("handler", ["handle_ok", "handle_list_action", "handle_close"])
def test_a_failed_button_or_dropdown_tells_the_user(monkeypatch, handler):
    """F-041: a failure in a button or dropdown escaped to discord.py, which
    only logs it, so the user got "This interaction failed" and no apology."""
    async def fail(*args):
        raise http_error(discord.NotFound, 404)

    monkeypatch.setattr(dc, handler, fail)

    async def press():
        item = {
            "handle_ok": lambda: dc.OkButton(1),
            "handle_list_action": lambda: dc.ListSelect("reset", [("1. x", 1, "next in 1d")]),
            "handle_close": dc.CloseButton,
        }[handler]()
        item.item._values = ["1"]  # what a dropdown pick sets; ignored by buttons
        interaction = FakeInteraction()
        await item.callback(interaction)
        return interaction

    interaction = run(press())
    [(text, kwargs)] = interaction.response.sent
    assert "went wrong" in text
    assert kwargs["ephemeral"] is True


def make_frontend(send_error=None):
    frontend = object.__new__(dc.DiscordFrontend)
    frontend.client = FakeClient(send_error)
    return frontend


def test_send_alert_posts_in_the_reminders_channel_with_an_ok_button():
    frontend = make_frontend()
    run(frontend.send_alert(dreminder(id=4, title="Stretch")))
    channel_id, text, kwargs = frontend.client.sent[0]
    assert channel_id == CHANNEL
    assert "Stretch" in text
    assert isinstance(kwargs["view"].children[0], dc.OkButton)


def http_error(cls, status):
    return cls(SimpleNamespace(status=status, reason="x"), "nope")


@pytest.mark.parametrize(
    "error,expected",
    [
        (http_error(discord.Forbidden, 403), core.DeliveryRejected),
        (http_error(discord.NotFound, 404), core.DeliveryRejected),
        (http_error(discord.HTTPException, 400), core.DeliveryRejected),
        (http_error(discord.DiscordServerError, 503), core.DeliveryRetry),
        (http_error(discord.HTTPException, 429), core.DeliveryRetry),
        (http_error(discord.HTTPException, 401), core.DeliveryRetry),  # F-028: token reset
        (asyncio.TimeoutError(), core.DeliveryRetry),
        (ConnectionResetError(), core.DeliveryRetry),
    ],
)
def test_send_alert_classifies_errors(error, expected):
    with pytest.raises(expected):
        run(make_frontend(error).send_alert(dreminder()))


@pytest.mark.parametrize(
    "access,channel,allowed",
    [
        (dc.DiscordAccess(), 555, True),
        (dc.DiscordAccess(channels={555}), 555, True),
        (dc.DiscordAccess(channels={555}), 777, False),
        (dc.DiscordAccess(guilds={1}), 555, True),    # channel 555 is in server 1
        (dc.DiscordAccess(guilds={1}), 777, False),   # channel 777 is in server 2
        (dc.DiscordAccess(guilds={1}), 888, True),    # not cached (a DM): server list skipped
        (dc.DiscordAccess(users={3}), 777, True),     # an alert has no user to check
    ],
)
def test_alerts_are_held_for_channels_outside_the_allow_lists(monkeypatch, access, channel, allowed):
    """F-019: an allow-listed bot must not keep posting to excluded channels."""
    monkeypatch.setattr(dc, "ACCESS", access)
    frontend = object.__new__(dc.DiscordFrontend)
    frontend.client = FakeClient(channel_guilds={555: 1, 777: 2})
    assert frontend.accepts(dreminder(chat_id=channel)) is allowed


def test_describe_error_is_registered_with_the_core():
    assert "Discord" in core.describe_error(http_error(discord.Forbidden, 403))


@pytest.mark.parametrize(
    "code,expected,not_expected",
    [
        (10062, "interaction expired", "channel"),   # Unknown interaction
        (10008, "message doesn't exist", "channel"),  # Unknown message
        (10003, "channel doesn't exist", None),      # Unknown channel
    ],
)
def test_not_found_is_described_by_its_code(code, expected, not_expected):
    """F-049: every NotFound read as a deleted channel, so an interaction the
    bot answered too late was logged as a channel problem."""
    error = discord.NotFound(
        SimpleNamespace(status=404, reason="Not Found"), {"code": code, "message": "x"},
    )
    text = core.describe_error(error)
    assert expected in text
    if not_expected:
        assert not_expected not in text


def log_from_discord_client(error, message, *args):
    logger = logging.getLogger("discord.client")
    try:
        raise error
    except Exception:
        logger.exception(message, *args)


def test_reconnect_traceback_becomes_one_readable_warning(caplog):
    import aiohttp

    dns_failure = aiohttp.ClientConnectorError(
        SimpleNamespace(host="gateway.discord.gg", port=443, ssl=True),
        socket.gaierror(-3, "Temporary failure in name resolution"),
    )
    with caplog.at_level(logging.WARNING, logger="discord.client"):
        log_from_discord_client(dns_failure, "Attempting a reconnect in %.2fs", 3.16)
    [record] = caplog.records
    assert record.levelno == logging.WARNING
    assert record.exc_info is None
    assert "DNS lookup for gateway.discord.gg failed" in record.getMessage()
    assert "next attempt in 3.16s" in record.getMessage()


def test_reconnect_filter_leaves_other_discord_errors_alone(caplog):
    with caplog.at_level(logging.WARNING, logger="discord.client"):
        log_from_discord_client(ValueError("boom"), "Ignoring exception in on_ready")
    [record] = caplog.records
    assert record.levelno == logging.ERROR
    assert record.exc_info is not None
