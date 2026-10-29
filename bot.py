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
from config import (BACKUP_HOUR_UTC, DATA_PAGE, IGN_MAX, IGN_MIN, LOA_COOLDOWN_DAYS, LOA_MAX_DAYS,
                    MEMBER_ROLE_NAME, RECENT_DONORS, REMINDER_MINUTES, VERSION, XP_CAP)

log = logging.getLogger("evil")
STARTED = datetime.now(timezone.utc)

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


def loa_block(guild_id, member_id, day, who="You are"):
    """Refusal text if the member is on LOA on `day` (ISO date), else None. Nothing is logged for an LOA day."""
    last = db.loa_active_on(bot.db, guild_id, day).get(member_id)
    return f"{who} on LOA until the end of {last} (UTC). Nothing is logged on LOA days." if last else None


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
                    value=f"Donated: {data['yesterday_count']}",
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



# --- data channel: a header message plus Donated / Not donated / Not linked, 15 rows per page ---

DATA_SECTIONS = {
    "donated": ("Donated today", ("Discord name", "In-game name", "Donated at")),
    "not_donated": ("Not donated yet", ("Discord name", "In-game name")),
    "on_loa": ("On LOA", ("In-game name", "Last LOA day")),
    "not_linked": ("Not linked", ("In-game name",)),
}
ATTEND_SECTIONS = {  # the attendance data channel, same layout
    "attended": ("Attended", ("Discord name", "In-game name", "Logged at")),
    "not_attended": ("Not attended", ("Discord name", "In-game name")),
    "attend_on_loa": ("On LOA", ("In-game name", "Last LOA day")),
    "today_totals": ("Bosses today (UTC day)", ("In-game name", "Attended")),
}
ALWAYS_OPEN = {"attend_on_loa", "today_totals"}  # attendance sections that show data between bosses too
data_pages = {}  # (guild_id, section) -> page shown. Resets to page 1 after a restart.


def data_header_embed(sections, guild):
    counts = {key: len(sections[key]) for key in DATA_SECTIONS}
    return brand(discord.Embed(
        title="Data",
        description=f"{db.today_utc()} (UTC) · resets <t:{board.next_reset_ts()}:t> (your local time)\n"
                    f"Donated: **{counts['donated']}** · Not donated: **{counts['not_donated']}** · "
                    f"On LOA: **{counts['on_loa']}** · Not linked: **{counts['not_linked']}**"), guild)


def closed_text(next_ts):
    """Shown on the attendance board and data channel between bosses."""
    return "**Logging is closed.**\n" + (f"It opens at the next guild boss: <t:{next_ts}:t> (<t:{next_ts}:R>)."
                                          if next_ts else "It opens when the next guild boss starts.")


def attend_header_embed(info, sections, guild):
    boss_ts, close_ts, _ = info
    text = f"Boss at <t:{boss_ts}:t> · logging open until <t:{close_ts}:t>"
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
        now = datetime.now(timezone.utc)
        info, sections = board.attend_data_sections(bot.db, guild.id, tracked_members(guild, row["member_role_id"]),
                                                    now)
        is_open = bool(info) and info[2] == "open"
        if is_open:
            embeds = [attend_header_embed(info, sections, guild)] + [data_section_embed(guild.id, key, sections[key])
                                                                     for key in ATTEND_SECTIONS]
        else:  # between bosses: the same messages, closed, with no page buttons
            upcoming = db.next_boss(bot.db, guild.id, now.isoformat(timespec="seconds"))
            text = closed_text(upcoming and int(iso_utc(upcoming["boss_time"]).timestamp()))
            embeds = [brand(discord.Embed(title="Boss attendance data", description=text), guild)]
            embeds += [data_section_embed(guild.id, key, sections[key]) if key in ALWAYS_OPEN else
                       brand(discord.Embed(title=ATTEND_SECTIONS[key][0], description="Closed until the next guild boss."))
                       for key in ATTEND_SECTIONS]
        return await show_sections(guild, channel, row["attend_data_message_ids"], "attend_data_message_ids",
                                   embeds, ATTEND_SECTIONS, buttons=None if is_open else ALWAYS_OPEN)


async def show_sections(guild, channel, ids_json, ids_field, embeds, keys, buttons=None):
    """Edit a header and its section messages in place. If one was deleted, post them all again.
    buttons = the section keys that get page buttons (default: all). Returns an error text if Discord
    refused, else None."""
    ids = json.loads(ids_json or "[]")
    views = [None] + [DataView(key) if buttons is None or key in buttons else None for key in keys]
    try:
        if len(ids) == len(embeds):
            for message_id, embed, view in zip(ids, embeds, views):
                await channel.get_partial_message(message_id).edit(embed=embed, view=view)
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
        posted = []
        for embed, view in zip(embeds, views):
            posted.append(await channel.send(embed=embed, view=view))
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
        if text := loa_block(interaction.guild_id, interaction.user.id, db.today_utc()):
            return await interaction.response.send_message(text, ephemeral=True)
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
            "Logged your donation for today.", ephemeral=True)
        await refresh_board(interaction.guild)


# --- attendance board: one message with the I attended button, kept at the bottom like the board ---

_attend_lock = asyncio.Lock()
_attend_seen = {}  # guild_id -> (boss id, state, day) the board last showed


def attend_embed(data, guild, next_ts=None):
    """Open: the boss, who attended and who did not. Closed: only when logging opens again."""
    embed = discord.Embed(title="Guild boss attendance")
    today_value = "\n".join(f"<t:{ts}:t> · {n} attended" for ts, n in data["today"]) if data else ""
    if not data or data["state"] != "open":
        embed.description = closed_text(next_ts)
        if today_value:
            embed.add_field(name="Today's bosses (UTC day)", value=today_value, inline=False)
        return brand(embed, guild)
    embed.description = (f"Boss at <t:{data['boss_ts']}:t>: logging open until <t:{data['close_ts']}:t> "
                         f"(<t:{data['close_ts']}:R>).")
    embed.add_field(name=f"Attended ({len(data['attended'])})",
                    value=board.join_capped([discord.utils.escape_markdown(n) for n in data["attended"]], sep="\n")
                    or "Nobody yet.", inline=False)
    embed.add_field(name=f"Not attended ({len(data['waiting'])})",
                    value=board.join_capped([discord.utils.escape_markdown(n) for n in data["waiting"]], sep="\n")
                    or "Everyone attended.", inline=False)
    embed.add_field(name="Today's bosses (UTC day)",
                    value=today_value or "None yet.",
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
        now = datetime.now(timezone.utc)
        data = board.attend_board_data(bot.db, guild.id, tracked_members(guild, row["member_role_id"]), now)
        upcoming = db.next_boss(bot.db, guild.id, now.isoformat(timespec="seconds"))
        embed = attend_embed(data, guild, upcoming and int(iso_utc(upcoming["boss_time"]).timestamp()))
        view = AttendView() if data and data["state"] == "open" else None  # no button while closed
        old = channel.get_partial_message(row["attend_message_id"])
        try:
            if channel.last_message_id == old.id:
                await old.edit(embed=embed, view=view)
            else:
                try:
                    await old.delete()
                except discord.NotFound:
                    pass
                new = await channel.send(embed=embed, view=view)
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
        if text := loa_block(interaction.guild_id, interaction.user.id, db.today_utc()):
            return await interaction.response.send_message(text, ephemeral=True)
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
        await refresh_attendance(interaction.guild)


async def attend_tick(guild, row, now):
    """Ping when a boss's logging opens. Refresh the board when the boss, its state, the next boss or the day
    changes."""
    now_text = now.isoformat(timespec="seconds")
    current, upcoming = db.started_boss(bot.db, guild.id, now_text), db.next_boss(bot.db, guild.id, now_text)
    state = current and boss.attend_state(iso_utc(current["boss_time"]), now)
    t = current and iso_utc(current["boss_time"])
    # claimed before sending, so a restart never double-pings. Bot back after the window: no ping
    if state == "open" and db.claim_boss_open(bot.db, current["id"]):
        channel = guild.get_channel(row["attend_channel_id"])
        role = guild.get_role(row["member_role_id"])
        if channel and role:
            warn_if_role_not_pinged(await channel.send(boss.attend_open_text(role.mention, t)), role.id)
            log.info("Attendance open for %s in guild %s", current["boss_time"], guild.id)
    key = (current and current["id"], state, now.date(), upcoming and upcoming["id"])
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
        await refresh_board(interaction.guild)


class HistoryView(discord.ui.View):
    """One UTC day: donated, then not donated, 15 rows per page with Previous / Next buttons.
    labels = (title, done, not done, not done field); also used for one boss's attendance.
    loa = [(in-game name, last LOA day)] shown last, under their own heading."""

    def __init__(self, labels, donated, waiting, loa=()):
        super().__init__(timeout=300)
        self.labels, self.page = labels, 0
        self.rows = ([("donated", f"{board.short(n)} · {board.short(i)} · <t:{t}:t>") for n, i, t in donated]
                     + [("waiting", discord.utils.escape_markdown(n)) for n in waiting]
                     + [("loa", f"{board.short(ign)} · last LOA day {last}") for ign, last in loa])
        self.counts = (len(donated), len(waiting), len(loa))
        self._update_buttons()

    def embed(self):
        chunk, self.page, pages = board.paginate(self.rows, self.page, DATA_PAGE)  # also clamps self.page
        donated = [text for kind, text in chunk if kind == "donated"]
        waiting = [text for kind, text in chunk if kind == "waiting"]
        loa = [text for kind, text in chunk if kind == "loa"]
        title, done, not_done, not_done_field = self.labels
        embed = discord.Embed(title=title,
                              description=f"{done}: **{self.counts[0]}** · {not_done}: **{self.counts[1]}**"
                                          + (f" · On LOA: **{self.counts[2]}**" if self.counts[2] else ""))
        if donated:
            embed.add_field(name=f"{done} ({self.counts[0]})", value="\n".join(donated), inline=False)
        if waiting:
            embed.add_field(name=f"{not_done_field} ({self.counts[1]})", value="\n".join(waiting), inline=False)
        if loa:
            embed.add_field(name=f"On LOA ({self.counts[2]})", value="\n".join(loa), inline=False)
        return brand(embed, footer=f"Page {self.page + 1}/{pages}")

    def _update_buttons(self):
        pages = board.paginate(self.rows, self.page, DATA_PAGE)[2]
        self.previous.disabled = self.page <= 0
        self.next.disabled = self.page >= pages - 1

    async def _show(self, interaction):
        embed = self.embed()
        self._update_buttons()
        await interaction.response.edit_message(embed=embed, view=self)

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
        self.add_view(LinkRequestView())
        self.add_view(XpPanelView())
        self.add_view(XpRequestView())
        self.add_view(LoaPanelView())
        self.add_view(LoaRequestView())
        for key in [*DATA_SECTIONS, *ATTEND_SECTIONS]:
            self.add_view(DataView(key))
        await self.tree.sync()
        log.info("Evil Donations v%s starting", VERSION)
        tick.start()

    async def on_member_remove(self, member):
        """A member who leaves is unlinked, so their in-game name is free again. Their history stays."""
        if db.leave_loa(self.db, member.guild.id, member.id, db.today_utc()):
            await refresh_loa_boards(member.guild)
        ign = db.get_ign(self.db, member.guild.id, member.id)
        if ign and db.unlink(self.db, member.guild.id, member.id):
            log.info("Unlinked %s (%s) after leaving guild %s", member.id, ign, member.guild.id)
            await staff_post(member.guild, f"{member.mention} ({discord.utils.escape_markdown(member.display_name)}) "
                                           f"left the server and was unlinked from **{discord.utils.escape_markdown(ign)}**.")
            await refresh_board(member.guild)

    async def on_member_join(self, member):
        """Ping each new person in the welcome channel and ask for their gear, playtime and level."""
        row = db.get_guild(self.db, member.guild.id)
        channel = row and row["welcome_channel_id"] and member.guild.get_channel(row["welcome_channel_id"])
        if not channel or member.bot:
            return
        try:
            await channel.send(f"Welcome {member.mention}. To join the guild, post screenshots of:\n"
                               "1. Your gear\n2. Your playtime\n3. Your level",
                               allowed_mentions=discord.AllowedMentions(users=[member]))
        except discord.HTTPException as e:
            log.warning("Welcome message failed in guild %s: %s", member.guild.id, e)

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


async def post_xp_ping(guild, row, now):
    """From 01:00 UTC: ping, in the XP channel, everyone under the cap yesterday, once per day."""
    day = (now.date() - timedelta(days=1)).isoformat()
    if now.hour < 1 or not db.claim_post(bot.db, guild.id, "xp_ping", day):
        return
    channel = guild.get_channel(row["xp_channel_id"])
    under = [(i, gain) for i, _, _, gain in xp_rows(guild, row, day) if gain is None or gain < XP_CAP]
    if not channel or not under:
        return
    for group in reminders.chunks(under, 40):
        await channel.send(f"Under {XP_CAP:,} XP on {day} (UTC):\n" + "\n".join(
            f"<@{i}> · " + (f"{gain:,} XP" if gain is not None else "not logged") for i, gain in group))
    log.info("XP ping posted for %s in guild %s", day, guild.id)
    await refresh_xp_panel(guild)  # the ping pushed the panel up: move it back to the bottom


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
    possible = len(rows) * report["days_in_month"] - sum(report["loa_days"].values())
    embed = discord.Embed(title=f"Monthly report · {title_month}", description=f"{len(rows)} linked members (UTC days)")
    embed.add_field(name="Donations", value=f"**{donated}** of {possible} member-days"
                    + (f" ({round(100 * donated / possible)}%)" if possible else ""))
    attended = sum(r[3] for r in rows)
    embed.add_field(name="Guild bosses", value=f"**{total}** bosses\n**{attended}** attendances"
                    + (f" ({round(100 * attended / (total * len(rows)))}%)" if total and rows else ""))
    lowest = sorted(rows, key=lambda r: (r[2], r[0].lower()))[:5]
    embed.add_field(name="Fewest donation days", inline=False,
                    value="\n".join(f"{esc(ign)} · {d} days" for ign, _, d, _ in lowest) or "No linked members.")
    csv_data = board.monthly_csv(report).encode("utf-8-sig")  # BOM: Excel reads UTF-8
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
            if row["loa_role_id"]:  # first: the role swap must come before any role ping
                await loa_tick(guild, row, today)
            if row["boss_channel_id"]:
                await boss_tick(guild, row, now)
            if row["attend_channel_id"]:
                await attend_tick(guild, row, now)
            if row["xp_channel_id"] and _xp_seen.get(guild.id) != today:  # new UTC day, or just started
                _xp_seen[guild.id] = today
                await refresh_xp_panel(guild)
            if row["xp_channel_id"]:
                await post_xp_ping(guild, row, now)
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


@bot.tree.command(description="Staff: post the donation panel and set the officer role (needs Manage Server).")
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
    await refresh_board(interaction.guild)


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
        await refresh_board(interaction.guild)


@roster_group.command(name="remove", description="Staff: remove a name from the list.")
async def roster_remove(interaction: discord.Interaction, name: str):
    if not await require_staff(interaction):
        return
    removed = db.roster_remove(bot.db, interaction.guild_id, name)
    msg = f"Removed **{name}**. Anyone already linked to it stays linked." if removed else "That name is not on the list."
    await interaction.response.send_message(msg, ephemeral=True)
    if removed:
        await audit(interaction, f"removed **{discord.utils.escape_markdown(name)}** from the roster.")
        await refresh_board(interaction.guild)


@roster_remove.autocomplete("name")
async def roster_remove_autocomplete(interaction: discord.Interaction, current: str):
    names = db.roster_names(bot.db, interaction.guild_id, current, limit=25)
    return [app_commands.Choice(name=n, value=n) for n in names]


class ListView(discord.ui.View):
    """A private list, DATA_PAGE lines per page, with Previous / Next buttons."""

    def __init__(self, title, description, lines):
        super().__init__(timeout=300)
        self.title, self.description, self.lines, self.page = title, description, lines, 0
        self._update_buttons()

    def embed(self):
        chunk, self.page, pages = board.paginate(self.lines, self.page, DATA_PAGE)
        embed = discord.Embed(title=self.title, description=self.description + "\n\n" + "\n".join(chunk))
        return brand(embed, footer=f"Page {self.page + 1}/{pages}")

    def _update_buttons(self):
        pages = board.paginate(self.lines, self.page, DATA_PAGE)[2]
        self.previous.disabled = self.page <= 0
        self.next.disabled = self.page >= pages - 1

    async def _show(self, interaction):
        embed = self.embed()
        self._update_buttons()
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="Previous")
    async def previous(self, interaction, button):
        self.page -= 1
        await self._show(interaction)

    @discord.ui.button(label="Next")
    async def next(self, interaction, button):
        self.page += 1
        await self._show(interaction)


@roster_group.command(name="list", description="Staff: show the roster names, who took each, and which are free.")
@app_commands.describe(search="Only names containing this text", show="Default: all names")
@app_commands.choices(show=[app_commands.Choice(name="all", value="all"), app_commands.Choice(name="free", value="free"),
                            app_commands.Choice(name="taken", value="taken")])
async def roster_list(interaction: discord.Interaction, search: str | None = None,
                      show: app_commands.Choice[str] | None = None):
    if not await require_staff(interaction):
        return
    names = db.roster_names(bot.db, interaction.guild_id, search or "")
    owners = {ign.lower(): i for i, ign in db.get_igns(bot.db, interaction.guild_id).items()}
    which = show.value if show else "all"
    lines = [f"**{discord.utils.escape_markdown(n)}** · " + (f"taken by <@{owners[n.lower()]}>" if n.lower() in owners
                                                             else "free")
             for n in names if which == "all" or (n.lower() in owners) == (which == "taken")]
    free = sum(n.lower() not in owners for n in names)
    if not lines:
        return await interaction.response.send_message("No roster names match." if names or search
                                                       else "The list is empty.", ephemeral=True)
    found = f" matching '{discord.utils.escape_markdown(search)}'" if search else ""
    view = ListView(f"Roster ({len(lines)})", f"{len(names)} names{found}: {len(names) - free} taken, {free} free.",
                    lines)
    await interaction.response.send_message(embed=view.embed(), view=view, ephemeral=True)


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
    embed.add_field(name="Today", value=f"**On LOA** until the end of {card['loa']}" if card["loa"] else
                    "**Donated**" if card["today"] else f"**Not yet**\nResets {reset}")
    badge = board.badge(card["streak"])
    embed.add_field(name="Current streak", value=f"**{card['streak']}** days" + (f"\n{badge}" if badge else ""))
    embed.add_field(name="Best streak", value=f"**{card['best']}** days")

    month_pct = round(100 * card["month_days"] / card["month_elapsed"]) if card["month_elapsed"] else 0
    embed.add_field(name="This month (UTC)",
                    value=f"**{card['month_days']}** of {card['month_elapsed']} days"
                          + (f" (+{card['month_loa']} on LOA)" if card["month_loa"] else "") + "\n"
                          f"`{board.bar(card['month_days'], card['month_elapsed'])}` {month_pct}%")
    embed.add_field(name="Rank this month", value=f"**#{card['month_rank']}** of {card['members']}")

    embed.add_field(name="All time", value=f"**{card['all_days']}** days")
    embed.add_field(name="Rank all time", value=f"**#{card['all_rank']}** of {card['members']}")

    strip = "".join("█" if done else "▒" if away else "░" for done, away in zip(card["last_14"], card["last_14_loa"]))
    embed.add_field(name="Last 14 days", value=f"`{strip}`\n14 days ago, left · today, right"
                    + (" · ▒ = on LOA" if "▒" in strip else ""), inline=False)

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
    if row["xp_channel_id"]:
        today = db.today_utc()
        week = [d for d in db.xp_week(bot.db, interaction.guild_id, user.id, today) if d["day"] <= today]
        totals = board.xp_week_totals(bot.db, interaction.guild_id,
                                      tracked_members(interaction.guild, row["member_role_id"]), today)
        mine = totals.get(user.id, 0)
        embed.add_field(name="XP this week", value=f"**{mine:,}** XP · resets <t:{board.next_week_ts()}:R>")
        embed.add_field(name="XP today", value=f"**{week[-1]['gain']:,}**" if week[-1]["gain"] is not None
                        else "**On LOA**" if card["loa"] else "Not logged")
        embed.add_field(name="XP rank this week", value=f"**#{1 + sum(t > mine for t in totals.values())}** of "
                        f"{len(totals)} · hit cap {sum(d['gain'] is not None and d['gain'] >= XP_CAP for d in week)}"
                        f" of {len(week)} days")
        cap_days = db.xp_cap_days(bot.db, interaction.guild_id, user.id)
        loa_days = db.loa_days(bot.db, interaction.guild_id).get(user.id, set())
        embed.add_field(name="XP cap streak", value=f"**{board.streak(cap_days, today, loa_days)}** days · best "
                        f"{board.best_streak(cap_days, loa_days)}")
        embed.add_field(name="XP by day (✓ = hit cap)", inline=False, value="```\n" + "\n".join(
            f"{datetime.strptime(d['day'], '%Y-%m-%d'):%a}  "
            + (f"{d['gain']:>6,}{' ✓' if d['gain'] >= XP_CAP else ''}" if d["gain"] is not None
               else "   LOA" if d["day"] in loa_days else "     -")
            for d in week) + "\n```")
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
        await interaction.response.send_message(f"Unlinked {target.display_name}.", ephemeral=True)
        await audit(interaction, f"unlinked {target.mention}.")
        return await refresh_board(interaction.guild)
    else:
        msg = f"{target.display_name} has no link."
    await interaction.response.send_message(msg, ephemeral=True)


class LinkRequestView(discord.ui.View):
    """Approve / Deny under a link request in the staff channel. The member and name are read back from the
    embed, so the buttons keep working after a restart."""

    def __init__(self):
        super().__init__(timeout=None)

    @staticmethod
    def request(message):
        fields = {f.name: f.value for f in message.embeds[0].fields}
        return int(fields["Member"].strip("<@!>")), fields["In-game name"]

    async def close(self, interaction, member_id, text, dm):
        embed = interaction.message.embeds[0]
        embed.add_field(name="Result", value=text, inline=False)
        await interaction.response.edit_message(embed=embed, view=None)
        member = interaction.guild.get_member(member_id)
        try:
            if member:
                await member.send(dm)
        except discord.HTTPException:  # DMs closed: the member sees it in /me instead
            pass

    @discord.ui.button(label="Approve", style=discord.ButtonStyle.green, custom_id="linkreq_approve")
    async def approve(self, interaction, button):
        if not await require_staff(interaction):
            return
        member_id, ign = self.request(interaction.message)
        try:
            name = db.link(bot.db, interaction.guild_id, member_id, ign)
        except db.NotInRoster:
            return await interaction.response.send_message("That name is no longer on the roster.", ephemeral=True)
        except db.AlreadyLinked:
            return await interaction.response.send_message("That member is already linked. Use /unlink first.",
                                                           ephemeral=True)
        except db.IgnTaken:
            return await interaction.response.send_message("That in-game name is already taken.", ephemeral=True)
        await self.close(interaction, member_id, f"Approved by {interaction.user.mention}",
                         f"Your link to **{name}** in {interaction.guild.name} was approved. You can log donations now.")
        await audit(interaction, f"approved the link request of <@{member_id}> for **{discord.utils.escape_markdown(name)}**.")
        await refresh_board(interaction.guild)

    @discord.ui.button(label="Deny", style=discord.ButtonStyle.secondary, custom_id="linkreq_deny")
    async def deny(self, interaction, button):
        if not await require_staff(interaction):
            return
        member_id, ign = self.request(interaction.message)
        await self.close(interaction, member_id, f"Denied by {interaction.user.mention}",
                         f"Your link request for **{ign}** in {interaction.guild.name} was denied. Ask an officer why.")
        await audit(interaction, f"denied the link request of <@{member_id}> for **{ign}**.")


@bot.tree.command(name="link_request", description="Ask staff to link you to your in-game name.")
@app_commands.guild_only()
@app_commands.describe(ingame_username="Pick your in-game name from the list")
@app_commands.autocomplete(ingame_username=link_autocomplete)
async def link_request(interaction: discord.Interaction, ingame_username: str):
    row = db.get_guild(bot.db, interaction.guild_id)
    if not row:
        return await interaction.response.send_message("Bot is not set up yet.", ephemeral=True)
    if not interaction.user.get_role(row["member_role_id"]):
        return await interaction.response.send_message(f"You need the {MEMBER_ROLE_NAME} role first.", ephemeral=True)
    current = db.get_ign(bot.db, interaction.guild_id, interaction.user.id)
    if current:
        return await interaction.response.send_message(f"You are already linked to **{current}**.", ephemeral=True)
    free = db.roster_names(bot.db, interaction.guild_id, ingame_username.strip(), only_free=True)
    name = next((n for n in free if n.lower() == ingame_username.strip().lower()), None)
    if not name:
        return await interaction.response.send_message(
            "That name is not on the roster or is already taken. Pick one from the list, or ask an officer.",
            ephemeral=True)
    channel = row["staff_channel_id"] and interaction.guild.get_channel(row["staff_channel_id"])
    if not channel:
        return await interaction.response.send_message("Link requests are not set up. Ask an officer to link you.",
                                                       ephemeral=True)
    embed = discord.Embed(title="Link request", description="Approve to link this member to the name.")
    embed.add_field(name="Member", value=interaction.user.mention)
    embed.add_field(name="In-game name", value=name)
    embed.set_author(name=interaction.user.display_name, icon_url=interaction.user.display_avatar.url)
    try:
        await channel.send(embed=brand(embed, footer="Requested"), view=LinkRequestView())
    except discord.HTTPException as e:
        log.warning("Link request post failed in guild %s: %s", interaction.guild_id, e)
        return await interaction.response.send_message("Could not reach the staff. Ask an officer to link you.",
                                                       ephemeral=True)
    await interaction.response.send_message(f"Request sent for **{name}**. You get a DM when staff answer.",
                                            ephemeral=True)


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
        await interaction.response.send_message(f"Linked {member.display_name} to **{name}**.", ephemeral=True)
        await audit(interaction, f"linked {member.mention} to **{discord.utils.escape_markdown(name)}**.")
        return await refresh_board(interaction.guild)
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
    donated, waiting, on_loa = board.day_lists(bot.db, interaction.guild_id,
                                               tracked_members(interaction.guild, row["member_role_id"]), day)
    view = HistoryView((f"Donations {day} (UTC)", "Donated", "Not donated", "Not donated yet"), donated, waiting,
                       on_loa)
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
    if text := loa_block(interaction.guild_id, member.id, date, f"{member.mention} is"):
        return await interaction.response.send_message(text, ephemeral=True)
    try:
        db.record(bot.db, interaction.guild_id, member.id, member.display_name, ign, date,
                  logged_by=interaction.user.id)
    except db.AlreadyDonated:
        return await interaction.response.send_message(
            "Member already has an approved donation that day.", ephemeral=True)
    await interaction.response.send_message(
        f"Logged a donation for **{ign}** on {date}.", ephemeral=True)
    await audit(interaction, f"logged a donation for {member.mention} on {date}.")
    await refresh_board(interaction.guild)


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
    logged, already, unlinked, on_loa = [], [], [], []
    for i in ids:
        member = interaction.guild.get_member(i)
        name = member.display_name if member else str(i)
        ign = db.get_ign(bot.db, interaction.guild_id, i)
        if not ign:
            unlinked.append(name)
            continue
        if loa_block(interaction.guild_id, i, date):
            on_loa.append(ign)
            continue
        try:
            db.record(bot.db, interaction.guild_id, i, name, ign, date, logged_by=interaction.user.id)
            logged.append(ign)
        except db.AlreadyDonated:
            already.append(ign)
    esc = discord.utils.escape_markdown
    lines = [f"Logged donations for {len(logged)} member(s) on {date}."]
    if logged:
        lines.append(f"Logged: {board.join_capped([esc(n) for n in logged], 500)}")
    if already:
        lines.append(f"Already donated that day: {board.join_capped([esc(n) for n in already], 500)}")
    if unlinked:
        lines.append(f"Not logged (no linked IGN): {board.join_capped([esc(n) for n in unlinked], 500)}")
    if on_loa:
        lines.append(f"Not logged (on LOA): {board.join_capped([esc(n) for n in on_loa], 500)}")
    await interaction.response.send_message("\n".join(lines), ephemeral=True)
    if logged:
        await audit(interaction, f"logged donations for {len(logged)} member(s) on {date} with /log_many: "
                                 f"{board.join_capped([esc(n) for n in logged], 1500)}.")
        await refresh_board(interaction.guild)


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
            content=f"Logged donations for {len(logged)} member(s) on {date}. Skipped "
                    f"{skip.display_name}.{not_linked}\nPress Undo within 15 minutes to take them all back.",
            view=UndoLogAllView([i for i, _ in logged]))
        await audit(interaction, f"logged donations for {len(logged)} member(s) on {date} with /log_all, "
                                 f"skipping {skip.mention}.")
        await refresh_board(interaction.guild)

    await interaction.response.send_message(
        f"Log a donation for **{len(to_log)}** member(s) on {date}, skipping {skip.display_name}?"
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
            await refresh_board(interaction.guild)
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
    await refresh_attendance(interaction.guild)
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

welcome_group = app_commands.Group(name="welcome", description="Greet new people and ask for their gear.",
                                   guild_only=True)


@welcome_group.command(name="start", description="Staff: ping each new person in a channel, asking for gear, "
                                                 "playtime and level screenshots.")
async def welcome_start(interaction: discord.Interaction, channel: discord.TextChannel):
    if not await require_staff(interaction):
        return
    if not db.get_guild(bot.db, interaction.guild_id):
        return await interaction.response.send_message("Run /setup first.", ephemeral=True)
    perms = channel.permissions_for(interaction.guild.me)
    if not (perms.view_channel and perms.send_messages):
        return await interaction.response.send_message(
            f"I cannot post in {channel.mention}. Give the bot View Channel and Send Messages there, then run "
            "/welcome start again.", ephemeral=True)
    db.save_guild(bot.db, interaction.guild_id, welcome_channel_id=channel.id)
    await interaction.response.send_message(
        f"New people get pinged in {channel.mention}. Make sure they can see and post in it.", ephemeral=True)
    await audit(interaction, f"set the welcome channel to {channel.mention}.")


@welcome_group.command(name="stop", description="Staff: stop greeting new people.")
async def welcome_stop(interaction: discord.Interaction):
    if not await require_staff(interaction):
        return
    db.save_guild(bot.db, interaction.guild_id, welcome_channel_id=None)
    await interaction.response.send_message("Welcome messages stopped.", ephemeral=True)
    await audit(interaction, "stopped the welcome messages.")


bot.tree.add_command(welcome_group)


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
    attended, waiting, on_loa = board.boss_lists(bot.db, interaction.guild_id,
                                                 tracked_members(interaction.guild, row["member_role_id"]), found["id"])
    labels = (f"Guild boss {day} {found['boss_time'][11:16]} UTC", "Attended", "Not attended", "Not attended")
    view = HistoryView(labels, attended, waiting, on_loa)
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
    if text := loa_block(interaction.guild_id, member.id, day, f"{member.mention} is"):
        return await interaction.response.send_message(text, ephemeral=True)
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
    await refresh_attendance(interaction.guild)


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
    await refresh_attendance(interaction.guild)


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


# --- LOA: members apply on the board, staff approve ---

_loa_lock = asyncio.Lock()


def loa_panel_embed(guild, active):
    embed = discord.Embed(
        title="Leave of absence",
        description="Need a break? Press **Apply for LOA**. Staff approve each request.\n"
                    f"• Up to {LOA_MAX_DAYS} days. After an LOA ends, wait {LOA_COOLDOWN_DAYS} days before the next.\n"
                    "• While on LOA you get no pings or reminders, and your days do not count against you in "
                    "donations, boss attendance or XP. Your streaks keep going.\n"
                    "• When it ends, you get a ping to come back and grind.")
    embed.add_field(name=f"On LOA now ({len(active)})", inline=False, value=board.join_capped(
        [f"{discord.utils.escape_markdown(ign)} · until the end of {last}" for ign, last in active], sep="\n")
        or "Nobody.")
    return brand(embed, guild, "Days use the UTC day")


def loa_block_text(error):
    if isinstance(error, db.LoaCooldown):
        return f"You can apply again from {error.args[0]} (UTC): there is a {LOA_COOLDOWN_DAYS}-day wait after an LOA."
    kind, last = error.args
    return "Your last request is still waiting for staff." if kind == "pending" else \
        f"You already have an LOA until the end of {last} (UTC)."


async def refresh_loa_panel(guild):
    """Update the LOA board. If something was posted below it, move it to the bottom of the channel."""
    async with _loa_lock:
        row = db.get_guild(bot.db, guild.id)
        channel = row and row["loa_channel_id"] and guild.get_channel(row["loa_channel_id"])
        if not channel:
            return
        embed = loa_panel_embed(guild, board.loa_on(bot.db, guild.id, db.today_utc()))
        old = row["loa_message_id"] and channel.get_partial_message(row["loa_message_id"])
        try:
            if old and channel.last_message_id == old.id:
                return await old.edit(embed=embed, view=LoaPanelView())
            if old:
                try:
                    await old.delete()
                except discord.NotFound:
                    pass
            new = await channel.send(embed=embed, view=LoaPanelView())
            db.save_guild(bot.db, guild.id, loa_message_id=new.id)
        except discord.HTTPException as e:
            log.warning("LOA panel refresh failed in guild %s: %s", guild.id, e)


class LoaPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Apply for LOA", style=discord.ButtonStyle.primary, custom_id="loa_apply")
    async def apply(self, interaction, button):
        row = db.get_guild(bot.db, interaction.guild_id)
        if not row or not row["loa_review_channel_id"]:
            return await interaction.response.send_message("LOA is not set up yet.", ephemeral=True)
        user = interaction.user
        if not (user.get_role(row["member_role_id"]) or (row["loa_role_id"] and user.get_role(row["loa_role_id"]))):
            return await interaction.response.send_message("Only guild members can apply for LOA.", ephemeral=True)
        if not db.get_ign(bot.db, interaction.guild_id, user.id):
            return await interaction.response.send_message(
                "Ask an officer to link your in-game name first.", ephemeral=True)
        try:
            db.loa_check(bot.db, interaction.guild_id, user.id, db.today_utc())
        except (db.LoaBusy, db.LoaCooldown) as e:
            return await interaction.response.send_message(loa_block_text(e), ephemeral=True)
        await interaction.response.send_modal(LoaModal())


class LoaModal(discord.ui.Modal, title="Leave of absence"):
    days = discord.ui.TextInput(label=f"Days away (1 to {LOA_MAX_DAYS})", max_length=2)
    start = discord.ui.TextInput(label="Start date (optional)", placeholder="YYYY-MM-DD. Empty = today",
                                 required=False, max_length=10)
    reason = discord.ui.TextInput(label="Reason", style=discord.TextStyle.paragraph, max_length=500)

    async def on_submit(self, interaction):
        row = db.get_guild(bot.db, interaction.guild_id)
        channel = row and row["loa_review_channel_id"] and interaction.guild.get_channel(row["loa_review_channel_id"])
        if not channel:
            return await interaction.response.send_message(
                "Could not reach the officers. Try again later.", ephemeral=True)
        reason, today = self.reason.value.strip(), db.today_utc()
        if not reason:
            return await interaction.response.send_message("Give a reason.", ephemeral=True)
        try:
            days, start = db.parse_loa(self.days.value, self.start.value, today)
            loa_id = db.loa_request(bot.db, interaction.guild_id, interaction.user.id, reason, days, start, today)
        except db.LoaInvalid as e:
            return await interaction.response.send_message(e.args[0], ephemeral=True)
        except (db.LoaBusy, db.LoaCooldown) as e:
            return await interaction.response.send_message(loa_block_text(e), ephemeral=True)
        embed = discord.Embed(title="LOA request", description="Approve to start the leave. It counts whole UTC days.")
        embed.add_field(name="Member", value=interaction.user.mention)
        embed.add_field(name="In-game name", value=discord.utils.escape_markdown(
            db.get_ign(bot.db, interaction.guild_id, interaction.user.id) or "not linked"))
        embed.add_field(name="Days", value=str(days))
        embed.add_field(name="Asked start (UTC)", value=start)
        embed.add_field(name="Reason", value=reason, inline=False)
        embed.add_field(name="Request", value=str(loa_id))
        embed.set_author(name=interaction.user.display_name, icon_url=interaction.user.display_avatar.url)
        try:
            await channel.send(embed=brand(embed, footer="Requested"), view=LoaRequestView())
        except discord.HTTPException as e:
            db.delete_loa_request(bot.db, interaction.guild_id, loa_id)
            log.warning("LOA request post failed in guild %s: %s", interaction.guild_id, e)
            return await interaction.response.send_message(
                "Could not reach the officers. Try again later.", ephemeral=True)
        await interaction.response.send_message("Request sent. You get a DM when staff answer.", ephemeral=True)


class LoaRequestView(discord.ui.View):
    """Approve / Deny under an LOA request in the officer channel. The request id is read back from the
    embed, so the buttons keep working after a restart."""

    def __init__(self):
        super().__init__(timeout=None)

    @staticmethod
    def request(interaction):
        fields = {f.name: f.value for f in interaction.message.embeds[0].fields}
        return db.get_loa(bot.db, interaction.guild_id, int(fields["Request"]))

    @discord.ui.button(label="Approve", style=discord.ButtonStyle.green, custom_id="loareq_approve")
    async def approve(self, interaction, button):
        if not await require_staff(interaction):
            return
        req = self.request(interaction)
        loa = req and db.approve_loa(bot.db, interaction.guild_id, req["id"], interaction.user.id, db.today_utc())
        if not loa:
            return await interaction.response.send_message("This request was already answered.", ephemeral=True)
        span = f"{loa['start_day']} to {loa['end_day']} (UTC days)"
        await LinkRequestView.close(self, interaction, loa["discord_id"], f"Approved by {interaction.user.mention}: {span}",
                                    f"Your LOA in {interaction.guild.name} was approved: {span}. "
                                    "You get a ping when it ends.")
        await audit(interaction, f"approved the LOA of <@{loa['discord_id']}>: {span}.")
        await refresh_loa_panel(interaction.guild)
        await refresh_board(interaction.guild)

    @discord.ui.button(label="Deny", style=discord.ButtonStyle.secondary, custom_id="loareq_deny")
    async def deny(self, interaction, button):
        if not await require_staff(interaction):
            return
        req = self.request(interaction)
        if not req or req["status"] != "pending":
            return await interaction.response.send_message("This request was already answered.", ephemeral=True)
        await interaction.response.send_modal(LoaDenyModal(req["id"]))


class LoaDenyModal(discord.ui.Modal, title="Deny LOA request"):
    reason = discord.ui.TextInput(label="Reason (the member gets it in a DM)", style=discord.TextStyle.paragraph,
                                  max_length=500)

    def __init__(self, loa_id):
        super().__init__()
        self.loa_id = loa_id

    async def on_submit(self, interaction):
        if not await require_staff(interaction):
            return
        reason = self.reason.value.strip()
        if not reason:
            return await interaction.response.send_message("Give a reason.", ephemeral=True)
        req = db.get_loa(bot.db, interaction.guild_id, self.loa_id)
        if not req or not db.deny_loa(bot.db, interaction.guild_id, self.loa_id, interaction.user.id, reason):
            return await interaction.response.send_message("This request was already answered.", ephemeral=True)
        await LinkRequestView.close(self, interaction, req["discord_id"],
                                    f"Denied by {interaction.user.mention}\nReason: {reason}",
                                    f"Your LOA request in {interaction.guild.name} was denied.\nReason: {reason}")
        await audit(interaction, f"denied the LOA request of <@{req['discord_id']}>.")


@bot.tree.command(name="loa_setup", description="Staff: post the LOA board and set the channel for LOA requests.")
@app_commands.guild_only()
@app_commands.describe(loa_channel="Where members see the board and press Apply",
                       review_channel="Private officer channel for LOA requests")
async def loa_setup(interaction: discord.Interaction, loa_channel: discord.TextChannel,
                    review_channel: discord.TextChannel):
    if not await require_staff(interaction):
        return
    row = db.get_guild(bot.db, interaction.guild_id)
    if not row:
        return await interaction.response.send_message("Run /setup first.", ephemeral=True)
    for channel in (loa_channel, review_channel):
        perms = channel.permissions_for(interaction.guild.me)
        if not (perms.view_channel and perms.send_messages and perms.embed_links):
            return await interaction.response.send_message(
                f"I cannot post in {channel.mention}. Give the bot View Channel, Send Messages and Embed Links "
                "there, then run /loa_setup again.", ephemeral=True)
    role = row["loa_role_id"] and interaction.guild.get_role(row["loa_role_id"])
    if not role and not interaction.guild.me.guild_permissions.manage_roles:
        return await interaction.response.send_message(
            "I need the Manage Roles permission to create the On LOA role, then run /loa_setup again.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    if not role:
        try:
            role = await interaction.guild.create_role(name="On LOA", reason="LOA setup")
        except discord.HTTPException as e:
            return await interaction.followup.send(f"Could not create the On LOA role: {e}", ephemeral=True)
    old = row["loa_channel_id"] and row["loa_message_id"] and interaction.guild.get_channel(row["loa_channel_id"])
    if old and old.id != loa_channel.id:  # moved: the old board would keep a working button
        try:
            await old.get_partial_message(row["loa_message_id"]).delete()
        except discord.HTTPException:
            pass
        db.save_guild(bot.db, interaction.guild_id, loa_message_id=None)
    db.save_guild(bot.db, interaction.guild_id, loa_channel_id=loa_channel.id, loa_review_channel_id=review_channel.id,
                  loa_role_id=role.id)
    await interaction.followup.send(
        f"LOA board in {loa_channel.mention}, requests go to {review_channel.mention}. Keep that one private to "
        f"officers. Give {role.mention} the same channel access as the member role: members swap to it while on LOA.",
        ephemeral=True)
    await audit(interaction, f"set the LOA channel to {loa_channel.mention} and LOA requests to {review_channel.mention}.")
    await refresh_loa_panel(interaction.guild)


async def swap_loa_role(guild, row, member_id, on):
    """On LOA: give the On LOA role (keep the member role). Off: remove the On LOA role.
    Returns an error text, or None when done."""
    member, loa_role = guild.get_member(member_id), guild.get_role(row["loa_role_id"])
    if not (member and loa_role):
        return "the member or the On LOA role was not found"
    try:
        if on:
            await member.add_roles(loa_role, reason="LOA")
        else:
            await member.remove_roles(loa_role, reason="LOA")
    except discord.HTTPException as e:
        return str(e)

async def finish_loa(guild, row, loa):
    """Give the member role back, then ping the member in the LOA channel and in a DM."""
    if error := await swap_loa_role(guild, row, loa["discord_id"], on=False):
        await staff_post(guild, f"Could not give <@{loa['discord_id']}> the member role back: {error}. "
                                "Please do it by hand.")
    text = f"<@{loa['discord_id']}> your LOA is over. Time to go back to grinding!"
    channel = row["loa_channel_id"] and guild.get_channel(row["loa_channel_id"])
    for target, message in ((channel, text), (guild.get_member(loa["discord_id"]), f"{text} ({guild.name})")):
        try:
            if target:
                await target.send(message)
        except discord.HTTPException as e:  # closed DMs, or no access to the channel
            log.warning("LOA end notice failed for %s in guild %s: %s", loa["discord_id"], guild.id, e)
    log.info("LOA %s ended in guild %s", loa["id"], guild.id)


async def loa_tick(guild, row, today):
    """Swap roles on the first LOA day. On the day after the last, swap back and ping the member in the LOA
    channel and in a DM. Each step is claimed in the database first, so a restart never repeats it."""
    changed = False
    for loa in db.loa_due_start(bot.db, guild.id, today):
        if db.claim_loa_start(bot.db, loa["id"]):
            changed = True
            if error := await swap_loa_role(guild, row, loa["discord_id"], on=True):
                await staff_post(guild, f"Could not move <@{loa['discord_id']}> to the On LOA role: {error}. The LOA "
                                        "is recorded, but they may still get role pings.")
    for loa in db.loa_due_end(bot.db, guild.id, today):
        if not db.claim_loa_end(bot.db, loa["id"]):
            continue
        changed = True
        await finish_loa(guild, row, loa)
    if changed:
        await refresh_loa_boards(guild)


async def refresh_loa_boards(guild):
    """Every board that lists or leaves out members on LOA."""
    for refresh in (refresh_loa_panel, refresh_board, refresh_attendance, refresh_xp_panel):
        await refresh(guild)


async def dm_member(member, text):
    try:
        if member:
            await member.send(text)
    except discord.HTTPException:  # DMs closed: the member sees it on the LOA board instead
        pass


@bot.tree.command(name="loa_end", description="Staff: end a member's LOA now. They get the member role back and a ping.")
@app_commands.guild_only()
async def loa_end(interaction: discord.Interaction, member: discord.Member):
    if not await require_staff(interaction):
        return
    row = db.get_guild(bot.db, interaction.guild_id)
    if not row or not row["loa_role_id"]:
        return await interaction.response.send_message("Run /loa_setup first.", ephemeral=True)
    today = db.today_utc()
    current = db.loa_current(bot.db, interaction.guild_id, member.id, today)
    loa = current and db.end_loa(bot.db, interaction.guild_id, current["id"], today)
    if not loa:
        return await interaction.response.send_message(f"{member.display_name} has no LOA to end.", ephemeral=True)
    await interaction.response.send_message(
        f"Ended the LOA of {member.mention}." if loa["status"] == "ended" else
        f"Cancelled the LOA of {member.mention}: it had not started.", ephemeral=True)
    await audit(interaction, f"ended the LOA of {member.mention}.")
    if loa["status"] == "ended":
        await finish_loa(interaction.guild, row, loa)
    await refresh_loa_boards(interaction.guild)


@bot.tree.command(name="loa_extend", description="Staff: add days to a member's LOA (an LOA lasts at most "
                                                f"{LOA_MAX_DAYS} days in all).")
@app_commands.guild_only()
@app_commands.describe(extra_days="Days to add after the last LOA day")
async def loa_extend(interaction: discord.Interaction, member: discord.Member,
                     extra_days: app_commands.Range[int, 1, LOA_MAX_DAYS]):
    if not await require_staff(interaction):
        return
    today = db.today_utc()
    current = db.loa_current(bot.db, interaction.guild_id, member.id, today)
    try:
        loa = current and db.extend_loa(bot.db, interaction.guild_id, current["id"], extra_days, today)
    except db.LoaInvalid as e:
        return await interaction.response.send_message(e.args[0], ephemeral=True)
    if not loa:
        return await interaction.response.send_message(f"{member.display_name} has no LOA to extend.", ephemeral=True)
    await interaction.response.send_message(
        f"{member.mention}'s LOA now ends after {loa['end_day']} (UTC).", ephemeral=True)
    await audit(interaction, f"extended the LOA of {member.mention} by {extra_days} day(s), to {loa['end_day']}.")
    await dm_member(member, f"Your LOA in {interaction.guild.name} was extended. "
                            f"The last day is now {loa['end_day']} (UTC).")
    await refresh_loa_boards(interaction.guild)


@bot.tree.command(name="loa_grant", description="Staff: start an LOA for a member today, with no form or wait.")
@app_commands.guild_only()
@app_commands.describe(days=f"1 to {LOA_MAX_DAYS}", reason="Why. Shown to staff only.")
async def loa_grant(interaction: discord.Interaction, member: discord.Member,
                    days: app_commands.Range[int, 1, LOA_MAX_DAYS], reason: str = "Granted by staff"):
    if not await require_staff(interaction):
        return
    row = db.get_guild(bot.db, interaction.guild_id)
    if not row or not row["loa_role_id"]:
        return await interaction.response.send_message("Run /loa_setup first.", ephemeral=True)
    if not db.get_ign(bot.db, interaction.guild_id, member.id):
        return await interaction.response.send_message("Member has no linked IGN.", ephemeral=True)
    try:
        loa_id = db.grant_loa(bot.db, interaction.guild_id, member.id, reason, days, db.today_utc(),
                              interaction.user.id)
    except db.LoaBusy as e:
        return await interaction.response.send_message(
            f"{member.display_name} already has an LOA {'request waiting' if e.args[0] == 'pending' else 'running'}.",
            ephemeral=True)
    loa = db.get_loa(bot.db, interaction.guild_id, loa_id)
    await interaction.response.send_message(
        f"{member.mention} is on LOA from {loa['start_day']} to {loa['end_day']} (UTC).", ephemeral=True)
    await audit(interaction, f"started an LOA for {member.mention}: {loa['start_day']} to {loa['end_day']}.")
    await dm_member(member, f"Staff started an LOA for you in {interaction.guild.name}: {loa['start_day']} to "
                            f"{loa['end_day']} (UTC days). You get a ping when it ends.")
    await loa_tick(interaction.guild, row, db.today_utc())  # swap the roles now, not at the next minute


@bot.tree.command(name="loa_list", description="Staff: members on LOA, and requests waiting for an answer.")
@app_commands.guild_only()
async def loa_list(interaction: discord.Interaction):
    if not await require_staff(interaction):
        return
    today, esc = db.today_utc(), discord.utils.escape_markdown
    lines = []
    for r in db.loa_list(bot.db, interaction.guild_id, today):
        ign = db.get_ign(bot.db, interaction.guild_id, r["discord_id"]) or "not linked"
        state = "request waiting" if r["status"] == "pending" else "running" if r["start_day"] <= today else "starts later"
        lines.append(f"<@{r['discord_id']}> ({esc(ign)}) · {state} · {r['start_day']} to {r['end_day']} (UTC) · "
                     f"{esc(r['reason'][:100])}")
    await send_text(interaction, f"**LOA** ({len(lines)})", lines or ["Nobody."], "loa_list.txt")


# --- Strikes ---

strike_group = app_commands.Group(name="strike", description="Member strikes (staff only).", guild_only=True)

@strike_group.command(name="setup", description="Staff: set the private channel where strikes are logged.")
async def strike_setup(interaction: discord.Interaction, channel: discord.TextChannel):
    if not await require_staff(interaction):
        return
    if not db.get_guild(bot.db, interaction.guild_id):
        return await interaction.response.send_message("Run /setup first.", ephemeral=True)
    perms = channel.permissions_for(interaction.guild.me)
    if not (perms.view_channel and perms.send_messages and perms.embed_links):
        return await interaction.response.send_message(
            f"I cannot post in {channel.mention}. Give the bot View Channel, Send Messages and Embed Links "
            "there, then run /strike setup again.", ephemeral=True)
    try:
        await channel.send("⚠️ **Strikes channel** set. Only officers should be able to see this channel.")
    except discord.HTTPException as e:
        return await interaction.response.send_message(
            f"Could not post in {channel.mention}: {e}", ephemeral=True)
    db.save_guild(bot.db, interaction.guild_id, strike_channel_id=channel.id)
    await interaction.response.send_message(
        f"Strikes will be logged in {channel.mention}. Keep this channel private to officers only.",
        ephemeral=True)
    await audit(interaction, f"set the strikes channel to {channel.mention}.")

@strike_group.command(name="give", description="Staff: give a strike to a member.")
@app_commands.describe(member="The member to strike", reason="Reason for the strike (optional)")
async def strike_give(interaction: discord.Interaction, member: discord.Member, reason: str | None = None):
    if not await require_staff(interaction):
        return
    row = db.get_guild(bot.db, interaction.guild_id)
    if not row:
        return await interaction.response.send_message("Run /setup first.", ephemeral=True)

    strike_id = db.add_strike(bot.db, interaction.guild_id, member.id, reason, interaction.user.id)
    count = len(db.get_strikes(bot.db, interaction.guild_id, member.id))

    # DM the member (same style used for XP approval and LOA)
    reason_text = f"\n**Reason:** {reason}" if reason else ""
    await dm_member(member,
        f"You have received a **strike** in **{interaction.guild.name}**.\n"
        f"This is strike **#{count}**.{reason_text}")

    # Post in the private strikes channel
    channel = row["strike_channel_id"] and interaction.guild.get_channel(row["strike_channel_id"])
    if channel:
        ign = db.get_ign(bot.db, interaction.guild_id, member.id) or "not linked"
        embed = discord.Embed(title="Strike issued", colour=BRAND)
        embed.add_field(name="Member", value=f"{discord.utils.escape_markdown(member.display_name)} · **{discord.utils.escape_markdown(ign)}**")
        embed.add_field(name="Strike #", value=str(count))
        embed.add_field(name="Issued by", value=interaction.user.mention)
        if reason:
            embed.add_field(name="Reason", value=reason, inline=False)
        embed.set_footer(text=f"Strike ID: {strike_id}")
        embed.timestamp = datetime.now(timezone.utc)
        try:
            await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException as e:
            log.warning("Failed to post strike in guild %s: %s", interaction.guild_id, e)

    await interaction.response.send_message(
        f"Gave strike **#{count}** to {member.mention}." + (f" Reason: {reason}" if reason else ""),
        ephemeral=True)
    await audit(interaction, f"gave strike #{count} to {member.mention}" + (f": {reason}" if reason else "."))

@strike_group.command(name="member", description="Staff: see all strikes of a member.")
async def strike_member(interaction: discord.Interaction, member: discord.Member):
    if not await require_staff(interaction):
        return
    strikes = db.get_strikes(bot.db, interaction.guild_id, member.id)
    if not strikes:
        return await interaction.response.send_message(f"{member.display_name} has no strikes.", ephemeral=True)

    lines = []
    for s in strikes:
        reason = s["reason"] or "No reason given"
        lines.append(f"**#{s['id']}** · <t:{int(datetime.fromisoformat(s['created_at']).timestamp())}:f> · "
                     f"by <@{s['issued_by']}> · {discord.utils.escape_markdown(reason)}")

    await send_text(interaction,
                    f"**Strikes for {member.display_name}** ({len(strikes)})",
                    lines, "strikes.txt")

@strike_group.command(name="list", description="Staff: see all members who have strikes.")
async def strike_list(interaction: discord.Interaction):
    if not await require_staff(interaction):
        return
    counts = db.strike_counts(bot.db, interaction.guild_id)
    if not counts:
        return await interaction.response.send_message("Nobody has any strikes.", ephemeral=True)

    sorted_counts = sorted(counts.items(), key=lambda x: (-x[1], x[0]))
    lines = []
    for discord_id, count in sorted_counts:
        member = interaction.guild.get_member(discord_id)
        name = member.display_name if member else f"Unknown ({discord_id})"
        lines.append(f"**{discord.utils.escape_markdown(name)}** · {count} strike{'s' if count != 1 else ''}")

    await send_text(interaction, f"**Members with strikes** ({len(lines)})", lines, "strike_list.txt")

bot.tree.add_command(strike_group)

# --- XP: officers log each member's weekly total; members press I hit cap for an officer to approve ---

_xp_lock = asyncio.Lock()
_xp_seen = {}  # guild_id -> UTC day the XP panel last showed


def xp_rows(guild, row, day):
    return board.xp_day(bot.db, guild.id, tracked_members(guild, row["member_role_id"]), day)


def xp_invalid_text(error):
    day, gain, cap = error.args
    return f"That would make {day} (UTC) a gain of **{gain:,}** XP. It must be from 0 to {cap:,}."


def xp_panel_embed(guild, rows, loa=()):
    capped = [discord.utils.escape_markdown(ign) for _, _, ign, gain in rows if gain is not None and gain >= XP_CAP]
    embed = discord.Embed(title="Guild XP",
                          description=f"Daily cap: **{XP_CAP:,}** XP. The day resets <t:{board.next_reset_ts()}:t>, "
                                      f"weekly XP goes back to 0 <t:{board.next_week_ts()}:R>.\n"
                                      "Hit the cap today? Press **I hit cap** and an officer approves it. "
                                      "Officers log everyone else.")
    embed.add_field(name=f"Hit cap today ({len(capped)} of {len(rows)})",
                    value=board.join_capped(capped, sep="\n") or "Nobody yet.", inline=False)
    if loa:
        embed.add_field(name=f"On LOA ({len(loa)})", inline=False, value=board.join_capped(
            [f"{discord.utils.escape_markdown(ign)} (until {last})" for ign, last in loa], sep="\n"))
    return brand(embed, guild, "Shows approved logs only")


async def refresh_xp_panel(guild):
    """Update the XP panel. If something was posted below it, move it to the bottom of the channel."""
    async with _xp_lock:
        row = db.get_guild(bot.db, guild.id)
        channel = row and row["xp_channel_id"] and guild.get_channel(row["xp_channel_id"])
        if not channel:
            return
        embed = xp_panel_embed(guild, xp_rows(guild, row, db.today_utc()), board.loa_on(bot.db, guild.id, db.today_utc()))
        old = row["xp_message_id"] and channel.get_partial_message(row["xp_message_id"])
        try:
            if old and channel.last_message_id == old.id:
                return await old.edit(embed=embed, view=XpPanelView())
            if old:
                try:
                    await old.delete()
                except discord.NotFound:
                    pass
            new = await channel.send(embed=embed, view=XpPanelView())
            db.save_guild(bot.db, guild.id, xp_message_id=new.id)
        except discord.HTTPException as e:
            log.warning("XP panel refresh failed in guild %s: %s", guild.id, e)


class XpPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="I hit cap", style=discord.ButtonStyle.green, custom_id="xp_cap")
    async def hit_cap(self, interaction, button):
        row = db.get_guild(bot.db, interaction.guild_id)
        if not row or not row["xp_review_channel_id"]:
            return await interaction.response.send_message("XP logging is not set up yet.", ephemeral=True)
        if text := loa_block(interaction.guild_id, interaction.user.id, db.today_utc()):
            return await interaction.response.send_message(text, ephemeral=True)
        if not interaction.user.get_role(row["member_role_id"]):
            return await interaction.response.send_message("Only guild members can log XP.", ephemeral=True)
        ign = db.get_ign(bot.db, interaction.guild_id, interaction.user.id)
        if not ign:
            return await interaction.response.send_message(
                "Ask an officer to link your in-game name first.", ephemeral=True)
        channel = interaction.guild.get_channel(row["xp_review_channel_id"])
        if not channel:
            return await interaction.response.send_message(
                "Could not reach the officers. Ask an officer to log your XP.", ephemeral=True)
        day = db.today_utc()
        try:
            request_id = db.xp_request(bot.db, interaction.guild_id, interaction.user.id, day)
        except db.AlreadyRequested:
            return await interaction.response.send_message("You already sent a request today.", ephemeral=True)
        logged = db.xp_gains(bot.db, interaction.guild_id, day).get(interaction.user.id)
        embed = discord.Embed(title="XP request: hit cap", description=f"Approve to log **{XP_CAP:,}** XP for the day.")
        embed.add_field(name="Member", value=interaction.user.mention)
        embed.add_field(name="In-game name", value=discord.utils.escape_markdown(ign))
        embed.add_field(name="Day (UTC)", value=day)
        embed.add_field(name="Logged now", value=f"{logged:,} XP" if logged is not None else "Not logged")
        embed.add_field(name="Request", value=str(request_id))
        embed.set_author(name=interaction.user.display_name, icon_url=interaction.user.display_avatar.url)
        try:
            await channel.send(embed=brand(embed, footer="Requested"), view=XpRequestView())
        except discord.HTTPException as e:
            db.delete_xp_request(bot.db, interaction.guild_id, request_id)
            log.warning("XP request post failed in guild %s: %s", interaction.guild_id, e)
            return await interaction.response.send_message(
                "Could not reach the officers. Ask an officer to log your XP.", ephemeral=True)
        await interaction.response.send_message(
            f"Request sent for {day} (UTC). You get a DM when an officer answers.", ephemeral=True)


class XpRequestView(discord.ui.View):
    """Approve / Deny under an I hit cap request in the officer channel. The request id is read back from the
    embed, so the buttons keep working after a restart."""

    def __init__(self):
        super().__init__(timeout=None)

    @staticmethod
    def request(interaction):
        fields = {f.name: f.value for f in interaction.message.embeds[0].fields}
        return db.get_xp_request(bot.db, interaction.guild_id, int(fields["Request"]))

    @discord.ui.button(label="Approve", style=discord.ButtonStyle.green, custom_id="xpreq_approve")
    async def approve(self, interaction, button):
        if not await require_staff(interaction):
            return
        req = self.request(interaction)
        if not req or req["status"] != "pending":
            return await interaction.response.send_message("This request was already answered.", ephemeral=True)
        if text := loa_block(interaction.guild_id, req["discord_id"], req["day_utc"], f"<@{req['discord_id']}> is"):
            return await interaction.response.send_message(f"{text} Deny this request.", ephemeral=True)
        try:
            db.xp_set(bot.db, interaction.guild_id, req["discord_id"], req["day_utc"], interaction.user.id,
                      gain=XP_CAP)
        except db.XpInvalid as e:
            return await interaction.response.send_message(
                f"{xp_invalid_text(e)} Fix that day with /xp_log, then approve again.", ephemeral=True)
        db.close_xp_request(bot.db, interaction.guild_id, req["id"], "approved", interaction.user.id)
        await LinkRequestView.close(self, interaction, req["discord_id"], f"Approved by {interaction.user.mention}",
                                    f"Your hit cap request for {req['day_utc']} (UTC) in {interaction.guild.name} "
                                    f"was approved: {XP_CAP:,} XP logged.")
        await audit(interaction, f"approved the hit cap request of <@{req['discord_id']}> for {req['day_utc']}.")
        await refresh_xp_panel(interaction.guild)

    @discord.ui.button(label="Deny", style=discord.ButtonStyle.secondary, custom_id="xpreq_deny")
    async def deny(self, interaction, button):
        if not await require_staff(interaction):
            return
        req = self.request(interaction)
        if not req or not db.close_xp_request(bot.db, interaction.guild_id, req["id"], "denied", interaction.user.id):
            return await interaction.response.send_message("This request was already answered.", ephemeral=True)
        await LinkRequestView.close(self, interaction, req["discord_id"], f"Denied by {interaction.user.mention}",
                                    f"Your hit cap request for {req['day_utc']} (UTC) in {interaction.guild.name} "
                                    "was denied. Ask an officer why.")
        await audit(interaction, f"denied the hit cap request of <@{req['discord_id']}> for {req['day_utc']}.")


@bot.tree.command(name="xp_setup", description="Staff: post the XP panel and set the officer channel for XP requests.")
@app_commands.guild_only()
@app_commands.describe(xp_channel="Where members see the panel and get pinged",
                       review_channel="Private officer channel for I hit cap requests")
async def xp_setup(interaction: discord.Interaction, xp_channel: discord.TextChannel,
                   review_channel: discord.TextChannel):
    if not await require_staff(interaction):
        return
    row = db.get_guild(bot.db, interaction.guild_id)
    if not row:
        return await interaction.response.send_message("Run /setup first.", ephemeral=True)
    for channel in (xp_channel, review_channel):
        perms = channel.permissions_for(interaction.guild.me)
        if not (perms.view_channel and perms.send_messages and perms.embed_links):
            return await interaction.response.send_message(
                f"I cannot post in {channel.mention}. Give the bot View Channel, Send Messages and Embed Links "
                "there, then run /xp_setup again.", ephemeral=True)
    old = row["xp_channel_id"] and row["xp_message_id"] and interaction.guild.get_channel(row["xp_channel_id"])
    if old and old.id != xp_channel.id:  # moved: the old panel would keep a working button
        try:
            await old.get_partial_message(row["xp_message_id"]).delete()
        except discord.HTTPException:
            pass
        db.save_guild(bot.db, interaction.guild_id, xp_message_id=None)
    db.save_guild(bot.db, interaction.guild_id, xp_channel_id=xp_channel.id, xp_review_channel_id=review_channel.id)
    db.claim_post(bot.db, interaction.guild_id, "xp_ping", db.yesterday_utc())  # no ping for a day before setup
    await interaction.response.send_message(
        f"XP panel in {xp_channel.mention}, requests go to {review_channel.mention}. Keep that one private to "
        "officers.", ephemeral=True)
    await audit(interaction, f"set the XP channel to {xp_channel.mention} and XP requests to {review_channel.mention}.")
    await refresh_xp_panel(interaction.guild)


def xp_target(interaction, member, date):
    """(day, ign, error text). The day must not be in the future."""
    try:
        day = parse_day(date)
    except ValueError:
        return None, None, BAD_DATE
    if day > db.today_utc():
        return None, None, "That day has not started yet (UTC)."
    ign = db.get_ign(bot.db, interaction.guild_id, member.id)
    return day, ign, None if ign else "Member has no linked IGN."


@bot.tree.command(name="xp_log", description="Staff: log a member's weekly XP total for a day. Logging a day again "
                                             "replaces it.")
@app_commands.guild_only()
@app_commands.describe(total="Weekly total shown in the game (it went back to 0 on Monday)",
                       date="Day as YYYY-MM-DD. Default: today (UTC).")
async def xp_log(interaction: discord.Interaction, member: discord.Member,
                 total: app_commands.Range[int, 0, XP_CAP * 7], date: str | None = None):
    if not await require_staff(interaction):
        return
    day, ign, error = xp_target(interaction, member, date)
    error = error or loa_block(interaction.guild_id, member.id, day, f"{member.mention} is")
    if error:
        return await interaction.response.send_message(error, ephemeral=True)
    try:
        db.xp_set(bot.db, interaction.guild_id, member.id, day, interaction.user.id, total=total)
    except db.XpInvalid as e:
        return await interaction.response.send_message(xp_invalid_text(e), ephemeral=True)
    gain = next(d["gain"] for d in db.xp_week(bot.db, interaction.guild_id, member.id, day) if d["day"] == day)
    await interaction.response.send_message(
        f"Logged **{discord.utils.escape_markdown(ign)}** on {day}: weekly total {total:,}, gain {gain:,} XP.",
        ephemeral=True)
    await audit(interaction, f"logged {member.mention}'s XP on {day}: total {total:,}, gain {gain:,}.")
    await refresh_xp_panel(interaction.guild)


@bot.tree.command(name="xp_delete", description="Staff: delete a member's XP log for a day.")
@app_commands.guild_only()
@app_commands.describe(date="Day as YYYY-MM-DD")
async def xp_delete(interaction: discord.Interaction, member: discord.Member, date: str):
    if not await require_staff(interaction):
        return
    day, ign, error = xp_target(interaction, member, date)
    if error:
        return await interaction.response.send_message(error, ephemeral=True)
    if not db.xp_delete(bot.db, interaction.guild_id, member.id, day):
        return await interaction.response.send_message(f"No XP log for {member.display_name} on {day}.",
                                                       ephemeral=True)
    await interaction.response.send_message(f"Deleted the XP log for **{discord.utils.escape_markdown(ign)}** "
                                            f"on {day}.", ephemeral=True)
    await audit(interaction, f"deleted {member.mention}'s XP log on {day}.")
    await refresh_xp_panel(interaction.guild)


@bot.tree.command(name="xp_day", description="Staff: every member's XP gain on a day (UTC).")
@app_commands.guild_only()
@app_commands.describe(date="Day as YYYY-MM-DD. Default: today (UTC).")
async def xp_day(interaction: discord.Interaction, date: str | None = None):
    if not await require_staff(interaction):
        return
    try:
        day = parse_day(date)
    except ValueError:
        return await interaction.response.send_message(BAD_DATE, ephemeral=True)
    row = db.get_guild(bot.db, interaction.guild_id)
    if not row:
        return await interaction.response.send_message("Run /setup first.", ephemeral=True)
    rows, pending = xp_rows(interaction.guild, row, day), db.xp_pending(bot.db, interaction.guild_id, day)
    esc = discord.utils.escape_markdown
    lines = [f"{esc(name)} · {esc(ign)} · " + (f"{gain:,} XP" if gain is not None else "not logged")
             + (" · request pending" if i in pending else "") for i, name, ign, gain in rows]
    lines += [f"{esc(ign)} · on LOA (last day {last})" for ign, last in board.loa_on(bot.db, interaction.guild_id, day)]
    capped = sum(gain is not None and gain >= XP_CAP for *_, gain in rows)
    missing = sum(gain is None for *_, gain in rows)
    header = (f"**XP on {day} (UTC)** · hit cap: {capped} · under cap: {len(rows) - capped - missing} · "
              f"not logged: {missing}")
    await send_text(interaction, header, lines or ["No linked members."], f"xp_{day}.txt")


@bot.tree.command(name="xp_member", description="Staff: a member's XP for each day of a week.")
@app_commands.guild_only()
@app_commands.describe(date="Any day of the week as YYYY-MM-DD. Default: this week.")
async def xp_member(interaction: discord.Interaction, member: discord.Member, date: str | None = None):
    if not await require_staff(interaction):
        return
    try:
        day = parse_day(date)
    except ValueError:
        return await interaction.response.send_message(BAD_DATE, ephemeral=True)
    ign = db.get_ign(bot.db, interaction.guild_id, member.id)
    if not ign:
        return await interaction.response.send_message("Member has no linked IGN.", ephemeral=True)
    week = [d for d in db.xp_week(bot.db, interaction.guild_id, member.id, day) if d["day"] <= db.today_utc()]
    logged = [d for d in week if d["gain"] is not None]
    lines = [f"**{discord.utils.escape_markdown(ign)}** ({member.mention}) · week of {db.week_start(day)} (UTC) · "
             f"total {logged[-1]['total'] if logged else 0:,} XP · hit cap {sum(d['gain'] >= XP_CAP for d in logged)} "
             f"of {len(week)} days"]
    lines += [f"{datetime.strptime(d['day'], '%Y-%m-%d'):%a} {d['day']} · "
              + (f"+{d['gain']:,} · total {d['total']:,} · by <@{d['logged_by']}>" if d["gain"] is not None
                 else "not logged") for d in week]
    await interaction.response.send_message("\n".join(lines), ephemeral=True,
                                            allowed_mentions=discord.AllowedMentions.none())


def command_lines():
    """(member lines, staff lines) for /help, read from the commands themselves, so new ones show up."""
    member, staff = [], []
    for command in bot.tree.get_commands():
        for c in command.commands if isinstance(command, app_commands.Group) else [command]:
            text = c.description
            (staff if text.startswith("Staff:") else member).append(
                f"`/{c.qualified_name}` · {text.removeprefix('Staff:').strip()}")
    return sorted(member), sorted(staff)


@bot.tree.command(description="Every command and what it does.")
@app_commands.guild_only()
async def help(interaction: discord.Interaction):
    member, staff = command_lines()
    embed = discord.Embed(title="Evil Donations · help",
                          description="Log a donation once per UTC day with the **Log donation** button, and log "
                                      "guild boss attendance with **I attended**. The day resets at 00:00 UTC.")
    for k, chunk in enumerate(board.pack_lines(member, 1024)):
        embed.add_field(name="Commands" if k == 0 else "Commands (more)", value=chunk, inline=False)
    if is_staff(interaction):
        for k, chunk in enumerate(board.pack_lines(staff, 1024)):
            embed.add_field(name="Staff commands" if k == 0 else "Staff commands (more)", value=chunk, inline=False)
    await interaction.response.send_message(embed=brand(embed, interaction.guild, f"Version {VERSION}"), ephemeral=True)


class DashboardView(discord.ui.View):
    """Shortcut buttons under /dashboard. Each runs the same code as its command."""

    def __init__(self):
        super().__init__(timeout=600)
        for label, command in (("Today's donations", history), ("Latest boss", boss_attendance),
                               ("Missed bosses", boss_missed), ("Export CSV", export), ("Backup", backup_db)):
            button = discord.ui.Button(label=label, style=discord.ButtonStyle.secondary)
            button.callback = self._run(command)
            self.add_item(button)

    @staticmethod
    def _run(command):
        async def callback(interaction):
            await command.callback(interaction)
        return callback


@bot.tree.command(description="Staff: today's numbers at a glance, with shortcut buttons.")
@app_commands.guild_only()
async def dashboard(interaction: discord.Interaction):
    if not await require_staff(interaction):
        return
    row = db.get_guild(bot.db, interaction.guild_id)
    if not row:
        return await interaction.response.send_message("Run /setup first.", ephemeral=True)
    guild, now = interaction.guild, datetime.now(timezone.utc)
    tracked = board.without_loa(bot.db, guild.id, tracked_members(guild, row["member_role_id"]), db.today_utc())[0]
    data = board.board_data(bot.db, guild.id, tracked, db.today_utc(), db.yesterday_utc())
    linked = db.get_igns(bot.db, guild.id)
    embed = discord.Embed(title="Staff dashboard", description=f"{db.today_utc()} (UTC) · resets <t:{data['reset_ts']}:R>")
    embed.add_field(name="Donated today", value=f"**{data['today_count']}** of {len(tracked)}\n"
                    f"`{board.bar(data['today_count'], len(tracked))}`")
    embed.add_field(name="Not donated", value=f"**{len(data['waiting'])}**")
    embed.add_field(name="Yesterday", value=f"**{data['yesterday_count']}** donated")
    embed.add_field(name="Linked", value=f"**{sum(i in linked for i, _ in tracked)}** of {len(tracked)} members\n"
                    f"{len(db.roster_names(bot.db, guild.id, only_free=True))} roster names free")
    latest = db.started_boss(bot.db, guild.id, now.isoformat(timespec="seconds"))
    if latest:
        t = iso_utc(latest["boss_time"])
        embed.add_field(name="Latest guild boss", value=f"<t:{int(t.timestamp())}:t> · logging "
                        f"{boss.attend_state(t, now)}\n**{len(db.attendance_for_boss(bot.db, latest['id']))}** attended")
    poll = db.latest_boss_poll(bot.db, guild.id)
    if row["boss_channel_id"] and poll:
        embed.add_field(name="Boss poll", value=f"Open for <t:{int(iso_utc(poll['cycle_start']).timestamp())}:t>"
                        if not poll["resolved_time"] else "Next poll soon")
    checked, missed = board.missed_in_row(bot.db, guild.id, tracked, now, 4)
    if checked:
        embed.add_field(name=f"Missed the last {checked} bosses", value=f"**{len(missed)}** members")

    stats, esc = board.dashboard_stats(bot.db, guild.id, tracked, now), discord.utils.escape_markdown
    top = max([n for _, n in stats["week"]] + [1])
    embed.add_field(name="Last 7 days (donated)", inline=False, value="```\n" + "\n".join(
        f"{d:%a %d}  {board.bar(n, top)}  {n}" for d, n in stats["week"]) + "\n```")
    for name, (done, possible) in (("This week", stats["week_rate"]), ("This month", stats["month_rate"])):
        embed.add_field(name=f"{name} (since {'Monday' if name == 'This week' else 'the 1st'})",
                        value=f"**{round(100 * done / possible) if possible else 0}%** · {done}/{possible} member-days")
    if stats["bosses_today"]:
        embed.add_field(name="Guild bosses today", value="\n".join(
            f"<t:{ts}:t> · {n}/{sum(i in linked for i, _ in tracked)} attended" for ts, n in stats["bosses_today"]))
    embed.add_field(name=f"Streaks at risk ({len(stats['at_risk'])})", inline=False,
                    value=board.join_capped([f"{esc(ign)} ({n} days)" for ign, n in stats["at_risk"]])
                    or "Nobody: every streak of 3+ days is logged today.")
    if stats["on_loa"]:
        embed.add_field(name=f"On LOA ({len(stats['on_loa'])})", inline=False, value=board.join_capped(
            [f"{esc(ign)} (until {last})" for ign, last in stats["on_loa"]]))
    problems = []
    if stats["not_linked"]:
        problems.append(f"**Not linked ({len(stats['not_linked'])}):** "
                        + board.join_capped([esc(n) for n in stats["not_linked"]], 300))
    if stats["stale_links"]:
        problems.append(f"**Linked but no {MEMBER_ROLE_NAME} role ({len(stats['stale_links'])}):** "
                        + board.join_capped([esc(n) for n in stats["stale_links"]], 300))
    embed.add_field(name="Roster problems", inline=False, value="\n".join(problems) or "None.")
    if row["xp_channel_id"]:
        rows = xp_rows(guild, row, db.today_utc())
        capped = sum(g is not None and g >= XP_CAP for *_, g in rows)
        missing = sum(g is None for *_, g in rows)
        embed.add_field(name="XP today", value=f"Hit cap **{capped}** · under **{len(rows) - capped - missing}** · "
                        f"not logged **{missing}**\n`{board.bar(capped, len(rows))}`")
        embed.add_field(name="XP requests pending", value=f"**{len(db.xp_pending(bot.db, guild.id))}** in "
                        f"<#{row['xp_review_channel_id']}>")
        counts = board.xp_cap_counts(bot.db, guild.id, tracked, db.today_utc())
        embed.add_field(name="XP this week (hit cap)", inline=False, value="```\n" + "\n".join(
            f"{d:%a %d}  {board.bar(n, len(rows))}  {n}" for d, n in counts) + "\n```")

    db_path = Path(os.getenv("DB_PATH", "donations.db"))
    size = f"{db_path.stat().st_size / 1024:,.0f} KB" if db_path.is_file() else "unknown"
    last_backup = backup.latest_backup(os.getenv("BACKUP_DIR", "backups"))
    embed.add_field(name="Bot", inline=False,
                    value=f"Version {VERSION} · up since <t:{int(STARTED.timestamp())}:R>\n"
                          f"Database {size} · last daily backup: {last_backup[11:21] if last_backup else 'none yet'}")
    channels = [("Panel", row["panel_channel_id"]), ("Boss polls", row["boss_channel_id"]),
             ("Attendance", row["attend_channel_id"]), ("Data", row["data_channel_id"]),
             ("Attendance data", row["attend_data_channel_id"]), ("Staff", row["staff_channel_id"]),
             ("Welcome", row["welcome_channel_id"]), ("XP", row["xp_channel_id"]),
             ("XP requests", row["xp_review_channel_id"])]
    embed.add_field(name="Channels", inline=False, value="\n".join(
        f"{name}: {f'<#{cid}>' if cid else 'not set'}" for name, cid in channels))
    await interaction.response.send_message(embed=brand(embed, guild, f"Version {VERSION}"), view=DashboardView(),
                                            ephemeral=True)


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
