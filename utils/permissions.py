"""
Lógica de permisos compartida por los 3 cogs de comandos
(whitelist, antinukeadmin, antinuke):

- AntiNukeCogBase: clase base con el cog_check que exige que el comando
  se use dentro de un servidor (nunca por DM). Los 3 cogs heredan de acá
  en vez de heredar directo de commands.Cog, así no se repite la misma
  verificación 3 veces.
- owner_only() / admin_only(): decoradores @commands.check() para usar en
  comandos híbridos concretos, arriba de lo que ya exige cog_check.

Jerarquía:
- Dueño del servidor (o quien quedó registrado como owner_id): puede todo.
- Admin del antinuke: maneja whitelist y configuración, pero no puede
  nombrar a otros admins.
- Whitelist: solo exento de castigos, sin poder de administración.
"""
from __future__ import annotations

from discord.ext import commands

from utils import database


class OwnerOnlyError(commands.CheckFailure):
    pass


class AdminOnlyError(commands.CheckFailure):
    pass


class AntiNukeCogBase(commands.Cog):
    """Cog base: exige servidor (no DMs) para TODOS los comandos de los
    cogs que hereden de acá, sin importar si se invocaron con prefijo o
    como slash."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_check(self, ctx: commands.Context) -> bool:
        if ctx.guild is None:
            raise commands.NoPrivateMessage("Este comando solo funciona dentro de un servidor.")
        return True


def _is_owner(ctx: commands.Context) -> bool:
    config = database.get_config(ctx.guild.id)
    return (
        ctx.author.id == ctx.guild.owner_id
        or ctx.author.id == config.get("owner_id")
    )


def _is_admin(ctx: commands.Context) -> bool:
    if _is_owner(ctx):
        return True
    config = database.get_config(ctx.guild.id)
    return ctx.author.id in config.get("admins", [])


def owner_only():
    async def predicate(ctx: commands.Context) -> bool:
        if not _is_owner(ctx):
            raise OwnerOnlyError("Solo el dueño del servidor puede usar este comando.")
        return True
    return commands.check(predicate)


def admin_only():
    async def predicate(ctx: commands.Context) -> bool:
        if not _is_admin(ctx):
            raise AdminOnlyError("Necesitas ser admin del antinuke (o dueño del servidor) para usar esto.")
        return True
    return commands.check(predicate)
