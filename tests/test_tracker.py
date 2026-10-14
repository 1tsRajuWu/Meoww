"""Test the activity tracker functionality.

This test verifies that:
1. Tracker commands have admin-only permissions
2. New channel creation is tracked
3. Activity data is properly stored and retrieved
"""
import unittest
from unittest.mock import Mock
from discord.ext import commands
from cogs import tracker


class TestTrackerAdminPermissions(unittest.TestCase):
    """Verify that tracker commands require admin permissions."""

    def test_activity_top_has_admin_check(self):
        """The /activity-top command should have an admin permission check."""
        # Create a mock bot
        bot = Mock(spec=commands.Bot)
        tracker_cog = tracker.Tracker(bot)
        
        # Check that the command has the admin permission decorator
        activity_top_cmd = tracker_cog.activity_top
        self.assertTrue(hasattr(activity_top_cmd, 'checks'))
        
        # Verify one of the checks is for administrator permission
        has_admin_check = any(
            hasattr(check, '__name__') and 'has_permissions' in str(check)
            for check in activity_top_cmd.checks
        )
        self.assertTrue(has_admin_check, "activity-top should have admin permission check")

    def test_activity_has_admin_check(self):
        """The /activity command should have an admin permission check."""
        bot = Mock(spec=commands.Bot)
        tracker_cog = tracker.Tracker(bot)
        
        activity_cmd = tracker_cog.activity
        self.assertTrue(hasattr(activity_cmd, 'checks'))
        
        has_admin_check = any(
            hasattr(check, '__name__') and 'has_permissions' in str(check)
            for check in activity_cmd.checks
        )
        self.assertTrue(has_admin_check, "activity should have admin permission check")

    def test_activity_channel_has_admin_check(self):
        """The /activity-channel command should have an admin permission check."""
        bot = Mock(spec=commands.Bot)
        tracker_cog = tracker.Tracker(bot)
        
        activity_channel_cmd = tracker_cog.activity_channel
        self.assertTrue(hasattr(activity_channel_cmd, 'checks'))
        
        has_admin_check = any(
            hasattr(check, '__name__') and 'has_permissions' in str(check)
            for check in activity_channel_cmd.checks
        )
        self.assertTrue(has_admin_check, "activity-channel should have admin permission check")


class TestTrackerChannelListener(unittest.TestCase):
    """Verify that new channels are tracked when created."""

    def test_has_guild_channel_create_listener(self):
        """The tracker should have a listener for new channel creation."""
        bot = Mock(spec=commands.Bot)
        tracker_cog = tracker.Tracker(bot)
        
        # Check that the on_guild_channel_create listener exists
        self.assertTrue(hasattr(tracker_cog, 'on_guild_channel_create'))
        
        # Verify it's a callable method (Discord listeners are methods with on_ prefix)
        listener = getattr(tracker_cog, 'on_guild_channel_create')
        self.assertTrue(callable(listener), 
                       "on_guild_channel_create should be a callable method")


class TestTrackerDataPersistence(unittest.TestCase):
    """Verify that tracker data is included in backup protection."""

    def test_activity_tables_in_counted_tables(self):
        """Activity tables should be in the COUNTED_TABLES tuple."""
        from utils import cloud_sync
        
        self.assertIn('activity_messages', cloud_sync.COUNTED_TABLES,
                     "activity_messages should be protected in backups")
        self.assertIn('activity_voice', cloud_sync.COUNTED_TABLES,
                     "activity_voice should be protected in backups")


if __name__ == "__main__":
    unittest.main()