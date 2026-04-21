import logging
import os
import sys
import asyncio
from arq import create_pool
from arq.connections import RedisSettings

logger = logging.getLogger(__name__)


async def _async_handle(*args, **options):
    redis_port = int(os.environ.get('REDIS_PORT', 6379))
    redis = await create_pool(RedisSettings(port=redis_port))

    for line in sys.stdin:
        line = line.rstrip("\n")
        if not line.strip():
            continue
        try:
            _log_timestamp, hostname, tag, filename, game_timestamp, content = line.split(
                " ", 5
            )
        except ValueError:
            logger.warning("Skipping malformed log line: %r", line[:200])
            continue
        if tag == "necesse":
            await redis.enqueue_job("process_necesse_log", line)
        else:
            await redis.enqueue_job("process_log_line", line)
        sys.stdout.write("OK\n")
        sys.stdout.flush()


def main():
    asyncio.run(_async_handle())


