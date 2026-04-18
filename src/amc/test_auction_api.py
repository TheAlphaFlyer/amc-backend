from asgiref.sync import sync_to_async
from django.test import TestCase
from ninja.testing import TestAsyncClient
from amc.factories import CharacterFactory
from amc.models import Player
from amc_finance.models import Account, JournalEntry
from amc_finance.services import register_player_deposit
from amc.api.auction_routes import router


class BalanceEndpointTests(TestCase):
    async def test_get_balance_existing_player(self):
        client = TestAsyncClient(router)
        character = await sync_to_async(CharacterFactory)()
        player = await sync_to_async(lambda: character.player)()
        await register_player_deposit(10000, character, player)
        response = await client.get(f"/balance/?player_id={player.discord_user_id}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["balance"], 10000)

    async def test_get_balance_zero(self):
        client = TestAsyncClient(router)
        character = await sync_to_async(CharacterFactory)()
        player = await sync_to_async(lambda: character.player)()
        response = await client.get(f"/balance/?player_id={player.discord_user_id}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["balance"], 0)

    async def test_get_balance_unknown_player(self):
        client = TestAsyncClient(router)
        response = await client.get("/balance/?player_id=999999999")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"], "Player not found")

    async def test_get_balance_invalid_player_id(self):
        client = TestAsyncClient(router)
        response = await client.get("/balance/?player_id=not_a_number")
        self.assertEqual(response.status_code, 404)

    async def test_get_balance_no_character(self):
        client = TestAsyncClient(router)
        player = await Player.objects.acreate(unique_id=99999, discord_user_id=88888)
        response = await client.get(f"/balance/?player_id={player.discord_user_id}")
        self.assertEqual(response.status_code, 404)
        self.assertIn("character", response.json()["error"].lower())


class EscrowEndpointTests(TestCase):
    async def test_escrow_success(self):
        client = TestAsyncClient(router)
        character = await sync_to_async(CharacterFactory)()
        player = await sync_to_async(lambda: character.player)()
        await register_player_deposit(10000, character, player)
        response = await client.post("/escrow/", json={
            "player_discord_id": str(player.discord_user_id), "amount": 5000
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["balance"], 5000)

    async def test_escrow_insufficient_funds(self):
        client = TestAsyncClient(router)
        character = await sync_to_async(CharacterFactory)()
        player = await sync_to_async(lambda: character.player)()
        await register_player_deposit(3000, character, player)
        response = await client.post("/escrow/", json={
            "player_discord_id": str(player.discord_user_id), "amount": 5000
        })
        self.assertEqual(response.status_code, 409)

    async def test_escrow_exact_balance(self):
        client = TestAsyncClient(router)
        character = await sync_to_async(CharacterFactory)()
        player = await sync_to_async(lambda: character.player)()
        await register_player_deposit(5000, character, player)
        response = await client.post("/escrow/", json={
            "player_discord_id": str(player.discord_user_id), "amount": 5000
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["balance"], 0)

    async def test_escrow_zero_balance(self):
        client = TestAsyncClient(router)
        character = await sync_to_async(CharacterFactory)()
        player = await sync_to_async(lambda: character.player)()
        response = await client.post("/escrow/", json={
            "player_discord_id": str(player.discord_user_id), "amount": 1
        })
        self.assertEqual(response.status_code, 409)

    async def test_escrow_player_not_found(self):
        client = TestAsyncClient(router)
        response = await client.post("/escrow/", json={
            "player_discord_id": "999999999", "amount": 5000
        })
        self.assertEqual(response.status_code, 404)

    async def test_escrow_no_character(self):
        client = TestAsyncClient(router)
        player = await Player.objects.acreate(unique_id=99998, discord_user_id=77777)
        response = await client.post("/escrow/", json={
            "player_discord_id": str(player.discord_user_id), "amount": 5000
        })
        self.assertEqual(response.status_code, 404)

    async def test_escrow_negative_amount(self):
        client = TestAsyncClient(router)
        character = await sync_to_async(CharacterFactory)()
        player = await sync_to_async(lambda: character.player)()
        response = await client.post("/escrow/", json={
            "player_discord_id": str(player.discord_user_id), "amount": -100
        })
        self.assertEqual(response.status_code, 409)

    async def test_escrow_zero_amount(self):
        client = TestAsyncClient(router)
        character = await sync_to_async(CharacterFactory)()
        player = await sync_to_async(lambda: character.player)()
        response = await client.post("/escrow/", json={
            "player_discord_id": str(player.discord_user_id), "amount": 0
        })
        self.assertEqual(response.status_code, 409)

    async def test_escrow_updates_checking_account(self):
        client = TestAsyncClient(router)
        character = await sync_to_async(CharacterFactory)()
        player = await sync_to_async(lambda: character.player)()
        await register_player_deposit(10000, character, player)
        await client.post("/escrow/", json={
            "player_discord_id": str(player.discord_user_id), "amount": 5000
        })
        checking = await Account.objects.aget(
            account_type=Account.AccountType.LIABILITY,
            book=Account.Book.BANK,
            character=character,
        )
        self.assertEqual(checking.balance, 5000)

    async def test_escrow_updates_escrow_account(self):
        client = TestAsyncClient(router)
        character = await sync_to_async(CharacterFactory)()
        player = await sync_to_async(lambda: character.player)()
        await register_player_deposit(10000, character, player)
        await client.post("/escrow/", json={
            "player_discord_id": str(player.discord_user_id), "amount": 5000
        })
        escrow = await Account.objects.aget(name="Auction Escrow")
        self.assertEqual(escrow.balance, 5000)

    async def test_escrow_creates_journal_entry(self):
        client = TestAsyncClient(router)
        character = await sync_to_async(CharacterFactory)()
        player = await sync_to_async(lambda: character.player)()
        await register_player_deposit(10000, character, player)
        await client.post("/escrow/", json={
            "player_discord_id": str(player.discord_user_id), "amount": 5000
        })
        je = await JournalEntry.objects.filter(description__contains="Auction Escrow").afirst()
        self.assertIsNotNone(je)

    async def test_escrow_two_players(self):
        client = TestAsyncClient(router)
        character = await sync_to_async(CharacterFactory)()
        player = await sync_to_async(lambda: character.player)()
        char2 = await sync_to_async(CharacterFactory)()
        player2 = await sync_to_async(lambda: char2.player)()
        await register_player_deposit(10000, character, player)
        await register_player_deposit(10000, char2, player2)

        await client.post("/escrow/", json={
            "player_discord_id": str(player.discord_user_id), "amount": 5000
        })
        await client.post("/escrow/", json={
            "player_discord_id": str(player2.discord_user_id), "amount": 5000
        })
        escrow = await Account.objects.aget(name="Auction Escrow")
        self.assertEqual(escrow.balance, 10000)

    async def test_escrow_after_refund(self):
        client = TestAsyncClient(router)
        character = await sync_to_async(CharacterFactory)()
        player = await sync_to_async(lambda: character.player)()
        await register_player_deposit(10000, character, player)
        await client.post("/escrow/", json={
            "player_discord_id": str(player.discord_user_id), "amount": 5000
        })
        await client.post("/refund/", json={
            "player_discord_id": str(player.discord_user_id), "amount": 5000
        })
        response = await client.post("/escrow/", json={
            "player_discord_id": str(player.discord_user_id), "amount": 3000
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["balance"], 7000)


class RefundEndpointTests(TestCase):
    async def test_refund_success(self):
        client = TestAsyncClient(router)
        character = await sync_to_async(CharacterFactory)()
        player = await sync_to_async(lambda: character.player)()
        await register_player_deposit(10000, character, player)
        await client.post("/escrow/", json={
            "player_discord_id": str(player.discord_user_id), "amount": 5000
        })
        response = await client.post("/refund/", json={
            "player_discord_id": str(player.discord_user_id), "amount": 5000
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["balance"], 10000)

    async def test_refund_player_not_found(self):
        client = TestAsyncClient(router)
        response = await client.post("/refund/", json={
            "player_discord_id": "999999999", "amount": 5000
        })
        self.assertEqual(response.status_code, 404)

    async def test_refund_no_character(self):
        client = TestAsyncClient(router)
        player = await Player.objects.acreate(unique_id=99997, discord_user_id=66666)
        response = await client.post("/refund/", json={
            "player_discord_id": str(player.discord_user_id), "amount": 5000
        })
        self.assertEqual(response.status_code, 404)

    async def test_refund_negative_amount(self):
        client = TestAsyncClient(router)
        character = await sync_to_async(CharacterFactory)()
        player = await sync_to_async(lambda: character.player)()
        response = await client.post("/refund/", json={
            "player_discord_id": str(player.discord_user_id), "amount": -100
        })
        self.assertEqual(response.status_code, 409)

    async def test_refund_updates_checking_account(self):
        client = TestAsyncClient(router)
        character = await sync_to_async(CharacterFactory)()
        player = await sync_to_async(lambda: character.player)()
        await register_player_deposit(10000, character, player)
        await client.post("/escrow/", json={
            "player_discord_id": str(player.discord_user_id), "amount": 5000
        })
        await client.post("/refund/", json={
            "player_discord_id": str(player.discord_user_id), "amount": 5000
        })
        checking = await Account.objects.aget(
            account_type=Account.AccountType.LIABILITY,
            book=Account.Book.BANK,
            character=character,
        )
        self.assertEqual(checking.balance, 10000)

    async def test_refund_updates_escrow_account(self):
        client = TestAsyncClient(router)
        character = await sync_to_async(CharacterFactory)()
        player = await sync_to_async(lambda: character.player)()
        await register_player_deposit(10000, character, player)
        await client.post("/escrow/", json={
            "player_discord_id": str(player.discord_user_id), "amount": 5000
        })
        await client.post("/refund/", json={
            "player_discord_id": str(player.discord_user_id), "amount": 5000
        })
        escrow = await Account.objects.aget(name="Auction Escrow")
        self.assertEqual(escrow.balance, 0)

    async def test_refund_partial(self):
        client = TestAsyncClient(router)
        character = await sync_to_async(CharacterFactory)()
        player = await sync_to_async(lambda: character.player)()
        await register_player_deposit(10000, character, player)
        await client.post("/escrow/", json={
            "player_discord_id": str(player.discord_user_id), "amount": 5000
        })
        await client.post("/refund/", json={
            "player_discord_id": str(player.discord_user_id), "amount": 3000
        })
        checking = await Account.objects.aget(
            account_type=Account.AccountType.LIABILITY,
            book=Account.Book.BANK,
            character=character,
        )
        self.assertEqual(checking.balance, 8000)

    async def test_refund_creates_journal_entry(self):
        client = TestAsyncClient(router)
        character = await sync_to_async(CharacterFactory)()
        player = await sync_to_async(lambda: character.player)()
        await register_player_deposit(10000, character, player)
        await client.post("/escrow/", json={
            "player_discord_id": str(player.discord_user_id), "amount": 5000
        })
        await client.post("/refund/", json={
            "player_discord_id": str(player.discord_user_id), "amount": 5000
        })
        count = await JournalEntry.objects.acount()
        self.assertGreaterEqual(count, 2)

    async def test_refund_escrow_account_integrity(self):
        client = TestAsyncClient(router)
        char1 = await sync_to_async(CharacterFactory)()
        player1 = await sync_to_async(lambda: char1.player)()
        char2 = await sync_to_async(CharacterFactory)()
        player2 = await sync_to_async(lambda: char2.player)()
        await register_player_deposit(10000, char1, player1)
        await register_player_deposit(10000, char2, player2)

        await client.post("/escrow/", json={
            "player_discord_id": str(player1.discord_user_id), "amount": 5000
        })
        await client.post("/escrow/", json={
            "player_discord_id": str(player2.discord_user_id), "amount": 3000
        })

        await client.post("/refund/", json={
            "player_discord_id": str(player1.discord_user_id), "amount": 5000
        })

        escrow = await Account.objects.aget(name="Auction Escrow")
        self.assertEqual(escrow.balance, 3000)


class SettleEndpointTests(TestCase):
    async def test_settle_success(self):
        client = TestAsyncClient(router)
        winner_char = await sync_to_async(CharacterFactory)()
        winner_player = await sync_to_async(lambda: winner_char.player)()
        seller_char = await sync_to_async(CharacterFactory)()
        seller_player = await sync_to_async(lambda: seller_char.player)()
        await register_player_deposit(10000, winner_char, winner_player)
        await client.post("/escrow/", json={
            "player_discord_id": str(winner_player.discord_user_id), "amount": 5000
        })

        response = await client.post("/settle/", json={
            "winner_discord_id": str(winner_player.discord_user_id),
            "seller_discord_id": str(seller_player.discord_user_id),
            "amount": 5000,
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["amount"], 5000)

    async def test_settle_winner_not_found(self):
        client = TestAsyncClient(router)
        seller_char = await sync_to_async(CharacterFactory)()
        seller_player = await sync_to_async(lambda: seller_char.player)()
        response = await client.post("/settle/", json={
            "winner_discord_id": "999999999",
            "seller_discord_id": str(seller_player.discord_user_id),
            "amount": 5000,
        })
        self.assertEqual(response.status_code, 404)

    async def test_settle_seller_not_found(self):
        client = TestAsyncClient(router)
        winner_char = await sync_to_async(CharacterFactory)()
        winner_player = await sync_to_async(lambda: winner_char.player)()
        await register_player_deposit(10000, winner_char, winner_player)
        await client.post("/escrow/", json={
            "player_discord_id": str(winner_player.discord_user_id), "amount": 5000
        })
        response = await client.post("/settle/", json={
            "winner_discord_id": str(winner_player.discord_user_id),
            "seller_discord_id": "999999999",
            "amount": 5000,
        })
        self.assertEqual(response.status_code, 404)

    async def test_settle_updates_escrow_account(self):
        client = TestAsyncClient(router)
        winner_char = await sync_to_async(CharacterFactory)()
        winner_player = await sync_to_async(lambda: winner_char.player)()
        seller_char = await sync_to_async(CharacterFactory)()
        seller_player = await sync_to_async(lambda: seller_char.player)()
        await register_player_deposit(10000, winner_char, winner_player)
        await client.post("/escrow/", json={
            "player_discord_id": str(winner_player.discord_user_id), "amount": 5000
        })
        await client.post("/settle/", json={
            "winner_discord_id": str(winner_player.discord_user_id),
            "seller_discord_id": str(seller_player.discord_user_id),
            "amount": 5000,
        })
        escrow = await Account.objects.aget(name="Auction Escrow")
        self.assertEqual(escrow.balance, 0)

    async def test_settle_updates_seller_account(self):
        client = TestAsyncClient(router)
        winner_char = await sync_to_async(CharacterFactory)()
        winner_player = await sync_to_async(lambda: winner_char.player)()
        seller_char = await sync_to_async(CharacterFactory)()
        seller_player = await sync_to_async(lambda: seller_char.player)()
        await register_player_deposit(10000, winner_char, winner_player)
        await client.post("/escrow/", json={
            "player_discord_id": str(winner_player.discord_user_id), "amount": 5000
        })
        await client.post("/settle/", json={
            "winner_discord_id": str(winner_player.discord_user_id),
            "seller_discord_id": str(seller_player.discord_user_id),
            "amount": 5000,
        })
        seller_checking = await Account.objects.aget(
            account_type=Account.AccountType.LIABILITY,
            book=Account.Book.BANK,
            character=seller_char,
        )
        self.assertEqual(seller_checking.balance, 5000)

    async def test_settle_creates_journal_entry(self):
        client = TestAsyncClient(router)
        winner_char = await sync_to_async(CharacterFactory)()
        winner_player = await sync_to_async(lambda: winner_char.player)()
        seller_char = await sync_to_async(CharacterFactory)()
        seller_player = await sync_to_async(lambda: seller_char.player)()
        await register_player_deposit(10000, winner_char, winner_player)
        await client.post("/escrow/", json={
            "player_discord_id": str(winner_player.discord_user_id), "amount": 5000
        })
        await client.post("/settle/", json={
            "winner_discord_id": str(winner_player.discord_user_id),
            "seller_discord_id": str(seller_player.discord_user_id),
            "amount": 5000,
        })
        je = await JournalEntry.objects.filter(description__contains="Settlement").afirst()
        self.assertIsNotNone(je)

    async def test_settle_seller_new_account(self):
        client = TestAsyncClient(router)
        winner_char = await sync_to_async(CharacterFactory)()
        winner_player = await sync_to_async(lambda: winner_char.player)()
        seller_char = await sync_to_async(CharacterFactory)()
        seller_player = await sync_to_async(lambda: seller_char.player)()
        await register_player_deposit(10000, winner_char, winner_player)
        await client.post("/escrow/", json={
            "player_discord_id": str(winner_player.discord_user_id), "amount": 5000
        })
        exists = await Account.objects.filter(
            account_type=Account.AccountType.LIABILITY,
            book=Account.Book.BANK,
            character=seller_char,
        ).aexists()
        self.assertFalse(exists)

        await client.post("/settle/", json={
            "winner_discord_id": str(winner_player.discord_user_id),
            "seller_discord_id": str(seller_player.discord_user_id),
            "amount": 5000,
        })
        seller_checking = await Account.objects.aget(
            account_type=Account.AccountType.LIABILITY,
            book=Account.Book.BANK,
            character=seller_char,
        )
        self.assertEqual(seller_checking.balance, 5000)

    async def test_settle_zero_amount(self):
        client = TestAsyncClient(router)
        winner_char = await sync_to_async(CharacterFactory)()
        winner_player = await sync_to_async(lambda: winner_char.player)()
        seller_char = await sync_to_async(CharacterFactory)()
        seller_player = await sync_to_async(lambda: seller_char.player)()
        response = await client.post("/settle/", json={
            "winner_discord_id": str(winner_player.discord_user_id),
            "seller_discord_id": str(seller_player.discord_user_id),
            "amount": 0,
        })
        self.assertEqual(response.status_code, 409)

    async def test_settle_winner_no_character(self):
        client = TestAsyncClient(router)
        seller_char = await sync_to_async(CharacterFactory)()
        seller_player = await sync_to_async(lambda: seller_char.player)()
        player = await Player.objects.acreate(unique_id=99996, discord_user_id=55555)
        response = await client.post("/settle/", json={
            "winner_discord_id": str(player.discord_user_id),
            "seller_discord_id": str(seller_player.discord_user_id),
            "amount": 5000,
        })
        self.assertEqual(response.status_code, 404)

    async def test_settle_to_treasury(self):
        client = TestAsyncClient(router)
        winner_char = await sync_to_async(CharacterFactory)()
        winner_player = await sync_to_async(lambda: winner_char.player)()
        await register_player_deposit(10000, winner_char, winner_player)
        await client.post("/escrow/", json={
            "player_discord_id": str(winner_player.discord_user_id), "amount": 5000
        })
        response = await client.post("/settle/", json={
            "winner_discord_id": str(winner_player.discord_user_id),
            "seller_discord_id": "0",
            "amount": 5000,
            "seller_type": "treasury",
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["seller_type"], "treasury")
        self.assertIsNone(response.json()["seller_id"])

        escrow = await Account.objects.aget(name="Auction Escrow")
        self.assertEqual(escrow.balance, 0)

        revenue = await Account.objects.aget(name="Auction Revenue")
        self.assertEqual(revenue.balance, 5000)
