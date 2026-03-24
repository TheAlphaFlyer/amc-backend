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
    apply_wealth_tax,
    get_non_performing_loans,
    get_character_npl_status,
    register_player_take_loan,
    register_player_repay_loan,
    make_treasury_bank_deposit,
    make_treasury_bank_withdrawal,
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

    async def test_threshold_balance_interest(self):
        """Balance at threshold with no last_online gets decayed interest."""
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
        # Character has no last_online → treated as 365d offline → log decay
        # Interest should be positive but less than full flat rate
        full_interest = 10_000_000 * Decimal("0.022") / Decimal("24")
        self.assertGreater(interest, 0)
        self.assertLess(interest, full_interest)

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
        # Flat rate would give: 50M * 0.022/24 ≈ 45833
        flat_interest = 50_000_000 * Decimal("0.022") / Decimal("24")
        # With fall-off, interest should be significantly less than flat rate
        self.assertGreater(interest, 0)
        self.assertLess(interest, flat_interest * Decimal("0.5"))

    async def test_smooth_log_decay(self):
        """Interest should decrease smoothly with offline time, not in steps."""
        # 3 days offline → should get less than full rate
        character_3d = await sync_to_async(CharacterFactory)()
        character_3d.last_online = timezone.now() - timedelta(days=3)  # pyrefly: ignore
        await character_3d.asave(update_fields=["last_online"])
        account_3d = await Account.objects.acreate(
            account_type=Account.AccountType.LIABILITY,
            book=Account.Book.BANK,
            character=character_3d,
            balance=1_000_000,
        )

        # 30 days offline → should get even less
        character_30d = await sync_to_async(CharacterFactory)()
        character_30d.last_online = timezone.now() - timedelta(days=30)  # pyrefly: ignore
        await character_30d.asave(update_fields=["last_online"])
        account_30d = await Account.objects.acreate(
            account_type=Account.AccountType.LIABILITY,
            book=Account.Book.BANK,
            character=character_30d,
            balance=1_000_000,
        )

        await apply_interest_to_bank_accounts({})
        await account_3d.arefresh_from_db()
        await account_30d.arefresh_from_db()

        interest_3d = account_3d.balance - 1_000_000
        interest_30d = account_30d.balance - 1_000_000

        self.assertGreater(interest_3d, 0)
        self.assertGreater(interest_30d, 0)
        # 30d offline should earn less interest than 3d offline
        self.assertGreater(interest_3d, interest_30d)


class WealthTaxTestCase(TestCase):
    async def test_exempt_bracket(self):
        """Balances at or below 500K should never be taxed."""
        from .services import calculate_wealth_tax
        self.assertEqual(calculate_wealth_tax(500_000, 1000), 0)
        self.assertEqual(calculate_wealth_tax(400_000, 5000), 0)
        self.assertEqual(calculate_wealth_tax(0, 10000), 0)

    async def test_no_tax_under_one_hour(self):
        """No tax if offline less than 1 hour."""
        from .services import calculate_wealth_tax
        self.assertEqual(calculate_wealth_tax(10_000_000, 0.5), 0)
        self.assertEqual(calculate_wealth_tax(10_000_000, 0), 0)

    async def test_low_bracket_only(self):
        """Balance in 500K-2.5M range should only use Low bracket (k=0.15)."""
        from .services import calculate_wealth_tax
        # 1M balance, 60 days offline
        tax = calculate_wealth_tax(1_000_000, 60 * 24)
        self.assertGreater(tax, 0)
        # Tax should be modest for Low bracket
        self.assertLess(tax, 1_000)  # well under 0.1% per hour

    async def test_progressive_all_brackets(self):
        """Balance of 15M should use all three brackets marginally."""
        from .services import calculate_wealth_tax
        hours = 90 * 24
        tax_15m = calculate_wealth_tax(15_000_000, hours)
        tax_5m = calculate_wealth_tax(5_000_000, hours)
        tax_1m = calculate_wealth_tax(1_000_000, hours)

        # Higher balance = more tax (progressively)
        self.assertGreater(tax_15m, tax_5m)
        self.assertGreater(tax_5m, tax_1m)
        self.assertGreater(tax_1m, 0)

    async def test_tax_increases_with_time(self):
        """Tax rate should increase with offline duration (then plateau)."""
        from .services import calculate_wealth_tax
        tax_7d = calculate_wealth_tax(10_000_000, 7 * 24)
        tax_30d = calculate_wealth_tax(10_000_000, 30 * 24)
        tax_90d = calculate_wealth_tax(10_000_000, 90 * 24)

        self.assertGreater(tax_30d, tax_7d)
        self.assertGreater(tax_90d, tax_30d)

    async def test_tax_rate_eventually_decreases(self):
        """Hourly rate should peak then decrease (ln²-decay property)."""
        from .services import wealth_tax_hourly_rate
        rate_30d = wealth_tax_hourly_rate(0.25, 30 * 24)
        rate_5yr = wealth_tax_hourly_rate(0.25, 5 * 365 * 24)

        # Rate should eventually decrease at very long offline times
        self.assertGreater(rate_30d, rate_5yr)

    async def test_apply_wealth_tax_creates_entries(self):
        """apply_wealth_tax should create journal entries for eligible accounts."""
        character = await sync_to_async(CharacterFactory)()
        character.last_online = timezone.now() - timedelta(days=60)  # pyrefly: ignore
        await character.asave(update_fields=["last_online"])

        account = await Account.objects.acreate(
            account_type=Account.AccountType.LIABILITY,
            book=Account.Book.BANK,
            character=character,
            balance=5_000_000,
        )

        await apply_wealth_tax({})
        await account.arefresh_from_db()

        # Balance should have decreased
        self.assertLess(account.balance, 5_000_000)

        # Wealth Tax Revenue account should exist and have a positive balance
        revenue = await Account.objects.aget(
            account_type=Account.AccountType.REVENUE,
            book=Account.Book.GOVERNMENT,
            name="Wealth Tax Revenue",
        )
        self.assertGreater(revenue.balance, 0)

        # Journal entry should exist
        je = await JournalEntry.objects.filter(description="Wealth Tax").afirst()
        self.assertIsNotNone(je)

    async def test_apply_wealth_tax_exempt_not_taxed(self):
        """Characters with balance at or below exempt threshold are not taxed."""
        character = await sync_to_async(CharacterFactory)()
        character.last_online = timezone.now() - timedelta(days=90)  # pyrefly: ignore
        await character.asave(update_fields=["last_online"])

        account = await Account.objects.acreate(
            account_type=Account.AccountType.LIABILITY,
            book=Account.Book.BANK,
            character=character,
            balance=500_000,
        )

        await apply_wealth_tax({})
        await account.arefresh_from_db()
        self.assertEqual(account.balance, 500_000)

    async def test_apply_wealth_tax_online_player_still_taxed(self):
        """Tax applies based on hours offline — even recently online players
        with high balances can be taxed if they have been offline > 1 hour."""
        character = await sync_to_async(CharacterFactory)()
        character.last_online = timezone.now() - timedelta(hours=2)  # pyrefly: ignore
        await character.asave(update_fields=["last_online"])

        account = await Account.objects.acreate(
            account_type=Account.AccountType.LIABILITY,
            book=Account.Book.BANK,
            character=character,
            balance=10_000_000,
        )

        await apply_wealth_tax({})
        await account.arefresh_from_db()
        # With only 2 hours offline, tax should be very small but nonzero
        self.assertLess(account.balance, 10_000_000)

    async def test_tax_never_drops_below_exempt(self):
        """Tax should not reduce balance below the exempt threshold."""
        character = await sync_to_async(CharacterFactory)()
        character.last_online = timezone.now() - timedelta(days=365)  # pyrefly: ignore
        await character.asave(update_fields=["last_online"])

        # Balance just above exempt — tax should be tiny
        account = await Account.objects.acreate(
            account_type=Account.AccountType.LIABILITY,
            book=Account.Book.BANK,
            character=character,
            balance=510_000,
        )

        await apply_wealth_tax({})
        await account.arefresh_from_db()
        self.assertGreaterEqual(account.balance, 500_000)


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

    async def test_repay_loan_does_not_reset_npl_warning(self):
        """Repaying a loan should NOT reset npl_warning_sent_at."""
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

        # Check that warning was NOT reset
        await loan_account.arefresh_from_db()
        self.assertIsNotNone(loan_account.npl_warning_sent_at)

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

    async def test_get_character_npl_status_no_loan(self):
        """No loan account should return None."""
        character = await sync_to_async(CharacterFactory)()
        result = await get_character_npl_status(character)
        self.assertIsNone(result)

    async def test_get_character_npl_status_below_threshold(self):
        """Loan below NPL_MIN_BALANCE should return None."""
        character = await sync_to_async(CharacterFactory)()
        await self._create_loan_account(character, balance=100_000)
        result = await get_character_npl_status(character)
        self.assertIsNone(result)

    async def test_get_character_npl_status_npl(self):
        """Loan with no repayments should return is_npl=True."""
        character = await sync_to_async(CharacterFactory)()
        await self._create_loan_account(character, balance=1_000_000)
        result = await get_character_npl_status(character)
        self.assertIsNotNone(result)
        self.assertTrue(result["is_npl"])
        self.assertEqual(result["period_days"], 7)
        self.assertEqual(result["min_required_repayment"], 100_000)
        self.assertEqual(result["total_repaid_in_period"], 0)

    async def test_get_character_npl_status_not_npl(self):
        """Loan with sufficient repayments should return is_npl=False."""
        character = await sync_to_async(CharacterFactory)()
        account = await self._create_loan_account(character, balance=1_000_000)
        await self._create_repayment(account, days_ago=3, amount=150_000)
        result = await get_character_npl_status(character)
        self.assertIsNotNone(result)
        self.assertFalse(result["is_npl"])


class TreasuryBankTransferTestCase(TestCase):
    async def test_treasury_bank_withdrawal(self):
        """Deposit into bank, then withdraw — all 4 accounts should return to original."""
        await make_treasury_bank_deposit(500_000, "Test deposit")

        treasury_fund = await Account.objects.aget(
            book=Account.Book.GOVERNMENT, name="Treasury Fund"
        )
        treasury_in_bank = await Account.objects.aget(
            book=Account.Book.GOVERNMENT, name="Treasury Fund (in Bank)"
        )
        bank_vault = await Account.objects.aget(
            book=Account.Book.BANK, character=None, account_type=Account.AccountType.ASSET
        )
        bank_equity = await Account.objects.aget(
            book=Account.Book.BANK, account_type=Account.AccountType.EQUITY
        )

        self.assertEqual(treasury_in_bank.balance, 500_000)
        self.assertEqual(bank_equity.balance, 500_000)

        await make_treasury_bank_withdrawal(500_000, "Test withdrawal")

        await treasury_fund.arefresh_from_db()
        await treasury_in_bank.arefresh_from_db()
        await bank_vault.arefresh_from_db()
        await bank_equity.arefresh_from_db()

        self.assertEqual(treasury_in_bank.balance, 0)
        self.assertEqual(bank_equity.balance, 0)

    async def test_treasury_bank_withdrawal_exceeds_balance(self):
        """Withdrawing more than deposited should raise ValueError."""
        await make_treasury_bank_deposit(100_000, "Small deposit")
        with self.assertRaises(ValueError):
            await make_treasury_bank_withdrawal(200_000, "Over-withdrawal")
