import os

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

import db
from config import DONATION_AMOUNT, MEMBER_ROLE_NAME

load_dotenv()

intents = discord.Intents.default()
intents.members = True  # privileged: needed for role checks and the not-donated list


def is_staff(interaction):
    user = interaction.user
    if user.guild_permissions.manage_guild:
        return True
    row = db.get_guild(bot.db, interaction.guild_id)
    return bool(row and row["officer_role_id"] and user.get_role(row["officer_role_id"]))


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


class Bot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix=commands.when_mentioned, intents=intents)
        self.db = db.connect(os.getenv("DB_PATH", "donations.db"))

    async def setup_hook(self):
        self.add_view(PanelView())  # keeps the panel button working after a restart
        await self.tree.sync()


bot = Bot()


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
    panel = await panel_channel.send("**Evil Donations**\nPress the button to log today's donation.",
                                     view=PanelView())
    board = await panel_channel.send("Status board will appear here.")  # real board comes in M4
    db.save_guild(bot.db, interaction.guild_id, panel_channel_id=panel_channel.id,
                  panel_message_id=panel.id, status_message_id=board.id,
                  officer_role_id=officer_role.id, member_role_id=member_role.id)
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
        msg = f"No donations for {target.display_name}."
    else:
        lines = [f"#{r['id']}  {r['day_utc']}  {r['amount']:,}  {r['status']}" for r in rows[:20]]
        msg = f"**{target.display_name}**\n" + "\n".join(lines)
    await interaction.response.send_message(msg, ephemeral=True)


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
    bot.run(token)
