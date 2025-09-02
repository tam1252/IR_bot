import discord

async def safe_defer(interaction: discord.Interaction, *, ephemeral: bool = True):
    if not interaction.response.is_done():
        try:
            await interaction.response.defer(ephemeral=ephemeral, thinking=True)
        except discord.InteractionResponded:
            # すでに応答済みなら何もしない
            pass