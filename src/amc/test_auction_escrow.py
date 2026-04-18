from decimal import Decimal
from asgiref.sync import sync_to_async
from django.test import TestCase
from amc.factories import CharacterFactory
from amc_finance.models import Account, JournalEntry
from amc_finance.services import register_player_deposit
from amc.api.auction_routes import (
    _get_or_create_auction_escrow,
    _get_or_create_checking,
)


class FullAuctionLifecycleTest(TestCase):
    async def test_full_lifecycle(self):
        seller_char = await sync_to_async(CharacterFactory)()
        seller = await sync_to_async(lambda: seller_char.player)()
        bidder_a_char = await sync_to_async(CharacterFactory)()
        bidder_a = await sync_to_async(lambda: bidder_a_char.player)()
        bidder_b_char = await sync_to_async(CharacterFactory)()
        bidder_b = await sync_to_async(lambda: bidder_b_char.player)()

        await register_player_deposit(10000, seller_char, seller)
        await register_player_deposit(10000, bidder_a_char, bidder_a)
        await register_player_deposit(10000, bidder_b_char, bidder_b)

        escrow = await _get_or_create_auction_escrow()

        checking_a = await _get_or_create_checking(bidder_a_char)
        await checking_a.arefresh_from_db()
        self.assertEqual(checking_a.balance, 10000)

        from amc.api.auction_routes import escrow_funds, refund_funds, settle_funds

        class FakePayload:
            def __init__(self, discord_id, amount, character_id=None):
                self.player_discord_id = str(discord_id)
                self.amount = amount
                self.character_id = character_id

        result = await escrow_funds(None, FakePayload(bidder_a.discord_user_id, 3000, character_id=bidder_a_char.pk))
        self.assertEqual(result[0], 200)
        await checking_a.arefresh_from_db()
        self.assertEqual(checking_a.balance, 7000)
        await escrow.arefresh_from_db()
        self.assertEqual(escrow.balance, 3000)

        result = await escrow_funds(None, FakePayload(bidder_b.discord_user_id, 5000, character_id=bidder_b_char.pk))
        self.assertEqual(result[0], 200)
        await escrow.arefresh_from_db()
        self.assertEqual(escrow.balance, 8000)

        result = await refund_funds(None, FakePayload(bidder_a.discord_user_id, 3000, character_id=bidder_a_char.pk))
        self.assertEqual(result[0], 200)
        await escrow.arefresh_from_db()
        self.assertEqual(escrow.balance, 5000)
        await checking_a.arefresh_from_db()
        self.assertEqual(checking_a.balance, 10000)

        class FakeSettlePayload:
            def __init__(self, winner_id, seller_id, amount, seller_type="player", winner_character_id=None, seller_character_id=None):
                self.winner_discord_id = str(winner_id)
                self.seller_discord_id = str(seller_id)
                self.amount = amount
                self.seller_type = seller_type
                self.winner_character_id = winner_character_id
                self.seller_character_id = seller_character_id

        result = await settle_funds(None, FakeSettlePayload(
            bidder_b.discord_user_id, seller.discord_user_id, 5000,
            winner_character_id=bidder_b_char.pk,
            seller_character_id=seller_char.pk,
        ))
        self.assertEqual(result[0], 200)

        await escrow.arefresh_from_db()
        self.assertEqual(escrow.balance, 0)

        checking_b = await _get_or_create_checking(bidder_b_char)
        await checking_b.arefresh_from_db()
        self.assertEqual(checking_b.balance, 5000)

        checking_seller = await _get_or_create_checking(seller_char)
        await checking_seller.arefresh_from_db()
        self.assertEqual(checking_seller.balance, 15000)


class DoubleEntryInvariantTest(TestCase):
    async def test_double_entry_invariant(self):
        char = await sync_to_async(CharacterFactory)()
        player = await sync_to_async(lambda: char.player)()
        await register_player_deposit(10000, char, player)

        from amc.api.auction_routes import escrow_funds, refund_funds

        class FakePayload:
            def __init__(self, discord_id, amount, character_id=None):
                self.player_discord_id = str(discord_id)
                self.amount = amount
                self.character_id = character_id

        await escrow_funds(None, FakePayload(player.discord_user_id, 5000, character_id=char.pk))

        asset_total = Decimal(0)
        liability_total = Decimal(0)
        async for account in Account.objects.all():
            if account.account_type in (Account.AccountType.ASSET, Account.AccountType.EXPENSE):
                asset_total += account.balance
            else:
                liability_total += account.balance
        self.assertEqual(asset_total, liability_total)

        await refund_funds(None, FakePayload(player.discord_user_id, 5000, character_id=char.pk))

        asset_total = Decimal(0)
        liability_total = Decimal(0)
        async for account in Account.objects.all():
            if account.account_type in (Account.AccountType.ASSET, Account.AccountType.EXPENSE):
                asset_total += account.balance
            else:
                liability_total += account.balance
        self.assertEqual(asset_total, liability_total)


class JournalEntryBalancedTest(TestCase):
    async def test_journal_entries_balanced(self):
        char = await sync_to_async(CharacterFactory)()
        player = await sync_to_async(lambda: char.player)()
        await register_player_deposit(10000, char, player)

        from amc.api.auction_routes import escrow_funds, refund_funds

        class FakePayload:
            def __init__(self, discord_id, amount, character_id=None):
                self.player_discord_id = str(discord_id)
                self.amount = amount
                self.character_id = character_id

        await escrow_funds(None, FakePayload(player.discord_user_id, 5000, character_id=char.pk))
        await refund_funds(None, FakePayload(player.discord_user_id, 5000, character_id=char.pk))

        async for je in JournalEntry.objects.all():
            entries = [e async for e in je.entries.all()]
            total_debits = sum(e.debit for e in entries)
            total_credits = sum(e.credit for e in entries)
            self.assertEqual(total_debits, total_credits)


class NoNegativeBalancesTest(TestCase):
    async def test_no_negative_balances_after_operations(self):
        char1 = await sync_to_async(CharacterFactory)()
        player1 = await sync_to_async(lambda: char1.player)()
        char2 = await sync_to_async(CharacterFactory)()
        player2 = await sync_to_async(lambda: char2.player)()

        await register_player_deposit(10000, char1, player1)
        await register_player_deposit(5000, char2, player2)

        from amc.api.auction_routes import escrow_funds, refund_funds

        class FakePayload:
            def __init__(self, discord_id, amount, character_id=None):
                self.player_discord_id = str(discord_id)
                self.amount = amount
                self.character_id = character_id

        await escrow_funds(None, FakePayload(player1.discord_user_id, 5000, character_id=char1.pk))
        await escrow_funds(None, FakePayload(player2.discord_user_id, 3000, character_id=char2.pk))
        await refund_funds(None, FakePayload(player1.discord_user_id, 5000, character_id=char1.pk))

        async for account in Account.objects.all():
            self.assertGreaterEqual(account.balance, 0)


class CancelAuctionRefundTest(TestCase):
    async def test_cancel_auction_full_refund(self):
        char1 = await sync_to_async(CharacterFactory)()
        player1 = await sync_to_async(lambda: char1.player)()
        char2 = await sync_to_async(CharacterFactory)()
        player2 = await sync_to_async(lambda: char2.player)()

        await register_player_deposit(10000, char1, player1)
        await register_player_deposit(10000, char2, player2)

        from amc.api.auction_routes import escrow_funds, refund_funds

        class FakePayload:
            def __init__(self, discord_id, amount, character_id=None):
                self.player_discord_id = str(discord_id)
                self.amount = amount
                self.character_id = character_id

        await escrow_funds(None, FakePayload(player1.discord_user_id, 5000, character_id=char1.pk))
        await escrow_funds(None, FakePayload(player2.discord_user_id, 3000, character_id=char2.pk))

        await refund_funds(None, FakePayload(player1.discord_user_id, 5000, character_id=char1.pk))
        await refund_funds(None, FakePayload(player2.discord_user_id, 3000, character_id=char2.pk))

        escrow = await _get_or_create_auction_escrow()
        await escrow.arefresh_from_db()
        self.assertEqual(escrow.balance, 0)

        checking1 = await _get_or_create_checking(char1)
        checking2 = await _get_or_create_checking(char2)
        await checking1.arefresh_from_db()
        await checking2.arefresh_from_db()
        self.assertEqual(checking1.balance, 10000)
        self.assertEqual(checking2.balance, 10000)


class ReEscrowAfterRefundTest(TestCase):
    async def test_re_escrow_after_refund(self):
        char = await sync_to_async(CharacterFactory)()
        player = await sync_to_async(lambda: char.player)()
        await register_player_deposit(10000, char, player)

        from amc.api.auction_routes import escrow_funds, refund_funds

        class FakePayload:
            def __init__(self, discord_id, amount, character_id=None):
                self.player_discord_id = str(discord_id)
                self.amount = amount
                self.character_id = character_id

        result = await escrow_funds(None, FakePayload(player.discord_user_id, 3000, character_id=char.pk))
        self.assertEqual(result[0], 200)
        self.assertEqual(result[1]["balance"], 7000)

        result = await refund_funds(None, FakePayload(player.discord_user_id, 3000, character_id=char.pk))
        self.assertEqual(result[0], 200)
        self.assertEqual(result[1]["balance"], 10000)

        result = await escrow_funds(None, FakePayload(player.discord_user_id, 5000, character_id=char.pk))
        self.assertEqual(result[0], 200)
        self.assertEqual(result[1]["balance"], 5000)

        escrow = await _get_or_create_auction_escrow()
        await escrow.arefresh_from_db()
        self.assertEqual(escrow.balance, 5000)


class MultipleAuctionsSamePlayerTest(TestCase):
    async def test_multiple_auctions_same_player(self):
        char = await sync_to_async(CharacterFactory)()
        player = await sync_to_async(lambda: char.player)()
        await register_player_deposit(10000, char, player)

        from amc.api.auction_routes import escrow_funds

        class FakePayload:
            def __init__(self, discord_id, amount, character_id=None):
                self.player_discord_id = str(discord_id)
                self.amount = amount
                self.character_id = character_id

        result1 = await escrow_funds(None, FakePayload(player.discord_user_id, 3000, character_id=char.pk))
        self.assertEqual(result1[0], 200)

        result2 = await escrow_funds(None, FakePayload(player.discord_user_id, 5000, character_id=char.pk))
        self.assertEqual(result2[0], 200)

        checking = await _get_or_create_checking(char)
        await checking.arefresh_from_db()
        self.assertEqual(checking.balance, 2000)

        escrow = await _get_or_create_auction_escrow()
        await escrow.arefresh_from_db()
        self.assertEqual(escrow.balance, 8000)


class SettleTransfersToSellerTest(TestCase):
    async def test_settle_transfers_to_seller(self):
        winner_char = await sync_to_async(CharacterFactory)()
        winner = await sync_to_async(lambda: winner_char.player)()
        seller_char = await sync_to_async(CharacterFactory)()
        seller = await sync_to_async(lambda: seller_char.player)()

        await register_player_deposit(10000, winner_char, winner)
        await register_player_deposit(0, seller_char, seller)

        from amc.api.auction_routes import escrow_funds, settle_funds

        class FakePayload:
            def __init__(self, discord_id, amount, character_id=None):
                self.player_discord_id = str(discord_id)
                self.amount = amount
                self.character_id = character_id

        class FakeSettlePayload:
            def __init__(self, winner_id, seller_id, amount, seller_type="player", winner_character_id=None, seller_character_id=None):
                self.winner_discord_id = str(winner_id)
                self.seller_discord_id = str(seller_id)
                self.amount = amount
                self.seller_type = seller_type
                self.winner_character_id = winner_character_id
                self.seller_character_id = seller_character_id

        await escrow_funds(None, FakePayload(winner.discord_user_id, 5000, character_id=winner_char.pk))
        await settle_funds(None, FakeSettlePayload(
            winner.discord_user_id, seller.discord_user_id, 5000,
            winner_character_id=winner_char.pk,
            seller_character_id=seller_char.pk,
        ))

        winner_checking = await _get_or_create_checking(winner_char)
        seller_checking = await _get_or_create_checking(seller_char)

        await winner_checking.arefresh_from_db()
        await seller_checking.arefresh_from_db()

        self.assertEqual(winner_checking.balance, 5000)
        self.assertEqual(seller_checking.balance, 5000)

        escrow = await _get_or_create_auction_escrow()
        await escrow.arefresh_from_db()
        self.assertEqual(escrow.balance, 0)


class SettleToTreasuryTest(TestCase):
    async def test_settle_to_treasury(self):
        winner_char = await sync_to_async(CharacterFactory)()
        winner = await sync_to_async(lambda: winner_char.player)()
        await register_player_deposit(10000, winner_char, winner)

        from amc.api.auction_routes import escrow_funds, settle_funds, _get_or_create_auction_revenue

        class FakePayload:
            def __init__(self, discord_id, amount, character_id=None):
                self.player_discord_id = str(discord_id)
                self.amount = amount
                self.character_id = character_id

        class FakeSettlePayload:
            def __init__(self, winner_id, seller_id, amount, seller_type="player", winner_character_id=None, seller_character_id=None):
                self.winner_discord_id = str(winner_id)
                self.seller_discord_id = str(seller_id)
                self.amount = amount
                self.seller_type = seller_type
                self.winner_character_id = winner_character_id
                self.seller_character_id = seller_character_id

        await escrow_funds(None, FakePayload(winner.discord_user_id, 5000, character_id=winner_char.pk))
        await settle_funds(None, FakeSettlePayload(
            winner.discord_user_id, "0", 5000, seller_type="treasury",
            winner_character_id=winner_char.pk,
        ))

        escrow = await _get_or_create_auction_escrow()
        await escrow.arefresh_from_db()
        self.assertEqual(escrow.balance, 0)

        revenue = await _get_or_create_auction_revenue()
        await revenue.arefresh_from_db()
        self.assertEqual(revenue.balance, 5000)

        asset_total = Decimal(0)
        liability_total = Decimal(0)
        async for account in Account.objects.all():
            if account.account_type in (Account.AccountType.ASSET, Account.AccountType.EXPENSE):
                asset_total += account.balance
            else:
                liability_total += account.balance
        self.assertEqual(asset_total, liability_total)


class MultiCharacterRefundTest(TestCase):
    async def test_refund_goes_to_correct_character(self):
        char1 = await sync_to_async(CharacterFactory)()
        player = await sync_to_async(lambda: char1.player)()
        char2 = await sync_to_async(CharacterFactory)(player=player)
        await register_player_deposit(10000, char1, player)
        await register_player_deposit(5000, char2, player)

        from amc.api.auction_routes import escrow_funds, refund_funds

        class FakePayload:
            def __init__(self, discord_id, amount, character_id=None):
                self.player_discord_id = str(discord_id)
                self.amount = amount
                self.character_id = character_id

        await escrow_funds(None, FakePayload(player.discord_user_id, 3000, character_id=char2.pk))

        checking1 = await _get_or_create_checking(char1)
        checking2 = await _get_or_create_checking(char2)
        await checking1.arefresh_from_db()
        await checking2.arefresh_from_db()
        self.assertEqual(checking1.balance, 10000)
        self.assertEqual(checking2.balance, 2000)

        await refund_funds(None, FakePayload(player.discord_user_id, 3000, character_id=char2.pk))
        await checking1.arefresh_from_db()
        await checking2.arefresh_from_db()
        self.assertEqual(checking1.balance, 10000)
        self.assertEqual(checking2.balance, 5000)
