"""
/antinukeadmin add|remove|list  ->  quién puede ADMINISTRAR el antinuke
(whitelist y configuración). Solo el dueño del servidor puede usar
add/remove; así un admin comprometido no puede nombrar cómplices.

Funciona como slash (/antinukeadmin add usuario:@x) y con prefijo
(!antinukeadmin add @x): es el mismo comando híbrido en los dos casos.
"""
from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from utils import database
from utils.embeds import error_embed, info_embed, success_embed
from utils.permissions import AntiNukeCogBase, owner_only


class AntinukeAdminCommands(AntiNukeCogBase):
    @commands.hybrid_group(
        name="antinukeadmin",
        description="Gestiona quién administra el antinuke",
        invoke_without_command=True,
    )
    async def antinukeadmin(self, ctx: commands.Context) -> None:
        prefix = ctx.prefix if ctx.interaction is None else "/"
        await ctx.send(
            embed=info_embed(f"`{prefix}antinukeadmin add|remove|list`", title="🛡️ Admins del antinuke"),
            ephemeral=True,
        )

    @antinukeadmin.command(name="add", description="Da permisos de administrador del antinuke a alguien")
    @app_commands.describe(usuario="Usuario que podrá manejar whitelist y configuración del antinuke")
    @owner_only()
    async def antinukeadmin_add(self, ctx: commands.Context, usuario: discord.Member) -> None:
        config = database.get_config(ctx.guild.id)
        if usuario.id in config["admins"]:
            await ctx.send(
                embed=error_embed(f"{usuario.mention} ya es admin del antinuke."), ephemeral=True,
            )
            return
        config["admins"].append(usuario.id)
        database.save_config(ctx.guild.id, config)
        await ctx.send(
            embed=success_embed(f"{usuario.mention} ahora es admin del antinuke (puede manejar whitelist y ajustes)."),
            ephemeral=True,
        )

    @antinukeadmin.command(name="remove", description="Quita permisos de administrador del antinuke")
    @app_commands.describe(usuario="Usuario al que se le quitará el rol de admin del antinuke")
    @owner_only()
    async def antinukeadmin_remove(self, ctx: commands.Context, usuario: discord.Member) -> None:
        config = database.get_config(ctx.guild.id)
        if usuario.id not in config["admins"]:
            await ctx.send(
                embed=error_embed(f"{usuario.mention} no es admin del antinuke."), ephemeral=True,
            )
            return
        config["admins"].remove(usuario.id)
        database.save_config(ctx.guild.id, config)
        await ctx.send(
            embed=success_embed(f"{usuario.mention} ya no es admin del antinuke."), ephemeral=True,
        )

    @antinukeadmin.command(name="list", description="Muestra los admins del antinuke")
    async def antinukeadmin_list(self, ctx: commands.Context) -> None:
        config = database.get_config(ctx.guild.id)
        if not config["admins"]:
            await ctx.send(
                embed=info_embed("Todavía no hay admins del antinuke configurados.", title="🛡️ Admins del antinuke"),
                ephemeral=True,
            )
            return
        mentions = "\n".join(f"• <@{uid}>" for uid in config["admins"])
        await ctx.send(embed=info_embed(mentions, title="🛡️ Admins del antinuke"), ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AntinukeAdminCommands(bot))
