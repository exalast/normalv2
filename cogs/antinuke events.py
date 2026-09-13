"""
Motor del antinuke: escucha los eventos "peligrosos" de Discord, revisa
quién los hizo (vía audit log), y si esa persona no está en la whitelist
ni es admin/dueño del antinuke, la castiga automáticamente.

Cosas importantes de diseño (léelas antes de tocar el código):

1. NO se confía en el permiso "Administrator" de Discord. Ese es
   justamente el punto de un antinuke: si a alguien le dieron admin
   (o le robaron la cuenta), igual se le castiga a menos que esté en
   la whitelist o sea admin del antinuke. Esto es intencional.

2. Casi todos los eventos de discord.py NO dicen quién ejecutó la
   acción (ej: on_guild_channel_delete solo te da el canal). Por eso
   se consulta el audit log (`guild.audit_logs`) justo después del
   evento para encontrar al responsable. Esto requiere el permiso
   "Ver registro de auditoría" (View Audit Log) para el bot.

3. Se usa un sistema de "umbral" (threshold): cuenta cuántas veces un
   usuario disparó cada acción en una ventana corta de segundos. El
   límite (cuántas veces hace falta) es configurable por servidor con
   `/antinuke limit` y se guarda en config["thresholds"] — por defecto
   viene en 1 para todo (máxima sensibilidad: una sola acción ya
   castiga, sin esperar a que se repita). CERO tolerancia por defecto
   quiere decir que hay que whitelistear a tu staff de confianza
   (`/whitelist add`) o se lo va a castigar en su primer movimiento.

4. fetch_executor() reintenta en vez de esperar siempre AUDIT_LOG_DELAY
   completo: consulta apenas entra el evento y, si el audit log todavía
   no lo tiene, reintenta cada AUDIT_LOG_RETRY_INTERVAL hasta encontrarlo
   o hasta agotar el tope. En la práctica reacciona bastante antes del
   máximo. Aun así, hay un piso que no se puede bajar más: Discord tarda
   un rato en publicar cada entrada del audit log, y cada acción (ban,
   borrar canal, etc.) es una llamada HTTP con su propia latencia — el
   bot SIEMPRE actúa después de la acción, nunca antes. Si alguien corre
   un script que borra todo en milisegundos, el antinuke va a banear al
   responsable apenas lo identifique, pero no puede deshacer lo que ya se
   borró (eso no lo soluciona ningún antinuke reactivo, de ningún bot).
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict

import discord
from discord.ext import commands

from utils import database
from utils.embeds import error_embed

log = logging.getLogger("antinuke")

# Ventana en segundos por tipo de acción: cuánto tiempo se cuentan los
# hits para el umbral. El LÍMITE (cuántas veces hace falta) ya NO está
# acá — es configurable por servidor con /antinuke limit y se lee de
# config["thresholds"] (ver utils/database.py, DEFAULT_THRESHOLDS).
WINDOWS: dict[str, int] = {
    "channel_delete": 8,
    "channel_create": 8,
    "role_delete": 8,
    "role_create": 8,
    "ban": 10,
    "kick": 10,
    "webhook_create": 5,
    "emoji_delete": 10,
}

# Cuánto esperar COMO MÁXIMO (segundos) a que el audit log de Discord se
# actualice antes de rendirse. No es una espera fija: fetch_executor()
# reintenta cada AUDIT_LOG_RETRY_INTERVAL en vez de dormir siempre el
# total de una — así, cuando el audit log ya está listo (lo más común),
# se reacciona mucho antes de llegar a este tope.
AUDIT_LOG_DELAY = 1.2
AUDIT_LOG_RETRY_INTERVAL = 0.3
AUDIT_LOG_MAX_AGE = 8  # ignorar entradas de audit log más viejas que esto


class Tracker:
    """Cuenta cuántas veces un usuario disparó una acción en una ventana de tiempo."""

    def __init__(self) -> None:
        self._events: dict[tuple[int, int, str], list[float]] = defaultdict(list)

    def hit(self, guild_id: int, user_id: int, action: str, limit: int, window: int) -> bool:
        key = (guild_id, user_id, action)
        now = time.time()
        bucket = [t for t in self._events[key] if now - t < window]
        bucket.append(now)
        self._events[key] = bucket
        return len(bucket) >= limit


def is_exempt(guild: discord.Guild, config: dict, user_id: int) -> bool:
    """True si el usuario NO debe ser castigado (dueño, admin, whitelist o el propio bot)."""
    if user_id == guild.owner_id:
        return True
    if user_id == config.get("owner_id"):
        return True
    if user_id in config.get("admins", []):
        return True
    if user_id in config.get("whitelist", []):
        return True
    me = guild.me
    if me is not None and user_id == me.id:
        return True
    return False


async def fetch_executor(
    guild: discord.Guild,
    action: discord.AuditLogAction,
    target_id: int | None = None,
    max_age: int = AUDIT_LOG_MAX_AGE,
):
    """Busca en el audit log quién ejecutó una acción reciente. Devuelve un
    User/Member o None.

    Reintenta cada AUDIT_LOG_RETRY_INTERVAL hasta encontrar la entrada o
    hasta agotar AUDIT_LOG_DELAY en total. Esto reacciona apenas el audit
    log esté listo en vez de esperar siempre el máximo — en la práctica
    suele resolver bastante antes del tope."""
    me = guild.me
    if me is None or not me.guild_permissions.view_audit_log:
        return None

    deadline = time.monotonic() + AUDIT_LOG_DELAY
    while True:
        try:
            async for entry in guild.audit_logs(limit=8, action=action):
                age = (discord.utils.utcnow() - entry.created_at).total_seconds()
                if age > max_age:
                    break  # esta y el resto (más viejas todavía) ya no sirven
                if target_id is not None:
                    target = entry.target
                    tid = getattr(target, "id", target)
                    if tid != target_id:
                        continue
                return entry.user
        except discord.Forbidden:
            return None

        if time.monotonic() >= deadline:
            return None
        await asyncio.sleep(AUDIT_LOG_RETRY_INTERVAL)


class AntiNukeEvents(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.tracker = Tracker()

    # ------------------------------------------------------------------ #
    # Núcleo: evaluar umbral y castigar si corresponde
    # ------------------------------------------------------------------ #
    async def _handle(
        self,
        guild: discord.Guild,
        action_name: str,
        executor: discord.abc.User | None,
        reason: str,
    ) -> None:
        if executor is None:
            return

        config = database.get_config(guild.id)
        if not config["modules"].get(action_name, True):
            return
        if is_exempt(guild, config, executor.id):
            return

        limit = config.get("thresholds", {}).get(action_name, 1)
        window = WINDOWS.get(action_name, 8)
        triggered = self.tracker.hit(guild.id, executor.id, action_name, limit, window)
        if not triggered:
            return

        await self._punish(guild, executor, reason, config)

    async def _punish(
        self,
        guild: discord.Guild,
        executor: discord.abc.User,
        reason: str,
        config: dict,
    ) -> None:
        punishment = config.get("punishment", "ban")
        member = guild.get_member(executor.id)

        try:
            if punishment == "ban":
                await guild.ban(
                    discord.Object(id=executor.id),
                    reason=f"[AntiNuke] {reason}",
                    delete_message_seconds=0,
                )
            elif punishment == "kick" and member is not None:
                await guild.kick(member, reason=f"[AntiNuke] {reason}")
            elif punishment == "strip" and member is not None:
                await member.edit(roles=[], reason=f"[AntiNuke] {reason}")
        except discord.Forbidden:
            log.warning(
                "Sin permisos suficientes para castigar a %s en %s (%s)",
                executor.id, guild.id, guild.name,
            )
        except discord.HTTPException as exc:
            log.warning("Fallo al castigar a %s: %s", executor.id, exc)

        await self._log(guild, executor, reason, config)

    async def _log(
        self,
        guild: discord.Guild,
        executor: discord.abc.User,
        reason: str,
        config: dict,
    ) -> None:
        channel_id = config.get("log_channel")
        if not channel_id:
            return
        channel = guild.get_channel(channel_id)
        if channel is None:
            return

        embed = discord.Embed(
            title="🛡️ Antinuke: acción bloqueada",
            description=reason,
            color=discord.Color.red(),
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="Usuario", value=f"{executor} (`{executor.id}`)", inline=False)
        embed.add_field(name="Castigo aplicado", value=config.get("punishment", "ban"), inline=True)
        try:
            await channel.send(embed=embed)
        except discord.HTTPException:
            pass

    # ------------------------------------------------------------------ #
    # Canales
    # ------------------------------------------------------------------ #
    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        executor = await fetch_executor(channel.guild, discord.AuditLogAction.channel_delete)
        await self._handle(channel.guild, "channel_delete", executor, f"Eliminó el canal **#{channel.name}**")

    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel: discord.abc.GuildChannel) -> None:
        executor = await fetch_executor(channel.guild, discord.AuditLogAction.channel_create)
        await self._handle(
            channel.guild, "channel_create", executor,
            f"Creó varios canales muy rápido (último: **#{channel.name}**)",
        )

    # ------------------------------------------------------------------ #
    # Roles
    # ------------------------------------------------------------------ #
    @commands.Cog.listener()
    async def on_guild_role_delete(self, role: discord.Role) -> None:
        executor = await fetch_executor(role.guild, discord.AuditLogAction.role_delete)
        await self._handle(role.guild, "role_delete", executor, f"Eliminó el rol **@{role.name}**")

    @commands.Cog.listener()
    async def on_guild_role_create(self, role: discord.Role) -> None:
        executor = await fetch_executor(role.guild, discord.AuditLogAction.role_create)
        await self._handle(
            role.guild, "role_create", executor,
            f"Creó varios roles muy rápido (último: **@{role.name}**)",
        )

    # ------------------------------------------------------------------ #
    # Baneos / expulsiones
    # ------------------------------------------------------------------ #
    @commands.Cog.listener()
    async def on_member_ban(self, guild: discord.Guild, user: discord.abc.User) -> None:
        executor = await fetch_executor(guild, discord.AuditLogAction.ban, target_id=user.id)
        await self._handle(guild, "ban", executor, f"Baneó a **{user}** (`{user.id}`)")

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        # on_member_remove también se dispara cuando alguien se va solo,
        # así que confirmamos con el audit log que de verdad fue un kick.
        executor = await fetch_executor(
            member.guild, discord.AuditLogAction.kick, target_id=member.id, max_age=5,
        )
        if executor is None:
            return
        await self._handle(member.guild, "kick", executor, f"Expulsó a **{member}** (`{member.id}`)")

    # ------------------------------------------------------------------ #
    # Webhooks (vector clásico de spam/raid)
    # ------------------------------------------------------------------ #
    @commands.Cog.listener()
    async def on_webhooks_update(self, channel: discord.abc.GuildChannel) -> None:
        executor = await fetch_executor(channel.guild, discord.AuditLogAction.webhook_create)
        if executor is None:
            return
        await self._handle(
            channel.guild, "webhook_create", executor,
            f"Creó un webhook en **#{channel.name}**",
        )

    # ------------------------------------------------------------------ #
    # Bots añadidos sin autorización
    # ------------------------------------------------------------------ #
    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        if not member.bot:
            return

        guild = member.guild
        executor = await fetch_executor(guild, discord.AuditLogAction.bot_add, target_id=member.id)
        if executor is None:
            return

        config = database.get_config(guild.id)
        if not config["modules"].get("bot_add", True):
            return
        if is_exempt(guild, config, executor.id):
            return

        try:
            await guild.kick(member, reason="[AntiNuke] Bot añadido sin autorización")
        except discord.HTTPException:
            pass

        await self._punish(guild, executor, f"Añadió al bot **{member}** sin autorización", config)

    # ------------------------------------------------------------------ #
    # Rol con permiso de Administrador otorgado manualmente a un miembro
    # ------------------------------------------------------------------ #
    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        added_roles = [r for r in after.roles if r not in before.roles]
        # Ojo: se mira el PERMISO "Administrador" del rol, no el nombre.
        admin_roles = [r for r in added_roles if r.permissions.administrator]
        if not admin_roles:
            return

        guild = after.guild
        config = database.get_config(guild.id)
        if not config["modules"].get("dangerous_role", True):
            return

        executor = await fetch_executor(guild, discord.AuditLogAction.member_role_update, target_id=after.id)
        if executor is None or is_exempt(guild, config, executor.id):
            return
        if executor.bot:
            # Solo nos importa cuando lo otorgó una persona a mano.
            return

        role_names = ", ".join(f"@{r.name}" for r in admin_roles)
        reason = f"Le otorgó el rol de admin **{role_names}** a **{after}** manualmente sin autorización"

        # A quien RECIBIÓ el rol: se le quita si lo tiene, si no se lo kickea.
        await self._strip_admin_or_kick(guild, after, admin_roles, reason)

        # A quien lo OTORGÓ: mismo trato.
        executor_member = guild.get_member(executor.id)
        if executor_member is not None:
            await self._strip_admin_or_kick(guild, executor_member, admin_roles, reason)

        await self._log(guild, executor, reason, config)

    async def _strip_admin_or_kick(
        self,
        guild: discord.Guild,
        member: discord.Member,
        admin_roles: list[discord.Role],
        reason: str,
    ) -> None:
        """Le quita a `member` el/los rol(es) de admin si los tiene
        puestos; si no los tiene, lo expulsa (kick) del servidor.
        Se usa tanto para quien recibió el rol como para quien lo dio."""
        try:
            has_role = any(r in member.roles for r in admin_roles)
            if has_role:
                await member.remove_roles(*admin_roles, reason=f"[AntiNuke] {reason}")
            else:
                await guild.kick(member, reason=f"[AntiNuke] {reason}")
        except discord.Forbidden:
            log.warning(
                "Sin permisos suficientes para castigar a %s en %s (%s)",
                member.id, guild.id, guild.name,
            )
        except discord.HTTPException as exc:
            log.warning("Fallo al castigar a %s: %s", member.id, exc)

    # ------------------------------------------------------------------ #
    # Cambios en la configuración del servidor
    # ------------------------------------------------------------------ #
    @commands.Cog.listener()
    async def on_guild_update(self, before: discord.Guild, after: discord.Guild) -> None:
        changed = (
            before.name != after.name
            or before.icon != after.icon
            or before.vanity_url_code != after.vanity_url_code
        )
        if not changed:
            return

        config = database.get_config(after.id)
        if not config["modules"].get("guild_update", True):
            return

        executor = await fetch_executor(after, discord.AuditLogAction.guild_update)
        if executor is None or is_exempt(after, config, executor.id):
            return

        await self._punish(after, executor, "Modificó el nombre/ícono/vanity URL del servidor", config)

    # ------------------------------------------------------------------ #
    # @everyone / @here: solo quien esté exento (whitelist, admin del
    # antinuke o dueño) lo puede usar. A cualquier otro se le borra el
    # mensaje y se le avisa que no puede.
    # ------------------------------------------------------------------ #
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.guild is None or message.author.bot:
            return
        if not message.mention_everyone:
            return

        guild = message.guild
        config = database.get_config(guild.id)
        if not config["modules"].get("everyone_mention", True):
            return
        if is_exempt(guild, config, message.author.id):
            return  # tiene whitelist (o es admin/dueño): puede usar @everyone/@here

        try:
            await message.delete()
        except discord.HTTPException:
            pass

        try:
            await message.channel.send(
                embed=error_embed(f"{message.author.mention}, no puedes usar @everyone ni @here."),
                delete_after=8,
            )
        except discord.HTTPException:
            pass

    # ------------------------------------------------------------------ #
    # Emojis
    # ------------------------------------------------------------------ #
    @commands.Cog.listener()
    async def on_guild_emojis_update(
        self, guild: discord.Guild, before: list, after: list,
    ) -> None:
        if len(after) >= len(before):
            return  # no fue una eliminación

        executor = await fetch_executor(guild, discord.AuditLogAction.emoji_delete)
        await self._handle(guild, "emoji_delete", executor, "Eliminó emojis del servidor")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AntiNukeEvents(bot))
