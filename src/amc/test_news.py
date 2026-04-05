from datetime import timedelta
from django.test import TestCase
from django.utils import timezone
from amc.models import NewsItem


class NewsItemModelTests(TestCase):
    def test_no_expires_at_by_default(self):
        """New item without explicit expires_at keeps it null."""
        item = NewsItem.objects.create(title="Test News")
        self.assertIsNone(item.expires_at)

    def test_custom_expiry_preserved(self):
        """Explicit expires_at is stored as-is."""
        custom = timezone.now() + timedelta(days=30)
        item = NewsItem.objects.create(title="Custom", expires_at=custom)
        self.assertAlmostEqual(item.expires_at.timestamp(), custom.timestamp(), delta=1)


class NewsItemGetActiveTests(TestCase):
    def setUp(self):
        now = timezone.now()
        # Active items with explicit expiry
        self.item1 = NewsItem.objects.create(
            title="News 1", expires_at=now + timedelta(days=3)
        )
        self.item2 = NewsItem.objects.create(
            title="News 2", expires_at=now + timedelta(days=5)
        )
        # Active item without expiry (created now, so within 7 days)
        self.item3 = NewsItem.objects.create(title="News 3")
        self.item4 = NewsItem.objects.create(
            title="News 4", expires_at=now + timedelta(days=2)
        )
        # Expired item (explicit expiry in the past)
        self.expired = NewsItem.objects.create(
            title="Old News", expires_at=now - timedelta(days=1)
        )
        # Extra active item (should be excluded by limit=4)
        self.item5 = NewsItem.objects.create(
            title="News 5", expires_at=now + timedelta(days=6)
        )

    async def test_returns_only_active_items(self):
        """Expired items are excluded."""
        items = await NewsItem.aget_active()
        titles = [i.title for i in items]
        self.assertNotIn("Old News", titles)

    async def test_limited_to_4(self):
        """At most 4 items returned."""
        items = await NewsItem.aget_active()
        self.assertEqual(len(items), 4)

    async def test_ordered_newest_first(self):
        """Items are ordered by -created_at (newest first)."""
        items = await NewsItem.aget_active()
        self.assertEqual(items[0].title, "News 5")

    async def test_empty_when_all_expired(self):
        """Returns empty list when all items have expired."""
        await NewsItem.objects.all().aupdate(
            expires_at=timezone.now() - timedelta(hours=1)
        )
        items = await NewsItem.aget_active()
        self.assertEqual(items, [])

    async def test_null_expiry_within_7_days_is_active(self):
        """Item with no expires_at created recently is active."""
        await NewsItem.objects.acreate(title="Fresh")
        items = await NewsItem.aget_active()
        titles = [i.title for i in items]
        self.assertIn("Fresh", titles)

    async def test_null_expiry_older_than_7_days_is_inactive(self):
        """Item with no expires_at created >7 days ago is inactive."""
        old = await NewsItem.objects.acreate(title="Ancient")
        # Backdate created_at
        await NewsItem.objects.filter(pk=old.pk).aupdate(
            created_at=timezone.now() - timedelta(days=8)
        )
        items = await NewsItem.aget_active()
        titles = [i.title for i in items]
        self.assertNotIn("Ancient", titles)
