import asyncio
from django.test import TestCase
from django.contrib.gis.geos import Point, Polygon
from decimal import Decimal
from unittest.mock import patch, MagicMock, AsyncMock
from amc.models import SubsidyRule, SubsidyArea, Cargo, DeliveryPoint
from amc.subsidies import get_subsidy_for_cargo, get_subsidies_text


class SubsidyLogicTest(TestCase):
    def setUp(self):
        # Create Cargos
        self.cargo_coal, _ = Cargo.objects.get_or_create(
            key="Coal", defaults={"label": "Coal"}
        )
        self.cargo_burger, _ = Cargo.objects.get_or_create(
            key="Burger_01_Signature", defaults={"label": "Burger"}
        )

        # Create Areas
        # Polygon around (0,0) to (10,10)
        self.area_gwangjin = SubsidyArea.objects.create(
            name="Gwangjin Area",
            polygon=Polygon(((0, 0), (0, 10), (10, 10), (10, 0), (0, 0)), srid=3857),
        )

        # Create Points (srid=3857)
        self.point_in = DeliveryPoint.objects.create(
            guid="p_in", name="In Point", type="T", coord=Point(5, 5, 0, srid=3857)
        )
        self.point_out = DeliveryPoint.objects.create(
            guid="p_out", name="Out Point", type="T", coord=Point(20, 20, 0, srid=3857)
        )

    async def test_basic_cargo_rule(self):
        # Rule: Coal gets 150% (1.5)
        rule = await SubsidyRule.objects.acreate(
            name="Coal Subsidy",
            reward_type=SubsidyRule.RewardType.PERCENTAGE,
            reward_value=Decimal("1.50"),
            priority=10,
            allocation=Decimal("1000000"),
        )
        await rule.cargos.aadd(self.cargo_coal)

        # Test Coal
        mock_cargo = MagicMock()
        mock_cargo.cargo_key = "Coal"
        mock_cargo.payment = 1000
        mock_cargo.sender_point = self.point_in
        mock_cargo.destination_point = self.point_out
        mock_cargo.data = {}
        mock_cargo.damage = 0.0

        amount, factor, rule = await get_subsidy_for_cargo(mock_cargo)
        self.assertEqual(factor, 1.5)
        self.assertEqual(amount, 1500)

        # Test Burger (should not match)
        mock_cargo.cargo_key = "Burger_01_Signature"
        amount, factor, rule = await get_subsidy_for_cargo(mock_cargo)
        self.assertEqual(amount, 0)

    async def test_source_area_restriction(self):
        # Rule: Any cargo from Gwangjin gets 2.0
        rule = await SubsidyRule.objects.acreate(
            name="Gwangjin Export",
            reward_type=SubsidyRule.RewardType.PERCENTAGE,
            reward_value=Decimal("2.00"),
            priority=10,
            allocation=Decimal("1000000"),
        )
        await rule.source_areas.aadd(self.area_gwangjin)

        # Test from IN point
        mock_cargo = MagicMock()
        mock_cargo.cargo_key = "Coal"
        mock_cargo.payment = 1000
        mock_cargo.sender_point = self.point_in
        mock_cargo.destination_point = self.point_out
        mock_cargo.data = {}
        mock_cargo.damage = 0.0

        amount, factor, rule = await get_subsidy_for_cargo(mock_cargo)
        self.assertEqual(factor, 2.0)

        # Test from OUT point
        mock_cargo.sender_point = self.point_out
        amount, factor, rule = await get_subsidy_for_cargo(mock_cargo)
        self.assertEqual(amount, 0)  # No match

    async def test_priority(self):
        # Low priority global rule: 1.1
        await SubsidyRule.objects.acreate(
            name="Global Low",
            reward_type=SubsidyRule.RewardType.PERCENTAGE,
            reward_value=Decimal("1.10"),
            priority=1,
            allocation=Decimal("1000000"),
        )

        # High priority specific rule: 2.0
        r2 = await SubsidyRule.objects.acreate(
            name="Specific High",
            reward_type=SubsidyRule.RewardType.PERCENTAGE,
            reward_value=Decimal("2.00"),
            priority=10,
            allocation=Decimal("1000000"),
        )
        await r2.cargos.aadd(self.cargo_coal)

        mock_cargo = MagicMock()
        mock_cargo.cargo_key = "Coal"
        mock_cargo.payment = 1000
        mock_cargo.sender_point = self.point_out
        mock_cargo.destination_point = self.point_out
        mock_cargo.data = {}
        mock_cargo.damage = 0.0

        amount, factor, rule = await get_subsidy_for_cargo(mock_cargo)
        self.assertEqual(factor, 2.0)  # High priority wins

    async def test_damage_scaling(self):
        # Rule with damage scaling flag (currently unused by get_subsidy_for_cargo)
        rule = await SubsidyRule.objects.acreate(
            name="Fragile",
            reward_type=SubsidyRule.RewardType.PERCENTAGE,
            reward_value=Decimal("2.00"),
            priority=10,
            scales_with_damage=True,
            allocation=Decimal("1000000"),
        )

        mock_cargo = MagicMock()
        mock_cargo.cargo_key = "Glass"
        mock_cargo.payment = 1000
        mock_cargo.sender_point = None
        mock_cargo.destination_point = None
        mock_cargo.data = {}
        mock_cargo.damage = 0.1  # 10% damage

        amount, factor, rule = await get_subsidy_for_cargo(mock_cargo)
        # Damage scaling is not applied in get_subsidy_for_cargo;
        # the raw reward_value is used as the factor.
        self.assertAlmostEqual(factor, 2.0)
        self.assertEqual(amount, 2000)

    async def test_get_subsidies_text(self):
        from amc.subsidies import get_subsidies_text

        # Create active rule
        r1 = await SubsidyRule.objects.acreate(
            name="Active Rule",
            reward_type=SubsidyRule.RewardType.PERCENTAGE,
            reward_value=Decimal("3.00"),
            active=True,
            priority=10,
            allocation=Decimal("1000000"),
        )
        await r1.cargos.aadd(self.cargo_burger)

        # Create inactive rule
        await SubsidyRule.objects.acreate(
            name="Inactive Rule",
            reward_type=SubsidyRule.RewardType.FLAT,
            reward_value=Decimal("5000"),
            active=False,
            priority=10,
        )

        # Create rule with areas
        r3 = await SubsidyRule.objects.acreate(
            name="Area Rule",
            reward_type=SubsidyRule.RewardType.FLAT,
            reward_value=Decimal("1000"),
            active=True,
            priority=5,
            allocation=Decimal("1000000"),
        )
        await r3.source_areas.aadd(self.area_gwangjin)
        await r3.source_delivery_points.aadd(self.point_in)

        text = await get_subsidies_text()

        # Note: Name isn't in text, but Cargo is.
        self.assertIn("Burger", text)
        self.assertIn("300%", text)

        self.assertNotIn("Inactive Rule", text)
        self.assertNotIn("5000 coins", text)

        self.assertIn("Any Cargo", text)  # From r3
        self.assertIn("1000 coins", text)
        self.assertIn(
            "From: Gwangjin Area, In Point", text
        )  # Note: order depends on query results but logically both should be there
        # Since we concat areas then points, Gwangjin Area is first.

    async def test_get_subsidies_text_points_only(self):
        # Create rule with ONLY points, no areas
        r_points = await SubsidyRule.objects.acreate(
            name="Points Only Rule",
            reward_type=SubsidyRule.RewardType.FLAT,
            reward_value=Decimal("500"),
            active=True,
            priority=10,
            allocation=Decimal("1000000"),
        )
        await r_points.source_delivery_points.aadd(self.point_in)
        await r_points.destination_delivery_points.aadd(self.point_out)

        text = await get_subsidies_text()

        self.assertIn("500 coins", text)

        # Verify that our specific points are listed
        self.assertIn("From: In Point", text)
        self.assertIn("To: Out Point", text)

    async def test_delivery_point_matching(self):
        # Rule: Source = point_in
        rule = await SubsidyRule.objects.acreate(
            name="Point Rule",
            reward_type=SubsidyRule.RewardType.PERCENTAGE,
            reward_value=Decimal("2.50"),
            priority=10,
            allocation=Decimal("1000000"),
        )
        await rule.source_delivery_points.aadd(self.point_in)

        # Test exact match
        mock_cargo = MagicMock()
        mock_cargo.cargo_key = "Coal"
        mock_cargo.payment = 1000
        mock_cargo.sender_point = self.point_in
        # Create a mock sender point that has coord
        # We need to simulate the sender_point object having a coord attribute
        # self.point_in is a global object, so it works.

        mock_cargo.destination_point = None
        mock_cargo.data = {}
        mock_cargo.damage = 0.0

        amount, factor, rule = await get_subsidy_for_cargo(mock_cargo)
        self.assertEqual(factor, 2.5)

        # Test nearby match (<1m)
        # Create a point slightly offset
        nearby_point = Point(
            self.point_in.coord.x + 0.5, self.point_in.coord.y, 0, srid=3857
        )
        # We can't easily modify self.point_in, but we can mock the cargo's sender_point
        mock_sender = MagicMock()
        mock_sender.coord = nearby_point
        mock_cargo.sender_point = mock_sender

        amount, factor, rule = await get_subsidy_for_cargo(mock_cargo)
        self.assertEqual(factor, 2.5)

        # Test far match (>1m)
        far_point = Point(
            self.point_in.coord.x + 2.0, self.point_in.coord.y, 0, srid=3857
        )
        mock_sender.coord = far_point
        amount, factor, rule = await get_subsidy_for_cargo(mock_cargo)
        self.assertEqual(factor, 0.0)

    async def test_fallback_logic(self):
        # Rule: Source = point_in OR Gwangjin Area
        rule = await SubsidyRule.objects.acreate(
            name="Fallback Rule",
            reward_type=SubsidyRule.RewardType.PERCENTAGE,
            reward_value=Decimal("3.00"),
            priority=10,
            allocation=Decimal("1000000"),
        )
        await rule.source_delivery_points.aadd(self.point_in)
        await rule.source_areas.aadd(self.area_gwangjin)

        # Case 1: Match Point
        mock_cargo = MagicMock()
        mock_cargo.cargo_key = "Coal"
        mock_cargo.payment = 1000
        mock_cargo.sender_point = self.point_in
        mock_cargo.destination_point = None
        mock_cargo.data = {}
        mock_cargo.damage = 0.0

        amount, factor, rule = await get_subsidy_for_cargo(mock_cargo)
        self.assertEqual(factor, 3.0)

        # Case 2: Match Area (but not point)
        # Use a point inside Gwangjin (0,0 to 10,10) but far from point_in (5,5)
        # Point(1, 1) is in area, distance to (5,5) is > 1m
        point_in_area = Point(1, 1, 0, srid=3857)
        mock_sender = MagicMock()
        mock_sender.coord = point_in_area
        mock_cargo.sender_point = mock_sender

        amount, factor, rule = await get_subsidy_for_cargo(mock_cargo)
        self.assertEqual(factor, 3.0)

        # Case 3: No Match
        point_outside = Point(20, 20, 0, srid=3857)
        mock_sender.coord = point_outside
        mock_cargo.sender_point = mock_sender

        amount, factor, rule = await get_subsidy_for_cargo(mock_cargo)
        self.assertEqual(factor, 0.0)


class CalculateLoanRepaymentTest(TestCase):
    """Tests for calculate_loan_repayment with the configurable REPAYMENT_FULL_AT curve."""

    def test_zero_utilisation(self):
        """At 0% utilisation, repayment rate should be 50%."""
        from amc_finance.loans import calculate_loan_repayment

        # payment=1000, loan=1, max_loan=1_000_000 → ~0% util → 50%
        result = calculate_loan_repayment(Decimal(1000), Decimal(1), Decimal(1_000_000))
        self.assertEqual(
            result, Decimal(1)
        )  # min(1, max(1, 1000*0.5=500)) → capped at loan_balance=1

    def test_half_utilisation(self):
        """At 50% utilisation, repayment should be 100% (with REPAYMENT_FULL_AT=0.5)."""
        from amc_finance.loans import calculate_loan_repayment

        # loan=500_000, max_loan=1_000_000 → 50% util → 100% rate
        result = calculate_loan_repayment(
            Decimal(10_000), Decimal(500_000), Decimal(1_000_000)
        )
        self.assertEqual(result, Decimal(10_000))  # 100% of payment

    def test_quarter_utilisation(self):
        """At 25% utilisation, repayment should be 75%."""
        from amc_finance.loans import calculate_loan_repayment

        result = calculate_loan_repayment(
            Decimal(10_000), Decimal(250_000), Decimal(1_000_000)
        )
        self.assertEqual(result, Decimal(7_500))  # 75% of 10000

    def test_full_utilisation(self):
        """At 100% utilisation, repayment should be 100%."""
        from amc_finance.loans import calculate_loan_repayment

        result = calculate_loan_repayment(
            Decimal(10_000), Decimal(1_000_000), Decimal(1_000_000)
        )
        self.assertEqual(result, Decimal(10_000))

    def test_over_limit_utilisation(self):
        """When loan exceeds max_loan, utilisation is capped at 1.0 → 100% rate."""
        from amc_finance.loans import calculate_loan_repayment

        result = calculate_loan_repayment(
            Decimal(10_000), Decimal(2_000_000), Decimal(1_000_000)
        )
        self.assertEqual(result, Decimal(10_000))

    def test_repayment_capped_at_loan_balance(self):
        """Repayment can never exceed the outstanding loan balance."""
        from amc_finance.loans import calculate_loan_repayment

        # payment far exceeds loan balance
        result = calculate_loan_repayment(
            Decimal(100_000), Decimal(500), Decimal(1_000_000)
        )
        self.assertEqual(result, Decimal(500))  # capped at balance

    def test_player_override_higher(self):
        """Player's custom rate is used when it exceeds the system rate."""
        from amc_finance.loans import calculate_loan_repayment

        # 0% util → system rate 50%, player rate 90% → effective 90%
        result = calculate_loan_repayment(
            Decimal(10_000),
            Decimal(100_000),
            Decimal(1_000_000),
            character_repayment_rate=Decimal("0.90"),
        )
        self.assertEqual(result, Decimal(9_000))

    def test_player_override_lower(self):
        """System rate wins when player's custom rate is lower."""
        from amc_finance.loans import calculate_loan_repayment

        # 50% util → system rate 100%, player rate 30% → system wins (100%)
        result = calculate_loan_repayment(
            Decimal(10_000),
            Decimal(500_000),
            Decimal(1_000_000),
            character_repayment_rate=Decimal("0.30"),
        )
        self.assertEqual(result, Decimal(10_000))


class RepayLoanNPLExitTest(TestCase):
    """Tests for NPL exit announcement in repay_loan_for_profit."""

    @patch("amc.game_server.announce", new_callable=AsyncMock)
    @patch("amc_finance.loans.register_player_repay_loan", new_callable=AsyncMock)
    @patch("amc.mod_server.transfer_money", new_callable=AsyncMock)
    @patch("amc_finance.loans.get_character_max_loan", new_callable=AsyncMock)
    @patch("amc_finance.loans.get_player_loan_balance", new_callable=AsyncMock)
    @patch("amc_finance.loans.is_character_npl", new_callable=AsyncMock)
    async def test_npl_exit_triggers_announcement(
        self,
        mock_is_npl,
        mock_balance,
        mock_max_loan,
        mock_transfer,
        mock_repay,
        mock_announce,
    ):
        """When player transitions NPL→not-NPL, announce it."""
        from amc_finance.loans import repay_loan_for_profit

        mock_balance.return_value = Decimal(600_000)
        mock_max_loan.return_value = (1_000_000, None)
        # First call: was NPL, second call: no longer NPL
        mock_is_npl.side_effect = [True, False]

        character = MagicMock()
        character.name = "TestPlayer"
        character.player.unique_id = 123
        character.loan_repayment_rate = None
        character.guid = "test-guid"
        session = MagicMock()

        await repay_loan_for_profit(character, 10_000, session)

        # Allow fire-and-forget task to run
        await asyncio.sleep(0.1)
        mock_announce.assert_called_once()
        call_msg = mock_announce.call_args[0][0]
        self.assertIn("TestPlayer", call_msg)
        self.assertIn("Non-Performing Loan", call_msg)
        self.assertEqual(mock_announce.call_args[1]["color"], "00FF00")

    @patch("amc.game_server.announce", new_callable=AsyncMock)
    @patch("amc_finance.loans.register_player_repay_loan", new_callable=AsyncMock)
    @patch("amc.mod_server.transfer_money", new_callable=AsyncMock)
    @patch("amc_finance.loans.get_character_max_loan", new_callable=AsyncMock)
    @patch("amc_finance.loans.get_player_loan_balance", new_callable=AsyncMock)
    @patch("amc_finance.loans.is_character_npl", new_callable=AsyncMock)
    async def test_no_announcement_when_still_npl(
        self,
        mock_is_npl,
        mock_balance,
        mock_max_loan,
        mock_transfer,
        mock_repay,
        mock_announce,
    ):
        """No announcement when player remains NPL after repayment."""
        from amc_finance.loans import repay_loan_for_profit

        mock_balance.return_value = Decimal(600_000)
        mock_max_loan.return_value = (1_000_000, None)
        mock_is_npl.side_effect = [True, True]  # Still NPL

        character = MagicMock()
        character.name = "StillNPL"
        character.player.unique_id = 456
        character.loan_repayment_rate = None
        character.guid = "test-guid-2"
        session = MagicMock()

        await repay_loan_for_profit(character, 10_000, session)

        await asyncio.sleep(0.1)
        mock_announce.assert_not_called()

    @patch("amc.game_server.announce", new_callable=AsyncMock)
    @patch("amc_finance.loans.register_player_repay_loan", new_callable=AsyncMock)
    @patch("amc.mod_server.transfer_money", new_callable=AsyncMock)
    @patch("amc_finance.loans.get_character_max_loan", new_callable=AsyncMock)
    @patch("amc_finance.loans.get_player_loan_balance", new_callable=AsyncMock)
    @patch("amc_finance.loans.is_character_npl", new_callable=AsyncMock)
    async def test_no_announcement_when_not_npl_before(
        self,
        mock_is_npl,
        mock_balance,
        mock_max_loan,
        mock_transfer,
        mock_repay,
        mock_announce,
    ):
        """No announcement when player was not NPL before repayment."""
        from amc_finance.loans import repay_loan_for_profit

        mock_balance.return_value = Decimal(600_000)
        mock_max_loan.return_value = (1_000_000, None)
        mock_is_npl.return_value = False  # Not NPL at all

        character = MagicMock()
        character.name = "NeverNPL"
        character.player.unique_id = 789
        character.loan_repayment_rate = None
        character.guid = "test-guid-3"
        session = MagicMock()

        await repay_loan_for_profit(character, 10_000, session)

        await asyncio.sleep(0.1)
        mock_announce.assert_not_called()
        # is_character_npl should only be called once (before check only)
        self.assertEqual(mock_is_npl.call_count, 1)

    @patch("amc.game_server.announce", new_callable=AsyncMock)
    @patch("amc_finance.loans.register_player_repay_loan", new_callable=AsyncMock)
    @patch("amc.mod_server.transfer_money", new_callable=AsyncMock)
    @patch("amc_finance.loans.get_player_loan_balance", new_callable=AsyncMock)
    @patch("amc_finance.loans.is_character_npl", new_callable=AsyncMock)
    async def test_repayment_override_bypasses_calculation(
        self,
        mock_is_npl,
        mock_balance,
        mock_transfer,
        mock_repay,
        mock_announce,
    ):
        """repayment_override uses exact amount instead of calculate_loan_repayment."""
        from amc_finance.loans import repay_loan_for_profit

        mock_balance.return_value = Decimal(600_000)
        mock_is_npl.return_value = False

        character = MagicMock()
        character.name = "Override"
        character.player.unique_id = 101
        character.guid = "test-guid-4"
        session = MagicMock()

        result = await repay_loan_for_profit(
            character,
            10_000,
            session,
            repayment_override=5_000,
        )

        self.assertEqual(result, 5_000)
        # transfer_money should use -5000
        mock_transfer.assert_called_once_with(
            session,
            -5_000,
            "ASEAN Loan Repayment",
            "101",
        )

    @patch("amc.game_server.announce", new_callable=AsyncMock)
    @patch("amc_finance.loans.register_player_repay_loan", new_callable=AsyncMock)
    @patch("amc.mod_server.transfer_money", new_callable=AsyncMock)
    @patch("amc_finance.loans.get_player_loan_balance", new_callable=AsyncMock)
    @patch("amc_finance.loans.is_character_npl", new_callable=AsyncMock)
    async def test_game_session_used_for_announce(
        self,
        mock_is_npl,
        mock_balance,
        mock_transfer,
        mock_repay,
        mock_announce,
    ):
        """game_session is used for announce instead of mod server session."""
        from amc_finance.loans import repay_loan_for_profit

        mock_balance.return_value = Decimal(600_000)
        mock_is_npl.side_effect = [True, False]

        character = MagicMock()
        character.name = "GameSession"
        character.player.unique_id = 202
        character.guid = "test-guid-5"
        mod_session = MagicMock(name="mod_session")
        game_session = MagicMock(name="game_session")

        await repay_loan_for_profit(
            character,
            10_000,
            mod_session,
            repayment_override=5_000,
            game_session=game_session,
        )

        await asyncio.sleep(0.1)
        mock_announce.assert_called_once()
        # The announce should use game_session, not mod_session
        self.assertEqual(mock_announce.call_args[0][1], game_session)
