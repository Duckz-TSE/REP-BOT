"""
Rep Bot — a per-role teammate reputation system for your macro group.

Idea: people rep someone FOR A SPECIFIC ROLE (e.g. "carry", "npc"). Each role
has its own +/- counts per member. When a member's standing in a role is good
enough, the bot automatically gives them the matching Discord role; if their
standing drops, it removes it.

ROLE RULE — a member HAS a rep-role when BOTH are true:
    * at least 5 +reps in that role, AND
    * +reps >= 2x -reps   (positive is at least double negative)
Otherwise the bot removes it. It re-checks after every vote, so the role can
come back on its own once the ratio recovers (no need to re-hit 5).

Commands (slash):
  /addrole   <name> <@discordrole>   (admin) link a rep-role to a Discord role
  /removerole <name>                 (admin) unlink a rep-role
  /roles                             list all rep-roles
  /rep   <name> @user <comment>      +rep someone for a role
  /derep <name> @user <comment>      -rep someone for a role
  /profile @user                     see someone's standing across all roles
  /leaderboard <name>                ranking for one role

Voting limit: up to 2 +rep AND 2 -rep per voter, per target, PER ROLE,
per rolling 24h. Comment required. No self-rep.

Setup notes are at the bottom (HOW TO RUN) — note the new Members Intent step.
"""

import os
import sqlite3
import datetime

import discord
from discord import app_commands

DB_PATH = "reps.db"

VOTE_LIMIT = 2          # max of each type per voter/target/role per window
WINDOW_HOURS = 24
MIN_POS = 3             # need at least this many +reps to earn a role
RATIO = 2               # +reps must be >= RATIO * -reps to hold the role


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def init_db():
    conn = sqlite3.connect(DB_PATH)
    # every individual vote, now tagged with which rep-role it's for
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS reps (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id  TEXT NOT NULL,
            role_name TEXT NOT NULL,
            target_id TEXT NOT NULL,
            voter_id  TEXT NOT NULL,
            value     INTEGER NOT NULL,
            comment   TEXT NOT NULL,
            created   TEXT NOT NULL
        )
        """
    )
    # mapping of rep-role name -> actual Discord role id, per guild
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS role_map (
            guild_id        TEXT NOT NULL,
            role_name       TEXT NOT NULL,
            discord_role_id TEXT NOT NULL,
            PRIMARY KEY (guild_id, role_name)
        )
        """
    )
    conn.commit()
    conn.close()


# --- role map helpers ------------------------------------------------------

def set_role(guild_id, role_name, discord_role_id):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT OR REPLACE INTO role_map (guild_id, role_name, discord_role_id) "
        "VALUES (?, ?, ?)",
        (str(guild_id), role_name.lower(), str(discord_role_id)),
    )
    conn.commit()
    conn.close()


def unset_role(guild_id, role_name):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute(
        "DELETE FROM role_map WHERE guild_id = ? AND role_name = ?",
        (str(guild_id), role_name.lower()),
    )
    deleted = cur.rowcount
    conn.commit()
    conn.close()
    return deleted > 0


def get_role(guild_id, role_name):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute(
        "SELECT discord_role_id FROM role_map WHERE guild_id = ? AND role_name = ?",
        (str(guild_id), role_name.lower()),
    )
    row = cur.fetchone()
    conn.close()
    return row[0] if row else None


def list_roles(guild_id):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute(
        "SELECT role_name, discord_role_id FROM role_map WHERE guild_id = ? "
        "ORDER BY role_name",
        (str(guild_id),),
    )
    rows = cur.fetchall()
    conn.close()
    return rows


# --- vote helpers ----------------------------------------------------------

def add_rep(guild_id, role_name, target_id, voter_id, value, comment):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO reps "
        "(guild_id, role_name, target_id, voter_id, value, comment, created) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (str(guild_id), role_name.lower(), str(target_id), str(voter_id),
         value, comment, datetime.datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()


def recent_votes_of_type(guild_id, role_name, target_id, voter_id, value):
    """This voter's votes of one type on this target FOR THIS ROLE in window."""
    cutoff = (
        datetime.datetime.utcnow() - datetime.timedelta(hours=WINDOW_HOURS)
    ).isoformat()
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute(
        "SELECT created FROM reps "
        "WHERE guild_id = ? AND role_name = ? AND target_id = ? AND voter_id = ? "
        "AND value = ? AND created >= ? ORDER BY created ASC",
        (str(guild_id), role_name.lower(), str(target_id), str(voter_id),
         value, cutoff),
    )
    rows = [datetime.datetime.fromisoformat(r[0]) for r in cur.fetchall()]
    conn.close()
    return rows


def role_totals(guild_id, role_name, target_id):
    """(positive, negative) counts for one member in one role."""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute(
        "SELECT "
        "  COALESCE(SUM(CASE WHEN value = 1 THEN 1 ELSE 0 END), 0), "
        "  COALESCE(SUM(CASE WHEN value = -1 THEN 1 ELSE 0 END), 0) "
        "FROM reps WHERE guild_id = ? AND role_name = ? AND target_id = ?",
        (str(guild_id), role_name.lower(), str(target_id)),
    )
    pos, neg = cur.fetchone()
    conn.close()
    return pos, neg


def member_role_summary(guild_id, target_id):
    """[(role_name, pos, neg), ...] for every role this member has any rep in."""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute(
        "SELECT role_name, "
        "  SUM(CASE WHEN value = 1 THEN 1 ELSE 0 END), "
        "  SUM(CASE WHEN value = -1 THEN 1 ELSE 0 END) "
        "FROM reps WHERE guild_id = ? AND target_id = ? "
        "GROUP BY role_name ORDER BY role_name",
        (str(guild_id), str(target_id)),
    )
    rows = cur.fetchall()
    conn.close()
    return rows


def role_leaderboard(guild_id, role_name, limit=10):
    """[(target_id, pos, neg), ...] for one role, ranked by net then positive."""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute(
        "SELECT target_id, "
        "  SUM(CASE WHEN value = 1 THEN 1 ELSE 0 END) AS pos, "
        "  SUM(CASE WHEN value = -1 THEN 1 ELSE 0 END) AS neg "
        "FROM reps WHERE guild_id = ? AND role_name = ? "
        "GROUP BY target_id ORDER BY (pos - neg) DESC, pos DESC LIMIT ?",
        (str(guild_id), role_name.lower(), limit),
    )
    rows = cur.fetchall()
    conn.close()
    return rows


# ---------------------------------------------------------------------------
# Core rule
# ---------------------------------------------------------------------------

def qualifies(pos, neg):
    """True if this standing earns the role: >=MIN_POS positives AND pos >= RATIO*neg."""
    return pos >= MIN_POS and pos >= RATIO * neg


def _fmt_remaining(td):
    total_min = max(0, int(td.total_seconds() // 60))
    h, m = divmod(total_min, 60)
    if h and m:
        return f"{h}h {m}m"
    if h:
        return f"{h}h"
    return f"{m}m"


# ---------------------------------------------------------------------------
# Bot setup  — Members Intent is required now (to add/remove roles)
# ---------------------------------------------------------------------------

intents = discord.Intents.default()
intents.members = True
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)


@client.event
async def on_ready():
    init_db()
    await tree.sync()
    print(f"Logged in as {client.user}. Slash commands synced.")


async def _sync_discord_role(interaction, member, role_name):
    """
    After a vote, add or remove the linked Discord role based on the rule.
    Returns a short status string for the embed, or None if no role is linked.
    """
    discord_role_id = get_role(interaction.guild_id, role_name)
    if not discord_role_id:
        return None

    role = interaction.guild.get_role(int(discord_role_id))
    if role is None:
        return "(linked Discord role was deleted — use /addrole to relink)"

    pos, neg = role_totals(interaction.guild_id, role_name, member.id)
    should_have = qualifies(pos, neg)
    has_now = role in member.roles

    try:
        if should_have and not has_now:
            await member.add_roles(role, reason=f"Earned rep-role '{role_name}'")
            return f"✅ {member.display_name} earned **{role.name}**!"
        if not should_have and has_now:
            await member.remove_roles(role, reason=f"Lost rep-role '{role_name}'")
            return f"❌ {member.display_name} lost **{role.name}**."
    except discord.Forbidden:
        return ("⚠️ I can't manage that role — give the bot **Manage Roles** "
                "and drag its role ABOVE the rep-role in Server Settings.")
    return None


# --- shared vote logic -----------------------------------------------------

async def _do_vote(interaction, role_name, member, comment, value):
    role_name = role_name.lower().strip()

    if get_role(interaction.guild_id, role_name) is None:
        await interaction.response.send_message(
            f"There's no rep-role called **{role_name}**. "
            f"An admin can add one with `/addrole`, or see `/roles`.",
            ephemeral=True,
        )
        return
    if member.id == interaction.user.id:
        await interaction.response.send_message(
            "You can't rep yourself. Nice try.", ephemeral=True)
        return
    if member.bot:
        await interaction.response.send_message(
            "Bots don't need rep.", ephemeral=True)
        return

    used = recent_votes_of_type(interaction.guild_id, role_name, member.id,
                                interaction.user.id, value)
    if len(used) >= VOTE_LIMIT:
        kind = "+rep" if value == 1 else "-rep"
        frees_at = used[0] + datetime.timedelta(hours=WINDOW_HOURS)
        remaining = frees_at - datetime.datetime.utcnow()
        await interaction.response.send_message(
            f"You've already given {member.display_name} {VOTE_LIMIT} {kind}s "
            f"for **{role_name}** in the last {WINDOW_HOURS}h. "
            f"Try again in **{_fmt_remaining(remaining)}**.",
            ephemeral=True,
        )
        return

    add_rep(interaction.guild_id, role_name, member.id, interaction.user.id,
            value, comment)
    pos, neg = role_totals(interaction.guild_id, role_name, member.id)

    if value == 1:
        title = f"➕ +rep for {member.display_name} — {role_name}"
        color = discord.Color.green()
    else:
        title = f"➖ -rep for {member.display_name} — {role_name}"
        color = discord.Color.red()

    embed = discord.Embed(title=title, description=f'"{comment}"', color=color)
    embed.add_field(name="From", value=interaction.user.mention, inline=True)
    embed.add_field(name=f"{role_name} standing",
                    value=f"{pos}↑ / {neg}↓", inline=True)
    status = qualifies(pos, neg)
    embed.add_field(name="Has role?",
                    value="Yes ✅" if status else "Not yet", inline=True)

    await interaction.response.send_message(embed=embed)

    note = await _sync_discord_role(interaction, member, role_name)
    if note:
        await interaction.followup.send(note)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

@tree.command(name="addrole",
              description="(Admin) Link a rep-role name to a Discord role")
@app_commands.describe(name="Rep-role name, e.g. carry",
                       discord_role="The Discord role to grant when earned")
async def addrole(interaction: discord.Interaction, name: str,
                  discord_role: discord.Role):
    if not interaction.user.guild_permissions.manage_roles:
        await interaction.response.send_message(
            "You need the **Manage Roles** permission to do that.",
            ephemeral=True)
        return
    set_role(interaction.guild_id, name, discord_role.id)
    await interaction.response.send_message(
        f"Linked rep-role **{name.lower()}** → {discord_role.mention}. "
        f"Members earn it at {MIN_POS}+ reps with a {RATIO}× positive ratio."
    )


@tree.command(name="removerole",
              description="(Admin) Unlink a rep-role")
@app_commands.describe(name="Rep-role name to remove")
async def removerole(interaction: discord.Interaction, name: str):
    if not interaction.user.guild_permissions.manage_roles:
        await interaction.response.send_message(
            "You need the **Manage Roles** permission to do that.",
            ephemeral=True)
        return
    ok = unset_role(interaction.guild_id, name)
    msg = (f"Removed rep-role **{name.lower()}**." if ok
           else f"No rep-role called **{name.lower()}**.")
    await interaction.response.send_message(msg, ephemeral=True)


@tree.command(name="roles", description="List all rep-roles in this server")
async def roles(interaction: discord.Interaction):
    rows = list_roles(interaction.guild_id)
    if not rows:
        await interaction.response.send_message(
            "No rep-roles yet. An admin can add one with `/addrole`.")
        return
    lines = []
    for role_name, drid in rows:
        r = interaction.guild.get_role(int(drid))
        lines.append(f"• **{role_name}** → {r.mention if r else '(deleted role)'}")
    embed = discord.Embed(
        title="Rep-roles",
        description="\n".join(lines)
        + f"\n\nEarn at **{MIN_POS}+** reps with **{RATIO}×** positive ratio.",
        color=discord.Color.blurple(),
    )
    await interaction.response.send_message(embed=embed)


@tree.command(name="rep", description="Give someone +rep for a role")
@app_commands.describe(role="Which rep-role, e.g. carry",
                       member="Who are you repping?",
                       comment="Why? e.g. carried the whole grind")
async def rep(interaction: discord.Interaction, role: str,
              member: discord.Member, comment: str):
    await _do_vote(interaction, role, member, comment, +1)


@tree.command(name="derep", description="Give someone -rep for a role")
@app_commands.describe(role="Which rep-role, e.g. carry",
                       member="Who are you derepping?",
                       comment="Why? e.g. DC'd, left early")
async def derep(interaction: discord.Interaction, role: str,
                member: discord.Member, comment: str):
    await _do_vote(interaction, role, member, comment, -1)


@tree.command(name="profile",
              description="See someone's standing across all rep-roles")
@app_commands.describe(member="Whose profile? (blank = yourself)")
async def profile(interaction: discord.Interaction,
                  member: discord.Member = None):
    member = member or interaction.user
    rows = member_role_summary(interaction.guild_id, member.id)
    embed = discord.Embed(title=f"📊 {member.display_name}'s rep",
                          color=discord.Color.blurple())
    if not rows:
        embed.description = "No reps yet."
    else:
        for role_name, pos, neg in rows:
            mark = "✅" if qualifies(pos, neg) else "—"
            embed.add_field(name=f"{role_name} {mark}",
                            value=f"{pos}↑ / {neg}↓", inline=True)
    await interaction.response.send_message(embed=embed)


@tree.command(name="leaderboard", description="Ranking for one rep-role")
@app_commands.describe(role="Which rep-role to rank, e.g. carry")
async def leaderboard(interaction: discord.Interaction, role: str):
    role = role.lower().strip()
    if get_role(interaction.guild_id, role) is None:
        await interaction.response.send_message(
            f"No rep-role called **{role}**. See `/roles`.", ephemeral=True)
        return
    rows = role_leaderboard(interaction.guild_id, role, limit=10)
    if not rows:
        await interaction.response.send_message(
            f"No reps yet for **{role}**.")
        return
    medals = ["🥇", "🥈", "🥉"]
    lines = []
    for i, (target_id, pos, neg) in enumerate(rows):
        prefix = medals[i] if i < 3 else f"`#{i + 1}`"
        mark = "✅" if qualifies(pos, neg) else ""
        lines.append(f"{prefix} <@{target_id}> — {pos}↑ / {neg}↓ {mark}")
    embed = discord.Embed(title=f"🏆 {role} leaderboard",
                          description="\n".join(lines),
                          color=discord.Color.gold())
    await interaction.response.send_message(embed=embed)


# ---------------------------------------------------------------------------
# HOW TO RUN
# ---------------------------------------------------------------------------
# 1. Discord Developer Portal -> your app -> Bot tab:
#       *** TURN ON "SERVER MEMBERS INTENT" *** (needed to manage roles).
#       Copy the TOKEN.
# 2. Invite/scopes: "bot" + "applications.commands".
#    Bot permissions MUST include "Manage Roles" (plus "Send Messages").
# 3. In Server Settings -> Roles, drag the BOT'S role ABOVE every rep-role,
#    or it won't be allowed to assign them.
# 4. Railway: set DISCORD_TOKEN in Variables.  Start command:  python bot.py
# 5. In Discord:  /addrole carry @Carry   then people use  /rep carry @user ...
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        raise SystemExit("No DISCORD_TOKEN found. Set it as an env var / secret.")
    client.run(token)
