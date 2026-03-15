import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "amc_backend.settings")

import django

django.setup()

import unittest  # noqa: E402
from unittest.mock import AsyncMock, MagicMock, patch  # noqa: E402
import discord  # noqa: E402
from typing import Any, cast  # noqa: E402
from amc_cogs.server import ServerCog, RestartConfirmView, RESTART_COOLDOWN_SECONDS  # noqa: E402


class ServerCogTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # Mock bot
        self.bot = MagicMock()
        self.bot.http_client_game = MagicMock()
        self.bot.get_channel = MagicMock(
            return_value=None
        )  # No audit channel by default

        # Create cog
        self.cog = ServerCog(self.bot)
        self.cog.audit_channel_id = 0

        # Mock interaction
        self.interaction = MagicMock()
        self.interaction.response = AsyncMock()
        self.interaction.followup = AsyncMock()
        self.interaction.edit_original_response = AsyncMock()
        self.interaction.user = MagicMock()
        self.interaction.user.id = 123456789
        self.interaction.user.display_name = "TestAdmin"
        self.interaction.user.mention = "<@123456789>"

    @patch("amc_cogs.server.get_players", new_callable=AsyncMock)
    async def test_restart_shows_confirmation(self, mock_get_players):
        """Test that /server restart shows a confirmation embed with player count."""
        mock_get_players.return_value = [
            ("1001", {"name": "Player1"}),
            ("1002", {"name": "Player2"}),
        ]

        await cast(Any, self.cog.restart_server.callback)(
            self.cog, self.interaction, 300, None
        )

        # Should have responded with an embed and view
        self.interaction.response.send_message.assert_called_once()
        call_kwargs = self.interaction.response.send_message.call_args.kwargs
        self.assertIn("embed", call_kwargs)
        self.assertIn("view", call_kwargs)
        self.assertTrue(call_kwargs["ephemeral"])

        embed = call_kwargs["embed"]
        # Check the embed has player count field
        player_field = next(f for f in embed.fields if f.name == "Players Online")
        self.assertEqual(player_field.value, "2")

    @patch("amc_cogs.server.get_players", new_callable=AsyncMock)
    async def test_countdown_too_short_rejected(self, mock_get_players):
        """Test that countdown < 10 seconds is rejected."""
        await cast(Any, self.cog.restart_server.callback)(
            self.cog, self.interaction, 5, None
        )

        self.interaction.response.send_message.assert_called_once()
        msg = self.interaction.response.send_message.call_args.args[0]
        self.assertIn("at least 10 seconds", msg)

    @patch("amc_cogs.server.get_players", new_callable=AsyncMock)
    async def test_countdown_too_long_rejected(self, mock_get_players):
        """Test that countdown > 3600 seconds is rejected."""
        await cast(Any, self.cog.restart_server.callback)(
            self.cog, self.interaction, 7200, None
        )

        self.interaction.response.send_message.assert_called_once()
        msg = self.interaction.response.send_message.call_args.args[0]
        self.assertIn("cannot exceed 3600", msg)

    @patch("amc_cogs.server.get_players", new_callable=AsyncMock)
    async def test_cooldown_prevents_rapid_restart(self, mock_get_players):
        """Test that the 5-minute cooldown prevents rapid re-invocation."""
        import time

        self.cog._last_restart_time = time.monotonic()  # Just restarted

        await cast(Any, self.cog.restart_server.callback)(
            self.cog, self.interaction, 300, None
        )

        self.interaction.response.send_message.assert_called_once()
        msg = self.interaction.response.send_message.call_args.args[0]
        self.assertIn("Cooldown active", msg)

    @patch("amc_cogs.server.get_players", new_callable=AsyncMock)
    async def test_no_cooldown_when_expired(self, mock_get_players):
        """Test that restart is allowed after cooldown expires."""
        import time

        self.cog._last_restart_time = time.monotonic() - RESTART_COOLDOWN_SECONDS - 1

        mock_get_players.return_value = [("1001", {"name": "Player1"})]

        await cast(Any, self.cog.restart_server.callback)(
            self.cog, self.interaction, 300, None
        )

        # Should show confirmation, not cooldown
        call_kwargs = self.interaction.response.send_message.call_args.kwargs
        self.assertIn("embed", call_kwargs)

    @patch("asyncio.create_subprocess_exec", new_callable=AsyncMock)
    @patch("amc_cogs.server.announce", new_callable=AsyncMock)
    async def test_cancel_prevents_restart(self, mock_announce, mock_subprocess):
        """Test that clicking Cancel prevents the subprocess from running."""
        view = RestartConfirmView(
            cog=self.cog,
            interaction=self.interaction,
            countdown_seconds=10,
            reason="testing",
            player_count=5,
        )

        # Simulate cancel
        cancel_interaction = MagicMock()
        cancel_interaction.response = AsyncMock()
        # discord.py button callbacks are called as callback(interaction)
        await view.cancel_button.callback(cancel_interaction)

        self.assertTrue(view.cancelled)
        cancel_interaction.response.send_message.assert_called_once()
        msg = cancel_interaction.response.send_message.call_args.args[0]
        self.assertIn("cancelled", msg)

        # Subprocess should never have been called
        mock_subprocess.assert_not_called()

    @patch("asyncio.create_subprocess_exec", new_callable=AsyncMock)
    @patch("amc_cogs.server.announce", new_callable=AsyncMock)
    async def test_execute_restart_success(self, mock_announce, mock_subprocess):
        """Test successful restart execution via mocked subprocess."""
        # Mock successful process
        mock_process = AsyncMock()
        mock_process.communicate.return_value = (b"OK", b"")
        mock_process.returncode = 0
        mock_subprocess.return_value = mock_process

        view = RestartConfirmView(
            cog=self.cog,
            interaction=self.interaction,
            countdown_seconds=10,
            reason=None,
            player_count=3,
        )

        await view._execute_restart(self.interaction)

        # Verify subprocess was called with the restart script
        mock_subprocess.assert_called_once()
        call_args = mock_subprocess.call_args
        self.assertIn("restart-motortown", call_args.args[0])

        # Verify success message
        self.interaction.followup.send.assert_called_once()
        msg = self.interaction.followup.send.call_args.args[0]
        self.assertIn("successfully", msg)

    @patch("asyncio.create_subprocess_exec", new_callable=AsyncMock)
    @patch("amc_cogs.server.announce", new_callable=AsyncMock)
    async def test_execute_restart_failure(self, mock_announce, mock_subprocess):
        """Test failed restart execution reports error."""
        mock_process = AsyncMock()
        mock_process.communicate.return_value = (b"", b"Permission denied")
        mock_process.returncode = 1
        mock_subprocess.return_value = mock_process

        view = RestartConfirmView(
            cog=self.cog,
            interaction=self.interaction,
            countdown_seconds=10,
            reason=None,
            player_count=3,
        )

        await view._execute_restart(self.interaction)

        # Verify failure message
        self.interaction.followup.send.assert_called_once()
        msg = self.interaction.followup.send.call_args.args[0]
        self.assertIn("failed", msg)
        self.assertIn("Permission denied", msg)

    @patch("asyncio.create_subprocess_exec", new_callable=AsyncMock)
    @patch("amc_cogs.server.announce", new_callable=AsyncMock)
    async def test_audit_log_sent_on_restart(self, mock_announce, mock_subprocess):
        """Test that an audit log embed is sent to the admin channel."""
        # Set up audit channel — must pass isinstance check for discord.abc.Messageable
        mock_channel = AsyncMock(spec=discord.abc.Messageable)
        self.bot.get_channel.return_value = mock_channel

        mock_process = AsyncMock()
        mock_process.communicate.return_value = (b"OK", b"")
        mock_process.returncode = 0
        mock_subprocess.return_value = mock_process

        view = RestartConfirmView(
            cog=self.cog,
            interaction=self.interaction,
            countdown_seconds=60,
            reason="mod update",
            player_count=10,
        )

        await view._execute_restart(self.interaction)

        # Verify audit log was sent
        mock_channel.send.assert_called_once()
        call_kwargs = mock_channel.send.call_args.kwargs
        embed = call_kwargs["embed"]
        self.assertIn("Restart", embed.title)
        reason_field = next(f for f in embed.fields if f.name == "Reason")
        self.assertEqual(reason_field.value, "mod update")

    async def test_format_time_seconds(self):
        """Test _format_time with various values."""
        self.assertEqual(RestartConfirmView._format_time(30), "30 seconds")
        self.assertEqual(RestartConfirmView._format_time(1), "1 second")
        self.assertEqual(RestartConfirmView._format_time(60), "1 minute")
        self.assertEqual(RestartConfirmView._format_time(120), "2 minutes")
        self.assertEqual(RestartConfirmView._format_time(90), "1m 30s")
        self.assertEqual(RestartConfirmView._format_time(300), "5 minutes")
