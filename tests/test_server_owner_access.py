import unittest
from types import SimpleNamespace
from unittest.mock import patch

from cogs.admin import can_manage_bot
from cogs.games import can_manage_giveaway


def make_context(author_id=200, owner_id=100, *, administrator=False, manage_guild=False):
    author = SimpleNamespace(
        id=author_id,
        guild_permissions=SimpleNamespace(administrator=administrator, manage_guild=manage_guild),
    )
    guild = SimpleNamespace(owner_id=owner_id)
    return SimpleNamespace(author=author, guild=guild)


class ServerOwnerAccessTests(unittest.TestCase):
    @patch("cogs.games.is_bot_admin", return_value=False)
    def test_guild_owner_can_start_giveaway_and_box_without_premium(self, _premium_admin):
        ctx = make_context(author_id=100, owner_id=100)
        self.assertTrue(can_manage_giveaway(ctx))

    @patch("cogs.games.is_bot_admin", return_value=False)
    def test_regular_member_cannot_start_giveaway_or_box_without_admin_access(self, _premium_admin):
        ctx = make_context(author_id=200, owner_id=100)
        self.assertFalse(can_manage_giveaway(ctx))

    @patch("cogs.games.is_bot_admin", return_value=True)
    def test_existing_bot_admin_access_is_preserved(self, _premium_admin):
        ctx = make_context(author_id=200, owner_id=100)
        self.assertTrue(can_manage_giveaway(ctx))

    @patch("cogs.admin.is_bot_admin", return_value=False)
    def test_server_owner_can_charge_or_deduct_balances_without_premium(self, _premium_admin):
        ctx = make_context(author_id=100, owner_id=100)
        self.assertTrue(can_manage_bot(ctx))

    @patch("cogs.admin.is_bot_admin", return_value=False)
    def test_other_members_do_not_gain_charge_or_deduct_access(self, _premium_admin):
        ctx = make_context(author_id=200, owner_id=100)
        self.assertFalse(can_manage_bot(ctx))


if __name__ == "__main__":
    unittest.main()
