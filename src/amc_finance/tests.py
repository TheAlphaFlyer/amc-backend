from datetime import timedelta
from decimal import Decimal
from django.test import TestCase
from django.utils import timezone
from django.contrib.gis.geos import Point
from asgiref.sync import sync_to_async
from amc.factories import CharacterFactory
from amc.models import CharacterLocation
from amc_finance.models import Account
from .services import (
    get_player_bank_balance,
    register_player_deposit,
    register_player_withdrawal,
    apply_interest_to_bank_accounts,
)


class BankAccountTestCase(TestCase):
    async def test_get_player_bank_balance(self):
        character = await sync_to_async(CharacterFactory)()
        balance = await get_player_bank_balance(character)
        self.assertEqual(balance, 0)

    async def test_register_player_deposit(self):
        character = await sync_to_async(CharacterFactory)()
        player = await sync_to_async(lambda: character.player)()
        await register_player_deposit(1000, character, player)
        balance = await get_player_bank_balance(character)
        self.assertEqual(balance, 1000)

    async def test_register_player_withdrawal(self):
        character = await sync_to_async(CharacterFactory)()
        player = await sync_to_async(lambda: character.player)()
        await register_player_deposit(1000, character, player)
        await register_player_withdrawal(100, character, player)
        balance = await get_player_bank_balance(character)
        self.assertEqual(balance, 900)

    async def test_register_player_withdrawal_more_than_balance(self):
        character = await sync_to_async(CharacterFactory)()
        player = await sync_to_async(lambda: character.player)()
        await register_player_deposit(100, character, player)
        with self.assertRaises(Exception):
            await register_player_withdrawal(1000, character, player)


class InterestTestCase(TestCase):
    async def test_offline_interest(self):
        character = await sync_to_async(CharacterFactory)()
        account = await Account.objects.acreate(
            account_type=Account.AccountType.LIABILITY,
            book=Account.Book.BANK,
            character=character,
            balance=100,
        )
        await apply_interest_to_bank_accounts({})
        await account.arefresh_from_db()
        self.assertGreater(account.balance, 100)

    async def test_onine_interest(self):
        character = await sync_to_async(CharacterFactory)()
        character.last_online = timezone.now() - timedelta(minutes=5)  # pyrefly: ignore
        await character.asave(update_fields=["last_online"])
        await CharacterLocation.objects.acreate(
            timestamp=timezone.now() - timedelta(minutes=5),
            character=character,
            location=Point(0, 0, 0),
        )
        account = await Account.objects.acreate(
            account_type=Account.AccountType.LIABILITY,
            book=Account.Book.BANK,
            character=character,
            balance=100,
        )
        await apply_interest_to_bank_accounts({})
        await account.arefresh_from_db()
        self.assertGreater(account.balance, 100)

    async def test_low_balance_full_interest(self):
        """Balances well below threshold should receive full interest."""
        character = await sync_to_async(CharacterFactory)()
        account = await Account.objects.acreate(
            account_type=Account.AccountType.LIABILITY,
            book=Account.Book.BANK,
            character=character,
            balance=1_000_000,
        )
        await apply_interest_to_bank_accounts({})
        await account.arefresh_from_db()
        interest = account.balance - 1_000_000
        self.assertGreater(interest, 0)

    async def test_threshold_balance_full_interest(self):
        """Balance at exactly the threshold should receive full interest."""
        character = await sync_to_async(CharacterFactory)()
        account = await Account.objects.acreate(
            account_type=Account.AccountType.LIABILITY,
            book=Account.Book.BANK,
            character=character,
            balance=10_000_000,
        )
        await apply_interest_to_bank_accounts({})
        await account.arefresh_from_db()
        interest = account.balance - 10_000_000
        # At threshold, multiplier = e^0 = 1.0, so full interest
        expected_full_interest = 10_000_000 * Decimal("0.022") / Decimal("192")
        self.assertAlmostEqual(float(interest), float(expected_full_interest), places=0)

    async def test_high_balance_reduced_interest(self):
        """Balances well above threshold should receive much less interest."""
        character = await sync_to_async(CharacterFactory)()
        account = await Account.objects.acreate(
            account_type=Account.AccountType.LIABILITY,
            book=Account.Book.BANK,
            character=character,
            balance=50_000_000,
        )
        await apply_interest_to_bank_accounts({})
        await account.arefresh_from_db()
        interest = account.balance - 50_000_000
        # Flat rate would give: 50M * 0.022/192 ≈ 5729
        flat_interest = 50_000_000 * Decimal("0.022") / Decimal("192")
        # With fall-off, interest should be significantly less than flat rate
        self.assertGreater(interest, 0)
        self.assertLess(interest, flat_interest * Decimal("0.5"))
