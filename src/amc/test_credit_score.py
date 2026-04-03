from decimal import Decimal
from datetime import timedelta
from django.test import TestCase
from django.utils import timezone
from unittest.mock import MagicMock, patch, AsyncMock
from asgiref.sync import sync_to_async
from amc_finance.loans import (
    calc_loan_fee,
    get_credit_score_label,
    evaluate_credit_scores,
    CREDIT_SCORE_MET,
    CREDIT_SCORE_EXCEEDED,
    CREDIT_SCORE_MISSED,
    CREDIT_SCORE_MIN,
    CREDIT_SCORE_MAX,
    CREDIT_UTILIZATION_HIGH_PENALTY,
    CREDIT_UTILIZATION_VERY_HIGH_PENALTY,
)


class CalcLoanFeeWithCreditScoreTest(TestCase):
    """Tests for calc_loan_fee credit score multiplier.

    Piecewise linear multiplier:
      Score 0→100:  multiplier 2.0→1.0
      Score 100→200: multiplier 1.0→0.5
    """

    def _fee(self, score, amount=100_000, max_loan=Decimal(1_000_000)):
        return calc_loan_fee(amount, MagicMock(), max_loan, credit_score=score)

    def test_neutral_score_unchanged(self):
        """Score 100 → 1.0× (no change)."""
        self.assertEqual(self._fee(100), 10_000)

    def test_excellent_score_halves_fee(self):
        """Score 200 → 0.5×."""
        self.assertEqual(self._fee(200), 5_000)

    def test_terrible_score_doubles_fee(self):
        """Score 0 → 2.0×."""
        self.assertEqual(self._fee(0), 20_000)

    def test_score_50(self):
        """Score 50 → 1.5×."""
        self.assertEqual(self._fee(50), 15_000)

    def test_score_150(self):
        """Score 150 → 0.75×."""
        self.assertEqual(self._fee(150), 7_500)

    def test_score_clamped_above_200(self):
        """Scores above 200 are clamped to 200."""
        self.assertEqual(self._fee(300), self._fee(200))

    def test_score_clamped_below_0(self):
        """Scores below 0 are clamped to 0."""
        self.assertEqual(self._fee(-50), self._fee(0))

    def test_default_credit_score_is_neutral(self):
        """Calling without credit_score defaults to 100 (neutral)."""
        default = calc_loan_fee(100_000, MagicMock(), Decimal(1_000_000))
        explicit = self._fee(100)
        self.assertEqual(default, explicit)


class GetCreditScoreLabelTest(TestCase):
    """Tests for credit score label generation."""

    def test_labels(self):
        self.assertEqual(get_credit_score_label(200), "Excellent")
        self.assertEqual(get_credit_score_label(171), "Excellent")
        self.assertEqual(get_credit_score_label(170), "Very Good")
        self.assertEqual(get_credit_score_label(131), "Very Good")
        self.assertEqual(get_credit_score_label(130), "Good")
        self.assertEqual(get_credit_score_label(101), "Good")
        self.assertEqual(get_credit_score_label(100), "Neutral")
        self.assertEqual(get_credit_score_label(99), "Fair")
        self.assertEqual(get_credit_score_label(71), "Fair")
        self.assertEqual(get_credit_score_label(70), "Poor")
        self.assertEqual(get_credit_score_label(41), "Poor")
        self.assertEqual(get_credit_score_label(40), "Very Poor")
        self.assertEqual(get_credit_score_label(0), "Very Poor")


class EvaluateCreditScoresTest(TestCase):
    """Tests for the evaluate_credit_scores cron function.

    All tests mock get_character_max_loan to control utilization.
    Default mock returns max_loan=1_000_000 so balance=600k → 60% utilization
    (below the 70% threshold, no utilization penalty).
    """

    _player_counter = 2000

    @classmethod
    def _next_player_id(cls):
        cls._player_counter += 1
        return cls._player_counter

    @sync_to_async
    def _make_character(self, credit_score=100):
        from amc.models import Character, Player
        player = Player.objects.create(unique_id=self._next_player_id())
        return Character.objects.create(
            player=player,
            name=f"TestPlayer_{player.unique_id}",
            credit_score=credit_score,
        )

    @sync_to_async
    def _make_loan_account(self, character, balance, last_evaluated=None):
        from amc_finance.models import Account
        account, _ = Account.objects.get_or_create(
            account_type=Account.AccountType.ASSET,
            book=Account.Book.BANK,
            character=character,
            defaults={
                "name": f"Loan:{character.name}",
                "balance": balance,
                "last_credit_score_evaluated_at": last_evaluated,
            },
        )
        if account.balance != balance or account.last_credit_score_evaluated_at != last_evaluated:
            account.balance = balance
            account.last_credit_score_evaluated_at = last_evaluated
            account.save()
        return account

    @sync_to_async
    def _create_repayment(self, account, amount, when=None):
        from amc_finance.models import JournalEntry, LedgerEntry
        now = when or timezone.now()
        je = JournalEntry.objects.create(
            date=now.date(),
            description="Test repayment",
        )
        LedgerEntry.objects.create(
            journal_entry=je,
            account=account,
            debit=Decimal(0),
            credit=Decimal(amount),
        )

    @sync_to_async
    def _refresh_score(self, character):
        character.refresh_from_db()
        return character.credit_score

    def _mock_max_loan(self, max_loan=1_000_000):
        """Patch get_character_max_loan to return a controlled value."""
        return patch(
            "amc_finance.loans.get_character_max_loan",
            new_callable=AsyncMock,
            return_value=(max_loan, None),
        )

    # Balance=600k, mock max_loan=1M → utilization=60% (below 70% threshold)
    # Required repayment = 10% of 600k = 60k.

    async def test_met_obligations(self):
        """Repaid >= required → score +10."""
        char = await self._make_character(credit_score=100)
        acct = await self._make_loan_account(char, balance=Decimal(600_000))
        await self._create_repayment(acct, 60_000)

        with self._mock_max_loan():
            await evaluate_credit_scores()

        self.assertEqual(await self._refresh_score(char), 100 + CREDIT_SCORE_MET)

    async def test_exceeded_obligations(self):
        """Repaid >= 200% of required → score +15."""
        char = await self._make_character(credit_score=100)
        acct = await self._make_loan_account(char, balance=Decimal(600_000))
        await self._create_repayment(acct, 130_000)

        with self._mock_max_loan():
            await evaluate_credit_scores()

        self.assertEqual(await self._refresh_score(char), 100 + CREDIT_SCORE_EXCEEDED)

    async def test_missed_obligations(self):
        """No repayments → score -30."""
        char = await self._make_character(credit_score=100)
        await self._make_loan_account(char, balance=Decimal(600_000))

        with self._mock_max_loan():
            await evaluate_credit_scores()

        self.assertEqual(await self._refresh_score(char), 100 + CREDIT_SCORE_MISSED)

    async def test_capped_at_max(self):
        """Score cannot exceed 200."""
        char = await self._make_character(credit_score=198)
        acct = await self._make_loan_account(char, balance=Decimal(600_000))
        await self._create_repayment(acct, 60_000)

        with self._mock_max_loan():
            await evaluate_credit_scores()

        self.assertEqual(await self._refresh_score(char), CREDIT_SCORE_MAX)

    async def test_floored_at_min(self):
        """Score cannot go below 0."""
        char = await self._make_character(credit_score=10)
        await self._make_loan_account(char, balance=Decimal(600_000))

        with self._mock_max_loan():
            await evaluate_credit_scores()

        self.assertEqual(await self._refresh_score(char), CREDIT_SCORE_MIN)

    async def test_no_change_without_qualifying_loan(self):
        """Score unchanged when loan balance is below threshold."""
        char = await self._make_character(credit_score=100)
        await self._make_loan_account(char, balance=Decimal(0))

        with self._mock_max_loan():
            await evaluate_credit_scores()

        self.assertEqual(await self._refresh_score(char), 100)

    async def test_skip_recently_evaluated(self):
        """Accounts evaluated within the period are skipped."""
        char = await self._make_character(credit_score=100)
        await self._make_loan_account(
            char,
            balance=Decimal(600_000),
            last_evaluated=timezone.now() - timedelta(days=3),
        )

        with self._mock_max_loan():
            await evaluate_credit_scores()

        self.assertEqual(await self._refresh_score(char), 100)

    async def test_reevaluate_after_period(self):
        """Accounts are re-evaluated once the period has elapsed."""
        char = await self._make_character(credit_score=100)
        acct = await self._make_loan_account(
            char,
            balance=Decimal(600_000),
            last_evaluated=timezone.now() - timedelta(days=8),
        )
        await self._create_repayment(acct, 60_000)

        with self._mock_max_loan():
            await evaluate_credit_scores()

        self.assertEqual(await self._refresh_score(char), 100 + CREDIT_SCORE_MET)

    # --- Utilization tests ---

    async def test_high_utilization_penalty(self):
        """Balance/max_loan > 70% → extra -5 penalty."""
        char = await self._make_character(credit_score=100)
        # Balance 800k, max_loan 1M → 80% utilization
        acct = await self._make_loan_account(char, balance=Decimal(800_000))
        await self._create_repayment(acct, 80_000)  # meet obligations

        with self._mock_max_loan(1_000_000):
            await evaluate_credit_scores()

        # +10 (met) + (-5 utilization) = +5
        self.assertEqual(await self._refresh_score(char), 100 + CREDIT_SCORE_MET + CREDIT_UTILIZATION_HIGH_PENALTY)

    async def test_very_high_utilization_penalty(self):
        """Balance/max_loan > 90% → extra -10 penalty."""
        char = await self._make_character(credit_score=100)
        # Balance 950k, max_loan 1M → 95% utilization
        acct = await self._make_loan_account(char, balance=Decimal(950_000))
        await self._create_repayment(acct, 95_000)  # meet obligations

        with self._mock_max_loan(1_000_000):
            await evaluate_credit_scores()

        # +10 (met) + (-10 utilization) = 0
        self.assertEqual(await self._refresh_score(char), 100 + CREDIT_SCORE_MET + CREDIT_UTILIZATION_VERY_HIGH_PENALTY)

    async def test_low_utilization_no_penalty(self):
        """Balance/max_loan <= 70% → no utilization penalty."""
        char = await self._make_character(credit_score=100)
        # Balance 600k, max_loan 1M → 60% utilization
        acct = await self._make_loan_account(char, balance=Decimal(600_000))
        await self._create_repayment(acct, 60_000)

        with self._mock_max_loan(1_000_000):
            await evaluate_credit_scores()

        # +10 (met), no utilization penalty
        self.assertEqual(await self._refresh_score(char), 100 + CREDIT_SCORE_MET)

    async def test_utilization_stacks_with_missed(self):
        """Missed obligations + very high utilization = severe hit."""
        char = await self._make_character(credit_score=100)
        # Balance 950k, max_loan 1M → 95% utilization, no repayments
        await self._make_loan_account(char, balance=Decimal(950_000))

        with self._mock_max_loan(1_000_000):
            await evaluate_credit_scores()

        # -30 (missed) + (-10 utilization) = -40
        self.assertEqual(await self._refresh_score(char), 100 + CREDIT_SCORE_MISSED + CREDIT_UTILIZATION_VERY_HIGH_PENALTY)
