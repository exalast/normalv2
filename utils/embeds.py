"""
Constructores de embeds reutilizados por todos los cogs, para que la
apariencia sea consistente en todo el bot:

- error_embed()   -> rojo. Permisos denegados, algo no encontrado, algo
                     que ya estaba en ese estado, argumentos inválidos.
- success_embed() -> verde. Se acaba de aplicar un cambio (agregar, quitar,
                     configurar, activar/desactivar).
- info_embed()    -> azul/blurple. Solo muestra información (listas,
                     configuración actual, ayuda, uso de un comando).

Todas las respuestas de comandos del bot usan alguno de estos tres, para
que nunca se mande texto plano.
"""
from __future__ import annotations

import discord

COLOR_ERROR = discord.Color.red()
COLOR_SUCCESS = discord.Color.green()
COLOR_INFO = discord.Color.blurple()


def error_embed(description: str, *, title: str = "❌ No se pudo completar") -> discord.Embed:
    return discord.Embed(title=title, description=description, color=COLOR_ERROR)


def success_embed(description: str, *, title: str = "✅ Listo") -> discord.Embed:
    return discord.Embed(title=title, description=description, color=COLOR_SUCCESS)


def info_embed(description: str | None = None, *, title: str) -> discord.Embed:
    return discord.Embed(title=title, description=description, color=COLOR_INFO)
