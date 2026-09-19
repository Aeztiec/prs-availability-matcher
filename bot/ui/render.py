"""Turn the bot's plain embed data into real discord.Embed objects."""

import discord

from bot.domain import season


def to_discord_embed(board_embed):
    """Turn a notify.BoardEmbed - plain, testable data - into the real
    discord.Embed object the API actually wants. Kept to this one
    spot so nothing else needs to know discord.Embed's shape."""
    embed = discord.Embed(description=board_embed.description,
                          color=discord.Color(season.EMBED_COLOR))
    if board_embed.title:
        embed.title = board_embed.title
    if board_embed.footer:
        embed.set_footer(text=board_embed.footer)
    for name, value, inline in board_embed.fields:
        embed.add_field(name=name, value=value, inline=inline)
    embed.set_image(url=season.EMBED_WIDTH_IMAGE)
    return embed
