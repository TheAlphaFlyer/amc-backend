from decimal import Decimal
from datetime import timedelta
from unittest.mock import AsyncMock, patch

from django.test import TestCase
from django.utils import timezone

from amc.models import Character, Player
from amc.ubi import handout_ubi, ACTIVE_GRANT_AMOUNT, MAX_LEVEL


def _make_ctx(http_client=None, http_client_mod=None):
    ctx = {}
    ctx["http_client"] = http_client or AsyncMock()
    ctx["http_client_mod"] = http_client_mod or AsyncMock()
    return ctx


class HandoutUbiLoanRepaymentTest(TestCase):
    """Test that UBI auto-repays loans when a character has an outstanding balance."""

    def setUp(self):
        self.player = Player.objects.create(unique_id=9999)
        self.character = Character.objects.create(
            player=self.player,
            name="TestDriver",
            guid="aabbccdd11223344aabbccdd11223344",
            driver_level=100,
        )

    def _mock_players(self):
        """Return a mock players list matching the test character."""
        return [
            (
                str(self.player.unique_id),
                {"character_guid": self.character.guid},
            )
        ]

    def _expected_amount(self):
        """Calculate the expected UBI amount for our test character."""
        grant = ACTIVE_GRANT_AMOUNT
        amount = min(
            Decimal(str(grant)),
            self.character.driver_level
            * Decimal(str(grant))
            * Decimal(str(self.character.ubi_multiplier))
            / MAX_LEVEL,
        )
        return amount

    @patch("amc.ubi.transfer_money", new_callable=AsyncMock)
    @patch("amc.ubi.send_fund_to_player_wallet", new_callable=AsyncMock)
    @patch("amc.ubi.get_player_loan_balance", new_callable=AsyncMock)
    @patch("amc.ubi.get_players", new_callable=AsyncMock)
    async def test_no_loan_no_repayment(
        self, mock_get_players, mock_loan_balance, mock_send, mock_transfer
    ):
        """When no loan exists, UBI is paid normally with no repayment."""
        mock_get_players.return_value = self._mock_players()
        mock_loan_balance.return_value = Decimal(0)

        ctx = _make_ctx(http_client_mod=mock_transfer)
        with patch(
            "amc.ubi.CharacterLocation.batch_get_character_activity",
            new_callable=AsyncMock,
            return_value={self.character.id: (True, True)},
        ):
            await handout_ubi(ctx)

        # UBI sent to wallet
        mock_send.assert_called_once()
        # transfer_money called once (UBI in, no repayment)
        mock_transfer.assert_called_once()
        assert mock_transfer.call_args[0][1] > 0  # positive amount

    @patch("amc.ubi.repay_loan_for_profit", new_callable=AsyncMock)
    @patch("amc.ubi.transfer_money", new_callable=AsyncMock)
    @patch("amc.ubi.send_fund_to_player_wallet", new_callable=AsyncMock)
    @patch("amc.ubi.get_player_loan_balance", new_callable=AsyncMock)
    @patch("amc.ubi.get_players", new_callable=AsyncMock)
    async def test_loan_larger_than_ubi(
        self, mock_get_players, mock_loan_balance, mock_send, mock_transfer,
        mock_repay,
    ):
        """When loan > UBI, repay_loan_for_profit is called with full UBI as override."""
        mock_get_players.return_value = self._mock_players()
        mock_loan_balance.return_value = Decimal(1_000_000)  # large loan

        ctx = _make_ctx(http_client_mod=mock_transfer)
        with patch(
            "amc.ubi.CharacterLocation.batch_get_character_activity",
            new_callable=AsyncMock,
            return_value={self.character.id: (True, True)},
        ):
            await handout_ubi(ctx)

        expected = self._expected_amount()

        # repay_loan_for_profit called with repayment_override=min(amount, loan)
        mock_repay.assert_called_once()
        call_kwargs = mock_repay.call_args
        self.assertEqual(call_kwargs[1]["repayment_override"], expected)
        self.assertIsNotNone(call_kwargs[1]["game_session"])

    @patch("amc.ubi.repay_loan_for_profit", new_callable=AsyncMock)
    @patch("amc.ubi.transfer_money", new_callable=AsyncMock)
    @patch("amc.ubi.send_fund_to_player_wallet", new_callable=AsyncMock)
    @patch("amc.ubi.get_player_loan_balance", new_callable=AsyncMock)
    @patch("amc.ubi.get_players", new_callable=AsyncMock)
    async def test_loan_smaller_than_ubi(
        self, mock_get_players, mock_loan_balance, mock_send, mock_transfer,
        mock_repay,
    ):
        """When loan < UBI, repayment_override is capped to loan balance."""
        small_loan = Decimal(100)  # smaller than any UBI
        mock_get_players.return_value = self._mock_players()
        mock_loan_balance.return_value = small_loan

        ctx = _make_ctx(http_client_mod=mock_transfer)
        with patch(
            "amc.ubi.CharacterLocation.batch_get_character_activity",
            new_callable=AsyncMock,
            return_value={self.character.id: (True, True)},
        ):
            await handout_ubi(ctx)

        # repayment_override = min(amount, loan_balance) = small_loan
        mock_repay.assert_called_once()
        call_kwargs = mock_repay.call_args
        self.assertEqual(call_kwargs[1]["repayment_override"], small_loan)

    @patch("amc.ubi.repay_loan_for_profit", new_callable=AsyncMock)
    @patch("amc.ubi.transfer_money", new_callable=AsyncMock)
    @patch("amc.ubi.send_fund_to_player_wallet", new_callable=AsyncMock)
    @patch("amc.ubi.get_player_loan_balance", new_callable=AsyncMock)
    @patch("amc.ubi.get_players", new_callable=AsyncMock)
    async def test_gov_employee_with_loan(
        self, mock_get_players, mock_loan_balance, mock_send, mock_transfer,
        mock_repay,
    ):
        """Gov employee gets 2x UBI, full amount goes to repayment."""
        self.character.gov_employee_until = timezone.now() + timedelta(hours=12)
        await self.character.asave()

        mock_get_players.return_value = self._mock_players()
        mock_loan_balance.return_value = Decimal(1_000_000)

        ctx = _make_ctx(http_client_mod=mock_transfer)
        with patch(
            "amc.ubi.CharacterLocation.batch_get_character_activity",
            new_callable=AsyncMock,
            return_value={self.character.id: (True, True)},
        ):
            await handout_ubi(ctx)

        # Gov employee gets 2x UBI, verify repayment_override uses doubled amount
        expected_base = self._expected_amount()
        expected_gov = expected_base * 2

        mock_repay.assert_called_once()
        call_kwargs = mock_repay.call_args
        self.assertEqual(call_kwargs[1]["repayment_override"], expected_gov)
