"""
/antinuke setup|settings|punishment|module  ->  configuración general del
sistema (canal de logs, castigo por defecto, módulos de protección
activos). También vive acá /help (alias de prefijo: "ayuda").

Funciona como slash (/antinuke setup canal:#x) y con prefijo
(!antinuke setup #x): es el mismo comando híbrido en los dos casos.
"""
from __future__ import annotations

from typing import Literal

import discord
from discord import app_commands
from discord.ext import commands

from utils import database
from utils.embeds import COLOR_INFO, info_embed, success_embed
from utils.permissions import AntiNukeCogBase, admin_only, owner_only

MODULE_CHOICES = [
    "channel_delete", "channel_create", "role_delete", "role_create",
    "ban", "kick", "webhook_create", "bot_add", "dangerous_role",
    "guild_update", "emoji_delete", "everyone_mention",
]

# Los únicos módulos que "cuentan" repeticiones (ver WINDOWS en
# cogs/antinuke_events.py) y por lo tanto tienen un límite ajustable.
# bot_add/dangerous_role/guild_update/everyone_mention quedan afuera:
# esos siempre actúan al instante, no hay nada que ajustar.
THRESHOLD_MODULES = [
    "channel_delete", "channel_create", "role_delete", "role_create",
    "ban", "kick", "webhook_create", "emoji_delete",
]

# typing.Literal con los valores válidos: discord.py lo convierte
# automáticamente en choices en el slash command y en un validador de
# texto en el comando con prefijo. Se arma dinámicamente para no repetir
# las listas de arriba a mano.
PunishmentChoice = Literal["ban", "kick", "strip"]
ModuleChoice = Literal[tuple(MODULE_CHOICES)]
ThresholdModuleChoice = Literal[tuple(THRESHOLD_MODULES)]


class AntinukeCommands(AntiNukeCogBase):
    @commands.hybrid_group(
        name="antinuke",
        description="Configuración general del sistema antinuke",
        invoke_without_command=True,
    )
    async def antinuke(self, ctx: commands.Context) -> None:
        prefix = ctx.prefix if ctx.interaction is None else "/"
        await ctx.send(
            embed=info_embed(f"`{prefix}antinuke setup|settings|punishment|module|limit`", title="⚙️ Antinuke"),
            ephemeral=True,
        )

    @antinuke.command(name="setup", description="Configura el canal donde el antinuke enviará sus alertas")
    @app_commands.describe(canal="Canal de texto donde se enviarán los logs del antinuke")
    @owner_only()
    async def antinuke_setup(self, ctx: commands.Context, canal: discord.TextChannel) -> None:
        config = database.get_config(ctx.guild.id)
        config["log_channel"] = canal.id
        if config.get("owner_id") is None:
            config["owner_id"] = ctx.guild.owner_id
        database.save_config(ctx.guild.id, config)
        await ctx.send(embed=success_embed(f"Canal de logs configurado en {canal.mention}."), ephemeral=True)

    @antinuke.command(name="punishment", description="Define el castigo por defecto del antinuke")
    @app_commands.describe(tipo="Qué hacer con quien dispare el antinuke")
    @admin_only()
    async def antinuke_punishment(self, ctx: commands.Context, tipo: PunishmentChoice) -> None:
        config = database.get_config(ctx.guild.id)
        config["punishment"] = tipo
        database.save_config(ctx.guild.id, config)
        await ctx.send(embed=success_embed(f"Castigo por defecto cambiado a **{tipo}**."), ephemeral=True)

    @antinuke.command(name="module", description="Activa o desactiva un módulo de protección del antinuke")
    @app_commands.describe(modulo="Módulo a modificar", estado="Activado o desactivado")
    @admin_only()
    async def antinuke_module(
        self, ctx: commands.Context, modulo: ModuleChoice, estado: bool,
    ) -> None:
        config = database.get_config(ctx.guild.id)
        config["modules"][modulo] = estado
        database.save_config(ctx.guild.id, config)
        emoji = "🟢" if estado else "🔴"
        await ctx.send(
            embed=success_embed(f"{emoji} Módulo `{modulo}` {'activado' if estado else 'desactivado'}."),
            ephemeral=True,
        )

    @antinuke.command(
        name="limit",
        description="Define cuántas veces hace falta una acción antes de castigar (1 = al instante)",
    )
    @app_commands.describe(
        modulo="Módulo a ajustar (solo los que cuentan repeticiones)",
        cantidad="Cuántas veces hace falta en pocos segundos para castigar (1 a 20)",
    )
    @admin_only()
    async def antinuke_limit(
        self,
        ctx: commands.Context,
        modulo: ThresholdModuleChoice,
        cantidad: commands.Range[int, 1, 20],
    ) -> None:
        config = database.get_config(ctx.guild.id)
        config.setdefault("thresholds", {})[modulo] = cantidad
        database.save_config(ctx.guild.id, config)
        detalle = "al instante (1ª vez ya castiga)" if cantidad == 1 else f"recién a la {cantidad}ª repetición"
        await ctx.send(
            embed=success_embed(f"`{modulo}` ahora castiga {detalle}."),
            ephemeral=True,
        )

    @antinuke.command(name="settings", description="Muestra la configuración actual del antinuke")
    async def antinuke_settings(self, ctx: commands.Context) -> None:
        config = database.get_config(ctx.guild.id)

        embed = discord.Embed(title="⚙️ Configuración del antinuke", color=COLOR_INFO)
        owner_id = config.get("owner_id")
        embed.add_field(name="Dueño registrado", value=f"<@{owner_id}>" if owner_id else "No definido", inline=False)
        embed.add_field(name="Castigo", value=config.get("punishment", "ban"), inline=True)
        log_ch = config.get("log_channel")
        embed.add_field(name="Canal de logs", value=f"<#{log_ch}>" if log_ch else "No configurado", inline=True)

        admins = ", ".join(f"<@{a}>" for a in config.get("admins", [])) or "Ninguno"
        whitelist = ", ".join(f"<@{w}>" for w in config.get("whitelist", [])) or "Ninguno"
        embed.add_field(name="Admins del antinuke", value=admins, inline=False)
        embed.add_field(name="Whitelist", value=whitelist, inline=False)

        modules = config.get("modules", {})
        thresholds = config.get("thresholds", {})

        tunable = "\n".join(
            f"{'🟢' if modules.get(k, True) else '🔴'} `{k}` — límite: {thresholds[k]}"
            for k in database.DEFAULT_THRESHOLDS
        )
        instant = "\n".join(
            f"{'🟢' if modules.get(k, True) else '🔴'} `{k}`"
            for k in modules if k not in database.DEFAULT_THRESHOLDS
        )
        embed.add_field(name="Módulos con límite ajustable", value=tunable or "N/A", inline=False)
        embed.add_field(name="Módulos siempre instantáneos", value=instant or "N/A", inline=False)

        await ctx.send(embed=embed, ephemeral=True)

    # ------------------------------------------------------------------ #
    # help (alias de prefijo: "ayuda") — agrupado por quién puede usar
    # cada comando, para que de un vistazo se sepa qué está disponible.
    # ------------------------------------------------------------------ #
    @commands.hybrid_command(name="help", aliases=["ayuda"], description="Muestra los comandos disponibles del antinuke")
    async def help_cmd(self, ctx: commands.Context) -> None:
        prefix = self.bot.command_prefix if isinstance(self.bot.command_prefix, str) else "!"

        embed = discord.Embed(
            title="🛡️ Comandos del antinuke",
            description=(
                f"Cada comando funciona como slash (`/comando`) o con el prefijo "
                f"`{prefix}` (ej: `{prefix}whitelist list`)."
            ),
            color=COLOR_INFO,
        )
        embed.add_field(
            name="👑 Solo el dueño del servidor",
            value=(
                "`antinukeadmin add <usuario>` — da permisos de admin del antinuke\n"
                "`antinukeadmin remove <usuario>` — quita permisos de admin\n"
                "`antinuke setup <canal>` — define el canal de logs"
            ),
            inline=False,
        )
        embed.add_field(
            name="🛡️ Dueño o admin del antinuke",
            value=(
                "`whitelist add <usuario>` — exime a alguien de los castigos\n"
                "`whitelist remove <usuario>` — vuelve a exponer a alguien\n"
                "`antinuke punishment <ban|kick|strip>` — castigo por defecto\n"
                "`antinuke module <módulo> <on|off>` — activa/desactiva una protección\n"
                "`antinuke limit <módulo> <1-20>` — repeticiones antes de castigar\n"
                "`backup now` — backup manual de categorías/canales\n"
                "`backup restore [backup_id] confirmar:true` — restaura un backup\n"
                "`backup auto <minutos>` — cada cuánto se hace backup solo (0 = off)"
            ),
            inline=False,
        )
        embed.add_field(
            name="🌐 Cualquiera en el servidor",
            value=(
                "`whitelist list` — muestra la whitelist\n"
                "`antinukeadmin list` — muestra los admins del antinuke\n"
                "`antinuke settings` — configuración actual\n"
                "`backup list` — muestra los backups guardados\n"
                "`help` — este menú"
            ),
            inline=False,
        )
        embed.set_footer(text=f"Prefijo de texto actual: {prefix}  •  también funciona todo con /")
        await ctx.send(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AntinukeCommands(bot))
