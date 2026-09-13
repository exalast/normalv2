"""
Almacenamiento simple en JSON (un archivo por servidor) para guardar:
- el dueño registrado del antinuke
- los admins del antinuke
- la whitelist
- el canal de logs
- el tipo de castigo
- qué módulos de protección están activos

Para producción con más de un par de servidores, o si necesitas más
resistencia, valdría la pena migrar esto a SQLite/Postgres. Se explica
en el README cómo hacerlo con un Volume de Railway para que estos
archivos no se pierdan en cada redeploy.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from threading import Lock
from typing import Any

DATA_DIR = Path(os.getenv("DATA_DIR", "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

_lock = Lock()

DEFAULT_MODULES = {
    "channel_delete": True,
    "channel_create": True,
    "role_delete": True,
    "role_create": True,
    "ban": True,
    "kick": True,
    "webhook_create": True,
    "bot_add": True,
    "dangerous_role": True,
    "guild_update": True,
    "emoji_delete": True,
    "everyone_mention": True,
}

# Cuántas veces hace falta que se repita una acción antes de castigar.
# Solo aplica a los módulos que se cuentan en una ventana de tiempo (ver
# WINDOWS en cogs/antinuke_events.py); bot_add/dangerous_role/guild_update
# no están acá porque esos siempre actúan al instante, sin contar nada.
DEFAULT_THRESHOLDS = {
    "channel_delete": 1,
    "channel_create": 1,
    "role_delete": 1,
    "role_create": 1,
    "ban": 1,
    "kick": 1,
    "webhook_create": 1,
    "emoji_delete": 1,
}

DEFAULT_CONFIG: dict[str, Any] = {
    "owner_id": None,
    "admins": [],
    "whitelist": [],
    "log_channel": None,
    "punishment": "ban",  # ban | kick | strip
    "modules": DEFAULT_MODULES,
    "thresholds": DEFAULT_THRESHOLDS,
    # Backup automático de canales/categorías (ver utils/backups.py y
    # cogs/backups.py). 0 = desactivado. Configurable con /backup auto.
    "backup_interval_minutes": 30,
    "last_auto_backup_at": None,  # timestamp unix del último backup automático
}


def _path(guild_id: int) -> Path:
    return DATA_DIR / f"{guild_id}.json"


def get_config(guild_id: int) -> dict[str, Any]:
    """Devuelve la configuración del servidor, creando valores por defecto si faltan."""
    path = _path(guild_id)
    if not path.exists():
        config = json.loads(json.dumps(DEFAULT_CONFIG))  # copia profunda simple
        return config

    with _lock:
        with open(path, "r", encoding="utf-8") as f:
            try:
                data = json.load(f)
            except json.JSONDecodeError:
                data = {}

    config = json.loads(json.dumps(DEFAULT_CONFIG))
    config.update(data)
    config["modules"] = {**DEFAULT_MODULES, **data.get("modules", {})}
    config["thresholds"] = {**DEFAULT_THRESHOLDS, **data.get("thresholds", {})}
    return config


def save_config(guild_id: int, config: dict[str, Any]) -> None:
    path = _path(guild_id)
    with _lock:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2, ensure_ascii=False)


def ensure_owner(guild_id: int, owner_id: int | None) -> dict[str, Any]:
    """Registra al dueño real de Discord la primera vez que el bot ve el servidor."""
    config = get_config(guild_id)
    if config.get("owner_id") is None and owner_id is not None:
        config["owner_id"] = owner_id
        save_config(guild_id, config)
    return config
