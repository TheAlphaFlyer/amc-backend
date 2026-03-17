import asyncio
from django.contrib.gis.geos import Point
from django.utils import timezone
from amc.models import Character, CharacterLocation
from amc.utils import skip_if_running
from amc.mod_server import show_popup, teleport_player
from django.conf import settings


point_of_interests = [
    (
        Point(**{"z": -20696.78, "y": 150230.13, "x": 1025.73}),
        300,
        """\
<Title>Corporation Rules</Title>
<Warning>Corporations are NOT ALLOWED</Warning> - if you are planning to use AI drivers.
Having too many AI vehicles on the server have caused traffic jams and other mishaps.
Unlicensed corporations will be closed down!

<Bold>You may ONLY start a corporation for the following purposes:</Bold>
- Spawning Campy's around the map
- Renting out vehicles for other players
- Showcasing liveries for car shows

For any other purposes, <Highlight>please contact the admins on the discord</Highlight>.
""",
    ),
    (
        Point(**{"x": -220383.08, "y": 141777.71, "z": -20186.82}),
        500,
        """\
<Title>Want to take out a loan?</Title>
<Warning>This bank charges high interest rate!</Warning> - many players have ended up in a debt spiral.

<Bold>Use Bank ASEAN instead!</Bold>
- Our loans are interest free
- Our loans have to repayment period
- You only have to pay them back when you make a profit

Use <Highlight>/bank</Highlight> to create a Bank ASEAN account today!
""",
    ),
    (
        Point(**{"z": -21564.73, "y": 157275.61, "x": -83784.37}),
        2000,
        f"""\
<Title>Welcome to the ASEAN Park</Title>

{settings.CREDITS_TEXT}
""",
    ),
]

portals = [
    # Meehoi house
    (
        Point(**{"x": 69664.27, "y": 651361.93, "z": -8214.26}),
        150,
        Point(**{"x": 68205.77, "y": 651084.19, "z": -7000.43}),
    ),
    (
        Point(**{"x": 68119.18, "y": 650502.15, "z": -6909.83}),
        120,
        Point(**{"x": 67912.23, "y": 650236.37, "z": -8512.19}),
    ),
    # Rooftop Bar
    (
        Point(**{"x": -67173.12, "y": 150561.7, "z": -20646.4}),
        150,
        Point(**{"x": -66531.100038674, "y": 150471.72884842, "z": -19706.865}),
    ),
    (
        Point(**{"x": -66733.74, "y": 150411.51, "z": -19703.15}),
        120,
        Point(**{"x": -67245.74, "y": 150831.6, "z": -20646.85}),
    ),
]


async def _check_pois_and_portals(character, old_location, new_location, ctx):
    """Check POI entries and portal triggers using the cached last_location."""
    player = character.player
    http_client_mod = ctx.get("http_client_mod")
    if http_client_mod is None:
        return

    for target_point, target_radius_meters, message in point_of_interests:
        distance_to_new = new_location.distance(target_point)
        distance_to_old = old_location.distance(target_point)

        was_outside = distance_to_old > target_radius_meters
        is_inside = distance_to_new <= target_radius_meters

        if was_outside and is_inside:
            await show_popup(http_client_mod, message, player_id=player.unique_id)
            await asyncio.sleep(0.1)

    for source_point, source_radius_meters, target_point in portals:
        distance_to_new = new_location.distance(source_point)
        distance_to_old = old_location.distance(source_point)

        was_outside = distance_to_old > source_radius_meters
        is_inside = distance_to_new <= source_radius_meters

        if was_outside and is_inside:
            await teleport_player(
                http_client_mod,
                str(player.unique_id),
                {"X": target_point.x, "Y": target_point.y, "Z": target_point.z},
            )
            await asyncio.sleep(0.1)


@skip_if_running
async def monitor_locations(ctx):
    http_client = ctx.get("http_client_mod")
    async with http_client.get("/players") as resp:
        players = (await resp.json()).get("data", [])

    if not players:
        return

    # Build lookup: CharacterGuid -> player_info
    guid_to_player_info = {
        p["CharacterGuid"]: p for p in players if p.get("CharacterGuid")
    }

    if not guid_to_player_info:
        return

    # Single batch query: fetch all matching characters
    characters = {
        c.guid: c
        async for c in Character.objects.select_related("player").filter(
            guid__in=guid_to_player_info.keys()
        )
    }

    new_locations = []
    characters_to_update = []
    now = timezone.now()

    for guid, player_info in guid_to_player_info.items():
        character = characters.get(guid)
        if not character:
            continue

        location_data = {
            axis.lower(): value for axis, value in player_info["Location"].items()
        }
        new_point = Point(**location_data)
        vehicle_key = player_info["VehicleKey"]

        # Use cached last_location instead of querying 175M-row table
        if character.last_location:
            await _check_pois_and_portals(
                character, character.last_location, new_point, ctx
            )

        # Queue for bulk operations
        new_locations.append(
            CharacterLocation(
                character=character,
                location=new_point,
                vehicle_key=vehicle_key,
            )
        )
        character.last_location = new_point
        character.last_vehicle_key = vehicle_key
        character.last_online = now
        characters_to_update.append(character)

    # Bulk insert all locations in one query (ignore timestamp conflicts)
    if new_locations:
        await CharacterLocation.objects.abulk_create(
            new_locations, ignore_conflicts=True
        )

    # Bulk update cached locations on Character
    if characters_to_update:
        await Character.objects.abulk_update(
            characters_to_update, ["last_location", "last_vehicle_key", "last_online"]
        )
