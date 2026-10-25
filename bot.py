import asyncio
import io
import json
import logging
import logging.handlers
import os
from pathlib import Path
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

load_dotenv()  # before importing config: it reads REMINDER_MINUTES from the environment

import backup
import board
import boss
import db
import reminders
from config import (BACKUP_HOUR_UTC, DATA_PAGE, DONATION_AMOUNT, IGN_MAX, IGN_MIN, MEMBER_ROLE_NAME,
                    RECENT_DONORS, REMINDER_MINUTES, VERSION)

log = logging.getLogger("evil")

intents = discord.Intents.default()
intents.members = True  # privileged: needed for role checks and the not-donated list


def is_staff(interaction):
    user = interaction.user
    if user.guild_permissions.manage_guild:
        return True
    row = db.get_guild(bot.db, interaction.guild_id)
    return bool(row and row["officer_role_id"] and user.get_role(row["officer_role_id"]))


async def require_staff(interaction):
    if is_staff(interaction):
        return True
    await interaction.response.send_message("Staff only.", ephemeral=True)
    return False


def tracked_members(guild, member_role_id):
    role = guild.get_role(member_role_id)
    return [(m.id, m.display_name) for m in role.members if not m.bot] if role else []


BRAND = discord.Colour(0xC0392B)


def brand(embed, guild=None, footer=None):
    """Brand colour, the guild icon (when given), and 'Last updated <time>' in the footer."""
    embed.colour = BRAND
    embed.timestamp = datetime.now(timezone.utc)
    embed.set_footer(text=f"{footer} · Last updated" if footer else "Last updated")
    if guild and guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
    return embed


def board_embed(data, guild):
    embed = discord.Embed(
        title="Evil Donations — guild board",
        description=f"Today (UTC): **{data['today_count']}** donated\n"
                    f"Resets <t:{data['reset_ts']}:t> (your local time)")
    # one line per donor: side-by-side columns stack up on mobile
    recent = "\n".join(f"{board.short(name)} · {board.short(ign)} · <t:{ts}:t>" for name, ign, ts in data["recent"])
    embed.add_field(name=f"Last {RECENT_DONORS} donors (Discord · in-game · time)", value=recent or "Nobody yet.",
                    inline=False)
    embed.add_field(name=f"Not donated yet ({len(data['waiting'])})",
                    value=board.join_capped([discord.utils.escape_markdown(n) for n in data["waiting"]],
                                            sep="\n")  # one name per line
                    or "Everyone has donated.", inline=False)
    embed.add_field(name="Yesterday (UTC)",
                    value=f"Donated: {data['yesterday_count']} · Coins: {data['yesterday_coins']:,}",
                    inline=False)
    if data["streaks"]:
        embed.add_field(name="Top streaks (days in a row)",
                        value="\n".join(f"{board.short(ign)} · {n} days"
                                         + (f" · {board.badge(n)}" if board.badge(n) else "")
                                         for ign, n in data["streaks"]), inline=False)
    return brand(embed, guild, "Press Log donation once per day, after you donate in the game.")


_board_lock = asyncio.Lock()  # two quick refreshes must not post two boards
_data_lock = asyncio.Lock()
_attend_data_lock = asyncio.Lock()


async def refresh_board(guild):
    await refresh_board_message(guild)
    await refresh_data(guild)


async def refresh_all(guild):
    """Every board and data channel. Runs after each command and button press."""
    # ponytail: about 10 message edits per action; debounce here if Discord starts rate limiting
    try:
        await refresh_board(guild)
        await refresh_attendance(guild)
    except Exception:
        log.exception("Refresh after an action failed in guild %s", guild.id)


# --- data channel: a header message plus Donated / Not donated / Not linked, 15 rows per page ---

DATA_SECTIONS = {
    "donated": ("Donated today", ("Discord name", "In-game name", "Donated at")),
    "not_donated": ("Not donated yet", ("Discord name", "In-game name")),
    "not_linked": ("Not linked", ("In-game name",)),
}
ATTEND_SECTIONS = {  # the attendance data channel, same layout
    "attended": ("Attended", ("Discord name", "In-game name", "Logged at")),
    "not_attended": ("Not attended", ("Discord name", "In-game name")),
    "today_totals": ("Bosses today (UTC day)", ("In-game name", "Attended")),
}
data_pages = {}  # (guild_id, section) -> page shown. Resets to page 1 after a restart.


def data_header_embed(sections, guild):
    counts = {key: len(sections[key]) for key in DATA_SECTIONS}
    return brand(discord.Embed(
        title="Data",
        description=f"{db.today_utc()} (UTC) · resets <t:{board.next_reset_ts()}:t> (your local time)\n"
                    f"Donated: **{counts['donated']}** · Not donated: **{counts['not_donated']}** · "
                    f"Not linked: **{counts['not_linked']}**"), guild)


def attend_header_embed(info, sections, guild):
    if not info:
        text = "No boss yet."
    else:
        boss_ts, close_ts, state = info
        text = f"Boss at <t:{boss_ts}:t> · logging {'open until' if state == 'open' else 'closed at'} <t:{close_ts}:t>"
    return brand(discord.Embed(
        title="Boss attendance data",
        description=f"{text}\nAttended: **{len(sections['attended'])}** · "
                    f"Not attended: **{len(sections['not_attended'])}**"), guild)


def data_section_embed(guild_id, key, rows):
    title, columns = {**DATA_SECTIONS, **ATTEND_SECTIONS}[key]
    chunk, page, pages = board.paginate(rows, data_pages.get((guild_id, key), 0), DATA_PAGE)
    data_pages[(guild_id, key)] = page
    embed = discord.Embed(title=f"{title} ({len(rows)})")
    if chunk:  # one line per person: side-by-side columns stack up on mobile
        lines = [" · ".join(f"<t:{cell}:t>" if column in ("Donated at", "Logged at") else board.short(cell)
                            for column, cell in zip(columns, row)) for row in chunk]
        embed.add_field(name=" · ".join(columns), value="\n".join(lines), inline=False)
    else:
        embed.description = "Nobody."
    return brand(embed, footer=f"Page {page + 1}/{pages}")


class DataView(discord.ui.View):
    """Previous / Next buttons under one data section. They change the shared message."""

    def __init__(self, key):
        super().__init__(timeout=None)
        for label, step in (("Previous", -1), ("Next", 1)):
            button = discord.ui.Button(label=label, custom_id=f"data:{key}:{step}")
            button.callback = self._callback(key, step)
            self.add_item(button)

    @staticmethod
    def _callback(key, step):
        async def callback(interaction):
            guild = interaction.guild
            row = db.get_guild(bot.db, guild.id)
            footer = interaction.message.embeds[0].footer.text or "Page 1/1"
            data_pages[(guild.id, key)] = int(footer.split()[1].split("/")[0]) - 1 + step
            tracked = tracked_members(guild, row["member_role_id"])
            if key in ATTEND_SECTIONS:
                sections = board.attend_data_sections(bot.db, guild.id, tracked, datetime.now(timezone.utc))[1]
            else:
                sections = board.data_sections(bot.db, guild.id, tracked, db.today_utc())
            await interaction.response.edit_message(embed=data_section_embed(guild.id, key, sections[key]))
            await refresh_all(guild)
        return callback


async def refresh_data(guild):
    """Edit the data messages in place. If one was deleted, post them all again.
    Returns an error text if Discord refused, else None."""
    async with _data_lock:
        row = db.get_guild(bot.db, guild.id)
        channel = row and row["data_channel_id"] and guild.get_channel(row["data_channel_id"])
        if not channel:
            return
        sections = board.data_sections(bot.db, guild.id, tracked_members(guild, row["member_role_id"]),
                                       db.today_utc())
        embeds = [data_header_embed(sections, guild)] + [data_section_embed(guild.id, key, sections[key])
                                                  for key in DATA_SECTIONS]
        return await show_sections(guild, channel, row["data_message_ids"], "data_message_ids", embeds,
                                   DATA_SECTIONS)


async def refresh_attend_data(guild):
    """refresh_data for the attendance data channel."""
    async with _attend_data_lock:
        row = db.get_guild(bot.db, guild.id)
        channel = row and row["attend_data_channel_id"] and guild.get_channel(row["attend_data_channel_id"])
        if not channel:
            return
        info, sections = board.attend_data_sections(bot.db, guild.id, tracked_members(guild, row["member_role_id"]),
                                                    datetime.now(timezone.utc))
        embeds = [attend_header_embed(info, sections, guild)] + [data_section_embed(guild.id, key, sections[key])
                                                          for key in ATTEND_SECTIONS]
        return await show_sections(guild, channel, row["attend_data_message_ids"], "attend_data_message_ids",
                                   embeds, ATTEND_SECTIONS)


async def show_sections(guild, channel, ids_json, ids_field, embeds, keys):
    """Edit a header and its section messages in place. If one was deleted, post them all again.
    Returns an error text if Discord refused, else None."""
    ids = json.loads(ids_json or "[]")
    try:
        if len(ids) == len(embeds):
            for message_id, embed in zip(ids, embeds):
                await channel.get_partial_message(message_id).edit(embed=embed)
            return
    except discord.NotFound:
        pass
    except discord.HTTPException as e:
        log.warning("Data refresh failed in guild %s: %s", guild.id, e)
        return str(e)
    for message_id in ids:  # clear what is left of the old set, then post a new one
        try:
            await channel.get_partial_message(message_id).delete()
        except discord.HTTPException:
            pass
    try:
        posted = [await channel.send(embed=embeds[0])]
        for key, embed in zip(keys, embeds[1:]):
            posted.append(await channel.send(embed=embed, view=DataView(key)))
    except discord.HTTPException as e:
        log.warning("Data post failed in guild %s: %s", guild.id, e)
        return str(e)
    db.save_guild(bot.db, guild.id, **{ids_field: json.dumps([m.id for m in posted])})


async def refresh_board_message(guild):
    """Update the board. If something was posted below it, move it to the bottom of the channel."""
    async with _board_lock:
        row = db.get_guild(bot.db, guild.id)
        channel = row and guild.get_channel(row["panel_channel_id"])
        if not channel or not row["status_message_id"]:
            return
        data = board.board_data(bot.db, guild.id, tracked_members(guild, row["member_role_id"]),
                                db.today_utc(), db.yesterday_utc())
        embed = board_embed(data, guild)
        old = channel.get_partial_message(row["status_message_id"])
        try:
            if channel.last_message_id == old.id:
                await old.edit(embed=embed, view=PanelView())  # view: brings the button back after /stop
            else:
                try:
                    await old.delete()
                except discord.NotFound:
                    pass
                new = await channel.send(embed=embed, view=PanelView())
                db.save_guild(bot.db, guild.id, status_message_id=new.id)
        except discord.HTTPException as e:
            log.warning("Board refresh failed in guild %s: %s", guild.id, e)


class PanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Log donation", style=discord.ButtonStyle.green, custom_id="donate")
    async def donate(self, interaction, button):
        row = db.get_guild(bot.db, interaction.guild_id)
        if not row:
            return await interaction.response.send_message("Bot is not set up yet.", ephemeral=True)
        if not interaction.user.get_role(row["member_role_id"]):
            return await interaction.response.send_message(
                "Only guild members can log donations.", ephemeral=True)
        ign = db.get_ign(bot.db, interaction.guild_id, interaction.user.id)
        if not ign:
            return await interaction.response.send_message(
                "Ask an officer to link your in-game name first.", ephemeral=True)
        try:
            db.record(bot.db, interaction.guild_id, interaction.user.id,
                      interaction.user.display_name, ign, db.today_utc())
        except db.AlreadyDonated:
            return await interaction.response.send_message(
                "You already logged a donation today.", ephemeral=True)
        await interaction.response.send_message(
            f"Logged {DONATION_AMOUNT:,} for today.", ephemeral=True)
        await refresh_all(interaction.guild)


# --- attendance board: one message with the I attended button, kept at the bottom like the board ---

_attend_lock = asyncio.Lock()
_attend_seen = {}  # guild_id -> (boss id, state, day) the board last showed


def attend_embed(data, guild):
    embed = discord.Embed(title="Guild boss attendance")
    if not data:
        embed.description = "No boss yet. Logging opens when the next boss starts."
        return brand(embed, guild)
    boss_at, close = f"<t:{data['boss_ts']}:t>", f"<t:{data['close_ts']}:t>"
    embed.description = (f"Boss at {boss_at}: logging open until {close} (<t:{data['close_ts']}:R>)."
                         if data["state"] == "open" else
                         f"Boss at {boss_at}: logging closed at {close}. It opens again when the next boss starts.")
    embed.add_field(name=f"Attended ({len(data['attended'])})",
                    value=board.join_capped([discord.utils.escape_markdown(n) for n in data["attended"]], sep="\n")
                    or "Nobody yet.", inline=False)
    embed.add_field(name=f"Not attended ({len(data['waiting'])})",
                    value=board.join_capped([discord.utils.escape_markdown(n) for n in data["waiting"]], sep="\n")
                    or "Everyone attended.", inline=False)
    embed.add_field(name="Today's bosses (UTC day)",
                    value="\n".join(f"<t:{ts}:t> · {n} attended" for ts, n in data["today"]) or "None yet.",
                    inline=False)
    return brand(embed, guild, "Press I attended during the hour after the boss starts.")


async def refresh_attendance(guild):
    await refresh_attend_board(guild)
    await refresh_attend_data(guild)


async def refresh_attend_board(guild):
    """Same as refresh_board_message, for the attendance board."""
    async with _attend_lock:
        row = db.get_guild(bot.db, guild.id)
        channel = row and row["attend_channel_id"] and guild.get_channel(row["attend_channel_id"])
        if not channel or not row["attend_message_id"]:
            return
        embed = attend_embed(board.attend_board_data(bot.db, guild.id, tracked_members(guild, row["member_role_id"]),
                                                     datetime.now(timezone.utc)), guild)
        old = channel.get_partial_message(row["attend_message_id"])
        try:
            if channel.last_message_id == old.id:
                await old.edit(embed=embed, view=AttendView())
            else:
                try:
                    await old.delete()
                except discord.NotFound:
                    pass
                new = await channel.send(embed=embed, view=AttendView())
                db.save_guild(bot.db, guild.id, attend_message_id=new.id)
        except discord.HTTPException as e:
            log.warning("Attendance board refresh failed in guild %s: %s", guild.id, e)


class AttendView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="I attended", style=discord.ButtonStyle.green, custom_id="attend")
    async def attend(self, interaction, button):
        row = db.get_guild(bot.db, interaction.guild_id)
        if not row:
            return await interaction.response.send_message("Bot is not set up yet.", ephemeral=True)
        if not interaction.user.get_role(row["member_role_id"]):
            return await interaction.response.send_message(
                "Only guild members can log attendance.", ephemeral=True)
        ign = db.get_ign(bot.db, interaction.guild_id, interaction.user.id)
        if not ign:
            return await interaction.response.send_message(
                "Ask an officer to link your in-game name first.", ephemeral=True)
        now = datetime.now(timezone.utc)
        current = db.started_boss(bot.db, interaction.guild_id, now.isoformat(timespec="seconds"))
        if not current or boss.attend_state(iso_utc(current["boss_time"]), now) != "open":
            return await interaction.response.send_message(
                "Logging is closed. It opens when the next guild boss starts.", ephemeral=True)
        try:
            db.attend(bot.db, current["id"], interaction.user.id, interaction.user.display_name, ign)
        except db.AlreadyAttended:
            return await interaction.response.send_message("You already logged this boss.", ephemeral=True)
        ts = int(iso_utc(current["boss_time"]).timestamp())
        await interaction.response.send_message(f"Logged attendance for the boss at <t:{ts}:t>.",
                                                ephemeral=True)
        await refresh_all(interaction.guild)


async def attend_tick(guild, row, now):
    """Ping when a boss's logging opens. Refresh the board when the boss, its state or the day changes."""
    current = db.started_boss(bot.db, guild.id, now.isoformat(timespec="seconds"))
    if not current:
        return
    t = iso_utc(current["boss_time"])
    state = boss.attend_state(t, now)
    # claimed before sending, so a restart never double-pings. Bot back after the window: no ping
    if state == "open" and db.claim_boss_open(bot.db, current["id"]):
        channel = guild.get_channel(row["attend_channel_id"])
        role = guild.get_role(row["member_role_id"])
        if channel and role:
            warn_if_role_not_pinged(await channel.send(boss.attend_open_text(role.mention, t)), role.id)
            log.info("Attendance open for %s in guild %s", current["boss_time"], guild.id)
    key = (current["id"], state, now.date())
    if _attend_seen.get(guild.id) != key:
        _attend_seen[guild.id] = key
        await refresh_attendance(guild)  # also moves the board below the ping


class ConfirmView(discord.ui.View):
    """Confirm / Cancel under a private preview of a big staff action. action(interaction) does it and
    answers by editing the preview."""

    def __init__(self, action):
        super().__init__(timeout=120)
        self.action = action

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction, button):
        self.stop()
        await self.action(interaction)
        if not bot.is_closed():
            await refresh_all(interaction.guild)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction, button):
        self.stop()
        await interaction.response.edit_message(content="Cancelled. Nothing was changed.", view=None)


class UndoLogAllView(discord.ui.View):
    """One Undo button under the /log_all result. It voids exactly the donations that run logged."""

    def __init__(self, submission_ids):
        super().__init__(timeout=900)  # ponytail: gone after 15 minutes or a restart; /undo by id still works
        self.submission_ids = submission_ids

    @discord.ui.button(label="Undo", style=discord.ButtonStyle.secondary)
    async def undo(self, interaction, button):
        self.stop()
        voided = sum(db.void(bot.db, interaction.guild_id, i, interaction.user.id) for i in self.submission_ids)
        await interaction.response.edit_message(content=f"Undid {voided} donation(s) from /log_all.", view=None)
        await audit(interaction, f"undid {voided} donation(s) from /log_all.")
        await refresh_all(interaction.guild)


class HistoryView(discord.ui.View):
    """One UTC day: donated, then not donated, 15 rows per page with Previous / Next buttons.
    labels = (title, done, not done, not done field); also used for one boss's attendance."""

    def __init__(self, labels, donated, waiting):
        super().__init__(timeout=300)
        self.labels, self.page = labels, 0
        self.rows = ([("donated", f"{board.short(n)} · {board.short(i)} · <t:{t}:t>") for n, i, t in donated]
                     + [("waiting", discord.utils.escape_markdown(n)) for n in waiting])
        self.counts = (len(donated), len(waiting))
        self._update_buttons()

    def embed(self):
        chunk, self.page, pages = board.paginate(self.rows, self.page, DATA_PAGE)  # also clamps self.page
        donated = [text for kind, text in chunk if kind == "donated"]
        waiting = [text for kind, text in chunk if kind == "waiting"]
        title, done, not_done, not_done_field = self.labels
        embed = discord.Embed(title=title,
                              description=f"{done}: **{self.counts[0]}** · {not_done}: **{self.counts[1]}**")
        if donated:
            embed.add_field(name=f"{done} ({self.counts[0]})", value="\n".join(donated), inline=False)
        if waiting:
            embed.add_field(name=f"{not_done_field} ({self.counts[1]})", value="\n".join(waiting), inline=False)
        return brand(embed, footer=f"Page {self.page + 1}/{pages}")

    def _update_buttons(self):
        pages = board.paginate(self.rows, self.page, DATA_PAGE)[2]
        self.previous.disabled = self.page <= 0
        self.next.disabled = self.page >= pages - 1

    async def _show(self, interaction):
        embed = self.embed()
        self._update_buttons()
        await interaction.response.edit_message(embed=embed, view=self)
        await refresh_all(interaction.guild)

    @discord.ui.button(label="Previous")
    async def previous(self, interaction, button):
        self.page -= 1
        await self._show(interaction)

    @discord.ui.button(label="Next")
    async def next(self, interaction, button):
        self.page += 1
        await self._show(interaction)


class Bot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix=commands.when_mentioned, intents=intents)
        self.db = db.connect(os.getenv("DB_PATH", "donations.db"))

    async def setup_hook(self):
        self.add_view(PanelView())  # keeps the panel button working after a restart
        self.add_view(AttendView())
        self.add_view(BossPollView())
        for key in [*DATA_SECTIONS, *ATTEND_SECTIONS]:
            self.add_view(DataView(key))
        await self.tree.sync()
        log.info("Evil Donations v%s starting", VERSION)
        tick.start()

    async def on_app_command_completion(self, interaction, command):
        if interaction.guild and not self.is_closed():  # /stop closes the bot inside the command
            await refresh_all(interaction.guild)

    async def close(self):
        if not self.is_closed():
            await post_offline_notice()
        await super().close()


bot = Bot()


async def post_offline_notice():
    """Replace each board with an offline notice and no button. Runs on /stop and Ctrl+C."""
    notice = discord.Embed(title="Evil Donations — offline",
                           description="Bot offline. Do not log donations. Contact an officer.",
                           colour=discord.Colour.dark_grey(), timestamp=datetime.now(timezone.utc))
    for guild in bot.guilds:
        row = db.get_guild(bot.db, guild.id)
        channel = row and guild.get_channel(row["panel_channel_id"])
        if not channel or not row["status_message_id"]:
            continue
        try:
            await channel.get_partial_message(row["status_message_id"]).edit(embed=notice, view=None)
        except discord.HTTPException as e:
            log.warning("Offline notice failed in guild %s: %s", guild.id, e)


async def send_reminder(guild, row):
    waiting = board.not_donated(bot.db, guild.id, tracked_members(guild, row["member_role_id"]),
                                db.today_utc())
    channel = guild.get_channel(row["panel_channel_id"])
    if not channel or not waiting:
        return
    reset = board.next_reset_ts()
    for group in reminders.chunks([i for i, _ in waiting]):
        await channel.send(f"Donation reminder: daily reset <t:{reset}:R>.\n"
                           + " ".join(f"<@{i}>" for i in group))
    await refresh_board(guild)  # the reminder pushed the board up: move it back to the bottom


def iso_utc(text):
    return datetime.fromisoformat(text)


def warn_if_role_not_pinged(message, role_id):
    if role_id not in [r.id for r in message.role_mentions]:  # what Discord actually notified
        log.warning("Role ping did not notify anyone in guild %s: turn on 'Allow anyone to @mention "
                    "this role' for the member role, or give the bot Mention All Roles", message.guild.id)


async def staff_post(guild, text=None, **kwargs):
    """Post in the staff channel, if one is set. Never pings. Failures are only logged."""
    row = db.get_guild(bot.db, guild.id)
    channel = row and row["staff_channel_id"] and guild.get_channel(row["staff_channel_id"])
    if not channel:
        return
    try:
        await channel.send(text, allowed_mentions=discord.AllowedMentions.none(), **kwargs)
    except discord.HTTPException as e:
        log.warning("Staff channel post failed in guild %s: %s", guild.id, e)


async def audit(interaction, text):
    """One line in the staff channel for each staff action: who did what."""
    await staff_post(interaction.guild, f"📝 {interaction.user.mention} {text}")


class BossPollView(discord.ui.View):
    """Skip button under each boss poll: staff close the poll with no ping, and the next poll follows."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Skip this boss (staff)", style=discord.ButtonStyle.secondary, custom_id="boss_skip")
    async def skip(self, interaction, button):
        if not await require_staff(interaction):
            return
        poll = db.poll_by_message(bot.db, interaction.message.id)
        now = datetime.now(timezone.utc)
        # resolved now: the next poll follows boss.NEXT_POLL_DELAY later
        if not poll or not db.resolve_boss_poll(bot.db, interaction.guild_id, poll["cycle_start"], now.isoformat()):
            return await interaction.response.send_message("This poll is already closed.", ephemeral=True)
        await interaction.response.edit_message(
            content=f"{interaction.message.content}\n**Result:** skipped by {interaction.user.mention}. "
                    "The next poll follows soon.", view=None, allowed_mentions=discord.AllowedMentions.none())
        await audit(interaction, f"skipped the guild boss poll for <t:{int(iso_utc(poll['cycle_start']).timestamp())}:t>.")
        await refresh_all(interaction.guild)


async def close_poll_message(channel, message_id, text):
    """Add the result line to a poll and remove its Skip button. A deleted poll is fine."""
    try:
        message = await channel.fetch_message(message_id)
        await message.edit(content=f"{message.content}\n{text}", view=None,
                           allowed_mentions=discord.AllowedMentions.none())
    except discord.HTTPException as e:
        log.warning("Could not close boss poll %s: %s", message_id, e)


async def clean_old_polls(guild, channel, now):
    """Delete poll and ping messages a day after their cycle. Marked done even if Discord refuses, so a
    failing delete is not retried every minute."""
    for poll in db.polls_to_clean(bot.db, guild.id, (now - timedelta(days=1)).isoformat()):
        for message_id in filter(None, (poll["message_id"], poll["ping_message_id"])):
            try:
                await channel.get_partial_message(message_id).delete()
            except discord.NotFound:
                pass
            except discord.HTTPException as e:
                log.warning("Could not delete old boss message %s: %s", message_id, e)
        db.mark_poll_cleaned(bot.db, guild.id, poll["cycle_start"])


async def post_boss_poll(guild, channel, role_id, cycle_start):
    message = await channel.send(boss.poll_text(f"<@&{role_id}>", cycle_start), view=BossPollView())
    warn_if_role_not_pinged(message, role_id)
    db.add_boss_poll(bot.db, guild.id, cycle_start.isoformat(), message.id)
    for emoji in boss.EMOJIS:
        await message.add_reaction(emoji)
    log.info("Boss poll posted for %s in guild %s", cycle_start.isoformat(), guild.id)


async def count_votes(guild, channel, message_id, role_id):
    """Votes per option: one per ingame guild member who reacted with that number."""
    votes = [0] * boss.SLOTS
    try:
        message = await channel.fetch_message(message_id)
        for reaction in message.reactions:
            if str(reaction.emoji) in boss.EMOJIS:
                index = boss.EMOJIS.index(str(reaction.emoji))
                async for user in reaction.users():
                    member = guild.get_member(user.id)
                    if member and not member.bot and member.get_role(role_id):
                        votes[index] += 1
    except discord.HTTPException as e:  # deleted, or no access: still ping, as if nobody voted
        log.warning("Cannot read boss poll %s (%s): counting no votes", message_id, e)
        return [0] * boss.SLOTS
    return votes


_boss_lock = asyncio.Lock()  # /boss start and the minute loop must not post the same poll twice


async def boss_tick(guild, row, now):
    async with _boss_lock:
        await _boss_tick(guild, row, now)


async def _boss_tick(guild, row, now):
    """Post polls, watch the votes, and ping the guild 10 minutes before the winning time."""
    channel = guild.get_channel(row["boss_channel_id"])
    role = guild.get_role(row["member_role_id"])
    if not channel or not role:
        log.warning("Boss channel or role missing in guild %s", guild.id)
        return
    latest = db.latest_boss_poll(bot.db, guild.id)
    state = latest and (iso_utc(latest["cycle_start"]),
                        latest["resolved_time"] and iso_utc(latest["resolved_time"]))
    action, when = boss.next_action(state, now)
    if action == "post":
        return await post_boss_poll(guild, channel, role.id, when)
    if action == "end":
        if db.resolve_boss_poll(bot.db, guild.id, latest["cycle_start"], when.isoformat()):
            await close_poll_message(channel, latest["message_id"], boss.result_text(None, 0))
        return
    if action == "wait":
        return await clean_old_polls(guild, channel, now)
    cycle = state[0]
    times = boss.slot_times(cycle)
    votes = await count_votes(guild, channel, latest["message_id"], role.id)
    pick = boss.pick_ping(now, cycle, votes)
    # the slot is claimed before sending, so a restart never double-pings
    if pick is not None and db.resolve_boss_poll(bot.db, guild.id, latest["cycle_start"],
                                                 times[pick].isoformat()):
        db.add_boss(bot.db, guild.id, times[pick].isoformat())  # attendance opens at this time
        ping = await channel.send(boss.ping_text(role.mention, times[pick], votes[pick]))
        warn_if_role_not_pinged(ping, role.id)
        db.save_ping_message(bot.db, guild.id, latest["cycle_start"], ping.id)
        await close_poll_message(channel, latest["message_id"], boss.result_text(times[pick], votes[pick]))
        log.info("Boss ping for %s in guild %s, votes %s", times[pick].isoformat(), guild.id, votes)


async def post_weekly_leaderboard(guild, row, now):
    """Mondays (UTC): last week's leaderboard in the panel channel, once per week."""
    week_start = now.date() - timedelta(days=7)
    if now.weekday() != 0 or not db.claim_post(bot.db, guild.id, "weekly", week_start.isoformat()):
        return
    channel = guild.get_channel(row["panel_channel_id"])
    if not channel:
        return
    data = board.weekly_leaderboard(bot.db, guild.id, tracked_members(guild, row["member_role_id"]), week_start)
    esc = discord.utils.escape_markdown
    last_day = week_start + timedelta(days=6)
    embed = discord.Embed(title="Weekly donation leaderboard",
                          description=f"{week_start:%d %b} to {last_day:%d %b %Y} (UTC days)")
    embed.add_field(name="Top donors", inline=False,
                    value="\n".join(f"**{k}.** {esc(ign)} · {n}/7 days" for k, (ign, n) in enumerate(data["top"], 1))
                    or "Nobody donated last week.")
    embed.add_field(name=f"Perfect week ({len(data['perfect'])})", inline=False,
                    value=board.join_capped([esc(n) for n in data["perfect"]]) or "Nobody this time.")
    embed.add_field(name="Most improved", inline=False,
                    value="\n".join(f"{esc(ign)} · +{n} days" for ign, n in data["improved"]) or "Nobody this time.")
    await channel.send(embed=brand(embed, guild, "A new leaderboard every Monday"))
    log.info("Weekly leaderboard posted for %s in guild %s", week_start, guild.id)
    await refresh_board(guild)  # the post pushed the board up: move it back to the bottom


async def post_monthly(guild, row, now):
    """On the 1st (UTC): last month's report and CSV in the staff channel, and the attendance
    leaderboard in the attendance channel. Once per month."""
    month = (now.date().replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
    if now.day != 1 or not db.claim_post(bot.db, guild.id, "monthly", month):
        return
    report = board.monthly_report(bot.db, guild.id, tracked_members(guild, row["member_role_id"]), month, now)
    rows, total, esc = report["rows"], report["boss_total"], discord.utils.escape_markdown
    title_month = datetime.strptime(month, "%Y-%m").strftime("%B %Y")
    donated = sum(r[2] for r in rows)
    possible = len(rows) * report["days_in_month"]
    embed = discord.Embed(title=f"Monthly report · {title_month}", description=f"{len(rows)} linked members (UTC days)")
    embed.add_field(name="Donations", value=f"**{donated}** of {possible} member-days"
                    + (f" ({round(100 * donated / possible)}%)" if possible else "")
                    + f"\n**{donated * DONATION_AMOUNT:,}** coins")
    attended = sum(r[3] for r in rows)
    embed.add_field(name="Guild bosses", value=f"**{total}** bosses\n**{attended}** attendances"
                    + (f" ({round(100 * attended / (total * len(rows)))}%)" if total and rows else ""))
    lowest = sorted(rows, key=lambda r: (r[2], r[0].lower()))[:5]
    embed.add_field(name="Fewest donation days", inline=False,
                    value="\n".join(f"{esc(ign)} · {d} days" for ign, _, d, _ in lowest) or "No linked members.")
    csv_data = board.monthly_csv(report, DONATION_AMOUNT).encode("utf-8-sig")  # BOM: Excel reads UTF-8
    await staff_post(guild, embed=brand(embed, guild, "Full numbers in the attached file"),
                     file=discord.File(io.BytesIO(csv_data), f"report-{month}.csv"))

    channel = row["attend_channel_id"] and guild.get_channel(row["attend_channel_id"])
    if channel and total:
        top = sorted(rows, key=lambda r: (-r[3], r[0].lower()))[:10]
        leaders = discord.Embed(title=f"Guild boss attendance · {title_month}",
                                     description=f"{total} guild bosses this month")
        leaders.add_field(name="Most attended", inline=False, value="\n".join(
            f"**{k}.** {esc(ign)} · {a}/{total}" for k, (ign, _, _, a) in enumerate(top, 1) if a) or "Nobody attended.")
        perfect = [esc(ign) for ign, _, _, a in rows if a == total]
        leaders.add_field(name=f"Perfect attendance ({len(perfect)})", inline=False,
                               value=board.join_capped(perfect) or "Nobody this time.")
        streaks = [(ign, n) for ign, n in report["streaks"] if n >= 2][:5]
        leaders.add_field(name="Longest running streaks", inline=False,
                               value="\n".join(f"{esc(ign)} · {n} bosses in a row" for ign, n in streaks) or "None yet.")
        try:
            await channel.send(embed=brand(leaders, guild, "A new leaderboard on the 1st of each month"))
        except discord.HTTPException as e:
            log.warning("Monthly attendance leaderboard failed in guild %s: %s", guild.id, e)
        await refresh_attendance(guild)  # move the attendance board back to the bottom
    log.info("Monthly report posted for %s in guild %s", month, guild.id)


_backup_failed_day = None


@tasks.loop(seconds=60)
async def tick():
    """Every minute: refresh the board on a new UTC day, send due reminders, write the daily backup."""
    now = datetime.now(timezone.utc)
    today = now.date().isoformat()
    for guild in bot.guilds:
        try:  # one guild's failure must not stop the loop: an uncaught error ends a tasks.loop for good
            row = db.get_guild(bot.db, guild.id)
            if not row:
                continue
            if row["boss_channel_id"]:
                await boss_tick(guild, row, now)
            if row["attend_channel_id"]:
                await attend_tick(guild, row, now)
            await post_weekly_leaderboard(guild, row, now)
            await post_monthly(guild, row, now)
            if row["board_day"] != today:
                await refresh_board(guild)
                db.save_guild(bot.db, guild.id, board_day=today)
                log.info("Board refreshed for new day %s in guild %s", today, guild.id)
            if not guild.get_role(row["member_role_id"]):
                log.warning("Role %s missing in guild %s: no reminders", MEMBER_ROLE_NAME, guild.id)
                continue
            for minutes in REMINDER_MINUTES:
                action = reminders.reminder_action(now, minutes)
                # the slot is claimed before sending, so a restart never double-sends
                if action != "wait" and db.mark_reminder(bot.db, guild.id, today, minutes):
                    if action == "send":
                        await send_reminder(guild, row)
                        log.info("Sent %s-minute reminder in guild %s", minutes, guild.id)
        except Exception:
            log.exception("tick failed for guild %s", guild.id)
    if now.hour >= BACKUP_HOUR_UTC:
        try:
            if backup.run_backup(bot.db, today, os.getenv("BACKUP_DIR", "backups"),
                                 os.getenv("ONEDRIVE_BACKUP_DIR")):
                log.info("Backup written for %s", today)
        except Exception:
            global _backup_failed_day
            if _backup_failed_day != today:  # once a day: a full traceback every minute floods the log
                _backup_failed_day = today
                log.exception("Backup failed (retrying every minute, logged once a day)")


@tick.before_loop
async def before_tick():
    await bot.wait_until_ready()
    db_path = os.getenv("DB_PATH", "donations.db")
    for guild in bot.guilds:
        if db.get_guild(bot.db, guild.id):
            continue
        # usually a moved bot whose old database was uploaded under another name
        text = (f"Evil Donations has no setup saved for **{guild.name}** in `{Path(db_path).name}`. "
                f"If you moved the bot, stop it and upload the old database as exactly `{Path(db_path).name}`. "
                f"Other .db files beside it: {', '.join(backup.other_db_files(db_path)) or 'none'}. "
                "New server: run /setup.")
        log.error(text)
        try:  # the owner sees it in Discord: no staff channel is saved yet
            await guild.owner.send(text)
        except (discord.HTTPException, AttributeError):  # DMs closed, or owner not cached
            pass
    for guild in bot.guilds:  # brings the board and button back after a /stop or crash
        try:  # an error here would stop the loop from ever starting
            await refresh_board(guild)
            await refresh_attendance(guild)
        except Exception:
            log.exception("Startup board refresh failed for guild %s", guild.id)


@bot.tree.command(description="Post the donation panel in a channel.")
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
@app_commands.checks.has_permissions(manage_guild=True)
async def setup(interaction: discord.Interaction, panel_channel: discord.TextChannel,
                officer_role: discord.Role):
    member_role = discord.utils.get(interaction.guild.roles, name=MEMBER_ROLE_NAME)
    if not member_role:
        return await interaction.response.send_message(
            f"Create a role named `{MEMBER_ROLE_NAME}` first.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    old = db.get_guild(bot.db, interaction.guild_id)
    try:
        status = await panel_channel.send(embed=discord.Embed(description="Loading board..."),
                                          view=PanelView())
    except discord.HTTPException as e:
        return await interaction.followup.send(
            f"Could not post in {panel_channel.mention}: {e}\nGive the bot View Channel, Send Messages "
            "and Embed Links there, then run /setup again.", ephemeral=True)
    old_channel = old and old["status_message_id"] and interaction.guild.get_channel(old["panel_channel_id"])
    if old_channel:  # remove the board from an earlier /setup, or it lingers and never updates
        try:
            await old_channel.get_partial_message(old["status_message_id"]).delete()
        except discord.HTTPException:
            pass
    db.save_guild(bot.db, interaction.guild_id, panel_channel_id=panel_channel.id,
                  status_message_id=status.id,
                  officer_role_id=officer_role.id, member_role_id=member_role.id)
    await interaction.followup.send(f"Panel posted in {panel_channel.mention}.", ephemeral=True)
    await audit(interaction, f"ran /setup: panel in {panel_channel.mention}, officer role {officer_role.mention}.")


async def link_autocomplete(interaction: discord.Interaction, current: str):
    names = db.roster_names(bot.db, interaction.guild_id, current, only_free=True, limit=25)
    return [app_commands.Choice(name=n, value=n) for n in names]


roster_group = app_commands.Group(name="roster", description="The in-game names staff pick in /link_member.",
                                  guild_only=True)


@roster_group.command(name="add", description="Staff: add in-game names, separated by commas or new lines.")
async def roster_add(interaction: discord.Interaction, names: str):
    if not await require_staff(interaction):
        return
    good, bad = db.parse_names(names)
    added = db.roster_add(bot.db, interaction.guild_id, good)
    msg = f"Added {added} new name(s). {len(good) - added} were already on the list."
    if bad:
        msg += f"\nSkipped (names need {IGN_MIN} to {IGN_MAX} characters): {board.join_capped(bad, 500)}"
    await interaction.response.send_message(msg, ephemeral=True)
    if added:
        await audit(interaction, f"added {added} roster name(s).")


@roster_group.command(name="remove", description="Staff: remove a name from the list.")
async def roster_remove(interaction: discord.Interaction, name: str):
    if not await require_staff(interaction):
        return
    removed = db.roster_remove(bot.db, interaction.guild_id, name)
    msg = f"Removed **{name}**. Anyone already linked to it stays linked." if removed else "That name is not on the list."
    await interaction.response.send_message(msg, ephemeral=True)
    if removed:
        await audit(interaction, f"removed **{discord.utils.escape_markdown(name)}** from the roster.")


@roster_remove.autocomplete("name")
async def roster_remove_autocomplete(interaction: discord.Interaction, current: str):
    names = db.roster_names(bot.db, interaction.guild_id, current, limit=25)
    return [app_commands.Choice(name=n, value=n) for n in names]


@roster_group.command(name="list", description="Staff: show the names and which are taken.")
async def roster_list(interaction: discord.Interaction):
    if not await require_staff(interaction):
        return
    names = db.roster_names(bot.db, interaction.guild_id)
    free = set(db.roster_names(bot.db, interaction.guild_id, only_free=True))
    text = "\n".join(n if n in free else f"{n} (taken)" for n in names)
    header = f"{len(names)} names, {len(free)} still free."
    if len(text) + len(header) > 1800:  # too long for a message: send as a file
        file = discord.File(io.BytesIO(text.encode("utf-8")), "roster.txt")
        return await interaction.response.send_message(header, file=file, ephemeral=True)
    await interaction.response.send_message(f"{header}\n{text}" if names else "The list is empty.",
                                            ephemeral=True)


bot.tree.add_command(roster_group)


@bot.tree.command(description="Staff: post everyone linked, with in-game name and Discord name.")
@app_commands.guild_only()
async def linked(interaction: discord.Interaction):
    if not await require_staff(interaction):
        return
    lines = board.link_lines(db.get_igns(bot.db, interaction.guild_id))
    if not lines:
        return await interaction.response.send_message("Nobody is linked yet.", ephemeral=True)
    none = discord.AllowedMentions.none()  # show the names without pinging anyone
    messages = board.pack_lines([f"**Linked members ({len(lines)})**\n", *lines])
    await interaction.response.send_message(messages[0], allowed_mentions=none)
    for text in messages[1:]:  # plain channel messages: a followup would show as a reply to the first
        await interaction.channel.send(text, allowed_mentions=none)


@bot.tree.command(description="Your donation card: today, streak, this month and your rank.")
@app_commands.guild_only()
async def me(interaction: discord.Interaction):
    row = db.get_guild(bot.db, interaction.guild_id)
    if not row:
        return await interaction.response.send_message("Bot is not set up yet.", ephemeral=True)
    ign = db.get_ign(bot.db, interaction.guild_id, interaction.user.id)
    if not ign:
        return await interaction.response.send_message(
            "You are not linked yet. Ask for a link with /link_request.", ephemeral=True)
    card = board.member_card(bot.db, interaction.guild_id, tracked_members(interaction.guild, row["member_role_id"]),
                             interaction.user.id, datetime.now(timezone.utc))
    user = interaction.user
    joined = f" · Member since <t:{int(user.joined_at.timestamp())}:D>" if getattr(user, "joined_at", None) else ""
    embed = discord.Embed(title=f"Donation card · {discord.utils.escape_markdown(ign)}",
                          description=f"{user.mention}{joined}")
    embed.set_author(name=user.display_name, icon_url=user.display_avatar.url)
    embed.set_thumbnail(url=user.display_avatar.url)

    reset = f"<t:{board.next_reset_ts()}:R>"
    embed.add_field(name="Today", value="**Donated**" if card["today"] else f"**Not yet**\nResets {reset}")
    badge = board.badge(card["streak"])
    embed.add_field(name="Current streak", value=f"**{card['streak']}** days" + (f"\n{badge}" if badge else ""))
    embed.add_field(name="Best streak", value=f"**{card['best']}** days")

    month_pct = round(100 * card["month_days"] / card["month_elapsed"])
    embed.add_field(name="This month (UTC)",
                    value=f"**{card['month_days']}** of {card['month_elapsed']} days\n"
                          f"`{board.bar(card['month_days'], card['month_elapsed'])}` {month_pct}%")
    embed.add_field(name="Coins this month", value=f"**{card['month_days'] * DONATION_AMOUNT:,}**")
    embed.add_field(name="Rank this month", value=f"**#{card['month_rank']}** of {card['members']}")

    embed.add_field(name="All time", value=f"**{card['all_days']}** days")
    embed.add_field(name="Coins all time", value=f"**{card['all_days'] * DONATION_AMOUNT:,}**")
    embed.add_field(name="Rank all time", value=f"**#{card['all_rank']}** of {card['members']}")

    strip = "".join("█" if done else "░" for done in card["last_14"])
    embed.add_field(name="Last 14 days", value=f"`{strip}`\n14 days ago, left · today, right", inline=False)

    if card["boss_total"]:
        embed.add_field(name="Guild boss this month",
                        value=f"**{card['boss_attended']}** of {card['boss_total']} attended\n"
                              f"`{board.bar(card['boss_attended'], card['boss_total'])}` "
                              f"{round(100 * card['boss_attended'] / card['boss_total'])}%")
    embed.add_field(name="Guild boss streak", value=f"**{card['boss_streak']}** bosses in a row")
    embed.add_field(name="Last donation", value=f"<t:{card['last_ts']}:R>" if card["last_ts"] else "None yet")
    if card["next_badge"]:
        to_go, name = card["next_badge"]
        embed.add_field(name="Next badge", value=f"{name} in **{to_go}** day(s)")
    await interaction.response.send_message(embed=brand(embed, footer="Days and ranks use the UTC day"),
                                            ephemeral=True)


@bot.tree.command(description="Remove a link. Staff can unlink other members.")
@app_commands.guild_only()
@app_commands.describe(member="Staff can also paste the user ID of someone who left the server")
async def unlink(interaction: discord.Interaction, member: discord.User | None = None):
    target = member or interaction.user  # User, not Member: a member who left keeps their name taken
    if target.id != interaction.user.id and not is_staff(interaction):
        msg = "Only staff can unlink other members."
    elif db.unlink(bot.db, interaction.guild_id, target.id):
        msg = f"Unlinked {target.display_name}."
        await audit(interaction, f"unlinked {target.mention}.")
    else:
        msg = f"{target.display_name} has no link."
    await interaction.response.send_message(msg, ephemeral=True)


@bot.tree.command(name="link_member", description="Staff: link a member to an in-game name from the roster.")
@app_commands.guild_only()
@app_commands.describe(member="The member to link", ingame_username="Pick the in-game name from the list")
@app_commands.autocomplete(ingame_username=link_autocomplete)
async def link_member(interaction: discord.Interaction, member: discord.Member, ingame_username: str):
    if not await require_staff(interaction):
        return
    try:
        name = db.link(bot.db, interaction.guild_id, member.id, ingame_username)
    except db.NotInRoster:
        msg = "That name is not on the roster. Add it with /roster add first."
    except db.AlreadyLinked:
        msg = f"{member.display_name} is already linked. Use /unlink first."
    except db.IgnTaken:
        msg = "That in-game name is already taken."
    else:
        msg = f"Linked {member.display_name} to **{name}**."
        await audit(interaction, f"linked {member.mention} to **{discord.utils.escape_markdown(name)}**.")
    await interaction.response.send_message(msg, ephemeral=True)


@bot.tree.command(description="Staff: show who donated and who has not on a day (UTC).")
@app_commands.guild_only()
@app_commands.describe(date="Day as YYYY-MM-DD. Default: today (UTC).")
async def history(interaction: discord.Interaction, date: str | None = None):
    if not await require_staff(interaction):
        return
    try:
        day = datetime.strptime(date, "%Y-%m-%d").date().isoformat() if date else db.today_utc()
    except ValueError:
        return await interaction.response.send_message("Use the date as YYYY-MM-DD, e.g. 2026-10-21.",
                                                       ephemeral=True)
    row = db.get_guild(bot.db, interaction.guild_id)
    if not row:
        return await interaction.response.send_message("Run /setup first.", ephemeral=True)
    donated, waiting = board.day_lists(bot.db, interaction.guild_id,
                                       tracked_members(interaction.guild, row["member_role_id"]), day)
    view = HistoryView((f"Donations {day} (UTC)", "Donated", "Not donated", "Not donated yet"), donated, waiting)
    await interaction.response.send_message(embed=view.embed(), view=view, ephemeral=True)


@bot.tree.command(name="log", description="Staff: log a donation for a member.")
@app_commands.guild_only()
@app_commands.choices(day=[app_commands.Choice(name="today", value="today"),
                           app_commands.Choice(name="yesterday", value="yesterday")])
async def log_donation(interaction: discord.Interaction, member: discord.Member,
                       day: app_commands.Choice[str] | None = None):
    if not await require_staff(interaction):
        return
    ign = db.get_ign(bot.db, interaction.guild_id, member.id)
    if not ign:
        return await interaction.response.send_message("Member has no linked IGN.", ephemeral=True)
    date = db.yesterday_utc() if day and day.value == "yesterday" else db.today_utc()
    try:
        db.record(bot.db, interaction.guild_id, member.id, member.display_name, ign, date,
                  logged_by=interaction.user.id)
    except db.AlreadyDonated:
        return await interaction.response.send_message(
            "Member already has an approved donation that day.", ephemeral=True)
    await interaction.response.send_message(
        f"Logged {DONATION_AMOUNT:,} for **{ign}** on {date}.", ephemeral=True)
    await audit(interaction, f"logged a donation for {member.mention} on {date}.")


@bot.tree.command(name="log_many", description="Staff: log a donation for several members at once.")
@app_commands.guild_only()
@app_commands.describe(members="@mention every member, e.g. @Ann @Bob @Cid", day="Default: today (UTC)")
@app_commands.choices(day=[app_commands.Choice(name="today", value="today"),
                           app_commands.Choice(name="yesterday", value="yesterday")])
async def log_many(interaction: discord.Interaction, members: str, day: app_commands.Choice[str] | None = None):
    if not await require_staff(interaction):
        return
    ids = board.member_ids(members)
    if not ids:
        return await interaction.response.send_message("@mention the members to log, e.g. @Ann @Bob.", ephemeral=True)
    date = db.yesterday_utc() if day and day.value == "yesterday" else db.today_utc()
    logged, already, unlinked = [], [], []
    for i in ids:
        member = interaction.guild.get_member(i)
        name = member.display_name if member else str(i)
        ign = db.get_ign(bot.db, interaction.guild_id, i)
        if not ign:
            unlinked.append(name)
            continue
        try:
            db.record(bot.db, interaction.guild_id, i, name, ign, date, logged_by=interaction.user.id)
            logged.append(ign)
        except db.AlreadyDonated:
            already.append(ign)
    esc = discord.utils.escape_markdown
    lines = [f"Logged {DONATION_AMOUNT:,} for {len(logged)} member(s) on {date}."]
    if logged:
        lines.append(f"Logged: {board.join_capped([esc(n) for n in logged], 500)}")
    if already:
        lines.append(f"Already donated that day: {board.join_capped([esc(n) for n in already], 500)}")
    if unlinked:
        lines.append(f"Not logged (no linked IGN): {board.join_capped([esc(n) for n in unlinked], 500)}")
    await interaction.response.send_message("\n".join(lines), ephemeral=True)
    if logged:
        await audit(interaction, f"logged donations for {len(logged)} member(s) on {date} with /log_many: "
                                 f"{board.join_capped([esc(n) for n in logged], 1500)}.")


@bot.tree.command(name="log_all", description="Staff: log today's donation for every linked member but one.")
@app_commands.guild_only()
@app_commands.describe(skip="The member who did not donate today")
async def log_all(interaction: discord.Interaction, skip: discord.Member):
    if not await require_staff(interaction):
        return
    row = db.get_guild(bot.db, interaction.guild_id)
    if not row:
        return await interaction.response.send_message("Run /setup first.", ephemeral=True)
    date = db.today_utc()
    tracked = tracked_members(interaction.guild, row["member_role_id"])
    to_log, unlinked = board.log_all_plan(bot.db, interaction.guild_id, tracked, date, skip.id)
    if not to_log:
        return await interaction.response.send_message(
            f"Nobody to log on {date}: every linked member but {skip.display_name} has already donated.", ephemeral=True)
    not_linked = f"\nNot logged (no linked IGN): {board.join_capped(unlinked, 1000)}" if unlinked else ""

    async def run(button_interaction):
        logged, _ = board.log_all(bot.db, interaction.guild_id, tracked, date, skip.id, interaction.user.id)
        await button_interaction.response.edit_message(
            content=f"Logged {DONATION_AMOUNT:,} for {len(logged)} member(s) on {date}. Skipped "
                    f"{skip.display_name}.{not_linked}\nPress Undo within 15 minutes to take them all back.",
            view=UndoLogAllView([i for i, _ in logged]))
        await audit(interaction, f"logged donations for {len(logged)} member(s) on {date} with /log_all, "
                                 f"skipping {skip.mention}.")

    await interaction.response.send_message(
        f"Log {DONATION_AMOUNT:,} for **{len(to_log)}** member(s) on {date}, skipping {skip.display_name}?"
        f"{not_linked}", view=ConfirmView(run), ephemeral=True)


@bot.tree.command(description="Staff: cancel a donation by its id (see /export).")
@app_commands.guild_only()
async def undo(interaction: discord.Interaction, submission_id: int):
    if not await require_staff(interaction):
        return
    row = db.get_submission(bot.db, interaction.guild_id, submission_id)
    if not row or row["status"] != "approved":
        return await interaction.response.send_message("That donation is not active.", ephemeral=True)

    async def run(button_interaction):
        if db.void(bot.db, interaction.guild_id, submission_id, interaction.user.id):
            await button_interaction.response.edit_message(content=f"Voided donation #{submission_id}.", view=None)
            await audit(interaction, f"voided donation #{submission_id} (**{discord.utils.escape_markdown(row['ign'])}**"
                                     f", {row['day_utc']}).")
        else:
            await button_interaction.response.edit_message(content="That donation is not active.", view=None)

    await interaction.response.send_message(
        f"Void donation #{submission_id} for **{discord.utils.escape_markdown(row['ign'])}** on {row['day_utc']}?",
        view=ConfirmView(run), ephemeral=True)


@bot.tree.command(description="Staff: post the offline notice and shut the bot down.")
@app_commands.guild_only()
async def stop(interaction: discord.Interaction):
    if not await require_staff(interaction):
        return

    async def run(button_interaction):
        await button_interaction.response.edit_message(
            content="Stopping. Start the bot again to bring the button back.", view=None)
        await audit(interaction, "stopped the bot with /stop.")
        await bot.close()

    await interaction.response.send_message(
        "Stop the bot? Members cannot log donations or attendance until someone starts it again on the server.",
        view=ConfirmView(run), ephemeral=True)


@bot.tree.command(name="backup", description="Staff: download a full copy of the bot's database.")
@app_commands.guild_only()
async def backup_db(interaction: discord.Interaction):
    if not await require_staff(interaction):
        return
    data = bot.db.serialize()  # consistent snapshot, even while the bot writes
    name = f"donations-{datetime.now(timezone.utc):%Y-%m-%d-%H%M}.db"
    await interaction.response.send_message(
        "Full database copy. To restore it, upload it to the server as `donations.db`, with the bot stopped.",
        file=discord.File(io.BytesIO(data), name), ephemeral=True)
    await audit(interaction, "downloaded a database backup.")


@bot.tree.command(description="Staff: download all donations as a CSV file.")
@app_commands.guild_only()
async def export(interaction: discord.Interaction):
    if not await require_staff(interaction):
        return
    data = db.export_csv(bot.db, interaction.guild_id).encode("utf-8-sig")  # BOM: Excel reads UTF-8
    await interaction.response.send_message(
        file=discord.File(io.BytesIO(data), "donations.csv"), ephemeral=True)


boss_group = app_commands.Group(name="boss", description="Guild boss time polls.", guild_only=True)


@boss_group.command(name="start", description="Staff: start the boss time polls in a channel.")
async def boss_start(interaction: discord.Interaction, channel: discord.TextChannel):
    if not await require_staff(interaction):
        return
    if not db.get_guild(bot.db, interaction.guild_id):
        return await interaction.response.send_message("Run /setup first.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    db.save_guild(bot.db, interaction.guild_id, boss_channel_id=channel.id)
    try:
        await boss_tick(interaction.guild, db.get_guild(bot.db, interaction.guild_id),
                        datetime.now(timezone.utc))
    except discord.HTTPException as e:
        return await interaction.followup.send(
            f"Could not post in {channel.mention}: {e}\nGive the bot View Channel, Send Messages, "
            "Add Reactions and Read Message History there, then run /boss start again.", ephemeral=True)
    await interaction.followup.send(f"Boss polls started in {channel.mention}.", ephemeral=True)


@boss_group.command(name="stop", description="Staff: stop the boss time polls.")
async def boss_stop(interaction: discord.Interaction):
    if not await require_staff(interaction):
        return
    db.save_guild(bot.db, interaction.guild_id, boss_channel_id=None)
    await interaction.response.send_message("Boss polls stopped.", ephemeral=True)


bot.tree.add_command(boss_group)

data_group = app_commands.Group(name="data", description="The data channel.", guild_only=True)


@data_group.command(name="start", description="Staff: show donation data in a channel.")
async def data_start(interaction: discord.Interaction, channel: discord.TextChannel):
    if not await require_staff(interaction):
        return
    if not db.get_guild(bot.db, interaction.guild_id):
        return await interaction.response.send_message("Run /setup first.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    db.save_guild(bot.db, interaction.guild_id, data_channel_id=channel.id)
    error = await refresh_data(interaction.guild)
    if error:
        return await interaction.followup.send(
            f"Could not post in {channel.mention}: {error}\nGive the bot View Channel, Send Messages and "
            "Embed Links in that channel, then run /data start again.", ephemeral=True)
    await interaction.followup.send(f"Data posted in {channel.mention}.", ephemeral=True)


@data_group.command(name="stop", description="Staff: stop updating the data channel.")
async def data_stop(interaction: discord.Interaction):
    if not await require_staff(interaction):
        return
    db.save_guild(bot.db, interaction.guild_id, data_channel_id=None)
    await interaction.response.send_message("Data channel stopped. Its messages stay as they are.",
                                            ephemeral=True)


bot.tree.add_command(data_group)

attend_group = app_commands.Group(name="attendance", description="Guild boss attendance board.", guild_only=True)


@attend_group.command(name="start", description="Staff: post the boss attendance board in a channel.")
async def attendance_start(interaction: discord.Interaction, channel: discord.TextChannel):
    if not await require_staff(interaction):
        return
    old = db.get_guild(bot.db, interaction.guild_id)
    if not old:
        return await interaction.response.send_message("Run /setup first.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    try:
        message = await channel.send(embed=discord.Embed(description="Loading attendance..."), view=AttendView())
    except discord.HTTPException as e:
        return await interaction.followup.send(
            f"Could not post in {channel.mention}: {e}\nGive the bot View Channel, Send Messages "
            "and Embed Links there, then run /attendance start again.", ephemeral=True)
    old_channel = old["attend_message_id"] and interaction.guild.get_channel(old["attend_channel_id"] or 0)
    if old_channel:  # remove the board from an earlier start, or it lingers and never updates
        try:
            await old_channel.get_partial_message(old["attend_message_id"]).delete()
        except discord.HTTPException:
            pass
    db.save_guild(bot.db, interaction.guild_id, attend_channel_id=channel.id, attend_message_id=message.id)
    await interaction.followup.send(f"Attendance board posted in {channel.mention}.", ephemeral=True)


@attend_group.command(name="stop", description="Staff: stop the boss attendance board.")
async def attendance_stop(interaction: discord.Interaction):
    if not await require_staff(interaction):
        return
    db.save_guild(bot.db, interaction.guild_id, attend_channel_id=None)
    await interaction.response.send_message("Attendance board stopped. Its messages stay as they are.",
                                            ephemeral=True)


bot.tree.add_command(attend_group)

attend_data_group = app_commands.Group(name="attendance_data", description="The boss attendance data channel.",
                                       guild_only=True)


@attend_data_group.command(name="start", description="Staff: show boss attendance data in a channel.")
async def attendance_data_start(interaction: discord.Interaction, channel: discord.TextChannel):
    if not await require_staff(interaction):
        return
    if not db.get_guild(bot.db, interaction.guild_id):
        return await interaction.response.send_message("Run /setup first.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    db.save_guild(bot.db, interaction.guild_id, attend_data_channel_id=channel.id)
    error = await refresh_attend_data(interaction.guild)
    if error:
        return await interaction.followup.send(
            f"Could not post in {channel.mention}: {error}\nGive the bot View Channel, Send Messages and "
            "Embed Links in that channel, then run /attendance_data start again.", ephemeral=True)
    await interaction.followup.send(f"Attendance data posted in {channel.mention}.", ephemeral=True)


@attend_data_group.command(name="stop", description="Staff: stop updating the attendance data channel.")
async def attendance_data_stop(interaction: discord.Interaction):
    if not await require_staff(interaction):
        return
    db.save_guild(bot.db, interaction.guild_id, attend_data_channel_id=None)
    await interaction.response.send_message("Attendance data stopped. Its messages stay as they are.",
                                            ephemeral=True)


bot.tree.add_command(attend_data_group)

staff_group = app_commands.Group(name="staff_channel", description="The private staff channel: audit log, "
                                 "reports and link requests.", guild_only=True)


@staff_group.command(name="start", description="Staff: send the audit log, reports and link requests to a channel.")
async def staff_start(interaction: discord.Interaction, channel: discord.TextChannel):
    if not await require_staff(interaction):
        return
    if not db.get_guild(bot.db, interaction.guild_id):
        return await interaction.response.send_message("Run /setup first.", ephemeral=True)
    try:
        await channel.send("📝 Staff channel set. Staff actions, reports and link requests show up here.")
    except discord.HTTPException as e:
        return await interaction.response.send_message(
            f"Could not post in {channel.mention}: {e}\nGive the bot View Channel, Send Messages, Embed Links "
            "and Attach Files there, then run /staff_channel start again.", ephemeral=True)
    db.save_guild(bot.db, interaction.guild_id, staff_channel_id=channel.id)
    await interaction.response.send_message(f"Staff channel set to {channel.mention}. Keep it private to staff.",
                                            ephemeral=True)


@staff_group.command(name="stop", description="Staff: stop posting to the staff channel.")
async def staff_stop(interaction: discord.Interaction):
    if not await require_staff(interaction):
        return
    db.save_guild(bot.db, interaction.guild_id, staff_channel_id=None)
    await interaction.response.send_message("Staff channel stopped.", ephemeral=True)


bot.tree.add_command(staff_group)


def parse_day(date):
    """YYYY-MM-DD, default today (UTC). Raises ValueError."""
    return datetime.strptime(date, "%Y-%m-%d").date().isoformat() if date else db.today_utc()


BAD_DATE = "Use the date as YYYY-MM-DD, e.g. 2026-10-21."


async def boss_autocomplete(interaction: discord.Interaction, current: str):
    """Bosses that started on the day typed in `date` (default today), as '14:00 UTC'."""
    try:
        day = parse_day(interaction.namespace.date)
    except ValueError:
        return []
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    times = [b["boss_time"] for b in db.bosses_for_day(bot.db, interaction.guild_id, day) if b["boss_time"] <= now]
    return [app_commands.Choice(name=f"{t[11:16]} UTC", value=t) for t in times if current in t[11:16]][:25]


def find_boss(guild_id, day, boss_time):
    """The chosen boss of that day, or the day's latest boss that has started. None if there is none."""
    if boss_time:
        row = db.get_boss(bot.db, guild_id, boss_time)
        return row if row and row["day_utc"] == day else None
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    started = [b for b in db.bosses_for_day(bot.db, guild_id, day) if b["boss_time"] <= now]
    return started[-1] if started else None


@bot.tree.command(name="boss_attendance", description="Staff: show who attended a guild boss and who did not.")
@app_commands.guild_only()
@app_commands.describe(date="Day as YYYY-MM-DD. Default: today (UTC).",
                       boss="Boss time. Default: the day's latest boss.")
@app_commands.autocomplete(boss=boss_autocomplete)
async def boss_attendance(interaction: discord.Interaction, date: str | None = None, boss: str | None = None):
    if not await require_staff(interaction):
        return
    try:
        day = parse_day(date)
    except ValueError:
        return await interaction.response.send_message(BAD_DATE, ephemeral=True)
    row = db.get_guild(bot.db, interaction.guild_id)
    if not row:
        return await interaction.response.send_message("Run /setup first.", ephemeral=True)
    found = find_boss(interaction.guild_id, day, boss)
    if not found:
        return await interaction.response.send_message(f"No guild boss found on {day}.", ephemeral=True)
    attended, waiting = board.boss_lists(bot.db, interaction.guild_id,
                                         tracked_members(interaction.guild, row["member_role_id"]), found["id"])
    labels = (f"Guild boss {day} {found['boss_time'][11:16]} UTC", "Attended", "Not attended", "Not attended")
    view = HistoryView(labels, attended, waiting)
    await interaction.response.send_message(embed=view.embed(), view=view, ephemeral=True)


@bot.tree.command(name="boss_member", description="Staff: how many guild bosses a member attended on a day.")
@app_commands.guild_only()
@app_commands.describe(date="Day as YYYY-MM-DD. Default: today (UTC).")
async def boss_member(interaction: discord.Interaction, member: discord.Member, date: str | None = None):
    if not await require_staff(interaction):
        return
    try:
        day = parse_day(date)
    except ValueError:
        return await interaction.response.send_message(BAD_DATE, ephemeral=True)
    ign = db.get_ign(bot.db, interaction.guild_id, member.id)
    if not ign:
        return await interaction.response.send_message("Member has no linked IGN.", ephemeral=True)
    bosses = board.member_day(bot.db, interaction.guild_id, member.id, day, datetime.now(timezone.utc))
    lines = [f"**{discord.utils.escape_markdown(ign)}**: {sum(a for _, a in bosses)} of {len(bosses)} "
             f"guild bosses on {day} (UTC)"]
    lines += [f"<t:{ts}:t> · {'attended' if attended else 'missed'}" for ts, attended in bosses]
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


@bot.tree.command(name="boss_add", description="Staff: mark a member as attended for a guild boss.")
@app_commands.guild_only()
@app_commands.describe(date="Day as YYYY-MM-DD. Default: today (UTC).",
                       boss="Boss time. Default: the day's latest boss.")
@app_commands.autocomplete(boss=boss_autocomplete)
async def boss_add(interaction: discord.Interaction, member: discord.Member, date: str | None = None,
                   boss: str | None = None):
    if not await require_staff(interaction):
        return
    try:
        day = parse_day(date)
    except ValueError:
        return await interaction.response.send_message(BAD_DATE, ephemeral=True)
    ign = db.get_ign(bot.db, interaction.guild_id, member.id)
    if not ign:
        return await interaction.response.send_message("Member has no linked IGN.", ephemeral=True)
    found = find_boss(interaction.guild_id, day, boss)
    if not found:
        return await interaction.response.send_message(f"No guild boss found on {day}.", ephemeral=True)
    try:
        db.attend(bot.db, found["id"], member.id, member.display_name, ign, logged_by=interaction.user.id)
    except db.AlreadyAttended:
        return await interaction.response.send_message("Member is already logged for that boss.", ephemeral=True)
    await interaction.response.send_message(
        f"Logged **{discord.utils.escape_markdown(ign)}** for the boss at {day} {found['boss_time'][11:16]} UTC.",
        ephemeral=True)
    await audit(interaction, f"marked {member.mention} as attended for the boss at {day} {found['boss_time'][11:16]} UTC.")


@bot.tree.command(name="boss_remove", description="Staff: remove a member's attendance for a guild boss.")
@app_commands.guild_only()
@app_commands.describe(member="Staff can also paste the user ID of someone who left the server",
                       date="Day as YYYY-MM-DD. Default: today (UTC).",
                       boss="Boss time. Default: the day's latest boss.")
@app_commands.autocomplete(boss=boss_autocomplete)
async def boss_remove(interaction: discord.Interaction, member: discord.User, date: str | None = None,
                      boss: str | None = None):
    if not await require_staff(interaction):
        return
    try:
        day = parse_day(date)
    except ValueError:
        return await interaction.response.send_message(BAD_DATE, ephemeral=True)
    found = find_boss(interaction.guild_id, day, boss)
    if not found:
        return await interaction.response.send_message(f"No guild boss found on {day}.", ephemeral=True)
    if not db.unattend(bot.db, found["id"], member.id):
        return await interaction.response.send_message("Member is not logged for that boss.", ephemeral=True)
    await interaction.response.send_message(
        f"Removed {member.display_name} from the boss at {day} {found['boss_time'][11:16]} UTC.", ephemeral=True)
    await audit(interaction, f"removed {member.mention} from the boss at {day} {found['boss_time'][11:16]} UTC.")


async def send_text(interaction, header, lines, filename):
    """Private reply. Sent as a file when too long for a message, like /roster list."""
    text = "\n".join(lines)
    if len(text) + len(header) > 1800:
        file = discord.File(io.BytesIO(text.encode("utf-8")), filename)
        return await interaction.response.send_message(header, file=file, ephemeral=True)
    await interaction.response.send_message(f"{header}\n{text}", ephemeral=True)


@bot.tree.command(name="boss_report", description="Staff: guild boss attendance per member over some days.")
@app_commands.guild_only()
@app_commands.rename(first="from", last="to")
@app_commands.describe(first="First day as YYYY-MM-DD. Default: 6 days before 'to'.",
                       last="Last day as YYYY-MM-DD. Default: today (UTC).")
async def boss_report(interaction: discord.Interaction, first: str | None = None, last: str | None = None):
    if not await require_staff(interaction):
        return
    try:
        last_day = datetime.strptime(parse_day(last), "%Y-%m-%d").date()
        first_day = datetime.strptime(first, "%Y-%m-%d").date() if first else last_day - timedelta(days=6)
    except ValueError:
        return await interaction.response.send_message(BAD_DATE, ephemeral=True)
    if not 0 <= (last_day - first_day).days <= 30:
        return await interaction.response.send_message(
            "'from' must be on or before 'to', and at most 31 days in all.", ephemeral=True)
    row = db.get_guild(bot.db, interaction.guild_id)
    if not row:
        return await interaction.response.send_message("Run /setup first.", ephemeral=True)
    total, rows = board.attend_report(bot.db, interaction.guild_id,
                                      tracked_members(interaction.guild, row["member_role_id"]),
                                      first_day.isoformat(), last_day.isoformat(), datetime.now(timezone.utc))
    header = f"Guild bosses {first_day} to {last_day} (UTC): {total} in all. Fewest first."
    await send_text(interaction, header, [f"{discord.utils.escape_markdown(ign)} · {n}/{total}" for ign, n in rows]
                    or ["No linked members."], "boss_report.txt")


@bot.tree.command(name="boss_missed", description="Staff: members who missed the last guild bosses in a row.")
@app_commands.guild_only()
@app_commands.describe(count="How many of the latest bosses. Default: 4 (about one day).")
async def boss_missed(interaction: discord.Interaction, count: app_commands.Range[int, 1, 50] = 4):
    if not await require_staff(interaction):
        return
    row = db.get_guild(bot.db, interaction.guild_id)
    if not row:
        return await interaction.response.send_message("Run /setup first.", ephemeral=True)
    checked, names = board.missed_in_row(bot.db, interaction.guild_id,
                                         tracked_members(interaction.guild, row["member_role_id"]),
                                         datetime.now(timezone.utc), count)
    if not checked:
        return await interaction.response.send_message("No guild bosses yet.", ephemeral=True)
    header = f"Missed all of the last {checked} guild bosses: {len(names)} member(s)."
    if checked < count:
        header += f" Only {checked} bosses so far."
    await send_text(interaction, header, [discord.utils.escape_markdown(n) for n in names] or ["Nobody."],
                    "boss_missed.txt")


@setup.error
async def on_setup_error(interaction: discord.Interaction, error):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("You need Manage Server.", ephemeral=True)
    else:
        raise error


if __name__ == "__main__":
    log_dir = Path(os.getenv("LOG_DIR", "logs"))
    log_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO, format="[%(asctime)s] [%(levelname)-7s] %(name)s: %(message)s",
        handlers=[logging.StreamHandler(),
                  logging.handlers.RotatingFileHandler(log_dir / "bot.log", maxBytes=1_000_000,
                                                       backupCount=3, encoding="utf-8")])
    # Setup errors exit with 0: restarting cannot fix them, so run_bot.ps1 must stop, not retry forever
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        log.error("DISCORD_TOKEN missing. Put it in .env.")
        raise SystemExit(0)
    try:
        bot.run(token, log_handler=None)
    except discord.LoginFailure:
        log.error("Discord refused the token. Reset it in the Developer Portal and update .env.")
        raise SystemExit(0)
    except discord.PrivilegedIntentsRequired:
        log.error("Turn on Server Members Intent in the Developer Portal (Bot tab), then start again.")
        raise SystemExit(0)
    except Exception:
        log.exception("Bot crashed")
        raise SystemExit(1)  # nonzero: run_bot.ps1 restarts it
