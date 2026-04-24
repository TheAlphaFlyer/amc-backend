"""Internal auction API — balance verification and escrow endpoints.

Only accessible within the Tailscale network (asean-mt-server:9000).
No additional auth required — network isolation is the security boundary.
"""

from decimal import Decimal

from asgiref.sync import sync_to_async
from django.utils import timezone
from django.http import HttpRequest
from ninja import Router, Schema

from amc.models import Player, Character
from amc_finance.loans import get_player_bank_balance
from amc_finance.models import Account
from amc_finance.services import create_journal_entry

router = Router()


class BalanceResponse(Schema):
    player_id: int
    discord_user_id: int
    character_id: int
    character_name: str
    balance: int


class CharacterEntry(Schema):
    character_id: int
    character_name: str
    balance: int


class CharactersResponse(Schema):
    player_id: int
    discord_user_id: int
    characters: list[CharacterEntry]


class BalanceErrorResponse(Schema):
    error: str


class EscrowRequest(Schema):
    player_discord_id: str
    amount: int
    character_id: int | None = None


class RefundRequest(Schema):
    player_discord_id: str
    amount: int
    character_id: int | None = None


class SettleRequest(Schema):
    winner_discord_id: str
    seller_discord_id: str
    amount: int
    seller_type: str = "player"
    winner_character_id: int | None = None
    seller_character_id: int | None = None


class SettleResponse(Schema):
    winner_id: int | None
    seller_id: int | None
    amount: int
    seller_type: str
    winner_character_id: int | None = None
    seller_character_id: int | None = None


class EscrowResponse(Schema):
    player_id: int
    discord_user_id: int
    character_id: int
    character_name: str
    balance: int


async def _resolve_player_character(discord_id: str, character_id: int | None = None):
    try:
        player = await Player.objects.aget(
            discord_user_id=int(discord_id)
        )
    except (Player.DoesNotExist, ValueError):
        return None, None

    if character_id is not None:
        try:
            character = await player.characters.aget(pk=character_id)
            return player, character
        except Character.DoesNotExist:
            return player, None

    try:
        character = await player.get_latest_character()
    except Exception:
        try:
            character = await player.characters.alatest("pk")
        except Exception:
            return player, None
    return player, character


async def _get_or_create_auction_escrow() -> Account:
    escrow, _ = await Account.objects.aget_or_create(
        account_type=Account.AccountType.LIABILITY,
        book=Account.Book.BANK,
        character=None,
        name="Auction Escrow",
    )
    return escrow


async def _get_or_create_checking(character) -> Account:
    account, _ = await Account.objects.aget_or_create(
        account_type=Account.AccountType.LIABILITY,
        book=Account.Book.BANK,
        character=character,
        defaults={"name": "Checking Account"},
    )
    return account


async def _get_or_create_auction_revenue() -> Account:
    revenue, _ = await Account.objects.aget_or_create(
        account_type=Account.AccountType.REVENUE,
        book=Account.Book.GOVERNMENT,
        character=None,
        name="Auction Revenue",
    )
    return revenue


@router.get(
    "/characters/",
    response={200: CharactersResponse, 404: BalanceErrorResponse},
)
async def list_characters(request: HttpRequest, player_id: str):
    try:
        player = await Player.objects.aget(discord_user_id=int(player_id))
    except (Player.DoesNotExist, ValueError):
        return 404, {"error": "Player not found"}

    characters = []
    async for character in player.characters.all():
        balance = await get_player_bank_balance(character)
        characters.append({
            "character_id": character.pk,
            "character_name": character.name,
            "balance": int(balance),
        })

    return 200, {
        "player_id": player.unique_id,
        "discord_user_id": player.discord_user_id,
        "characters": characters,
    }


@router.get(
    "/balance/",
    response={200: BalanceResponse, 404: BalanceErrorResponse},
)
async def get_balance(request: HttpRequest, player_id: str, character_id: int | None = None):
    player, character = await _resolve_player_character(player_id, character_id)
    if not player:
        return 404, {"error": "Player not found"}
    if not character:
        return 404, {"error": "No character found for this player"}

    balance = await get_player_bank_balance(character)

    return 200, {
        "player_id": player.unique_id,
        "discord_user_id": player.discord_user_id,
        "character_id": character.pk,
        "character_name": character.name,
        "balance": int(balance),
    }


@router.post(
    "/escrow/",
    response={200: EscrowResponse, 404: BalanceErrorResponse, 409: BalanceErrorResponse},
)
async def escrow_funds(request: HttpRequest, payload: EscrowRequest):
    if payload.amount <= 0:
        return 409, {"error": "Amount must be positive"}

    player, character = await _resolve_player_character(
        payload.player_discord_id, payload.character_id
    )
    if not player:
        return 404, {"error": "Player not found"}
    if not character:
        return 404, {"error": "No character found for this player"}

    checking = await _get_or_create_checking(character)
    amount = Decimal(str(payload.amount))

    # Refresh to ensure balance is current before checking sufficiency.
    # aget_or_create may return a cached object if the account already existed.
    await checking.arefresh_from_db()
    if checking.balance < amount:
        return 409, {"error": f"Insufficient funds. Balance: {checking.balance}"}

    escrow = await _get_or_create_auction_escrow()

    await sync_to_async(create_journal_entry, thread_sensitive=True)(
        timezone.now(),
        f"Auction Escrow - {character.name}",
        character,
        [
            {"account": escrow, "debit": 0, "credit": amount},
            {"account": checking, "debit": amount, "credit": 0},
        ],
    )

    await checking.arefresh_from_db()
    return 200, {
        "player_id": player.unique_id,
        "discord_user_id": player.discord_user_id,
        "character_id": character.pk,
        "character_name": character.name,
        "balance": int(checking.balance),
    }


@router.post(
    "/refund/",
    response={200: EscrowResponse, 404: BalanceErrorResponse, 409: BalanceErrorResponse},
)
async def refund_funds(request: HttpRequest, payload: RefundRequest):
    if payload.amount <= 0:
        return 409, {"error": "Amount must be positive"}

    player, character = await _resolve_player_character(
        payload.player_discord_id, payload.character_id
    )
    if not player:
        return 404, {"error": "Player not found"}
    if not character:
        return 404, {"error": "No character found for this player"}

    checking = await _get_or_create_checking(character)
    escrow = await _get_or_create_auction_escrow()
    amount = Decimal(str(payload.amount))

    if escrow.balance < amount:
        return 409, {"error": f"Escrow insufficient. Escrow balance: {escrow.balance}"}

    await sync_to_async(create_journal_entry, thread_sensitive=True)(
        timezone.now(),
        f"Auction Refund - {character.name}",
        character,
        [
            {"account": checking, "debit": 0, "credit": amount},
            {"account": escrow, "debit": amount, "credit": 0},
        ],
    )

    await checking.arefresh_from_db()
    return 200, {
        "player_id": player.unique_id,
        "discord_user_id": player.discord_user_id,
        "character_id": character.pk,
        "character_name": character.name,
        "balance": int(checking.balance),
    }


@router.post(
    "/settle/",
    response={200: SettleResponse, 404: BalanceErrorResponse, 409: BalanceErrorResponse},
)
async def settle_funds(request: HttpRequest, payload: SettleRequest):
    if payload.amount <= 0:
        return 409, {"error": "Amount must be positive"}

    winner_player, winner_character = await _resolve_player_character(
        payload.winner_discord_id, payload.winner_character_id
    )
    if not winner_player or not winner_character:
        return 404, {"error": "Winner not found"}

    escrow = await _get_or_create_auction_escrow()
    amount = Decimal(str(payload.amount))

    if escrow.balance < amount:
        return 409, {"error": f"Escrow insufficient. Escrow balance: {escrow.balance}"}

    if payload.seller_type == "treasury":
        revenue = await _get_or_create_auction_revenue()

        await sync_to_async(create_journal_entry, thread_sensitive=True)(
            timezone.now(),
            f"Auction Settlement - {winner_character.name} to Treasury",
            None,
            [
                {"account": escrow, "debit": amount, "credit": 0},
                {"account": revenue, "debit": 0, "credit": amount},
            ],
        )

        return 200, {
            "winner_id": winner_player.unique_id,
            "seller_id": None,
            "amount": payload.amount,
            "seller_type": "treasury",
            "winner_character_id": winner_character.pk,
            "seller_character_id": None,
        }
    else:
        seller_player, seller_character = await _resolve_player_character(
            payload.seller_discord_id, payload.seller_character_id
        )
        if not seller_player or not seller_character:
            return 404, {"error": "Seller not found"}

        seller_checking = await _get_or_create_checking(seller_character)

        await sync_to_async(create_journal_entry, thread_sensitive=True)(
            timezone.now(),
            f"Auction Settlement - {winner_character.name} to {seller_character.name}",
            None,
            [
                {"account": seller_checking, "debit": 0, "credit": amount},
                {"account": escrow, "debit": amount, "credit": 0},
            ],
        )

        return 200, {
            "winner_id": winner_player.unique_id,
            "seller_id": seller_player.unique_id,
            "amount": payload.amount,
            "seller_type": "player",
            "winner_character_id": winner_character.pk,
            "seller_character_id": seller_character.pk,
        }
