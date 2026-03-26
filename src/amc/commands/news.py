import asyncio
from datetime import timedelta
from django.utils import timezone
from django.utils.translation import gettext_lazy
from amc.command_framework import registry, CommandContext
from amc.models import NewsItem
from amc.mod_server import show_popup


def format_news_popup(news_items):
    """Format news items into a popup message string."""
    lines = ["<Title>News</>"]
    for item in news_items:
        date_str = item.created_at.strftime("%d %b %Y")
        lines.append(f"\n<Bold>{item.title}</> ({date_str})")
        if item.body:
            lines.append(item.body)
    return "\n".join(lines)


@registry.register(
    "/news",
    description=gettext_lazy("Show latest server news"),
    category="General",
)
async def cmd_news(ctx: CommandContext):
    news_items = await NewsItem.aget_active()
    if not news_items:
        await ctx.reply("No news at the moment.")
        return
    asyncio.create_task(
        show_popup(
            ctx.http_client_mod,
            format_news_popup(news_items),
            character_guid=ctx.character.guid,
            player_id=str(ctx.player.unique_id),
        )
    )
