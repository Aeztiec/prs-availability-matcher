"""Configuration, read from the environment (or a local .env file).

Nothing secret is ever committed: .env is gitignored, because this repo is
public and a leaked bot token is a bot somebody else controls.
"""

from __future__ import annotations

import os

HERE = os.path.dirname(os.path.abspath(__file__))
ENV_FILE = os.path.join(os.path.dirname(HERE), ".env")


def _load_env_file(path=ENV_FILE):
    """Minimal KEY=value reader, so a .env works without python-dotenv."""
    try:
        with open(path, encoding="utf-8") as handle:
            lines = handle.readlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_env_file()


def _int(name, default=0):
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


TOKEN = os.environ.get("DISCORD_TOKEN", "")
GUILD_ID = _int("DISCORD_GUILD_ID")
STAFF_ROLE_ID = _int("DISCORD_STAFF_ROLE_ID")
REFEREE_ROLE_ID = _int("DISCORD_REFEREE_ROLE_ID")

# Which timings sheet the fallback and the slot list come from. Blank means the
# default competition (newest season, preferring Clubs).
COMPETITION = os.environ.get("PRS_COMPETITION", "") or None

# Who gets pinged at the top of a gameweek announcement. "@everyone" or a role
# mention like "<@&123456789>"; set it empty to announce without pinging.
# Only the first post pings - editing a message does not re-notify, and the
# board is edited rather than reposted as fixtures get times.
ANNOUNCE_MENTION = os.environ.get("DISCORD_ANNOUNCE_MENTION", "@everyone").strip()

# Reminder offsets before the deadline, in hours.
REMINDERS = (12, 2)


def missing():
    """Config that must be set before the bot can start."""
    gaps = []
    if not TOKEN:
        gaps.append("DISCORD_TOKEN")
    if not GUILD_ID:
        gaps.append("DISCORD_GUILD_ID")
    return gaps
