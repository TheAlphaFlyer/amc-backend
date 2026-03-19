from datetime import timedelta
from decimal import Decimal
from django.test import TestCase
from django.utils import timezone
from django.contrib.gis.geos import Point
from asgiref.sync import sync_to_async
from amc.factories import CharacterFactory
from amc.models import CharacterLocation
from amc_finance.models import Account, JournalEntry, LedgerEntry
from .services import (
    get_player_bank_balance,
    register_player_deposit,
    register_player_withdrawal,
    apply_interest_to_bank_accounts,
    get_non_performing_loans,
    register_player_take_loan,
    register_player_repay_loan,
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


class NPLTestCase(TestCase):
    async def _create_loan_account(self, character, balance=1_000_000):
        """Helper to create a loan account with a given balance."""
        return await Account.objects.acreate(
            account_type=Account.AccountType.ASSET,
            book=Account.Book.BANK,
            character=character,
            name=f"Loan #{character.id} - {character.name}",
            balance=balance,
        )

    async def _create_repayment(self, account, days_ago=0, amount=10_000):
        """Helper to create a repayment ledger entry at a given time."""
        je = await JournalEntry.objects.acreate(
            date=timezone.now().date(),
            description="Player Loan Repayment",
            creator=account.character,
        )
        # Manually set created_at to simulate a past repayment
        created_at = timezone.now() - timedelta(days=days_ago)
        await JournalEntry.objects.filter(pk=je.pk).aupdate(created_at=created_at)

        await LedgerEntry.objects.acreate(
            journal_entry=je,
            account=account,
            debit=0,
            credit=amount,
        )

    async def test_npl_with_no_repayment(self):
        """A loan account with no repayments should appear as NPL."""
        character = await sync_to_async(CharacterFactory)()
        await self._create_loan_account(character)

        npls = await sync_to_async(get_non_performing_loans)()
        npl_ids = [a.id for a in npls]
        loan_account = await Account.objects.aget(
            account_type=Account.AccountType.ASSET,
            book=Account.Book.BANK,
            character=character,
        )
        self.assertIn(loan_account.id, npl_ids)

    async def test_npl_with_sufficient_repayment(self):
        """A loan with repayment meeting the threshold should NOT be NPL."""
        character = await sync_to_async(CharacterFactory)()
        account = await self._create_loan_account(character)  # 1M loan
        # 10% of 1,000,000 = 100,000 required; repay 150,000 to be safe
        await self._create_repayment(account, days_ago=3, amount=150_000)

        npls = await sync_to_async(get_non_performing_loans)()
        npl_ids = [a.id for a in npls]
        self.assertNotIn(account.id, npl_ids)

    async def test_npl_with_insufficient_repayment(self):
        """A loan with a tiny repayment (below threshold) SHOULD still be NPL."""
        character = await sync_to_async(CharacterFactory)()
        account = await self._create_loan_account(character)  # 1M loan
        # 10% of 1,000,000 = 100,000 required; repay only 100
        await self._create_repayment(account, days_ago=3, amount=100)

        npls = await sync_to_async(get_non_performing_loans)()
        npl_ids = [a.id for a in npls]
        self.assertIn(account.id, npl_ids)

    async def test_repay_loan_resets_npl_warning(self):
        """Repaying a loan should reset npl_warning_sent_at."""
        character = await sync_to_async(CharacterFactory)()
        # Set up: give them a deposit so repayment has vault funds
        player = await sync_to_async(lambda: character.player)()
        await register_player_deposit(200_000, character, player)

        # Take a loan
        await register_player_take_loan(100_000, character)

        # Find the loan account and set the warning
        loan_account = await Account.objects.aget(
            account_type=Account.AccountType.ASSET,
            book=Account.Book.BANK,
            character=character,
        )
        loan_account.npl_warning_sent_at = timezone.now()
        await loan_account.asave(update_fields=["npl_warning_sent_at"])

        # Repay some of the loan
        await register_player_repay_loan(10_000, character)

        # Check that warning was reset
        await loan_account.arefresh_from_db()
        self.assertIsNone(loan_account.npl_warning_sent_at)

    async def test_vehicle_sold_repays_loan(self):
        """Selling a vehicle should auto-repay the loan from sale proceeds."""
        from unittest.mock import AsyncMock, patch
        from amc.tasks import on_vehicle_sold
        from amc_finance.services import get_player_loan_balance

        character = await sync_to_async(CharacterFactory)()
        player = await sync_to_async(lambda: character.player)()
        await register_player_deposit(200_000, character, player)
        await register_player_take_loan(100_000, character)

        loan_before = await get_player_loan_balance(character)
        self.assertGreater(loan_before, 0)

        # Mock transfer_money since it calls the game server
        with patch("amc.subsidies.transfer_money", new_callable=AsyncMock):
            await on_vehicle_sold(character, "Tuscan", None)

        loan_after = await get_player_loan_balance(character)
        self.assertLess(loan_after, loan_before)

    async def test_vehicle_sold_no_loan_no_repayment(self):
        """Selling a vehicle with no loan should do nothing."""
        from unittest.mock import AsyncMock, patch
        from amc.tasks import on_vehicle_sold

        character = await sync_to_async(CharacterFactory)()

        # No loan taken — should exit early without calling transfer_money
        with patch("amc.subsidies.transfer_money", new_callable=AsyncMock) as mock_transfer:
            await on_vehicle_sold(character, "Tuscan", None)
            mock_transfer.assert_not_called()
