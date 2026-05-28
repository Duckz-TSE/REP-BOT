"""
Rep Bot — a teammate reputation system for your macro group.

Commands (slash):
  /rep    @user <comment>    -> give +rep with a comment
  /derep  @user <comment>    -> give -rep with a comment
  /profile @user             -> see someone's score + recent comments
  /leaderboard               -> top-repped members in the server

Rules:
  - Unlimited votes per person
  - Free-text comment required on every vote
  - You cannot rep yourself
  - Voter + comment are public

Setup is at the bottom of this file (look for "HOW TO RUN").
"""

import os
import sqlite3
import datetime

import discord
from discord import app_commands

# ---------------------------------------------------------------------------
# Database — a tiny SQLite file living next to the bot. Stores every vote so
# you keep full history (who repped who, when, the comment, +1 or -1).
# ---------------------------------------------------------------------------

DB_PATH = "reps.db"


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS reps (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id  TEXT NOT NULL,
            target_id TEXT NOT NULL,   -- person receiving the rep
            voter_id  TEXT NOT NULL,   -- person giving the rep
            value     INTEGER NOT NULL,-- +1 or -1
            comment   TEXT NOT NULL,
            created   TEXT NOT NULL    -- ISO timestamp
        )
        """
    )
    conn.commit()
    conn.close()


def add_rep(guild_id, target_id, voter_id, value, comment):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO reps (guild_id, target_id, voter_id, value, comment, created) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            str(guild_id),
            str(target_id),
            str(voter_id),
            value,
            comment,
            datetime.datetime.utcnow().isoformat(),
        ),
    )
    conn.commit()
    conn.close()


def get_totals(guild_id, target_id):
    """Return (positive_count, negative_count, net_score)."""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute(
        "SELECT "
        "  COALESCE(SUM(CASE WHEN value = 1 THEN 1 ELSE 0 END), 0), "
        "  COALESCE(SUM(CASE WHEN value = -1 THEN 1 ELSE 0 END), 0) "
        "FROM reps WHERE guild_id = ? AND target_id = ?",
        (str(guild_id), str(target_id)),
    )
    pos, neg = cur.fetchone()
    conn.close()
    return pos, neg, pos - neg


def get_recent(guild_id, target_id, limit=5):
    """Return recent votes as a list of (value, voter_id, comment, created)."""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute(
        "SELECT value, voter_id, comment, created FROM reps "
        "WHERE guild_id = ? AND target_id = ? ORDER BY id DESC LIMIT ?",
        (str(guild_id), str(target_id), limit),
    )
    rows = cur.fetchall()
    conn.close()
    return rows


def get_leaderboard(guild_id, limit=10):
    """Return [(target_id, net_score, pos, neg), ...] ranked by net score."""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute(
        "SELECT target_id, "
        "  SUM(value) AS net, "
        "  SUM(CASE WHEN value = 1 THEN 1 ELSE 0 END) AS pos, "
        "  SUM(CASE WHEN value = -1 THEN 1 ELSE 0 END) AS neg "
        "FROM reps WHERE guild_id = ? "
        "GROUP BY target_id ORDER BY net DESC LIMIT ?",
        (str(guild_id), limit),
    )
    rows = cur.fetchall()
    conn.close()
    return rows


# ---------------------------------------------------------------------------
# Bot setup
# ---------------------------------------------------------------------------

intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)


@client.event
async def on_ready():
    init_db()
    await tree.sync()  # registers slash commands with Discord
    print(f"Logged in as {client.user}. Slash commands synced.")


# --- shared helper so /rep and /derep don't duplicate logic ----------------
async def _do_vote(interaction: discord.Interaction, member: discord.Member,
                   comment: str, value: int):
    # block self-rep
    if member.id == interaction.user.id:
        await interaction.response.send_message(
            "You can't rep yourself. Nice try.", ephemeral=True
        )
        return
    # ignore bots
    if member.bot:
        await interaction.response.send_message(
            "Bots don't need rep.", ephemeral=True
        )
        return

    add_rep(interaction.guild_id, member.id, interaction.user.id, value, comment)
    pos, neg, net = get_totals(interaction.guild_id, member.id)

    if value == 1:
        title = f"➕ +rep for {member.display_name}"
        color = discord.Color.green()
    else:
        title = f"➖ -rep for {member.display_name}"
        color = discord.Color.red()

    embed = discord.Embed(title=title, description=f'"{comment}"', color=color)
    embed.add_field(name="From", value=interaction.user.mention, inline=True)
    embed.add_field(name="Net score", value=f"**{net}**  ({pos}↑ / {neg}↓)",
                    inline=True)
    await interaction.response.send_message(embed=embed)


@tree.command(name="rep", description="Give someone +rep with a comment")
@app_commands.describe(member="Who are you repping?",
                       comment="Why? e.g. carried hard, never DC'd")
async def rep(interaction: discord.Interaction, member: discord.Member,
              comment: str):
    await _do_vote(interaction, member, comment, +1)


@tree.command(name="derep", description="Give someone -rep with a comment")
@app_commands.describe(member="Who are you derepping?",
                       comment="Why? e.g. DC'd 3 times, left early")
async def derep(interaction: discord.Interaction, member: discord.Member,
                comment: str):
    await _do_vote(interaction, member, comment, -1)


@tree.command(name="profile", description="See someone's rep profile")
@app_commands.describe(member="Whose profile? (leave blank for yourself)")
async def profile(interaction: discord.Interaction,
                  member: discord.Member = None):
    member = member or interaction.user
    pos, neg, net = get_totals(interaction.guild_id, member.id)
    recent = get_recent(interaction.guild_id, member.id, limit=5)

    embed = discord.Embed(
        title=f"📊 {member.display_name}'s rep",
        color=discord.Color.blurple(),
    )
    embed.add_field(name="Net score", value=f"**{net}**", inline=True)
    embed.add_field(name="👍 Positive", value=str(pos), inline=True)
    embed.add_field(name="👎 Negative", value=str(neg), inline=True)

    if recent:
        lines = []
        for value, voter_id, comment, created in recent:
            sign = "➕" if value == 1 else "➖"
            lines.append(f'{sign} <@{voter_id}>: "{comment}"')
        embed.add_field(name="Recent", value="\n".join(lines), inline=False)
    else:
        embed.add_field(name="Recent", value="No reps yet.", inline=False)

    await interaction.response.send_message(embed=embed)


@tree.command(name="leaderboard",
              description="Top-repped members in this server")
async def leaderboard(interaction: discord.Interaction):
    rows = get_leaderboard(interaction.guild_id, limit=10)
    if not rows:
        await interaction.response.send_message("No reps yet in this server.")
        return

    medals = ["🥇", "🥈", "🥉"]
    lines = []
    for i, (target_id, net, pos, neg) in enumerate(rows):
        prefix = medals[i] if i < 3 else f"`#{i + 1}`"
        lines.append(f"{prefix} <@{target_id}> — **{net}** ({pos}↑ / {neg}↓)")

    embed = discord.Embed(
        title="🏆 Rep Leaderboard",
        description="\n".join(lines),
        color=discord.Color.gold(),
    )
    await interaction.response.send_message(embed=embed)


# ---------------------------------------------------------------------------
# HOW TO RUN
# ---------------------------------------------------------------------------
# 1. Make a bot: https://discord.com/developers/applications
#       -> New Application -> Bot tab -> Reset/Copy the TOKEN.
# 2. In that same Bot tab, no special intents are needed (this bot only uses
#    default intents + slash commands).
# 3. Invite it: OAuth2 -> URL Generator -> scopes: "bot" + "applications.commands"
#       -> bot permissions: "Send Messages". Open the generated URL, pick your server.
# 4. Set your token as an environment variable named DISCORD_TOKEN
#    (on Replit/Railway use their Secrets tab — do NOT paste it in the code).
# 5. Run this file.  First launch registers the slash commands (can take a
#    minute to appear in Discord).
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        raise SystemExit(
            "No DISCORD_TOKEN found. Set it as an environment variable / secret."
        )
    client.run(token)
