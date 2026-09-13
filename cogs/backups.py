"""
/backup now|list|restore|auto  ->  backup automático (y manual) de la
estructura de categorías y canales del servidor, para poder restaurarla
después de un nuke.

Funciona como slash (/backup now) y con prefijo (!backup now): es el
mismo comando híbrido en los dos casos, igual que el resto del bot.

DISEÑO IMPORTANTE (léelo antes de tocar el scheduler):
a propósito, un backup automático NUNCA se dispara en reacción directa a
un canal/categoría borrado. Si lo hiciera, un nuke en curso (que borra
todo en segundos) pisaría el último backup bueno con el servidor ya
vacío, justo cuando más se necesita — el peor momento posible para que
el "seguro" se rompa. En cambio hay 3 disparadores, todos "seguros" en
ese sentido:

  1. Al conectar el bot (primer on_ready).
  2. Periódico, cada intervalo configurable con /backup auto (por
     defecto 30 minutos).
  3. Manual, con /backup now.

Ninguno de los tres reacciona a un borrado, así que el snapshot más
reciente casi siempre representa al servidor ANTES de cualquier ataque.
Como contrapartida, si alguien borra un canal a propósito (limpieza
normal), el backup no se "entera" hasta el próximo tick periódico — es
un trade-off intencional a favor de nunca arruinar el respaldo bueno.

La restauración (/backup restore) NUNCA borra nada que no esté en el
backup — solo crea lo que falta y actualiza (nombre/posición/permisos)
lo que ya existe con el mismo id o el mismo nombre+tipo. Si el atacante
dejó canales de spam, esos no se tocan; hay que borrarlos a mano (o ya
estarán baneados por el antinuke, que corre en paralelo a esto).
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

from utils import backups, database
from utils.embeds import COLOR_INFO, error_embed, info_embed, success_embed
from utils.permissions import AntiNukeCogBase, admin_only

log = logging.getLogger("antinuke")

DEFAULT_INTERVAL_MINUTES = 30
SCHEDULER_TICK_MINUTES = 5
MIN_NONZERO_INTERVAL = 5
SLEEP_BETWEEN_CALLS = 0.4
RESTORE_REASON = "[AntiNuke] Restauración de backup"


def _relative_label(iso_string: str | None) -> str:
    """Texto plano ('hace 12 min') para usar en autocomplete, donde
    Discord muestra el string tal cual (no interpreta markdown)."""
    if not iso_string:
        return "?"
    try:
        dt = datetime.fromisoformat(iso_string)
    except ValueError:
        return "?"
    secs = int((datetime.now(timezone.utc) - dt).total_seconds())
    if secs < 60:
        return "hace segundos"
    if secs < 3600:
        return f"hace {secs // 60} min"
    if secs < 86400:
        return f"hace {secs // 3600} h"
    return f"hace {secs // 86400} d"


def _discord_timestamp(iso_string: str | None) -> str:
    """<t:...:R> para usarlo en embeds, donde Discord SÍ lo renderiza
    como 'hace 12 minutos' (traducido al idioma de quien lo ve)."""
    if not iso_string:
        return "?"
    try:
        dt = datetime.fromisoformat(iso_string)
    except ValueError:
        return "?"
    return f"<t:{int(dt.timestamp())}:R>"


class RestoreReport:
    def __init__(self) -> None:
        self.created = 0
        self.updated = 0
        self.errors: list[str] = []


class BackupCommands(AntiNukeCogBase):
    def __init__(self, bot: commands.Bot) -> None:
        super().__init__(bot)
        self._restore_locks: dict[int, asyncio.Lock] = {}

    async def cog_unload(self) -> None:
        self.auto_backup_loop.cancel()

    def _lock_for(self, guild_id: int) -> asyncio.Lock:
        return self._restore_locks.setdefault(guild_id, asyncio.Lock())

    # ------------------------------------------------------------------ #
    # Scheduler
    # ------------------------------------------------------------------ #
    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if not self.auto_backup_loop.is_running():
            self.auto_backup_loop.start()

    @tasks.loop(minutes=SCHEDULER_TICK_MINUTES)
    async def auto_backup_loop(self) -> None:
        for guild in list(self.bot.guilds):
            try:
                await self._maybe_auto_backup(guild)
            except Exception:
                log.exception("Fallo el backup automático de '%s' (%s)", guild.name, guild.id)

    @auto_backup_loop.before_loop
    async def _before_auto_backup_loop(self) -> None:
        await self.bot.wait_until_ready()

    async def _maybe_auto_backup(self, guild: discord.Guild) -> None:
        config = database.get_config(guild.id)
        interval = config.get("backup_interval_minutes", DEFAULT_INTERVAL_MINUTES)
        if interval <= 0:
            return
        last = config.get("last_auto_backup_at") or 0
        if time.time() - last < interval * 60:
            return
        trigger = "startup" if last == 0 else "periodic"
        await self._run_backup(guild, trigger=trigger)

    async def _run_backup(self, guild: discord.Guild, trigger: str) -> dict:
        snapshot = backups.snapshot_guild(guild, trigger)
        backup_id = backups.save_backup(guild.id, snapshot)
        config = database.get_config(guild.id)
        config["last_auto_backup_at"] = time.time()
        database.save_config(guild.id, config)
        num_channels = sum(len(c["channels"]) for c in snapshot["categories"]) + len(
            snapshot["uncategorized"]
        )
        log.info(
            "Backup '%s' (%s) guardado para '%s' (%s): %d categorías, %d canales.",
            backup_id, trigger, guild.name, guild.id, len(snapshot["categories"]), num_channels,
        )
        snapshot["id"] = backup_id
        return snapshot

    # ------------------------------------------------------------------ #
    # Comandos
    # ------------------------------------------------------------------ #
    @commands.hybrid_group(
        name="backup",
        description="Backup automático de categorías y canales del servidor",
        invoke_without_command=True,
    )
    async def backup(self, ctx: commands.Context) -> None:
        prefix = ctx.prefix if ctx.interaction is None else "/"
        await ctx.send(
            embed=info_embed(f"`{prefix}backup now|list|restore|auto`", title="💾 Backup"),
            ephemeral=True,
        )

    @backup.command(name="now", description="Guarda un backup manual ahora mismo")
    @admin_only()
    async def backup_now(self, ctx: commands.Context) -> None:
        await ctx.defer(ephemeral=True)
        snapshot = await self._run_backup(ctx.guild, trigger="manual")
        num_channels = sum(len(c["channels"]) for c in snapshot["categories"]) + len(
            snapshot["uncategorized"]
        )
        await ctx.send(
            embed=success_embed(
                f"Backup `{snapshot['id']}` guardado: **{len(snapshot['categories'])}** "
                f"categorías y **{num_channels}** canales.",
            ),
            ephemeral=True,
        )

    @backup.command(name="list", description="Muestra los backups guardados de este servidor")
    async def backup_list(self, ctx: commands.Context) -> None:
        items = backups.list_backups(ctx.guild.id)
        if not items:
            await ctx.send(
                embed=info_embed(
                    "Todavía no hay ningún backup. Se toma uno automáticamente apenas el bot "
                    "está conectado, y podés forzar uno con `/backup now`.",
                    title="💾 Backups",
                ),
                ephemeral=True,
            )
            return
        lines = [
            f"`{item['id']}` — {_discord_timestamp(item['created_at'])} ({item['trigger']}) — "
            f"{item['categories']} categorías, {item['channels']} canales"
            for item in items
        ]
        await ctx.send(
            embed=info_embed("\n".join(lines), title="💾 Backups guardados (más nuevo primero)"),
            ephemeral=True,
        )

    @backup.command(name="auto", description="Cada cuánto se guarda un backup automático (0 = desactivar)")
    @app_commands.describe(minutos="Minutos entre backups automáticos (0 desactiva; mínimo 5 si no es 0)")
    @admin_only()
    async def backup_auto(self, ctx: commands.Context, minutos: commands.Range[int, 0, 1440]) -> None:
        if 0 < minutos < MIN_NONZERO_INTERVAL:
            await ctx.send(
                embed=error_embed(f"El mínimo es {MIN_NONZERO_INTERVAL} minutos (o 0 para desactivar)."),
                ephemeral=True,
            )
            return
        config = database.get_config(ctx.guild.id)
        config["backup_interval_minutes"] = minutos
        database.save_config(ctx.guild.id, config)
        if minutos == 0:
            msg = "Backups automáticos **desactivados**. `/backup now` sigue disponible a mano."
        else:
            msg = f"Backup automático cada **{minutos} minutos**."
        await ctx.send(embed=success_embed(msg), ephemeral=True)

    @backup.command(name="restore", description="Restaura categorías y canales desde un backup guardado")
    @app_commands.describe(
        backup_id="Qué backup restaurar (vacío = el más reciente)",
        confirmar="Tenés que poner true para que se ejecute de verdad",
    )
    @admin_only()
    async def backup_restore(
        self, ctx: commands.Context, backup_id: str | None = None, confirmar: bool = False,
    ) -> None:
        target_id = backup_id or backups.latest_backup_id(ctx.guild.id)
        if target_id is None:
            await ctx.send(embed=error_embed("No hay ningún backup guardado todavía."), ephemeral=True)
            return

        snapshot = backups.load_backup(ctx.guild.id, target_id)
        if snapshot is None:
            await ctx.send(
                embed=error_embed(f"No encontré ningún backup con id «{target_id}»."), ephemeral=True,
            )
            return

        num_channels = sum(len(c["channels"]) for c in snapshot["categories"]) + len(
            snapshot["uncategorized"]
        )

        if not confirmar:
            await ctx.send(
                embed=info_embed(
                    f"Esto va a crear (o actualizar, si ya existen con el mismo nombre) "
                    f"**{len(snapshot['categories'])}** categorías y **{num_channels}** canales, "
                    f"según el backup de {_discord_timestamp(snapshot['created_at'])}.\n\n"
                    f"No se borra nada que no esté en el backup. Para confirmar, ejecutá de "
                    f"nuevo con `confirmar: true`.",
                    title="⚠️ Confirmar restauración",
                ),
                ephemeral=True,
            )
            return

        lock = self._lock_for(ctx.guild.id)
        if lock.locked():
            await ctx.send(
                embed=error_embed("Ya hay una restauración en curso en este servidor — esperá a que termine."),
                ephemeral=True,
            )
            return

        await ctx.defer(ephemeral=True)
        async with lock:
            report = await self._restore(ctx.guild, snapshot)

        summary = f"**{report.created}** canales/categorías creados, **{report.updated}** actualizados."
        if report.errors:
            shown = report.errors[:10]
            summary += "\n\n⚠️ Problemas:\n" + "\n".join(f"• {e}" for e in shown)
            extra = len(report.errors) - len(shown)
            if extra > 0:
                summary += f"\n• …y {extra} más."
        await ctx.send(embed=success_embed(summary, title="💾 Restauración completa"), ephemeral=True)

        config = database.get_config(ctx.guild.id)
        log_channel = ctx.guild.get_channel(config.get("log_channel")) if config.get("log_channel") else None
        if log_channel is not None:
            embed = discord.Embed(
                title="💾 Backup restaurado",
                description=(
                    f"{ctx.author.mention} restauró el backup `{target_id}` "
                    f"({report.created} creados, {report.updated} actualizados)."
                ),
                color=COLOR_INFO,
                timestamp=discord.utils.utcnow(),
            )
            try:
                await log_channel.send(embed=embed)
            except discord.HTTPException:
                pass

    @backup_restore.autocomplete("backup_id")
    async def _backup_id_autocomplete(
        self, interaction: discord.Interaction, current: str,
    ) -> list[app_commands.Choice[str]]:
        if interaction.guild_id is None:
            return []
        choices = []
        for item in backups.list_backups(interaction.guild_id):
            label = (
                f"{item['id']} — {_relative_label(item['created_at'])} "
                f"({item['trigger']}, {item['categories']} cat / {item['channels']} canales)"
            )
            if current.lower() in label.lower():
                choices.append(app_commands.Choice(name=label[:100], value=item["id"]))
        return choices[:25]

    # ------------------------------------------------------------------ #
    # Motor de restauración
    # ------------------------------------------------------------------ #
    async def _restore(self, guild: discord.Guild, snapshot: dict) -> RestoreReport:
        report = RestoreReport()
        role_by_id = {r.id: r for r in guild.roles}
        role_by_name = {r.name.lower(): r for r in guild.roles}

        def resolve_overwrites(entries: list[dict]) -> dict:
            result = {}
            for entry in entries:
                allow = discord.Permissions(entry["allow"])
                deny = discord.Permissions(entry["deny"])
                ow = discord.PermissionOverwrite.from_pair(allow, deny)
                if entry["type"] == "role":
                    target = role_by_id.get(entry["id"]) or role_by_name.get(
                        entry["name"].lstrip("@").lower()
                    )
                else:
                    target = guild.get_member(entry["id"])
                if target is not None:
                    result[target] = ow
            return result

        # 1) Categorías: reusar por id o nombre, o crear.
        existing_cat_by_id = {c.id: c for c in guild.categories}
        existing_cat_by_name = {c.name.lower(): c for c in guild.categories}
        category_objs: list[discord.CategoryChannel | None] = []

        for cat_data in snapshot["categories"]:
            existing = existing_cat_by_id.get(cat_data["id"]) or existing_cat_by_name.get(
                cat_data["name"].lower()
            )
            overwrites = resolve_overwrites(cat_data.get("overwrites", []))
            if existing is not None:
                # La categoría YA existe de verdad — aunque falle el
                # rename/overwrites, sigue siendo un lugar válido donde
                # poner sus canales, así que NO se trata como fallo total.
                try:
                    await existing.edit(name=cat_data["name"], overwrites=overwrites, reason=RESTORE_REASON)
                    report.updated += 1
                except discord.HTTPException as exc:
                    report.errors.append(f"No se pudo actualizar la categoría «{cat_data['name']}»: {exc}")
                category_objs.append(existing)
            else:
                try:
                    new_cat = await guild.create_category(
                        cat_data["name"], overwrites=overwrites, reason=RESTORE_REASON,
                    )
                    report.created += 1
                    category_objs.append(new_cat)
                    existing_cat_by_name[cat_data["name"].lower()] = new_cat
                except discord.Forbidden:
                    report.errors.append(f"Sin permiso para crear la categoría «{cat_data['name']}»")
                    category_objs.append(None)
                except discord.HTTPException as exc:
                    report.errors.append(f"No se pudo crear la categoría «{cat_data['name']}»: {exc}")
                    category_objs.append(None)
            await asyncio.sleep(SLEEP_BETWEEN_CALLS)

        # 2) Orden de categorías (de arriba hacia abajo, edits secuenciales).
        for idx, cat in enumerate(category_objs):
            if cat is None:
                continue
            try:
                await cat.edit(position=idx, reason=f"{RESTORE_REASON} (orden)")
            except discord.HTTPException:
                pass
            await asyncio.sleep(SLEEP_BETWEEN_CALLS)

        # 3) Canales. El lookup por id es GLOBAL (no solo dentro de la
        # categoría "correcta") para poder encontrar y reubicar un canal
        # que sigue existiendo pero quedó en otro lado.
        all_channels_by_id = {
            c.id: c for c in guild.channels if not isinstance(c, discord.CategoryChannel)
        }

        for cat_data, category in zip(snapshot["categories"], category_objs):
            if category is None:
                # La categoría no se pudo crear — sus canales se omiten
                # a propósito en vez de crearlos sueltos sin categoría,
                # que sería más confuso que simplemente no crearlos.
                if cat_data["channels"]:
                    report.errors.append(
                        f"Se omitieron {len(cat_data['channels'])} canal(es) de "
                        f"«{cat_data['name']}» porque la categoría no se pudo crear."
                    )
                continue
            await self._restore_channel_list(
                guild, cat_data["channels"], category, all_channels_by_id, resolve_overwrites, report,
            )

        # 4) Canales sin categoría.
        await self._restore_channel_list(
            guild, snapshot["uncategorized"], None, all_channels_by_id, resolve_overwrites, report,
        )

        return report

    async def _restore_channel_list(
        self,
        guild: discord.Guild,
        channel_entries: list[dict],
        category: discord.CategoryChannel | None,
        all_channels_by_id: dict[int, discord.abc.GuildChannel],
        resolve_overwrites,
        report: RestoreReport,
    ) -> None:
        if category is not None:
            siblings = category.channels
        else:
            siblings = [
                c for c in guild.channels
                if not isinstance(c, discord.CategoryChannel) and c.category is None
            ]
        existing_by_name = {(c.name.lower(), c.type.name): c for c in siblings}

        channel_objs: list[discord.abc.GuildChannel | None] = []
        for ch_data in channel_entries:
            existing = all_channels_by_id.get(ch_data["id"])
            if existing is not None and existing.type.name != ch_data["type"]:
                existing = None
            if existing is None:
                existing = existing_by_name.get((ch_data["name"].lower(), ch_data["type"]))

            overwrites = resolve_overwrites(ch_data.get("overwrites", []))
            try:
                obj = await self._create_or_update_channel(guild, category, ch_data, existing, overwrites)
                channel_objs.append(obj)
                if existing is not None:
                    report.updated += 1
                else:
                    report.created += 1
                    all_channels_by_id[obj.id] = obj
            except discord.Forbidden:
                report.errors.append(f"Sin permiso para «{ch_data['name']}»")
                channel_objs.append(None)
            except discord.HTTPException as exc:
                report.errors.append(f"Canal «{ch_data['name']}»: {exc}")
                channel_objs.append(None)
            await asyncio.sleep(SLEEP_BETWEEN_CALLS)

        for idx, obj in enumerate(channel_objs):
            if obj is None:
                continue
            try:
                await obj.edit(position=idx, reason=f"{RESTORE_REASON} (orden)")
            except discord.HTTPException:
                pass
            await asyncio.sleep(SLEEP_BETWEEN_CALLS)

    async def _create_or_update_channel(
        self,
        guild: discord.Guild,
        category: discord.CategoryChannel | None,
        data: dict,
        existing: discord.abc.GuildChannel | None,
        overwrites: dict,
    ) -> discord.abc.GuildChannel:
        ch_type = data["type"]
        extra = backups.type_specific_kwargs(ch_type, data)

        if existing is not None:
            await existing.edit(
                name=data["name"], category=category, overwrites=overwrites,
                reason=RESTORE_REASON, **extra,
            )
            return existing

        if ch_type in ("text", "news"):
            return await guild.create_text_channel(
                data["name"], category=category, overwrites=overwrites,
                news=(ch_type == "news"), reason=RESTORE_REASON, **extra,
            )
        if ch_type == "voice":
            return await guild.create_voice_channel(
                data["name"], category=category, overwrites=overwrites, reason=RESTORE_REASON, **extra,
            )
        if ch_type == "stage_voice":
            return await guild.create_stage_channel(
                data["name"], category=category, overwrites=overwrites, reason=RESTORE_REASON, **extra,
            )
        if ch_type in ("forum", "media"):
            return await guild.create_forum(
                data["name"], category=category, overwrites=overwrites,
                media=(ch_type == "media"), reason=RESTORE_REASON, **extra,
            )
        raise ValueError(f"Tipo de canal no soportado: {ch_type}")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(BackupCommands(bot))
