import asyncio
import io
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
import db
import reminders
from config import (BACKUP_HOUR_UTC, DONATION_AMOUNT, MEMBER_ROLE_NAME, RECENT_DONORS,
                    REMINDER_MINUTES)

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
    recent = "\n".join(f"{ign} — {amount:,} coins" for ign, amount in data["recent"])
    embed.add_field(name=f"Last {RECENT_DONORS} donors", value=recent or "Nobody yet.", inline=False)
    embed.add_field(name=f"Not donated yet ({len(data['waiting'])})",
                    value=board.join_capped(data["waiting"]) or "Everyone has donated.", inline=False)
    embed.add_field(name="Yesterday (UTC)",
                    value=f"Donated: {data['yesterday_count']} · Coins: {data['yesterday_coins']:,}",
                    inline=False)
    embed.set_footer(text="Press Log donation once per day, after you donate in the game.")
    return embed


_board_lock = asyncio.Lock()  # two quick refreshes must not post two boards


async def refresh_board(guild):
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


@tasks.loop(seconds=60)
async def tick():
    """Every minute: refresh the board on a new UTC day, send due reminders, write the daily backup."""
    now = datetime.now(timezone.utc)
    today = now.date().isoformat()
    for guild in bot.guilds:
        row = db.get_guild(bot.db, guild.id)
        if not row:
            continue
        try:
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
            log.exception("backup failed")


@tick.before_loop
async def before_tick():
    await bot.wait_until_ready()
    for guild in bot.guilds:  # brings the board and button back after a /stop or crash
        await refresh_board(guild)


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
    status = await panel_channel.send(embed=discord.Embed(description="Loading board..."),
                                      view=PanelView())
    db.save_guild(bot.db, interaction.guild_id, panel_channel_id=panel_channel.id,
                  status_message_id=status.id,
                  officer_role_id=officer_role.id, member_role_id=member_role.id)
    await refresh_board(interaction.guild)
    await interaction.followup.send(f"Panel posted in {panel_channel.mention}.", ephemeral=True)


@bot.tree.command(description="Link your in-game name.")
@app_commands.guild_only()
async def link(interaction: discord.Interaction, ign: str):
    try:
        name = db.link(bot.db, interaction.guild_id, interaction.user.id, ign)
    except ValueError as e:
        msg = str(e)
    except db.AlreadyLinked:
        msg = "You are already linked. Use /unlink first to change it."
    except db.IgnTaken:
        msg = "That in-game name is already taken."
    else:
        msg = f"Linked to **{name}**."
    await interaction.response.send_message(msg, ephemeral=True)


@bot.tree.command(description="Remove a link. Staff can unlink other members.")
@app_commands.guild_only()
async def unlink(interaction: discord.Interaction, member: discord.Member | None = None):
    target = member or interaction.user
    if target != interaction.user and not is_staff(interaction):
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
