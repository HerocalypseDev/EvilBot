import asyncio
import io
import json
import logging
import logging.handlers
import os
from pathlib import Path
from datetime import datetime, timezone

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
                    RECENT_DONORS, REMINDER_MINUTES)

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


def board_embed(data):
    embed = discord.Embed(
        title="Evil Donations — guild board",
        description=f"Today (UTC): **{data['today_count']}** donated\n"
                    f"Resets <t:{data['reset_ts']}:t> (your local time)")
    if data["recent"]:  # three inline fields line up as columns: one row per donor
        embed.add_field(name=f"Discord name (last {RECENT_DONORS})",
                        value="\n".join(board.short(name) for name, _, _ in data["recent"]))
        embed.add_field(name="In-game name", value="\n".join(board.short(ign) for _, ign, _ in data["recent"]))
        embed.add_field(name="Donated at", value="\n".join(f"<t:{ts}:t>" for _, _, ts in data["recent"]))
    else:
        embed.add_field(name=f"Last {RECENT_DONORS} donors", value="Nobody yet.", inline=False)
    embed.add_field(name=f"Not donated yet ({len(data['waiting'])})",
                    value=board.join_capped([discord.utils.escape_markdown(n) for n in data["waiting"]])
                    or "Everyone has donated.", inline=False)
    embed.add_field(name="Yesterday (UTC)",
                    value=f"Donated: {data['yesterday_count']} · Coins: {data['yesterday_coins']:,}",
                    inline=False)
    embed.set_footer(text="Press Log donation once per day, after you donate in the game.")
    return embed


_board_lock = asyncio.Lock()  # two quick refreshes must not post two boards
_data_lock = asyncio.Lock()


async def refresh_board(guild):
    await refresh_board_message(guild)
    await refresh_data(guild)


# --- data channel: a header message plus Donated / Not donated / Not linked, 15 rows per page ---

DATA_SECTIONS = {
    "donated": ("Donated today", ("Discord name", "In-game name", "Donated at")),
    "not_donated": ("Not donated yet", ("Discord name", "In-game name")),
    "not_linked": ("Not linked", ("In-game name",)),
}
data_pages = {}  # (guild_id, section) -> page shown. Resets to page 1 after a restart.


def data_header_embed(sections):
    counts = {key: len(sections[key]) for key in DATA_SECTIONS}
    return discord.Embed(
        title="Data",
        description=f"{db.today_utc()} (UTC) · resets <t:{board.next_reset_ts()}:t> (your local time)\n"
                    f"Donated: **{counts['donated']}** · Not donated: **{counts['not_donated']}** · "
                    f"Not linked: **{counts['not_linked']}**")


def data_section_embed(guild_id, key, rows):
    title, columns = DATA_SECTIONS[key]
    chunk, page, pages = board.paginate(rows, data_pages.get((guild_id, key), 0), DATA_PAGE)
    data_pages[(guild_id, key)] = page
    embed = discord.Embed(title=f"{title} ({len(rows)})")
    if chunk:
        for n, column in enumerate(columns):
            values = [f"<t:{row[n]}:t>" if column == "Donated at" else board.short(row[n]) for row in chunk]
            embed.add_field(name=column, value="\n".join(values))
    else:
        embed.description = "Nobody."
    embed.set_footer(text=f"Page {page + 1}/{pages}")
    return embed


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
            sections = board.data_sections(bot.db, guild.id, tracked_members(guild, row["member_role_id"]),
                                           db.today_utc())
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
        ids = json.loads(row["data_message_ids"] or "[]")
        embeds = [data_header_embed(sections)] + [data_section_embed(guild.id, key, sections[key])
                                                  for key in DATA_SECTIONS]
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
            for key, embed in zip(DATA_SECTIONS, embeds[1:]):
                posted.append(await channel.send(embed=embed, view=DataView(key)))
        except discord.HTTPException as e:
            log.warning("Data post failed in guild %s: %s", guild.id, e)
            return str(e)
        db.save_guild(bot.db, guild.id, data_message_ids=json.dumps([m.id for m in posted]))


async def refresh_board_message(guild):
    """Update the board. If something was posted below it, move it to the bottom of the channel."""
    async with _board_lock:
        row = db.get_guild(bot.db, guild.id)
        channel = row and guild.get_channel(row["panel_channel_id"])
        if not channel or not row["status_message_id"]:
            return
        data = board.board_data(bot.db, guild.id, tracked_members(guild, row["member_role_id"]),
                                db.today_utc(), db.yesterday_utc())
        embed = board_embed(data)
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
                "Link your in-game name first with /link.", ephemeral=True)
        try:
            db.record(bot.db, interaction.guild_id, interaction.user.id,
                      interaction.user.display_name, ign, db.today_utc())
        except db.AlreadyDonated:
            return await interaction.response.send_message(
                "You already logged a donation today.", ephemeral=True)
        await interaction.response.send_message(
            f"Logged {DONATION_AMOUNT:,} for today.", ephemeral=True)
        await refresh_board(interaction.guild)


class HistoryView(discord.ui.View):
    """10 donations per page with Previous / Next buttons."""

    def __init__(self, name, rows):
        super().__init__(timeout=300)
        self.name, self.rows, self.page = name, rows, 0
        self._update_buttons()

    def text(self):
        chunk, self.page, pages = board.paginate(self.rows, self.page)
        lines = [f"#{r['id']}  {r['day_utc']}  {r['amount']:,}  {r['status']}" for r in chunk]
        return f"**{self.name}**  page {self.page + 1}/{pages}\n" + "\n".join(lines)

    def _update_buttons(self):
        pages = board.paginate(self.rows, self.page)[2]
        self.previous.disabled = self.page <= 0
        self.next.disabled = self.page >= pages - 1

    async def _show(self, interaction):
        text = self.text()  # also clamps self.page
        self._update_buttons()
        await interaction.response.edit_message(content=text, view=self)

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
        for key in DATA_SECTIONS:
            self.add_view(DataView(key))
        await self.tree.sync()
        tick.start()

    async def close(self):
        if not self.is_closed():
            await post_offline_notice()
        await super().close()


bot = Bot()


async def post_offline_notice():
    """Replace each board with an offline notice and no button. Runs on /stop and Ctrl+C."""
    notice = discord.Embed(title="Evil Donations — offline",
                           description="Bot offline. Do not log donations. Contact an officer.")
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


async def post_boss_poll(guild, channel, role_id, cycle_start):
    message = await channel.send(boss.poll_text(f"<@&{role_id}>", cycle_start))
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
    if latest is None:
        return await post_boss_poll(guild, channel, role.id, boss.next_cycle_start(now))
    cycle = iso_utc(latest["cycle_start"])
    if latest["resolved_time"]:
        if now >= iso_utc(latest["resolved_time"]) + boss.NEXT_POLL_DELAY:
            await post_boss_poll(guild, channel, role.id, boss.upcoming_cycle(cycle + boss.CYCLE, now))
        return
    times = boss.slot_times(cycle)
    ahead = [t for t in times if t > now]
    if not ahead:  # every time passed without a ping
        db.resolve_boss_poll(bot.db, guild.id, latest["cycle_start"], times[-1].isoformat())
        return
    if now < ahead[0] - boss.LEAD:  # no ping can be due yet, so skip counting votes
        return
    votes = await count_votes(guild, channel, latest["message_id"], role.id)
    pick = boss.pick_ping(now, cycle, votes)
    # the slot is claimed before sending, so a restart never double-pings
    if pick is not None and db.resolve_boss_poll(bot.db, guild.id, latest["cycle_start"],
                                                 times[pick].isoformat()):
        warn_if_role_not_pinged(await channel.send(boss.ping_text(role.mention, times[pick])), role.id)
        log.info("Boss ping for %s in guild %s, votes %s", times[pick].isoformat(), guild.id, votes)


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
    for guild in bot.guilds:  # brings the board and button back after a /stop or crash
        try:  # an error here would stop the loop from ever starting
            await refresh_board(guild)
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
    await refresh_board(interaction.guild)
    await interaction.followup.send(f"Panel posted in {panel_channel.mention}.", ephemeral=True)


@bot.tree.command(description="Link your in-game name.")
@app_commands.guild_only()
@app_commands.describe(ingame_username="Pick your in-game name from the list")
async def link(interaction: discord.Interaction, ingame_username: str):
    try:
        name = db.link(bot.db, interaction.guild_id, interaction.user.id, ingame_username)
    except db.NotInRoster:
        msg = "Pick your name from the list. If it is not there, ask an officer to add it."
    except db.AlreadyLinked:
        msg = "You are already linked. Use /unlink first to change it."
    except db.IgnTaken:
        msg = "That in-game name is already taken."
    else:
        msg = f"Linked to **{name}**."
    await interaction.response.send_message(msg, ephemeral=True)


@link.autocomplete("ingame_username")
async def link_autocomplete(interaction: discord.Interaction, current: str):
    names = db.roster_names(bot.db, interaction.guild_id, current, only_free=True, limit=25)
    return [app_commands.Choice(name=n, value=n) for n in names]


roster_group = app_commands.Group(name="roster", description="The in-game names members pick in /link.",
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


@roster_group.command(name="remove", description="Staff: remove a name from the list.")
async def roster_remove(interaction: discord.Interaction, name: str):
    if not await require_staff(interaction):
        return
    if db.roster_remove(bot.db, interaction.guild_id, name):
        msg = f"Removed **{name}**. Anyone already linked to it stays linked."
    else:
        msg = "That name is not on the list."
    await interaction.response.send_message(msg, ephemeral=True)


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


@bot.tree.command(description="Remove a link. Staff can unlink other members.")
@app_commands.guild_only()
@app_commands.describe(member="Staff can also paste the user ID of someone who left the server")
async def unlink(interaction: discord.Interaction, member: discord.User | None = None):
    target = member or interaction.user  # User, not Member: a member who left keeps their name taken
    if target.id != interaction.user.id and not is_staff(interaction):
        msg = "Only staff can unlink other members."
    elif db.unlink(bot.db, interaction.guild_id, target.id):
        msg = f"Unlinked {target.display_name}."
    else:
        msg = f"{target.display_name} has no link."
    await interaction.response.send_message(msg, ephemeral=True)


@bot.tree.command(description="Show donation history.")
@app_commands.guild_only()
async def history(interaction: discord.Interaction, member: discord.Member | None = None):
    target = member or interaction.user
    if target != interaction.user and not is_staff(interaction):
        return await interaction.response.send_message(
            "You can only see your own history.", ephemeral=True)
    rows = db.history(bot.db, interaction.guild_id, target.id)
    if not rows:
        return await interaction.response.send_message(
            f"No donations for {target.display_name}.", ephemeral=True)
    view = HistoryView(target.display_name, rows)
    await interaction.response.send_message(view.text(), view=view, ephemeral=True)


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
    await refresh_board(interaction.guild)


@bot.tree.command(description="Staff: cancel a donation by its id (see /history or /export).")
@app_commands.guild_only()
async def undo(interaction: discord.Interaction, submission_id: int):
    if not await require_staff(interaction):
        return
    if db.void(bot.db, interaction.guild_id, submission_id, interaction.user.id):
        await interaction.response.send_message(f"Voided donation #{submission_id}.", ephemeral=True)
        await refresh_board(interaction.guild)
    else:
        await interaction.response.send_message("That donation is not active.", ephemeral=True)


@bot.tree.command(description="Staff: post the offline notice and shut the bot down.")
@app_commands.guild_only()
async def stop(interaction: discord.Interaction):
    if not await require_staff(interaction):
        return
    await interaction.response.send_message("Stopping. Start the bot again to bring the button back.",
                                            ephemeral=True)
    await bot.close()


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


@setup.error
async def on_setup_error(interaction: discord.Interaction, error):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message("You need Manage Server.", ephemeral=True)
    else:
        raise error


if __name__ == "__main__":
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise SystemExit("DISCORD_TOKEN missing. Put it in .env.")
    log_dir = Path(os.getenv("LOG_DIR", "logs"))
    log_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO, format="[%(asctime)s] [%(levelname)-7s] %(name)s: %(message)s",
        handlers=[logging.StreamHandler(),
                  logging.handlers.RotatingFileHandler(log_dir / "bot.log", maxBytes=1_000_000,
                                                       backupCount=3, encoding="utf-8")])
    try:
        bot.run(token, log_handler=None)
    except Exception:
        log.exception("Bot crashed")
        raise SystemExit(1)  # nonzero: run_bot.ps1 restarts it
