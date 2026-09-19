"""Who may run the staff commands, and how a refusal reads."""

from __future__ import annotations


from discord import app_commands

from bot import config


def is_staff(interaction):
    """Staff role if configured, otherwise anyone who can manage the server."""
    if interaction.guild is None:
        # Commands are registered globally so /availability works in DMs, which
        # means the staff ones are offered there too. There is no role or
        # permission to check in a DM, so they simply do not apply.
        return False
    if interaction.guild.owner_id == interaction.user.id:
        return True
    if config.STAFF_ROLE_ID:
        role_ids = {r.id for r in getattr(interaction.user, "roles", []) or []}
        return config.STAFF_ROLE_ID in role_ids
    try:
        perms = interaction.permissions
        return bool(perms.manage_guild or perms.administrator)
    except AttributeError:
        return False


def staff_refusal(interaction):
    """Why a staff command was refused, phrased so it can be acted on."""
    if interaction.guild is None:
        return ("Staff commands only work in the server, not in DMs. Discord "
                "gives me no roles or permissions to check here.\n"
                "-# `/availability` and `/ref dropout` do work in DMs.")
    if config.STAFF_ROLE_ID:
        return ("That's a staff command. It needs the configured staff role "
                "(`DISCORD_STAFF_ROLE_ID`).")
    return ("That's a staff command. It needs **Manage Server**, or set "
            "`DISCORD_STAFF_ROLE_ID` in .env to gate by role instead.")


def staff_only():
    async def predicate(interaction):
        if is_staff(interaction):
            return True
        await interaction.response.send_message(
            staff_refusal(interaction), ephemeral=True
        )
        return False
    return app_commands.check(predicate)
