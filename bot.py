# bot.py
import os
import json
import asyncio
from typing import Optional, Dict, Any, List
from datetime import datetime

import discord
from discord import app_commands
from discord.ext import commands
from aiohttp import web

# ---------- Configuration ----------
# Environment variables used:
# DISCORD_TOKEN -> bot token
# GUILD_ID -> integer guild id (for registering guild commands)
# PORT -> (optional) port for the tiny webserver Render requires (set automatically on Render)
GUILD_ID = int(os.environ.get("GUILD_ID", "0"))  # set on Render and locally via env
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN")
PARTIES_FILE = "parties.json"
ROLE_ALLOWED = "Boss Carrier"         # role that can use /createparty
CATEGORY_NAME = "Temp Boss Parties"  # category where temp channels will be created
MAX_SLOTS = 6                        # total slots per party (including carrier)
# ------------------------------------

intents = discord.Intents.default()
intents.message_content = False  # not needed
intents.members = True           # needed to look up members and set perms

bot = commands.Bot(command_prefix="!", intents=intents)

# storage in memory: parties: dict[str, dict]
parties: Dict[str, Dict[str, Any]] = {}

# Utility functions
def save_parties():
    with open(PARTIES_FILE, "w") as f:
        json.dump(parties, f, indent=2)

def load_parties():
    global parties
    if os.path.isfile(PARTIES_FILE):
        with open(PARTIES_FILE, "r") as f:
            try:
                parties = json.load(f)
            except:
                parties = {}
    else:
        parties = {}

def next_party_id() -> str:
    """Return next party identifier like 001, 002..."""
    if not parties:
        return "001"
    ids = sorted([int(pid) for pid in parties.keys()])
    nxt = ids[-1] + 1
    return f"{nxt:03d}"

def format_party_summary(p: Dict[str, Any]) -> str:
    filled = 1 + len(p.get("members", []))  # carrier + members
    return (
        f"ID: `{p['id']}`\n"
        f"Carrier: **{p['carrier_name']}** ({p.get('carrier_class','unknown')})\n"
        f"Date/Time: {p.get('date_time') or 'Unscheduled'}\n"
        f"Slots: `{filled}/{MAX_SLOTS}`\n"
    )

async def ensure_category(guild: discord.Guild) -> discord.CategoryChannel:
    """Find or create the category by name."""
    category = discord.utils.get(guild.categories, name=CATEGORY_NAME)
    if category:
        return category
    return await guild.create_category(CATEGORY_NAME, reason="OF Bot - create temp party category")

async def create_party_channel(guild: discord.Guild, party_id: str, carrier: discord.Member) -> discord.TextChannel:
    category = await ensure_category(guild)
    # Create channel with restrictive perms
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),  # hide from @everyone
        carrier: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_messages=True)
    }
    channel_name = f"party-{party_id}"
    channel = await guild.create_text_channel(channel_name, overwrites=overwrites, category=category, reason="OF Bot party channel")
    return channel

async def add_member_to_channel(channel: discord.TextChannel, member: discord.Member):
    await channel.set_permissions(member, view_channel=True, send_messages=True, read_messages=True)

async def remove_member_from_channel(channel: discord.TextChannel, member: discord.Member):
    await channel.set_permissions(member, overwrite=None)

@bot.event
async def on_ready():
    load_parties()
    # Register commands to the guild for fast propagation
    if GUILD_ID != 0:
        try:
            guild = discord.Object(id=GUILD_ID)
            await bot.tree.sync(guild=guild)
            print(f"Commands synced to guild {GUILD_ID}")
        except Exception as e:
            print("Error syncing commands:", e)
    else:
        await bot.tree.sync()
        print("Commands synced globally (no GUILD_ID set).")
    print(f"Bot ready. Logged in as {bot.user} ({bot.user.id})")

# ---------- Slash commands ----------

@bot.tree.command(name="createparty", description="Create a new boss party (Boss Carrier role only).")
@app_commands.describe(carrier_class="Your character class", date_time="Optional date/time (human readable). Leave empty to create unscheduled party.")
async def createparty(interaction: discord.Interaction, carrier_class: str, date_time: Optional[str] = None):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild
    member = interaction.user

    # role check
    has_role = discord.utils.get(member.roles, name=ROLE_ALLOWED)
    if not has_role:
        await interaction.followup.send(f"Only members with the `{ROLE_ALLOWED}` role can create parties.", ephemeral=True)
        return

    # create party record
    pid = next_party_id()
    channel = await create_party_channel(guild, pid, member)
    # store party: id, carrier_id, carrier_name, class, date_time, members (list of dicts), channel_id
    parties[pid] = {
        "id": pid,
        "carrier_id": member.id,
        "carrier_name": str(member),
        "carrier_class": carrier_class,
        "date_time": date_time,
        "members": [],  # other members (non-carrier)
        "channel_id": channel.id,
        "created_at": datetime.utcnow().isoformat()
    }
    save_parties()

    # post initial message inside the party channel with party info
    summary = format_party_summary(parties[pid])
    await channel.send(f"**Party {pid}** created by {member.mention}\n{summary}\nTo join use `/joinparty {pid}`. Carrier can end the party with `/endparty {pid}`.")
    # confirm to user
    await interaction.followup.send(f"Party `{pid}` created successfully. Temporary channel: {channel.mention}", ephemeral=True)

@bot.tree.command(name="joinparty", description="Join an existing party using its party ID (e.g. 001).")
@app_commands.describe(party_id="Party ID to join (e.g. 001)")
async def joinparty(interaction: discord.Interaction, party_id: str):
    await interaction.response.defer(ephemeral=True)
    pid = party_id.strip()
    if pid not in parties:
        await interaction.followup.send(f"Party `{pid}` not found.", ephemeral=True)
        return

    p = parties[pid]
    # compute current filled
    filled = 1 + len(p.get("members", []))
    if filled >= MAX_SLOTS:
        await interaction.followup.send(f"Party `{pid}` is full.", ephemeral=True)
        return

    user = interaction.user
    # prevent double join
    if any(m["id"] == user.id for m in p["members"]) or user.id == p["carrier_id"]:
        await interaction.followup.send("You are already in this party.", ephemeral=True)
        return

    # add to party list
    p["members"].append({"id": user.id, "name": str(user)})
    save_parties()

    # add perms to channel and post update
    chan = interaction.guild.get_channel(p["channel_id"])
    if chan:
        await add_member_to_channel(chan, user)
        await chan.send(f"{user.mention} has joined the party `{pid}`. ({len(p['members']) + 1}/{MAX_SLOTS})")

    await interaction.followup.send(f"You've joined party `{pid}`!", ephemeral=True)

@bot.tree.command(name="partyavailable", description="List all active parties with open slots.")
async def partyavailable(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    if not parties:
        await interaction.followup.send("No active parties found.", ephemeral=True)
        return
    out_lines = []
    for pid, p in parties.items():
        filled = 1 + len(p.get("members", []))
        if filled < MAX_SLOTS:
            out_lines.append(format_party_summary(p))
    if not out_lines:
        await interaction.followup.send("No parties currently have open slots.", ephemeral=True)
        return
    await interaction.followup.send("\n\n".join(out_lines), ephemeral=True)

@bot.tree.command(name="partyinfo", description="Show full information for a specific party.")
@app_commands.describe(party_id="Party ID (e.g. 001)")
async def partyinfo(interaction: discord.Interaction, party_id: str):
    await interaction.response.defer(ephemeral=True)
    pid = party_id.strip()
    if pid not in parties:
        await interaction.followup.send(f"Party `{pid}` not found.", ephemeral=True)
        return
    p = parties[pid]
    members = p.get("members", [])
    member_list = "\n".join([f"- {m['name']}" for m in members]) if members else "No members yet."
    filled = 1 + len(members)
    chan = interaction.guild.get_channel(p.get("channel_id"))
    chan_mention = chan.mention if chan else "Channel not found"
    text = (
        f"**Party `{pid}`**\n"
        f"Carrier: **{p['carrier_name']}** ({p.get('carrier_class')})\n"
        f"Date/Time: {p.get('date_time') or 'Unscheduled'}\n"
        f"Channel: {chan_mention}\n"
        f"Slots: `{filled}/{MAX_SLOTS}`\n\n"
        f"Members:\n{member_list}"
    )
    await interaction.followup.send(text, ephemeral=True)

@bot.tree.command(name="leaveparty", description="Leave a party you previously joined.")
@app_commands.describe(party_id="Party ID to leave (e.g. 001)")
async def leaveparty(interaction: discord.Interaction, party_id: str):
    await interaction.response.defer(ephemeral=True)
    pid = party_id.strip()
    if pid not in parties:
        await interaction.followup.send(f"Party `{pid}` not found.", ephemeral=True)
        return
    p = parties[pid]
    user = interaction.user
    # cannot let carrier leave using this command
    if user.id == p["carrier_id"]:
        await interaction.followup.send("As the carrier, you cannot use /leaveparty. Use /endparty to close the party.", ephemeral=True)
        return
    # remove from members list
    new_members = [m for m in p["members"] if m["id"] != user.id]
    if len(new_members) == len(p["members"]):
        await interaction.followup.send("You are not a member of that party.", ephemeral=True)
        return
    p["members"] = new_members
    save_parties()
    chan = interaction.guild.get_channel(p.get("channel_id"))
    if chan:
        await remove_member_from_channel(chan, user)
        await chan.send(f"{user.mention} has left the party `{pid}`.")
    await interaction.followup.send(f"You left party `{pid}`.", ephemeral=True)

@bot.tree.command(name="endparty", description="End (close) a party. Only the carrier or server admins can end it.")
@app_commands.describe(party_id="Party ID to end (e.g. 001)")
async def endparty(interaction: discord.Interaction, party_id: str):
    await interaction.response.defer(ephemeral=True)
    pid = party_id.strip()
    if pid not in parties:
        await interaction.followup.send(f"Party `{pid}` not found.", ephemeral=True)
        return
    p = parties[pid]
    user = interaction.user
    # allow if user is carrier or has manage_guild
    if user.id != p["carrier_id"] and not interaction.user.guild_permissions.manage_guild:
        await interaction.followup.send("Only the carrier or a server admin (Manage Server) can end this party.", ephemeral=True)
        return
    # delete channel if exists
    chan = interaction.guild.get_channel(p.get("channel_id"))
    if chan:
        try:
            await chan.delete(reason=f"OF Bot - party {pid} ended by {user}")
        except Exception as e:
            print("Failed to delete channel:", e)
    # remove party record
    del parties[pid]
    save_parties()
    await interaction.followup.send(f"Party `{pid}` ended and channel removed.", ephemeral=True)

@bot.tree.command(name="help_ofbot", description="Show OF Bot commands and usage.")
async def help_ofbot(interaction: discord.Interaction):
    text = (
        "**OF Bot Commands**\n"
        "/createparty  - Create a new party (Boss Carrier role only).\n"
        "/joinparty <id> - Join a party with ID (e.g. 001).\n"
        "/partyavailable - List parties with open slots.\n"
        "/partyinfo <id> - Show party details.\n"
        "/leaveparty <id> - Leave a party (non-carrier).\n"
        "/endparty <id> - End (close) a party (carrier or admin).\n"
        "Notes: Channels are created under the category 'Temp Boss Parties'.\n"
    )
    await interaction.response.send_message(text, ephemeral=True)

# Cleanup: If someone manually deletes a channel, remove party record
@bot.event
async def on_guild_channel_delete(channel):
    # if this channel corresponds to a party, remove it
    for pid, p in list(parties.items()):
        if p.get("channel_id") == channel.id:
            del parties[pid]
    save_parties()

# A small webserver so Render is happy (it expects a web service listening on $PORT)
async def handle_root(request):
    return web.Response(text="OF Bot is running.")

def start_webserver():
    port = int(os.environ.get("PORT", 5000))
    app = web.Application()
    app.router.add_get("/", handle_root)
    runner = web.AppRunner(app)
    loop = asyncio.get_event_loop()
    async def _run():
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", port)
        await site.start()
    loop.create_task(_run())

if __name__ == "__main__":
    if not DISCORD_TOKEN:
        print("Error: DISCORD_TOKEN environment variable not set.")
        exit(1)
    # start small webserver for Render
    start_webserver()
    # --- Keepalive Webserver for Render ---
import threading
from aiohttp import web
import os
import asyncio

async def handle(request):
    return web.Response(text="OF Bot is running!")

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", 8080)))
    await site.start()

loop = asyncio.get_event_loop()
loop.create_task(start_web_server())
# --- End Keepalive Webserver ---
import os
TOKEN = os.getenv("DISCORD_TOKEN")
bot.run(TOKEN)
