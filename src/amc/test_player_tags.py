import pytest
from unittest.mock import patch, AsyncMock, MagicMock
from amc.player_tags import (
    strip_all_tags,
    build_display_name,
    refresh_player_name,
)


# --- build_display_name ---


def test_build_display_name_no_tags():
    assert build_display_name("PlayerOne") == "PlayerOne"
    assert (
        build_display_name("PlayerOne", has_custom_parts=False, gov_level=0)
        == "PlayerOne"
    )


def test_build_display_name_mod_only():
    assert build_display_name("PlayerOne", has_custom_parts=True) == "[M] PlayerOne"


def test_build_display_name_gov_only():
    assert build_display_name("PlayerOne", gov_level=3) == "[G3] PlayerOne"


def test_build_display_name_gov_multi_digit():
    assert build_display_name("PlayerOne", gov_level=23) == "[G23] PlayerOne"


def test_build_display_name_mod_and_gov():
    assert (
        build_display_name("PlayerOne", has_custom_parts=True, gov_level=3)
        == "[MG3] PlayerOne"
    )


def test_build_display_name_police_only():
    assert build_display_name("PlayerOne", police_level=1) == "[P1] PlayerOne"


def test_build_display_name_police_and_mods():
    assert (
        build_display_name("PlayerOne", police_level=1, has_custom_parts=True)
        == "[MP1] PlayerOne"
    )


def test_build_display_name_police_and_gov():
    assert (
        build_display_name("PlayerOne", police_level=1, gov_level=3)
        == "[P1G3] PlayerOne"
    )


def test_build_display_name_police_mods_and_gov():
    assert (
        build_display_name(
            "PlayerOne", police_level=1, has_custom_parts=True, gov_level=3
        )
        == "[MP1G3] PlayerOne"
    )


def test_build_display_name_police_suppresses_crim():
    """Police membership suppresses criminal tag."""
    assert (
        build_display_name("PlayerOne", police_level=1, criminal_level=1)
        == "[P1] PlayerOne"
    )


def test_build_display_name_police_suppresses_crim_with_gov():
    """Police suppresses criminal tag (gov does not)."""
    assert (
        build_display_name("PlayerOne", police_level=1, criminal_level=3, gov_level=3)
        == "[P1G3] PlayerOne"
    )


def test_build_display_name_crim_level_1():
    assert build_display_name("PlayerOne", criminal_level=1) == "[C1] PlayerOne"


def test_build_display_name_crim_level_5():
    assert build_display_name("PlayerOne", criminal_level=5) == "[C5] PlayerOne"


def test_build_display_name_crim_multi_digit():
    assert build_display_name("PlayerOne", criminal_level=12) == "[C12] PlayerOne"


def test_build_display_name_crim_not_suppressed_without_police():
    """Criminal tag is NOT suppressed when player is not police."""
    assert (
        build_display_name("PlayerOne", criminal_level=2, has_custom_parts=True)
        == "[MC2] PlayerOne"
    )


def test_build_display_name_crim_and_mods():
    assert (
        build_display_name("PlayerOne", criminal_level=1, has_custom_parts=True)
        == "[MC1] PlayerOne"
    )


def test_build_display_name_crim_not_suppressed_by_gov():
    """Criminal tag is NOT suppressed by gov (only police suppresses it)."""
    assert (
        build_display_name("PlayerOne", criminal_level=3, gov_level=3)
        == "[C3G3] PlayerOne"
    )


def test_build_display_name_all_active_crim_not_suppressed():
    """All flags active: criminal NOT suppressed by gov, so [MC2G3]."""
    assert (
        build_display_name(
            "PlayerOne", criminal_level=2, has_custom_parts=True, gov_level=3
        )
        == "[MC2G3] PlayerOne"
    )


def test_build_display_name_all_flags_with_police():
    """All flags + police: criminal suppressed, so [MP1G3]."""
    assert (
        build_display_name(
            "PlayerOne",
            criminal_level=5,
            has_custom_parts=True,
            police_level=1,
            gov_level=3,
        )
        == "[MP1G3] PlayerOne"
    )


def test_build_display_name_police_level_2():
    assert build_display_name("PlayerOne", police_level=2) == "[P2] PlayerOne"


def test_build_display_name_police_level_10():
    assert build_display_name("PlayerOne", police_level=10) == "[P10] PlayerOne"


def test_build_display_name_wanted_only():
    assert (
        build_display_name("PlayerOne", wanted_stars=5)
        == "[*****] PlayerOne"
    )


def test_build_display_name_wanted_w1():
    assert (
        build_display_name("PlayerOne", wanted_stars=1)
        == "[*] PlayerOne"
    )


def test_build_display_name_wanted_w3():
    assert (
        build_display_name("PlayerOne", wanted_stars=3)
        == "[***] PlayerOne"
    )


def test_build_display_name_wanted_and_crim():
    assert (
        build_display_name("PlayerOne", criminal_level=3, wanted_stars=4)
        == "[****C3] PlayerOne"
    )


def test_build_display_name_wanted_and_mods():
    assert (
        build_display_name("PlayerOne", has_custom_parts=True, wanted_stars=2)
        == "[M**] PlayerOne"
    )


def test_build_display_name_wanted_with_police():
    """Wanted tag shows even when police is active."""
    assert (
        build_display_name("PlayerOne", police_level=1, wanted_stars=5)
        == "[P1*****] PlayerOne"
    )


def test_build_display_name_wanted_with_gov():
    """Wanted tag shows even when gov is active."""
    assert (
        build_display_name("PlayerOne", gov_level=3, wanted_stars=5)
        == "[*****G3] PlayerOne"
    )


def test_build_display_name_wanted_with_police_and_gov():
    """Wanted tag shows even when both police and gov are active."""
    assert (
        build_display_name("PlayerOne", police_level=1, gov_level=3, wanted_stars=5)
        == "[P1*****G3] PlayerOne"
    )


def test_build_display_name_wanted_with_crim_police_gov():
    """Wanted shows alongside police+gov; crim suppressed."""
    assert (
        build_display_name(
            "PlayerOne", criminal_level=3, police_level=1, gov_level=3, wanted_stars=5
        )
        == "[P1*****G3] PlayerOne"
    )


def test_build_display_name_wanted_with_crim_mods():
    assert (
        build_display_name(
            "PlayerOne", criminal_level=1, has_custom_parts=True, wanted_stars=3
        )
        == "[M***C1] PlayerOne"
    )


# --- strip_all_tags ---


def test_strip_new_format():
    assert strip_all_tags("[M] PlayerOne") == "PlayerOne"
    assert strip_all_tags("[G3] PlayerOne") == "PlayerOne"
    assert strip_all_tags("[MG3] PlayerOne") == "PlayerOne"
    assert strip_all_tags("[C] PlayerOne") == "PlayerOne"
    assert strip_all_tags("[CM] PlayerOne") == "PlayerOne"
    assert strip_all_tags("[CMG23] PlayerOne") == "PlayerOne"
    assert strip_all_tags("[P] PlayerOne") == "PlayerOne"
    assert strip_all_tags("[MP] PlayerOne") == "PlayerOne"
    assert strip_all_tags("[MPG3] PlayerOne") == "PlayerOne"
    assert strip_all_tags("[PG3] PlayerOne") == "PlayerOne"
    assert strip_all_tags("[C1] PlayerOne") == "PlayerOne"
    assert strip_all_tags("[C5] PlayerOne") == "PlayerOne"
    assert strip_all_tags("[C12] PlayerOne") == "PlayerOne"
    assert strip_all_tags("[MC2] PlayerOne") == "PlayerOne"
    assert strip_all_tags("[MC1G3] PlayerOne") == "PlayerOne"
    assert strip_all_tags("[*****] PlayerOne") == "PlayerOne"
    assert strip_all_tags("[W5] PlayerOne") == "PlayerOne"  # legacy W-prefix
    assert strip_all_tags("[W12] PlayerOne") == "PlayerOne"  # legacy W-prefix
    assert strip_all_tags("[C3*****] PlayerOne") == "PlayerOne"
    assert strip_all_tags("[MC1***] PlayerOne") == "PlayerOne"
    assert strip_all_tags("[\u2605\u2605\u2605\u2605\u2605] PlayerOne") == "PlayerOne"
    assert strip_all_tags("[MC1\u2605\u2605\u2605\u2605] PlayerOne") == "PlayerOne"
    assert (
        strip_all_tags("[P1\u2605\u2605\u2605\u2605\u2605G3] PlayerOne") == "PlayerOne"
    )


def test_strip_legacy_format():
    assert strip_all_tags("[MODS] PlayerOne") == "PlayerOne"
    assert strip_all_tags("[GOV1] PlayerOne") == "PlayerOne"
    assert strip_all_tags("[DOT] PlayerOne") == "PlayerOne"
    assert strip_all_tags("[MODS] [GOV3] PlayerOne") == "PlayerOne"
    assert strip_all_tags("[CRIM] PlayerOne") == "PlayerOne"
    assert strip_all_tags("[CRIM] [MODS] [GOV3] PlayerOne") == "PlayerOne"


def test_strip_legacy_subscript_format():
    assert strip_all_tags("[G₃] PlayerOne") == "PlayerOne"
    assert strip_all_tags("[MG₃] PlayerOne") == "PlayerOne"
    assert strip_all_tags("[CMG₂₃] PlayerOne") == "PlayerOne"


def test_strip_all_tags_preserves_base_name():
    assert strip_all_tags("PlayerOne [123]") == "PlayerOne [123]"
    assert strip_all_tags("PlayerOne") == "PlayerOne"


# --- refresh_player_name integration tests ---


@pytest.mark.asyncio
@pytest.mark.django_db
@patch("amc.player_tags.set_character_name", new_callable=AsyncMock)
async def test_refresh_player_name_updates_custom_name(mock_set_name):
    from amc.factories import CharacterFactory, PlayerFactory
    from asgiref.sync import sync_to_async

    player = await sync_to_async(PlayerFactory)()
    character = await sync_to_async(CharacterFactory)(
        player=player,
        name="TestPlayer",
        guid="test-guid-1",
    )

    session = MagicMock()
    await refresh_player_name(character, session, has_custom_parts=True)

    await character.arefresh_from_db()
    assert character.custom_name == "[M] TestPlayer"
    from amc.player_tags import set_character_name

    set_character_name.assert_awaited_once_with(
        session, "test-guid-1", "[M] TestPlayer"
    )


@pytest.mark.asyncio
@pytest.mark.django_db
@patch("amc.player_tags.set_character_name", new_callable=AsyncMock)
async def test_refresh_player_name_preserves_mod_state_legacy(mock_set_name):
    """Preserves mod state from legacy [MODS] tag."""
    from amc.factories import CharacterFactory, PlayerFactory
    from asgiref.sync import sync_to_async

    player = await sync_to_async(PlayerFactory)()
    character = await sync_to_async(CharacterFactory)(
        player=player,
        name="TestPlayer",
        custom_name="[MODS] TestPlayer",
        guid="test-guid-2",
    )

    session = MagicMock()
    await refresh_player_name(character, session)

    await character.arefresh_from_db()
    assert character.custom_name == "[M] TestPlayer"


@pytest.mark.asyncio
@pytest.mark.django_db
@patch("amc.player_tags.set_character_name", new_callable=AsyncMock)
async def test_refresh_player_name_wanted(mock_set_name):
    """Wanted record → ***** (300 remaining, ceil(300/60) = 5, capped at 5)."""
    from amc.factories import CharacterFactory, PlayerFactory
    from amc.models import Wanted
    from asgiref.sync import sync_to_async

    player = await sync_to_async(PlayerFactory)()
    character = await sync_to_async(CharacterFactory)(
        player=player,
        name="TestPlayer",
        guid="test-guid-wanted-1",
    )

    await Wanted.objects.acreate(character=character, wanted_remaining=300)

    session = MagicMock()
    await refresh_player_name(character, session)

    await character.arefresh_from_db()
    assert character.custom_name == "[*****] TestPlayer"
    mock_set_name.assert_awaited_once_with(
        session, "test-guid-wanted-1", "[*****] TestPlayer"
    )


@pytest.mark.asyncio
@pytest.mark.django_db
@patch("amc.player_tags.set_character_name", new_callable=AsyncMock)
async def test_refresh_player_name_wanted_rounds_up(mock_set_name):
    """241 remaining → ceil(241/60) = 5 → W5."""
    from amc.factories import CharacterFactory, PlayerFactory
    from amc.models import Wanted
    from asgiref.sync import sync_to_async

    player = await sync_to_async(PlayerFactory)()
    character = await sync_to_async(CharacterFactory)(
        player=player,
        name="TestPlayer",
        guid="test-guid-wanted-2",
    )

    await Wanted.objects.acreate(character=character, wanted_remaining=241)

    session = MagicMock()
    await refresh_player_name(character, session)

    await character.arefresh_from_db()
    assert character.custom_name == "[*****] TestPlayer"


@pytest.mark.asyncio
@pytest.mark.django_db
@patch("amc.player_tags.set_character_name", new_callable=AsyncMock)
async def test_refresh_player_name_wanted_61s(mock_set_name):
    """61 remaining → ceil(61/60) = 2 → W2."""
    from amc.factories import CharacterFactory, PlayerFactory
    from amc.models import Wanted
    from asgiref.sync import sync_to_async

    player = await sync_to_async(PlayerFactory)()
    character = await sync_to_async(CharacterFactory)(
        player=player,
        name="TestPlayer",
        guid="test-guid-wanted-3",
    )

    await Wanted.objects.acreate(character=character, wanted_remaining=61)

    session = MagicMock()
    await refresh_player_name(character, session)

    await character.arefresh_from_db()
    assert character.custom_name == "[**] TestPlayer"


@pytest.mark.asyncio
@pytest.mark.django_db
@patch("amc.player_tags.set_character_name", new_callable=AsyncMock)
async def test_refresh_player_name_wanted_zero_protection(mock_set_name):
    """Wanted with wanted_remaining=0 → no tag."""
    from amc.factories import CharacterFactory, PlayerFactory
    from amc.models import Wanted
    from asgiref.sync import sync_to_async

    player = await sync_to_async(PlayerFactory)()
    character = await sync_to_async(CharacterFactory)(
        player=player,
        name="TestPlayer",
        guid="test-guid-wanted-4",
    )

    await Wanted.objects.acreate(character=character, wanted_remaining=0)

    session = MagicMock()
    await refresh_player_name(character, session)

    await character.arefresh_from_db()
    assert character.custom_name is None


@pytest.mark.asyncio
@pytest.mark.django_db
@patch("amc.player_tags.set_character_name", new_callable=AsyncMock)
async def test_refresh_player_name_wanted_with_crim(mock_set_name):
    """Wanted + criminal record → [W5C1]."""
    from amc.factories import CharacterFactory, PlayerFactory
    from amc.models import CriminalRecord, Wanted
    from asgiref.sync import sync_to_async
    from django.utils import timezone
    from datetime import timedelta

    player = await sync_to_async(PlayerFactory)()
    character = await sync_to_async(CharacterFactory)(
        player=player,
        name="TestPlayer",
        guid="test-guid-wanted-crim-1",
    )

    await CriminalRecord.objects.acreate(
        character=character,
        reason="Money delivery",
        expires_at=timezone.now() + timedelta(days=7),
    )
    await Wanted.objects.acreate(character=character, wanted_remaining=300)

    session = MagicMock()
    await refresh_player_name(character, session)

    await character.arefresh_from_db()
    assert character.custom_name == "[*****C1] TestPlayer"


@pytest.mark.asyncio
@pytest.mark.django_db
@patch("amc.player_tags.set_character_name", new_callable=AsyncMock)
async def test_refresh_player_name_wanted_with_police(mock_set_name):
    """Wanted + police → [P1W5] (wanted not suppressed)."""
    from amc.factories import CharacterFactory, PlayerFactory
    from amc.models import PoliceSession, Wanted
    from asgiref.sync import sync_to_async

    player = await sync_to_async(PlayerFactory)()
    character = await sync_to_async(CharacterFactory)(
        player=player,
        name="TestPlayer",
        guid="test-guid-wanted-police-1",
    )

    await PoliceSession.objects.acreate(character=character)
    await Wanted.objects.acreate(character=character, wanted_remaining=300)

    session = MagicMock()
    await refresh_player_name(character, session)

    await character.arefresh_from_db()
    assert character.custom_name == "[P1*****] TestPlayer"


@pytest.mark.asyncio
@pytest.mark.django_db
@patch("amc.player_tags.set_character_name", new_callable=AsyncMock)
async def test_refresh_player_name_police_session(mock_set_name):
    """Active police session → [P1] tag."""
    from amc.factories import CharacterFactory, PlayerFactory
    from amc.models import PoliceSession
    from asgiref.sync import sync_to_async

    player = await sync_to_async(PlayerFactory)()
    character = await sync_to_async(CharacterFactory)(
        player=player,
        name="TestPlayer",
        guid="test-guid-police-1",
    )

    await PoliceSession.objects.acreate(character=character)

    session = MagicMock()
    await refresh_player_name(character, session)

    await character.arefresh_from_db()
    assert character.custom_name == "[P1] TestPlayer"
    mock_set_name.assert_awaited_once_with(
        session, "test-guid-police-1", "[P1] TestPlayer"
    )


@pytest.mark.asyncio
@pytest.mark.django_db
@patch("amc.player_tags.set_character_name", new_callable=AsyncMock)
async def test_refresh_player_name_police_suppresses_crim(mock_set_name):
    """Police session + criminal record → [P1] (criminal suppressed)."""
    from amc.factories import CharacterFactory, PlayerFactory
    from amc.models import CriminalRecord, PoliceSession
    from asgiref.sync import sync_to_async
    from django.utils import timezone
    from datetime import timedelta

    player = await sync_to_async(PlayerFactory)()
    character = await sync_to_async(CharacterFactory)(
        player=player,
        name="TestPlayer",
        guid="test-guid-police-2",
    )

    await PoliceSession.objects.acreate(character=character)
    await CriminalRecord.objects.acreate(
        character=character,
        reason="Money delivery",
        expires_at=timezone.now() + timedelta(days=7),
    )

    session = MagicMock()
    await refresh_player_name(character, session)

    await character.arefresh_from_db()
    assert character.custom_name == "[P1] TestPlayer"
