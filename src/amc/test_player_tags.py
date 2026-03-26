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
    assert build_display_name("PlayerOne", has_custom_parts=False, gov_level=0) == "PlayerOne"

def test_build_display_name_mod_only():
    assert build_display_name("PlayerOne", has_custom_parts=True) == "[M] PlayerOne"

def test_build_display_name_gov_only():
    assert build_display_name("PlayerOne", gov_level=3) == "[G3] PlayerOne"

def test_build_display_name_gov_multi_digit():
    assert build_display_name("PlayerOne", gov_level=23) == "[G23] PlayerOne"

def test_build_display_name_mod_and_gov():
    assert build_display_name("PlayerOne", has_custom_parts=True, gov_level=3) == "[MG3] PlayerOne"

def test_build_display_name_crim_only():
    assert build_display_name("PlayerOne", has_criminal_record=True) == "[C] PlayerOne"

def test_build_display_name_crim_and_mods():
    assert build_display_name("PlayerOne", has_criminal_record=True, has_custom_parts=True) == "[CM] PlayerOne"

def test_build_display_name_crim_suppressed_by_gov():
    """Criminal tag is suppressed when gov level > 0."""
    assert build_display_name("PlayerOne", has_criminal_record=True, gov_level=3) == "[G3] PlayerOne"

def test_build_display_name_all_active_crim_suppressed():
    """All flags active: criminal suppressed by gov, so [MG₃]."""
    assert build_display_name("PlayerOne", has_criminal_record=True, has_custom_parts=True, gov_level=3) == "[MG3] PlayerOne"


# --- strip_all_tags ---

def test_strip_new_format():
    assert strip_all_tags("[M] PlayerOne") == "PlayerOne"
    assert strip_all_tags("[G3] PlayerOne") == "PlayerOne"
    assert strip_all_tags("[MG3] PlayerOne") == "PlayerOne"
    assert strip_all_tags("[C] PlayerOne") == "PlayerOne"
    assert strip_all_tags("[CM] PlayerOne") == "PlayerOne"
    assert strip_all_tags("[CMG23] PlayerOne") == "PlayerOne"

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
    set_character_name.assert_awaited_once_with(session, "test-guid-1", "[M] TestPlayer")


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
    # Should auto-heal to new format
    assert character.custom_name == "[M] TestPlayer"


@pytest.mark.asyncio
@pytest.mark.django_db
@patch("amc.player_tags.set_character_name", new_callable=AsyncMock)
async def test_refresh_player_name_preserves_mod_state_new(mock_set_name):
    """Preserves mod state from new [M] tag."""
    from amc.factories import CharacterFactory, PlayerFactory
    from asgiref.sync import sync_to_async

    player = await sync_to_async(PlayerFactory)()
    character = await sync_to_async(CharacterFactory)(
        player=player,
        name="TestPlayer",
        custom_name="[M] TestPlayer",
        guid="test-guid-2b",
    )

    session = MagicMock()
    await refresh_player_name(character, session)

    await character.arefresh_from_db()
    assert character.custom_name == "[M] TestPlayer"


@pytest.mark.asyncio
@pytest.mark.django_db
@patch("amc.player_tags.set_character_name", new_callable=AsyncMock)
async def test_refresh_player_name_removes_mod_state(mock_set_name):
    from amc.factories import CharacterFactory, PlayerFactory
    from asgiref.sync import sync_to_async

    player = await sync_to_async(PlayerFactory)()
    character = await sync_to_async(CharacterFactory)(
        player=player,
        name="TestPlayer",
        custom_name="[M] TestPlayer",
        guid="test-guid-3",
    )

    session = MagicMock()
    await refresh_player_name(character, session, has_custom_parts=False)

    await character.arefresh_from_db()
    assert character.custom_name is None


@pytest.mark.asyncio
@pytest.mark.django_db
@patch("amc.player_tags.set_character_name", new_callable=AsyncMock)
async def test_refresh_player_name_adds_crim_tag(mock_set_name):
    """Active criminal record → [C] tag."""
    from amc.factories import CharacterFactory, PlayerFactory
    from amc.models import CriminalRecord
    from asgiref.sync import sync_to_async
    from django.utils import timezone
    from datetime import timedelta

    player = await sync_to_async(PlayerFactory)()
    character = await sync_to_async(CharacterFactory)(
        player=player,
        name="TestPlayer",
        guid="test-guid-crim-1",
    )

    await CriminalRecord.objects.acreate(
        character=character,
        reason="Money delivery",
        expires_at=timezone.now() + timedelta(days=7),
    )

    session = MagicMock()
    await refresh_player_name(character, session)

    await character.arefresh_from_db()
    assert character.custom_name == "[C] TestPlayer"
    mock_set_name.assert_awaited_once_with(session, "test-guid-crim-1", "[C] TestPlayer")


@pytest.mark.asyncio
@pytest.mark.django_db
@patch("amc.player_tags.set_character_name", new_callable=AsyncMock)
async def test_refresh_player_name_crim_and_mods(mock_set_name):
    """Criminal + mods → [CM]."""
    from amc.factories import CharacterFactory, PlayerFactory
    from amc.models import CriminalRecord
    from asgiref.sync import sync_to_async
    from django.utils import timezone
    from datetime import timedelta

    player = await sync_to_async(PlayerFactory)()
    character = await sync_to_async(CharacterFactory)(
        player=player,
        name="TestPlayer",
        guid="test-guid-crim-2",
    )

    await CriminalRecord.objects.acreate(
        character=character,
        reason="Money delivery",
        expires_at=timezone.now() + timedelta(days=7),
    )

    session = MagicMock()
    await refresh_player_name(character, session, has_custom_parts=True)

    await character.arefresh_from_db()
    assert character.custom_name == "[CM] TestPlayer"


@pytest.mark.asyncio
@pytest.mark.django_db
@patch("amc.player_tags.set_character_name", new_callable=AsyncMock)
async def test_refresh_player_name_expired_crim_no_tag(mock_set_name):
    """Expired criminal record → no [C] tag."""
    from amc.factories import CharacterFactory, PlayerFactory
    from amc.models import CriminalRecord
    from asgiref.sync import sync_to_async
    from django.utils import timezone
    from datetime import timedelta

    player = await sync_to_async(PlayerFactory)()
    character = await sync_to_async(CharacterFactory)(
        player=player,
        name="TestPlayer",
        guid="test-guid-crim-3",
    )

    await CriminalRecord.objects.acreate(
        character=character,
        reason="Money delivery",
        expires_at=timezone.now() - timedelta(days=1),
    )

    session = MagicMock()
    await refresh_player_name(character, session)

    await character.arefresh_from_db()
    assert character.custom_name is None
