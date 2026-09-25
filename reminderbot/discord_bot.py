"""The Discord front-end: slash commands, buttons and alert delivery.

The same reminders as on Telegram, the Discord way:
    /new   opens a small form asking for the title and how often
    /list  shows the channel's reminders, with a "Reset…" and a "Delete…"
           dropdown and a Close button
    /help  explains the above
When a reminder is due the bot posts it in the channel (or DM) it was made
in, with an OK button that restarts its timer.

Reminders belong to the channel or DM they were created in, just as Telegram
reminders belong to a chat. Access can be narrowed with three allow-lists in
conf.env — DISCORD_ALLOWED_GUILD_IDS, DISCORD_ALLOWED_USER_IDS and
DISCORD_ALLOWED_CHANNEL_IDS. Every list that is set must allow the
interaction; the server list doesn't apply to DMs, which have no server.

Buttons and dropdowns are "dynamic items": their custom ids carry everything
needed to handle them, so they keep working after the bot restarts.
"""

import asyncio
import os
from dataclasses import dataclass, field

import aiohttp
import discord
from discord import app_commands

from . import config, core
from .core import MAX_TITLE_LEN, format_duration, log

PLATFORM = "discord"

# Discord messages hold at most 2000 characters, and a dropdown at most 25
# options. The /list message stays under both so it is never refused.
MAX_LIST_ITEMS = 25
MAX_LIST_CHARS = 1900

# Dropdown option labels and descriptions are capped at 100 characters.
MAX_OPTION_LEN = 100

NOT_ALLOWED_TEXT = "🚫 This bot isn't enabled here."


# ---------------------------------------------------------------------------
# Access control
# ---------------------------------------------------------------------------


@dataclass
class DiscordAccess:
    """The optional allow-lists. An empty list means "no restriction"."""

    guilds: set = field(default_factory=set)
    users: set = field(default_factory=set)
    channels: set = field(default_factory=set)

    @classmethod
    def from_env(cls):
        return cls(
            guilds=config.id_list_from_env("DISCORD_ALLOWED_GUILD_IDS"),
            users=config.id_list_from_env("DISCORD_ALLOWED_USER_IDS"),
            channels=config.id_list_from_env("DISCORD_ALLOWED_CHANNEL_IDS"),
        )

    def is_restricted(self):
        return bool(self.guilds or self.users or self.channels)

    def allows(self, guild_id, channel_id, user_id):
        """Every list that is set must allow it. DMs (no guild) skip the server list."""
        if self.users and user_id not in self.users:
            return False
        return self.allows_channel(guild_id, channel_id)

    def allows_channel(self, guild_id, channel_id):
        """The channel and server lists only.

        An alert is posted to a channel, not sent by a user, so this is all it
        can be checked against. guild_id None means a DM, or a channel not in
        the cache; either way the server list is skipped, as for a DM.
        """
        if self.channels and channel_id not in self.channels:
            return False
        if self.guilds and guild_id is not None and guild_id not in self.guilds:
            return False
        return True


# Populated from conf.env when the front-end starts.
ACCESS = DiscordAccess()


async def guard(interaction):
    """True if this interaction may proceed; otherwise say so and return False."""
    user_id = interaction.user.id if interaction.user else None
    if ACCESS.allows(interaction.guild_id, interaction.channel_id, user_id):
        return True
    log.warning(
        "Ignoring a Discord interaction from user %s in channel %s (server %s) "
        "— not allowed by the DISCORD_ALLOWED_* lists.",
        user_id, interaction.channel_id, interaction.guild_id or "none, a DM",
    )
    try:
        await interaction.response.send_message(NOT_ALLOWED_TEXT, ephemeral=True)
    except discord.HTTPException:
        pass
    return False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def esc(text):
    """Escape user-supplied text for Discord markdown."""
    return discord.utils.escape_markdown(str(text))


def clip(text, limit=MAX_OPTION_LEN):
    return text if len(text) <= limit else text[: limit - 1] + "…"


def alert_text(reminder):
    return f"🔔 **{esc(reminder['title'])}**\n\nYou have to check it!"


def alert_view(reminder_id):
    view = discord.ui.View(timeout=None)
    view.add_item(OkButton(reminder_id))
    return view


HELP_TEXT = (
    "**How I work**\n\n"
    "1. Use `/new` and fill in a **title** and **how often**.\n"
    "2. When the time is up I post the reminder here with an **OK** button.\n"
    "3. Press **OK** and the timer restarts for the same interval.\n\n"
    "Interval examples: `5 days`, `30 minutes`, `2 hours`, `1 week`, `45 seconds`.\n\n"
    "Other commands:\n"
    "• `/list` – this channel's reminders, with **Reset…** and **Delete…** "
    "dropdowns and a **Close** button\n\n"
    "Reminders belong to the channel (or DM) they were made in."
)


async def build_reminder_list(channel_id):
    """Build the /list message for one channel: (text, view).

    The text lists every reminder, numbered, closest to firing first. Below it
    are two dropdowns — pick a reminder in "Reset…" to restart its timer, or in
    "Delete…" to remove it — and a Close button.

    Discord caps a message at 2000 characters and a dropdown at 25 options, so
    the list stops before either; whatever doesn't fit is counted in a
    closing line rather than silently dropped.
    """
    view = discord.ui.View(timeout=None)
    reminders = await core.reminders_for(PLATFORM, channel_id)
    if not reminders:
        view.add_item(CloseButton())
        return "You have no reminders yet. Use `/new` to add one.", view

    lines = ["📋 **Your reminders**", ""]
    options = []
    used = sum(len(line) + 1 for line in lines)
    for i, r in enumerate(reminders, start=1):
        status = core.status_text(r)
        entry = f"{i}. **{esc(r['title'])}** — every {r['amount']} {esc(r['unit'])}, {status}"
        too_many = i > MAX_LIST_ITEMS
        too_long = used + len(entry) + 1 > MAX_LIST_CHARS
        if too_many or too_long:
            lines.append("")
            lines.append(f"_…and {len(reminders) - (i - 1)} more. Delete a few to see the rest._")
            break
        lines.append(entry)
        used += len(entry) + 1
        options.append((f"{i}. {r['title']}", r["id"], status))

    view.add_item(ListSelect("reset", options))
    view.add_item(ListSelect("del", options))
    view.add_item(CloseButton())
    return "\n".join(lines), view


# ---------------------------------------------------------------------------
# Interaction handlers (kept free of UI classes so they are easy to test)
# ---------------------------------------------------------------------------


async def handle_help(interaction):
    await interaction.response.send_message(HELP_TEXT)


async def handle_new_submit(interaction, title, interval_text):
    """Create a reminder from the /new form. Problems are shown only to the sender."""
    title = (title or "").strip()
    if not title:
        await interaction.response.send_message(
            "That title is empty. Run `/new` again with a few words to remind you about.",
            ephemeral=True,
        )
        return
    if len(title) > MAX_TITLE_LEN:
        await interaction.response.send_message(
            f"That title is a bit long ({len(title)} characters). Keep it under "
            f"{MAX_TITLE_LEN} and run `/new` again.",
            ephemeral=True,
        )
        return
    parsed = core.parse_interval(interval_text or "")
    if parsed is None:
        await interaction.response.send_message(
            f"I didn't get “{clip(interval_text or '', 40)}” as an interval. Run `/new` "
            "again and try something like `5 days` or `30 minutes` "
            f"(at most {core.MAX_INTERVAL_WEEKS} weeks).",
            ephemeral=True,
        )
        return

    amount, unit_word, seconds = parsed
    reminder = await core.add_reminder(
        PLATFORM, interaction.channel_id, title, amount, unit_word, seconds,
    )
    await interaction.response.send_message(
        f"✅ Done! I'll remind you about **{esc(reminder['title'])}** every "
        f"{amount} {esc(unit_word)} (next {format_duration(seconds)})."
    )


async def handle_list(interaction):
    client = interaction.client
    channel_id = interaction.channel_id

    # Replace the previously sent list so the channel only ever shows one.
    old_id = client.list_messages.pop(channel_id, None)
    if old_id is not None:
        try:
            await client.get_partial_messageable(channel_id).get_partial_message(old_id).delete()
        except discord.HTTPException:
            pass  # already gone, or the bot can't delete it

    text, view = await build_reminder_list(channel_id)
    await interaction.response.send_message(text, view=view)
    try:
        message = await interaction.original_response()
    except discord.HTTPException:
        return
    client.list_messages[channel_id] = message.id


async def handle_list_action(interaction, action, reminder_id):
    """A pick from the Reset… or Delete… dropdown: apply it, refresh the list."""
    channel_id = interaction.channel_id
    await core.act_on_reminder(PLATFORM, channel_id, reminder_id, action)
    text, view = await build_reminder_list(channel_id)
    await interaction.response.edit_message(content=text, view=view)
    if interaction.message is not None:
        interaction.client.list_messages[channel_id] = interaction.message.id


async def handle_close(interaction):
    interaction.client.list_messages.pop(interaction.channel_id, None)
    await interaction.response.defer()
    try:
        await interaction.delete_original_response()
    except discord.HTTPException:
        # Can't delete it: blank it out instead.
        await interaction.edit_original_response(
            content="List closed. Use `/list` to reopen.", view=None,
        )


async def handle_ok(interaction, reminder_id):
    """OK on a due-reminder alert: restart the timer and say so in place."""
    reminder = await core.act_on_reminder(
        PLATFORM, interaction.channel_id, reminder_id, "reset",
    )
    if reminder is None:
        await interaction.response.edit_message(
            content="This reminder no longer exists.", view=None,
        )
        return
    left = format_duration(reminder["interval_seconds"])
    await interaction.response.edit_message(
        content=f"✅ **{esc(reminder['title'])}** — timer restarted (next {left}).",
        view=None,
    )


# ---------------------------------------------------------------------------
# UI components
# ---------------------------------------------------------------------------


class OkButton(discord.ui.DynamicItem[discord.ui.Button], template=r"remind:ok:(?P<id>[0-9]+)"):
    """The OK button on an alert. Its custom id carries the reminder id."""

    def __init__(self, reminder_id):
        super().__init__(
            discord.ui.Button(
                label="OK (restart timer)",
                emoji="✅",
                style=discord.ButtonStyle.success,
                custom_id=f"remind:ok:{reminder_id}",
            )
        )
        self.reminder_id = reminder_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match["id"]))

    async def interaction_check(self, interaction):
        return await guard(interaction)

    async def callback(self, interaction):
        await handle_ok(interaction, self.reminder_id)


class ListSelect(discord.ui.DynamicItem[discord.ui.Select], template=r"remind:list:(?P<action>reset|del)"):
    """The Reset… / Delete… dropdowns on a /list message."""

    PLACEHOLDERS = {
        "reset": "🔄 Reset a reminder's timer…",
        "del": "🗑 Delete a reminder…",
    }

    def __init__(self, action, options=()):
        super().__init__(
            discord.ui.Select(
                custom_id=f"remind:list:{action}",
                placeholder=self.PLACEHOLDERS[action],
                options=[
                    discord.SelectOption(
                        label=clip(label), value=str(reminder_id), description=clip(status),
                    )
                    for label, reminder_id, status in options
                ],
            )
        )
        self.action = action

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["action"])

    async def interaction_check(self, interaction):
        return await guard(interaction)

    async def callback(self, interaction):
        try:
            reminder_id = int(self.item.values[0])
        except (IndexError, ValueError):
            return
        await handle_list_action(interaction, self.action, reminder_id)


class CloseButton(discord.ui.DynamicItem[discord.ui.Button], template=r"remind:list:close"):
    """The Close button on a /list message."""

    def __init__(self):
        super().__init__(
            discord.ui.Button(
                label="Close list", emoji="✖", style=discord.ButtonStyle.secondary,
                custom_id="remind:list:close",
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls()

    async def interaction_check(self, interaction):
        return await guard(interaction)

    async def callback(self, interaction):
        await handle_close(interaction)


class NewReminderModal(discord.ui.Modal, title="New reminder"):
    """The /new form: a title and how often."""

    title_field = discord.ui.Label(
        text="What should I remind you about?",
        component=discord.ui.TextInput(placeholder="Check Uni", max_length=MAX_TITLE_LEN),
    )
    interval_field = discord.ui.Label(
        text="How often?",
        description="For example: 5 days, 30 minutes, 2 hours, 1 week",
        component=discord.ui.TextInput(placeholder="5 days", max_length=40),
    )

    async def interaction_check(self, interaction):
        return await guard(interaction)

    async def on_submit(self, interaction):
        await handle_new_submit(
            interaction,
            self.title_field.component.value,
            self.interval_field.component.value,
        )

    async def on_error(self, interaction, error):
        await report_interaction_error(interaction, error)


# ---------------------------------------------------------------------------
# Slash commands
# ---------------------------------------------------------------------------


@app_commands.command(name="new", description="Create a recurring reminder in this channel")
async def new_command(interaction: discord.Interaction):
    await interaction.response.send_modal(NewReminderModal())


@app_commands.command(name="list", description="See this channel's reminders")
async def list_command(interaction: discord.Interaction):
    await handle_list(interaction)


@app_commands.command(name="help", description="How the reminder bot works")
async def help_command(interaction: discord.Interaction):
    await handle_help(interaction)


class GuardedTree(app_commands.CommandTree):
    """Applies the allow-lists before any slash command runs."""

    async def interaction_check(self, interaction):
        return await guard(interaction)

    async def on_error(self, interaction, error):
        if isinstance(error, app_commands.CheckFailure):
            return  # guard() already answered
        await report_interaction_error(interaction, getattr(error, "original", error))


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


def describe_error(error: Exception) -> str:
    """Turn a Discord exception into one short, human-readable line, or ""."""
    if isinstance(error, discord.LoginFailure):
        return (
            "❌ Discord rejected the bot token. Check DISCORD_BOT_TOKEN in your "
            "conf.env file (reset it in the Discord Developer Portal if needed, "
            "or run setup_wizard.py again)."
        )
    if isinstance(error, discord.PrivilegedIntentsRequired):
        return (
            "❌ Discord refused the connection because of privileged intents. "
            "This bot doesn't need any — check the Developer Portal settings."
        )
    if isinstance(error, discord.Forbidden):
        return (
            "🚫 Discord refused a message (Forbidden). Usually the bot lacks "
            "permission to post in that channel, or the user's DMs are closed."
        )
    if isinstance(error, discord.NotFound):
        return (
            "🚫 Discord says that channel doesn't exist any more (NotFound), "
            "or the bot can no longer see it."
        )
    if isinstance(error, discord.DiscordServerError):
        return (
            f"🌐 Discord had a server error ({error.status}). Temporary — "
            "the bot keeps retrying automatically; no action needed."
        )
    if isinstance(error, discord.HTTPException):
        return f"⚠️  Discord rejected a request we sent ({error.status}: {error.text})."
    if isinstance(error, (aiohttp.ClientError, asyncio.TimeoutError)):
        detail = str(error).strip() or error.__class__.__name__
        return (
            f"🌐 Network hiccup while talking to Discord ({detail}) - "
            "the bot reconnects automatically"
        )
    return ""


core.ERROR_DESCRIBERS.append(describe_error)


async def report_interaction_error(interaction, error):
    """Log a failed command/form and tell the user instead of going silent."""
    friendly = describe_error(error)
    if friendly:
        log.warning(friendly)
    else:
        log.error("Unexpected error while handling a Discord interaction:", exc_info=error)
    text = "😵 Sorry, something went wrong handling that. Please try again."
    try:
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)
    except discord.HTTPException:
        pass  # the log line above is already the record


# ---------------------------------------------------------------------------
# Front-end lifecycle
# ---------------------------------------------------------------------------


class ReminderClient(discord.Client):
    def __init__(self):
        # The bot never joins voice, so discord.py's "voice will NOT be
        # supported" warnings are noise — and would count as errors in the
        # daily log. (getattr: older discord.py has no warn_dave.)
        for flag in ("warn_nacl", "warn_dave"):
            if getattr(discord.VoiceClient, flag, False):
                setattr(discord.VoiceClient, flag, False)
        # Slash commands and buttons arrive as interactions, which need no
        # privileged intents. "guilds" keeps the channel cache sensible.
        intents = discord.Intents.none()
        intents.guilds = True
        super().__init__(
            intents=intents,
            # Reminder titles are user text: never let one ping @everyone.
            allowed_mentions=discord.AllowedMentions.none(),
        )
        self.tree = GuardedTree(self)
        for command in (new_command, list_command, help_command):
            self.tree.add_command(command)
        # channel id -> message id of the last /list, so a new one replaces it.
        self.list_messages = {}

    async def setup_hook(self):
        self.add_dynamic_items(OkButton, ListSelect, CloseButton)
        try:
            synced = await self.tree.sync()
        except discord.HTTPException as e:
            log.warning(
                "Discord: couldn't register the slash commands (%s). Commands from "
                "an earlier run keep working; restart the bot to try again.", e,
            )
        else:
            log.info("Discord: registered %d slash commands.", len(synced))

    async def on_ready(self):
        log.info("Discord: connected as %s, in %d server(s).", self.user, len(self.guilds))


class DiscordFrontend:
    """Runs the Discord bot alongside the others; see core.run_frontends()."""

    platform = PLATFORM

    def __init__(self, token):
        self.token = token
        access = DiscordAccess.from_env()
        ACCESS.guilds, ACCESS.users, ACCESS.channels = (
            access.guilds, access.users, access.channels,
        )
        if ACCESS.is_restricted():
            log.info(
                "Discord: allow-lists active — %d server(s), %d user(s), %d channel(s).",
                config.id_count(ACCESS.guilds), config.id_count(ACCESS.users),
                config.id_count(ACCESS.channels),
            )
        self.client = ReminderClient()
        self._task = None
        self._closing = False

    async def start(self, fatal):
        try:
            await self.client.login(self.token)
        except (discord.LoginFailure, discord.HTTPException, aiohttp.ClientError, OSError) as e:
            # Close the HTTP session login() opened, or aiohttp complains
            # about an unclosed connector on the way out.
            await self.client.close()
            if isinstance(e, discord.LoginFailure):
                raise core.FrontendStartError(describe_error(e)) from e
            raise core.FrontendStartError(
                f"Couldn't log in to Discord: {describe_error(e) or e}"
            ) from e
        self._task = asyncio.create_task(self._connect(fatal))

    async def _connect(self, fatal):
        """Keep the gateway connection up. discord.py reconnects by itself;
        this only returns or raises when it has given up for good."""
        try:
            await self.client.connect(reconnect=True)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            if not self._closing:
                fatal(f"Discord connection stopped: {describe_error(e) or repr(e)}")
            return
        if not self._closing:
            fatal("Discord connection closed unexpectedly.")

    async def stop(self):
        self._closing = True
        await self.client.close()
        if self._task is not None:
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    def accepts(self, reminder):
        """Whether the allow-lists still let this reminder's channel use the bot."""
        channel = self.client.get_channel(reminder["chat_id"])
        guild = getattr(channel, "guild", None)
        return ACCESS.allows_channel(guild.id if guild else None, reminder["chat_id"])

    async def send_alert(self, reminder):
        channel = self.client.get_partial_messageable(reminder["chat_id"])
        try:
            await channel.send(alert_text(reminder), view=alert_view(reminder["id"]))
        except (discord.Forbidden, discord.NotFound) as e:
            # No permission, closed DMs, or the channel is gone: retrying
            # can't fix that.
            raise core.DeliveryRejected(e) from e
        except discord.DiscordServerError as e:
            raise core.DeliveryRetry(e) from e
        except discord.HTTPException as e:
            if e.status == 401:
                # The token stopped working (reset in the Developer Portal).
                # Not this reminder's fault, so leave it due for when the bot
                # runs with a good token, as Telegram does for InvalidToken.
                raise core.DeliveryRetry(e, "Discord rejected the bot token") from e
            if 400 <= e.status < 500 and e.status != 429:
                raise core.DeliveryRejected(e) from e
            raise core.DeliveryRetry(e, "the Discord API call failed") from e
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
            # Couldn't reach Discord: retry on the next tick.
            raise core.DeliveryRetry(e) from e


def get_token():
    return os.environ.get("DISCORD_BOT_TOKEN", "").strip()
