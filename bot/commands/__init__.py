"""Every slash command, one module per group."""

from bot.commands import (
    availability, fixtures, gameweeks, managers, referees, results, testing,
)

MODULES = (availability, fixtures, referees, gameweeks, managers, results, testing)


def register(bot):
    """Attach every command to the bot's tree."""
    for module in MODULES:
        module.setup(bot)
