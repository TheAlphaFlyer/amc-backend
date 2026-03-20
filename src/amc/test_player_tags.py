import pytest
from unittest.mock import patch, AsyncMock, MagicMock
from amc.player_tags import (
    strip_all_tags,
    build_display_name,
    refresh_player_name,
)

def test_build_display_name_no_tags():
    assert build_display_name("PlayerOne") == "PlayerOne"
    assert build_display_name("PlayerOne", has_custom_parts=False, gov_level=0) == "PlayerOne"

def test_build_display_name_mod_only():
    assert build_display_name("PlayerOne", has_custom_parts=True) == "[MODS] PlayerOne"

def test_build_display_name_gov_only():
    assert build_display_name("PlayerOne", gov_level=3) == "[GOV3] PlayerOne"

def test_build_display_name_mod_and_gov():
    assert build_display_name("PlayerOne", has_custom_parts=True, gov_level=3) == "[MODS] [GOV3] PlayerOne"

def test_strip_all_tags():
    assert strip_all_tags("[MODS] PlayerOne") == "PlayerOne"
    assert strip_all_tags("[GOV1] PlayerOne") == "PlayerOne"
    assert strip_all_tags("[DOT] PlayerOne") == "PlayerOne"
    assert strip_all_tags("[MODS] [GOV3] PlayerOne") == "PlayerOne"
    assert strip_all_tags("[MODS][GOV3] PlayerOne") == "PlayerOne"
    
def test_strip_all_tags_preserves_base_name():
    assert strip_all_tags("PlayerOne [123]") == "PlayerOne [123]"
    assert strip_all_tags("PlayerOne") == "PlayerOne"

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
    assert character.custom_name == "[MODS] TestPlayer"
    from amc.player_tags import set_character_name
    set_character_name.assert_awaited_once_with(session, "test-guid-1", "[MODS] TestPlayer")

@pytest.mark.asyncio
@pytest.mark.django_db
@patch("amc.player_tags.set_character_name", new_callable=AsyncMock)
async def test_refresh_player_name_preserves_mod_state(mock_set_name):
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
    # Explicitly not passing has_custom_parts (None)
    await refresh_player_name(character, session)
    
    await character.arefresh_from_db()
    assert character.custom_name == "[MODS] TestPlayer"
    
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
        custom_name="[MODS] TestPlayer",
        guid="test-guid-3",
    )
    
    session = MagicMock()
    # Explicitly passing False to remove the tag
    await refresh_player_name(character, session, has_custom_parts=False)
    
    await character.arefresh_from_db()
    assert character.custom_name is None
