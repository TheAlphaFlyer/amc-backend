import asyncio
import aiohttp
from concurrent.futures import ThreadPoolExecutor
from arq.connections import RedisSettings
from arq import cron
import django

django.setup()
from django.conf import settings  # noqa: E402
from django.utils import timezone  # noqa: E402
from amc.tasks import process_log_line  # noqa: E402
import amc.tasks as tasks_module  # noqa: E402
from necesse.tasks import process_necesse_log  # noqa: E402
from amc.events import monitor_events, send_event_embeds  # noqa: E402
from amc.locations import monitor_locations  # noqa: E402
from amc.characterlocation_stats import refresh_all_vehicle_stats  # noqa: E402
from amc.webhook import monitor_webhook  # noqa: E402
from amc.ubi import handout_ubi, TASK_FREQUENCY as UBI_TASK_FREQUENCY  # noqa: E402
from amc.deliverypoints import monitor_deliverypoints  # noqa: E402
from amc.jobs import monitor_jobs  # noqa: E402
from amc.status import monitor_server_status  # noqa: E402
from amc.gov_employee import expire_gov_employees  # noqa: E402
from amc.supply_chain import monitor_supply_chain_events  # noqa: E402
import discord  # noqa: E402
from amc.discord_client import bot as discord_client  # noqa: E402
from amc_finance.services import apply_interest_to_bank_accounts  # noqa: E402

REDIS_SETTINGS = RedisSettings(**settings.REDIS_SETTINGS)

# Global timeout for all game/mod server API calls (prevents 5-min default)
GAME_SERVER_TIMEOUT = aiohttp.ClientTimeout(total=10)

bot_task_handle = None
# pyrefly: ignore [unknown-name]
loop = None


def run_blocking_bot():
    discord.utils.setup_logging(root=False)
    try:
        discord_client.run(settings.DISCORD_TOKEN)
    except Exception as e:
        print(f"Error in bot thread: {e}")
    except asyncio.CancelledError:
        # pyrefly: ignore [unused-coroutine]
        discord_client.close()


async def run_discord():
    global loop
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(ThreadPoolExecutor(max_workers=1), run_blocking_bot)


async def startup(ctx):
    global bot_task_handle
    ctx["startup_time"] = timezone.now()
    ctx["http_client"] = aiohttp.ClientSession(
        base_url=settings.GAME_SERVER_API_URL, timeout=GAME_SERVER_TIMEOUT
    )
    ctx["http_client_mod"] = aiohttp.ClientSession(
        base_url=settings.MOD_SERVER_API_URL, timeout=GAME_SERVER_TIMEOUT
    )
    ctx["http_client_webhook"] = aiohttp.ClientSession(
        base_url=settings.WEBHOOK_SERVER_API_URL, timeout=GAME_SERVER_TIMEOUT
    )
    ctx["http_client_event"] = aiohttp.ClientSession(
        base_url=settings.EVENT_GAME_SERVER_API_URL, timeout=GAME_SERVER_TIMEOUT
    )
    ctx["http_client_event_mod"] = aiohttp.ClientSession(
        base_url=settings.EVENT_MOD_SERVER_API_URL, timeout=GAME_SERVER_TIMEOUT
    )


    if settings.DISCORD_TOKEN:
        ctx["discord_client"] = discord_client
        bot_task_handle = asyncio.create_task(run_discord())
        # Set Discord client reference for the message queue
        tasks_module._discord_client_ref = discord_client


async def shutdown(ctx):
    if http_client := ctx.get("http_client"):
        await http_client.close()

    if http_client_mod := ctx.get("http_client_mod"):
        await http_client_mod.close()

    if http_client_mod := ctx.get("http_client_webhook"):
        await http_client_mod.close()

    if http_client := ctx.get("http_client_event"):
        await http_client.close()

    if http_client := ctx.get("http_client_event_mod"):
        await http_client.close()



    if bot_task_handle and (discord_client := ctx.get("discord_client")):
        asyncio.run_coroutine_threadsafe(discord_client.close(), discord_client.loop)
        await bot_task_handle


async def monitor_event_locations(ctx):
    await monitor_locations({"http_client_mod": ctx["http_client_event_mod"]})


async def monitor_events_main(ctx):
    await monitor_events(ctx, ctx["http_client_mod"])


async def monitor_events_event(ctx):
    await monitor_events(ctx, ctx["http_client_event_mod"])


class WorkerSettings:
    functions = [
        process_log_line,
        process_necesse_log,
    ]
    cron_jobs = [
        # pyrefly: ignore [bad-argument-type]
        cron(monitor_webhook, second=set(range(0, 60, 4))),
        # pyrefly: ignore [bad-argument-type]
        cron(monitor_locations, second=None),
        # pyrefly: ignore [bad-argument-type]
        cron(handout_ubi, minute=set(range(0, 60, UBI_TASK_FREQUENCY)), second=37),
        # pyrefly: ignore [bad-argument-type]
        cron(apply_interest_to_bank_accounts, hour=None, minute=0, second=0),
        # cron(monitor_events_main, second=None),
        # pyrefly: ignore [bad-argument-type]
        cron(monitor_events_event, second=None),
        # pyrefly: ignore [bad-argument-type]
        cron(send_event_embeds, second=set(range(0, 60, 10))),
        # cron(monitor_event_locations, second=None),
        # pyrefly: ignore [bad-argument-type]
        cron(monitor_deliverypoints, second=set(range(0, 60, 30))),
        # pyrefly: ignore [bad-argument-type]
        cron(monitor_jobs, second=37),
        # cron(monitor_corporations, second=23),
        # pyrefly: ignore [bad-argument-type]
        cron(monitor_server_status, second=set(range(3, 60, 10))),
        # pyrefly: ignore [bad-argument-type]
        cron(expire_gov_employees, minute=set(range(0, 60, 5)), second=47),
        # pyrefly: ignore [bad-argument-type]
        cron(refresh_all_vehicle_stats, hour=None, minute=30, second=0),
        # pyrefly: ignore [bad-argument-type]
        cron(monitor_supply_chain_events, second=47),
        # cron(monitor_server_condition, minute=set(range(3, 60, 5))),
        # cron(monitor_rp_mode, second=set(range(7, 60, 13))),
    ]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = REDIS_SETTINGS
    max_jobs = 100
