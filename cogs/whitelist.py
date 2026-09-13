"""
/whitelist add|remove|list  ->  quién queda EXENTO de los castigos del
antinuke (dueño y admins del antinuke lo pueden usar).

Funciona como slash (/whitelist add usuario:@x) y con prefijo
(!whitelist add @x): es el mismo comando híbrido en los dos casos.
"""
from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from utils import database
from utils.embeds import error_embed, info_embed, success_embed
from utils.permissions import AntiNukeCogBase, admin_only


class WhitelistCommands(AntiNukeCogBase):
    @commands.hybrid_group(
        name="whitelist",
        description="Gestiona quién queda exento del antinuke",
        invoke_without_command=True,
    )
    async def whitelist(self, ctx: commands.Context) -> None:
        prefix = ctx.prefix if ctx.interaction is None else "/"
        await ctx.send(
            embed=info_embed(f"`{prefix}whitelist add|remove|list`", title="📋 Whitelist"),
            ephemeral=True,
        )

    @whitelist.command(name="add", description="Añade a alguien a la whitelist del antinuke")
    @app_commands.describe(usuario="Usuario que quedará exento de los castigos del antinuke")
    @admin_only()
    async def whitelist_add(self, ctx: commands.Context, usuario: discord.Member) -> None:
        config = database.get_config(ctx.guild.id)
        if usuario.id in config["whitelist"]:
            await ctx.send(
                embed=error_embed(f"{usuario.mention} ya está en la whitelist."), ephemeral=True,
            )
            return
        config["whitelist"].append(usuario.id)
        database.save_config(ctx.guild.id, config)
        await ctx.send(
            embed=success_embed(f"{usuario.mention} añadido a la whitelist. Ya no será castigado por el antinuke."),
            ephemeral=True,
        )

    @whitelist.command(name="remove", description="Quita a alguien de la whitelist del antinuke")
    @app_commands.describe(usuario="Usuario a quitar de la whitelist")
    @admin_only()
    async def whitelist_remove(self, ctx: commands.Context, usuario: discord.Member) -> None:
        config = database.get_config(ctx.guild.id)
        if usuario.id not in config["whitelist"]:
            await ctx.send(
                embed=error_embed(f"{usuario.mention} no está en la whitelist."), ephemeral=True,
            )
            return
        config["whitelist"].remove(usuario.id)
        database.save_config(ctx.guild.id, config)
        await ctx.send(
            embed=success_embed(f"{usuario.mention} eliminado de la whitelist."), ephemeral=True,
        )

    @whitelist.command(name="list", description="Muestra la whitelist actual del antinuke")
    async def whitelist_list(self, ctx: commands.Context) -> None:
        config = database.get_config(ctx.guild.id)
        if not config["whitelist"]:
            await ctx.send(embed=info_embed("La whitelist está vacía.", title="📋 Whitelist"), ephemeral=True)
            return
        mentions = "\n".join(f"• <@{uid}>" for uid in config["whitelist"])
        await ctx.send(embed=info_embed(mentions, title="📋 Whitelist del antinuke"), ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(WhitelistCommands(bot))
