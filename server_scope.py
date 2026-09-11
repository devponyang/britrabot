"""The only Discord guild this bot is allowed to serve."""
ALLOWED_GUILD_ID = 1545016047332237332


def allowed_guild(guild):
    return guild is not None and guild.id == ALLOWED_GUILD_ID
