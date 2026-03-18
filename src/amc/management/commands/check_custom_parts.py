"""
Management command to check online players' vehicles for custom/modded parts.

Usage:
    amcm check_custom_parts                     # Check all online players
    amcm check_custom_parts --player-id 12345   # Check a specific player
"""

import asyncio
import logging

import aiohttp
from django.core.management.base import BaseCommand
from django.conf import settings

from amc.mod_server import list_player_vehicles
from amc.game_server import get_players
from amc.vehicles import format_vehicle_name
from amc.mod_detection import detect_custom_parts, format_custom_parts_plain

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Check online players' active vehicles for custom/modded parts"

    def add_arguments(self, parser):
        parser.add_argument(
            "--player-id",
            type=str,
            default=None,
            help="Check a specific player by unique ID",
        )

    def handle(self, *args, **options):
        asyncio.run(self._async_handle(**options))

    async def _async_handle(self, **options):
        player_id = options.get("player_id")
        timeout = aiohttp.ClientTimeout(total=10)

        async with aiohttp.ClientSession(
            base_url=settings.MOD_SERVER_API_URL, timeout=timeout
        ) as http_mod, aiohttp.ClientSession(
            base_url=settings.GAME_SERVER_API_URL, timeout=timeout
        ) as http_game:
            if player_id:
                await self._check_player(http_mod, player_id)
            else:
                await self._check_all(http_mod, http_game)

    async def _check_player(self, http_mod, player_id: str):
        player_vehicles = await list_player_vehicles(
            http_mod, player_id, active=True, complete=True
        )
        if not player_vehicles:
            self.stdout.write(f"Player {player_id} has no spawned vehicles")
            return

        found_any = False
        for v_id, vehicle in player_vehicles.items():

            vehicle_name = format_vehicle_name(vehicle["fullName"])
            parts = vehicle.get("parts", [])
            custom = detect_custom_parts(parts)

            self.stdout.write(
                f"\n{vehicle_name} (#{vehicle.get('vehicleId', v_id)}) — "
                f"{len(parts)} parts total"
            )
            if custom:
                found_any = True
                self.stdout.write(
                    self.style.WARNING(
                        f"  ⚠ {len(custom)} custom part(s):\n"
                        f"{format_custom_parts_plain(custom)}"
                    )
                )
            else:
                self.stdout.write(self.style.SUCCESS("  ✓ All stock parts"))

        if not found_any:
            self.stdout.write(self.style.SUCCESS("\nNo custom parts detected"))

    async def _check_all(self, http_mod, http_game):
        try:
            players = await get_players(http_game)
        except Exception as e:
            self.stderr.write(f"Failed to get online players: {e}")
            return

        if not players:
            self.stdout.write("No players online")
            return

        self.stdout.write(f"Checking {len(players)} online player(s)...\n")

        flagged = 0
        checked = 0

        for player_id, player_data in players:
            player_name = player_data.get("name", player_id)

            try:
                player_vehicles = await list_player_vehicles(
                    http_mod, player_id, active=True, complete=True
                )
            except Exception:
                self.stderr.write(f"  ✗ Failed to get vehicles for {player_name}")
                continue

            if not player_vehicles:
                continue

            for v_id, vehicle in player_vehicles.items():

                checked += 1
                vehicle_name = format_vehicle_name(vehicle["fullName"])
                custom = detect_custom_parts(vehicle.get("parts", []))

                if custom:
                    flagged += 1
                    self.stdout.write(
                        self.style.WARNING(
                            f"⚠ {player_name} ({player_id}) — "
                            f"{vehicle_name}: {len(custom)} custom part(s)"
                        )
                    )
                    self.stdout.write(format_custom_parts_plain(custom))
                else:
                    self.stdout.write(
                        f"  ✓ {player_name} ({player_id}) — {vehicle_name}"
                    )

        self.stdout.write(
            f"\nDone: {checked} vehicle(s) checked, "
            f"{flagged} with custom parts"
        )
