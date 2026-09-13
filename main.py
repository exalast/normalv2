"""
Bot de Discord con sistema antinuke.
Arranque: python main.py  (usa la variable de entorno DISCORD_TOKEN)

Todos los comandos son "híbridos": funcionan tanto como comando de barra
(/antinuke setup) como con el prefijo de texto configurado abajo
(!antinuke setup). Es el mismo comando y la misma lógica en los dos casos.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

from utils.embeds import error_embed

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
log = logging.getLogger("antinuke")

TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    log.error("Falta la variable de entorno DISCORD_TOKEN. Configúrala antes de arrancar el bot.")
    sys.exit(1)

# Prefijo de texto para los comandos (ej: "!", "?", "-"). Configurable con la
# variable de entorno COMMAND_PREFIX; por defecto "!".
PREFIX = os.getenv("COMMAND_PREFIX", "!")

intents = discord.Intents.default()
intents.members = True          # necesario para on_member_join/remove/update
intents.moderation = True       # necesario para on_member_ban/unban
intents.message_content = True  # necesario para leer comandos con prefijo (ej: "!whitelist add @user")

bot = commands.Bot(
    command_prefix=PREFIX,
    intents=intents,
    help_command=None,
    case_insensitive=True,
)

EXTENSIONS = (
    "cogs.antinuke_events",
    "cogs.whitelist",
    "cogs.antinukeadmin",
    "cogs.antinuke",
    "cogs.backups",
)


@bot.event
async def on_ready() -> None:
    log.info("Conectado como %s (ID: %s)", bot.user, bot.user.id if bot.user else "?")
    try:
        synced = await bot.tree.sync()
        log.info("Sincronizados %d comandos slash.", len(synced))
    except discord.HTTPException as exc:
        log.exception("Error sincronizando comandos slash: %s", exc)

    print("=" * 50)
    print(f"  Bot activo: {bot.user}")
    print(f"  Servidores: {len(bot.guilds)}")
    print(f"  Prefijo de texto: {PREFIX}")
    print("  Antinuke listo. Ejecuta /antinuke setup (o !antinuke setup) en cada servidor.")
    print("=" * 50)


@bot.event
async def on_guild_join(guild: discord.Guild) -> None:
    from utils import database
    database.ensure_owner(guild.id, guild.owner_id)
    log.info("Bot añadido al servidor '%s' (%s)", guild.name, guild.id)


# ---------------------------------------------------------------------- #
# Errores de comandos de aplicación "puros" (ninguno queda en este bot,
# pero se deja como red de seguridad por si se agrega alguno en el futuro
# que NO sea híbrido).
# ---------------------------------------------------------------------- #
@bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction, error: app_commands.AppCommandError,
) -> None:
    if isinstance(error, app_commands.CheckFailure):
        message = str(error) or "No tienes permiso para usar este comando."
    elif isinstance(error, app_commands.CommandOnCooldown):
        message = f"Espera {error.retry_after:.1f}s antes de volver a usar este comando."
    else:
        log.exception("Error inesperado en un comando de aplicación", exc_info=error)
        message = "Ocurrió un error inesperado ejecutando el comando."

    embed = error_embed(message)
    try:
        if interaction.response.is_done():
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=True)
    except discord.HTTPException:
        pass


# ---------------------------------------------------------------------- #
# Errores de comandos híbridos y de prefijo.
#
# IMPORTANTE: un comando híbrido (@commands.hybrid_command / hybrid_group)
# despacha SUS errores acá, en on_command_error, incluso cuando se invocó
# como "/comando" desde Discord. on_app_command_error (arriba) NO se
# dispara para comandos híbridos; discord.py los enruta siempre por acá
# porque por dentro corren sobre el mismo Context, sin importar si vinieron
# de un mensaje con prefijo o de una interacción de barra.
# ---------------------------------------------------------------------- #
@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError) -> None:
    if isinstance(error, commands.CommandNotFound):
        return  # sin esto, cualquier "!lo-que-sea" de otro bot loguearía error

    if isinstance(error, commands.NoPrivateMessage):
        message = "Este comando solo funciona dentro de un servidor."
    elif isinstance(error, commands.CheckFailure):
        # Cubre nuestros OwnerOnlyError / AdminOnlyError (traen su propio
        # mensaje) y cualquier otro check de discord.py que falle.
        message = str(error) or "No tienes permiso para usar este comando."
    elif isinstance(error, commands.CommandOnCooldown):
        message = f"Espera {error.retry_after:.1f}s antes de volver a usar este comando."
    elif isinstance(error, commands.MissingRequiredArgument):
        message = f"Falta el parámetro `{error.param.displayed_name or error.param.name}`."
    elif isinstance(error, commands.BadLiteralArgument):
        opciones = ", ".join(str(v) for v in error.literals)
        message = f"«{error.argument}» no es válido para `{error.param.displayed_name or error.param.name}`. Opciones: {opciones}."
    elif isinstance(error, commands.MemberNotFound):
        message = f"No encontré ningún miembro «{error.argument}» en este servidor."
    elif isinstance(error, commands.ChannelNotFound):
        message = f"No encontré ningún canal «{error.argument}» en este servidor."
    elif isinstance(error, commands.RoleNotFound):
        message = f"No encontré ningún rol «{error.argument}» en este servidor."
    elif isinstance(error, commands.BadBoolArgument):
        message = f"«{error.argument}» no es un valor válido. Usa true/false (o sí/no, on/off)."
    elif isinstance(error, commands.RangeError):
        limites = []
        if error.minimum is not None:
            limites.append(f"mínimo {error.minimum}")
        if error.maximum is not None:
            limites.append(f"máximo {error.maximum}")
        message = f"«{error.value}» está fuera de rango ({', '.join(limites)})."
    elif isinstance(error, commands.BadArgument):
        message = str(error) or "Uno de los parámetros no es válido."
    else:
        log.exception("Error inesperado en un comando", exc_info=error)
        message = "Ocurrió un error inesperado ejecutando el comando."

    try:
        await ctx.send(embed=error_embed(message), ephemeral=True)
    except discord.HTTPException:
        pass


async def main() -> None:
    async with bot:
        for extension in EXTENSIONS:
            await bot.load_extension(extension)
            log.info("Extensión cargada: %s", extension)
        await bot.start(TOKEN)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except discord.LoginFailure:
        log.error("El DISCORD_TOKEN es inválido. Copia el token de nuevo desde el Developer Portal.")
        sys.exit(1)
    except discord.PrivilegedIntentsRequired:
        log.error(
            "Faltan intents privilegiados. Ve al Developer Portal -> tu app -> Bot "
            "y activa 'SERVER MEMBERS INTENT' y 'MESSAGE CONTENT INTENT'."
        )
        sys.exit(1)
