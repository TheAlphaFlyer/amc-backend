from datetime import timedelta
from django.test import SimpleTestCase
from django.utils import timezone
from amc.tasks import get_welcome_message


class GetWelcomeMessageTests(SimpleTestCase):
    def test_new_player(self):
        """last_online=None → new player greeting."""
        message, is_new = get_welcome_message(None, "TestPlayer")
        self.assertTrue(is_new)
        self.assertIn("Welcome TestPlayer", message)
        self.assertIn("/help", message)

    def test_recent_login_under_1_hour(self):
        """last_online < 1 hour ago → no greeting."""
        last_online = timezone.now() - timedelta(minutes=30)
        message, is_new = get_welcome_message(last_online, "TestPlayer")
        self.assertIsNone(message)
        self.assertFalse(is_new)

    def test_returning_player_over_1_hour(self):
        """last_online > 1 hour but < 7 days → 'Welcome back'."""
        last_online = timezone.now() - timedelta(hours=5)
        message, is_new = get_welcome_message(last_online, "TestPlayer")
        self.assertEqual(message, "Welcome back TestPlayer!")
        self.assertFalse(is_new)

    def test_long_absence_over_7_days(self):
        """last_online > 7 days → 'Long time no see'."""
        last_online = timezone.now() - timedelta(days=10)
        message, is_new = get_welcome_message(last_online, "TestPlayer")
        self.assertEqual(message, "Long time no see! Welcome back TestPlayer")
        self.assertFalse(is_new)

    def test_total_seconds_not_seconds(self):
        """Regression: 8 days ago must use total_seconds, not .seconds.

        timedelta(days=8, hours=2).seconds == 7200 (ignores days!),
        but .total_seconds() == 698400. The old code would wrongly
        return 'Welcome back' instead of 'Long time no see'.
        """
        last_online = timezone.now() - timedelta(days=8, hours=2)
        message, _ = get_welcome_message(last_online, "TestPlayer")
        self.assertEqual(message, "Long time no see! Welcome back TestPlayer")

    def test_exactly_1_hour_boundary(self):
        """At exactly 1 hour (3600s), > 3600 is False, so no greeting."""
        last_online = timezone.now() - timedelta(seconds=3600)
        message, is_new = get_welcome_message(last_online, "TestPlayer")
        # Due to tiny elapsed time between now() calls, this may be slightly > 3600
        # so we accept either None or "Welcome back"
        if message is not None:
            self.assertEqual(message, "Welcome back TestPlayer!")
        self.assertFalse(is_new)

    def test_just_over_1_hour(self):
        """Just over 1 hour returns 'Welcome back'."""
        last_online = timezone.now() - timedelta(hours=1, seconds=1)
        message, is_new = get_welcome_message(last_online, "TestPlayer")
        self.assertEqual(message, "Welcome back TestPlayer!")
        self.assertFalse(is_new)
