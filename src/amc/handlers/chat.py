"""Chat event handlers.

Handles: ServerSendChat (from MTDediMod webhook events)

This is the sole processor for in-game chat messages. The log pipeline
(PlayerChatMessageLogEvent in tasks.py) is intentionally disabled — all
chat processing (logging, commands, Discord forwarding, SSE) happens here.
"""

from __future__ import annotations

import asyncio
import logging

from django.conf import settings
from django.utils import timezone

from amc.handlers import register
from amc.models import PlayerChatLog
from amc.mod_server import get_player
from amc.tasks import enqueue_discord_message

logger = logging.getLogger("amc.webhook.handlers.chat")


@register("ServerSendChat")
async def handle_server_send_chat(event, player, character, ctx):
    data = event.get("data", {})
    message = data.get("Message", "")
    category = data.get("Category", 0)
    character_guid = data.get("CharacterGuid", "")
    unique_id = data.get("UniqueID", "")

    if not message or not character_guid:
        return 0, 0, 0, 0

    is_normal_chat = category == 0

    try:
        await PlayerChatLog.objects.acreate(
            timestamp=timezone.now(),
            character=character,
            text=message,
        )
    except Exception:
        logger.exception("Failed to create PlayerChatLog for ServerSendChat webhook event")

    if is_normal_chat and player and character:
        from amc.command_framework import registry, CommandContext

        player_info = {}
        if ctx.http_client_mod and unique_id:
            try:
                player_info = await get_player(ctx.http_client_mod, unique_id) or {}
            except Exception:
                logger.debug("Failed to fetch player_info for chat command context")

        cmd_ctx = CommandContext(
            timestamp=timezone.now(),
            character=character,
            player=player,
            http_client=ctx.http_client,
            http_client_mod=ctx.http_client_mod,
            discord_client=ctx.discord_client,
            player_info=player_info,
            is_current_event=True,
        )

        asyncio.create_task(registry.execute(message, cmd_ctx))

    if is_normal_chat and ctx.discord_client:
        player_name = character.name if character else "Unknown"
        enqueue_discord_message(
            settings.DISCORD_GAME_CHAT_CHANNEL_ID,
            f"**{player_name}:** {message}",
            timezone.now(),
        )

    if is_normal_chat:
        from amc.api.bot_events import emit_bot_event

        player_name = character.name if character else "Unknown"
        player_id = str(player.unique_id) if player else None
        discord_id = player.discord_user_id if player else None

        is_bot_command = message.startswith("/bot ")
        asyncio.create_task(
            emit_bot_event(
                {
                    "type": "chat_message",
                    "timestamp": timezone.now().isoformat(),
                    "player_name": player_name,
                    "player_id": player_id,
                    "discord_id": discord_id,
                    "character_guid": str(character.guid) if character and character.guid else None,
                    "message": message[5:] if is_bot_command else message,
                    "is_bot_command": is_bot_command,
                }
            )
        )

    return 0, 0, 0, 0