"""
Snapshots del estado actual (categorías + canales, EN ORDEN) de un
servidor, para poder restaurarlo después de un nuke.

Diseño clave: el orden se guarda de forma IMPLÍCITA en el orden de las
listas del JSON — el primer elemento de "categories" es la primera
categoría de arriba hacia abajo, el primer canal de una categoría es el
de más arriba dentro de ella, etc. No se guarda el "position" crudo de
Discord porque no sirve de nada reutilizarlo tal cual: tras un nuke los
IDs y posiciones se renumeran todos, así que lo único que importa es
reproducir el MISMO ORDEN relativo, no el mismo número. Ver
cogs/backups.py para cómo se usa esto al restaurar.

CUÁNDO se toma un snapshot automático es decisión de cogs/backups.py, no
de este módulo — acá solo vive la lógica de "cómo armar un snapshot" y
"dónde guardarlo/leerlo". Pero para que quede documentado en un solo
lugar: a propósito NUNCA se dispara un backup en reacción directa a un
canal/categoría borrado, porque un nuke en curso pisaría el último
backup bueno con el servidor ya vacío, justo cuando más se necesita.

Archivos: un JSON por snapshot en
<DATA_DIR>/backups/<guild_id>/<timestamp_unix>.json. En vez de pisar
siempre el mismo archivo se guardan varios, y se podan los más viejos
más allá de MAX_BACKUPS_PER_GUILD — así, si un nuke justo coincidiera
con un snapshot automático, todavía queda una historia reciente de
snapshots buenos anteriores para elegir de dónde restaurar.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from threading import Lock
from typing import Any

import discord

from utils.database import DATA_DIR

BACKUPS_DIR = DATA_DIR / "backups"
MAX_BACKUPS_PER_GUILD = 10
SCHEMA_VERSION = 1

_lock = Lock()

# Tipos de canal que forman parte de la estructura "canales y categorías"
# que cubre este backup. Los hilos (threads) quedan afuera a propósito:
# son temporales, no son parte fija de la jerarquía de categorías, y
# discord.py ni siquiera los expone en guild.channels (viven aparte en
# guild.threads), así que no hace falta filtrarlos, pero se documenta
# igual para que quede claro que es intencional si alguien pregunta "¿y
# los hilos?".
SUPPORTED_CHANNEL_TYPES = {"text", "news", "voice", "stage_voice", "forum", "media"}


def _guild_dir(guild_id: int) -> Path:
    d = BACKUPS_DIR / str(guild_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _overwrites_to_data(overwrites: dict) -> list[dict[str, Any]]:
    """Serializa los permission overwrites de un canal/categoría. Se
    guarda el id Y el nombre del rol/miembro: el id sirve si todavía
    existe tal cual, el nombre es el fallback si hubo que recrearlo
    (o si el id ya no existe) al restaurar."""
    data = []
    for target, ow in overwrites.items():
        allow, deny = ow.pair()
        kind = "role" if isinstance(target, discord.Role) else "member"
        data.append({
            "type": kind,
            "id": target.id,
            "name": str(target),
            "allow": allow.value,
            "deny": deny.value,
        })
    return data


def _channel_to_data(channel: discord.abc.GuildChannel) -> dict[str, Any] | None:
    ch_type = channel.type.name
    if ch_type not in SUPPORTED_CHANNEL_TYPES:
        return None

    data: dict[str, Any] = {
        "id": channel.id,
        "name": channel.name,
        "type": ch_type,
        "overwrites": _overwrites_to_data(channel.overwrites),
    }

    if ch_type in ("text", "news"):
        data["topic"] = channel.topic or ""
        data["nsfw"] = channel.nsfw
        data["slowmode_delay"] = channel.slowmode_delay
    elif ch_type in ("voice", "stage_voice"):
        data["bitrate"] = channel.bitrate
        data["user_limit"] = channel.user_limit
        data["nsfw"] = getattr(channel, "nsfw", False)
        data["rtc_region"] = str(channel.rtc_region) if channel.rtc_region else None
        data["video_quality_mode"] = channel.video_quality_mode.name
    elif ch_type in ("forum", "media"):
        data["topic"] = channel.topic or ""
        data["nsfw"] = channel.nsfw
        data["slowmode_delay"] = channel.slowmode_delay

    return data


def _channel_sort_key(channel: discord.abc.GuildChannel) -> tuple[bool, int]:
    # Mismo criterio que ya usa discord.py en CategoryChannel.channels:
    # canales de texto/foro primero, de voz/stage después, y dentro de
    # cada grupo por su position real. Se aplica acá también a los
    # canales SIN categoría (discord.py no expone un equivalente listo
    # para ese caso), para que el orden capturado sea consistente con
    # el de dentro de una categoría.
    is_voice_like = channel.type.name in ("voice", "stage_voice")
    return (is_voice_like, channel.position)


def type_specific_kwargs(ch_type: str, data: dict[str, Any]) -> dict[str, Any]:
    """Arma los kwargs de discord.py específicos de cada tipo de canal a
    partir de los datos guardados, para pasarlos tanto a create_* como a
    .edit(). Vive acá (no en cogs/backups.py) porque es lógica pura de
    mapeo de datos, sin llamadas a la API."""
    if ch_type in ("text", "news"):
        return {
            "topic": data.get("topic") or "",
            "nsfw": data.get("nsfw", False),
            "slowmode_delay": data.get("slowmode_delay", 0),
        }
    if ch_type in ("voice", "stage_voice"):
        vqm_name = data.get("video_quality_mode") or "auto"
        vqm = getattr(discord.VideoQualityMode, vqm_name, discord.VideoQualityMode.auto)
        return {
            "bitrate": data.get("bitrate") or 64000,
            "user_limit": data.get("user_limit", 0),
            "nsfw": data.get("nsfw", False),
            "rtc_region": data.get("rtc_region"),
            "video_quality_mode": vqm,
        }
    if ch_type in ("forum", "media"):
        return {
            "topic": data.get("topic") or "",
            "nsfw": data.get("nsfw", False),
            "slowmode_delay": data.get("slowmode_delay", 0),
        }
    return {}


def snapshot_guild(guild: discord.Guild, trigger: str) -> dict[str, Any]:
    """Arma el snapshot del estado actual de categorías y canales, en el
    mismo orden en que Discord los muestra (de arriba hacia abajo)."""
    categories_data = []
    for category in sorted(guild.categories, key=lambda c: c.position):
        channels_data = [
            d for c in category.channels if (d := _channel_to_data(c)) is not None
        ]
        categories_data.append({
            "id": category.id,
            "name": category.name,
            "overwrites": _overwrites_to_data(category.overwrites),
            "channels": channels_data,
        })

    uncategorized = [
        c for c in guild.channels
        if not isinstance(c, discord.CategoryChannel)
        and c.category is None
        and c.type.name in SUPPORTED_CHANNEL_TYPES
    ]
    uncategorized.sort(key=_channel_sort_key)
    uncategorized_data = [_channel_to_data(c) for c in uncategorized]

    return {
        "version": SCHEMA_VERSION,
        "guild_id": guild.id,
        "guild_name": guild.name,
        "created_at": discord.utils.utcnow().isoformat(),
        "trigger": trigger,
        "categories": categories_data,
        "uncategorized": uncategorized_data,
    }


def save_backup(guild_id: int, snapshot: dict[str, Any]) -> str:
    """Guarda el snapshot como un archivo nuevo (no pisa los anteriores)
    y poda los más viejos. Devuelve el backup_id (timestamp unix)."""
    backup_id = str(int(time.time()))
    path = _guild_dir(guild_id) / f"{backup_id}.json"
    with _lock:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, indent=2, ensure_ascii=False)
        _prune(guild_id)
    return backup_id


def _prune(guild_id: int) -> None:
    files = sorted(_guild_dir(guild_id).glob("*.json"), key=lambda p: p.stem, reverse=True)
    for old in files[MAX_BACKUPS_PER_GUILD:]:
        try:
            old.unlink()
        except OSError:
            pass


def list_backups(guild_id: int) -> list[dict[str, Any]]:
    """Metadata de los backups guardados, más nuevo primero.

    Cada archivo se procesa de forma aislada: si uno solo está corrupto,
    incompleto (crash a mitad de escritura) o tiene un schema viejo al
    que le falta alguna clave, se lo salta en vez de tirar abajo el
    comando entero (antes un solo backup con datos mal formados hacía
    fallar TODO /backup list con un error genérico).
    """
    results = []
    with _lock:
        paths = sorted(_guild_dir(guild_id).glob("*.json"), key=lambda p: p.stem, reverse=True)
        for path in paths:
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                num_categories = len(data.get("categories", []))
                num_channels = sum(
                    len(c.get("channels", [])) for c in data.get("categories", [])
                ) + len(data.get("uncategorized", []))
                results.append({
                    "id": path.stem,
                    "created_at": data.get("created_at"),
                    "trigger": data.get("trigger", "?"),
                    "categories": num_categories,
                    "channels": num_channels,
                })
            except (OSError, json.JSONDecodeError, KeyError, TypeError, AttributeError):
                continue
    return results


def load_backup(guild_id: int, backup_id: str) -> dict[str, Any] | None:
    # backup_id llega de un usuario (comando/autocomplete): nunca se
    # concatena directo a una ruta sin validar el formato, para no
    # abrir la puerta a path traversal (ej. "../../algo").
    if not backup_id.isdigit():
        return None
    path = _guild_dir(guild_id) / f"{backup_id}.json"
    if not path.exists():
        return None
    with _lock:
        with open(path, "r", encoding="utf-8") as f:
            try:
                return json.load(f)
            except json.JSONDecodeError:
                return None


def latest_backup_id(guild_id: int) -> str | None:
    files = sorted(_guild_dir(guild_id).glob("*.json"), key=lambda p: p.stem, reverse=True)
    return files[0].stem if files else None
