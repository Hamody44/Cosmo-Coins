import unittest
from types import SimpleNamespace
from unittest.mock import patch

from cogs.admin import can_configure_guild_management_roles, can_manage_bot
from cogs.games import can_manage_server_games


def make_context(author_id=200, owner_id=100, *, administrator=False, manage_guild=False, roles=(), role_map=None):
    author = SimpleNamespace(
        id=author_id,
        guild_permissions=SimpleNamespace(administrator=administrator, manage_guild=manage_guild),
        roles=list(roles),
    )
    guild = SimpleNamespace(
        id=111,
        owner_id=owner_id,
        get_role=lambda role_id: (role_map or {}).get(role_id),
    )
    return SimpleNamespace(author=author, guild=guild)


class ServerOwnerAccessTests(unittest.TestCase):
    def test_discord_admin_and_manage_guild_can_configure_management_roles(self):
        with patch("cogs.admin.get_guild_management_role_ids", return_value=(None, None)), \
             patch("cogs.admin.is_bot_admin", return_value=False):
            self.assertTrue(can_configure_guild_management_roles(
                make_context(administrator=True)
            ))
            self.assertTrue(can_configure_guild_management_roles(
                make_context(manage_guild=True)
            ))

    def test_limited_admin_role_cannot_configure_management_roles(self):
        limited_admin_role = object()
        ctx = make_context(roles=[limited_admin_role], role_map={502: limited_admin_role})
        with patch("cogs.admin.get_guild_management_role_ids", return_value=(501, 502)), \
             patch("cogs.admin.is_bot_admin", return_value=False):
            self.assertFalse(can_configure_guild_management_roles(ctx))

    @patch("cogs.games.is_bot_admin", return_value=False)
    def test_guild_owner_can_start_giveaway_box_and_roulette_without_premium(self, _premium_admin):
        ctx = make_context(author_id=100, owner_id=100)
        self.assertTrue(can_manage_server_games(ctx))

    @patch("cogs.games.is_bot_admin", return_value=False)
    def test_regular_member_cannot_start_reward_games_without_admin_access(self, _premium_admin):
        ctx = make_context(author_id=200, owner_id=100)
        self.assertFalse(can_manage_server_games(ctx))

    @patch("cogs.games.is_bot_admin", return_value=True)
    def test_existing_bot_admin_access_is_preserved(self, _premium_admin):
        ctx = make_context(author_id=200, owner_id=100)
        self.assertTrue(can_manage_server_games(ctx))

    @patch("cogs.admin.is_bot_admin", return_value=False)
    def test_server_owner_can_charge_or_deduct_balances_without_premium(self, _premium_admin):
        ctx = make_context(author_id=100, owner_id=100)
        self.assertTrue(can_manage_bot(ctx))

    @patch("cogs.admin.get_guild_management_role_ids", return_value=(None, None))
    @patch("cogs.admin.is_bot_admin", return_value=False)
    def test_other_members_do_not_gain_charge_or_deduct_access(self, _premium_admin, _roles):
        ctx = make_context(author_id=200, owner_id=100)
        self.assertFalse(can_manage_bot(ctx))

    @patch("cogs.admin.get_guild_management_role_ids", return_value=(501, None))
    @patch("cogs.admin.is_bot_admin", return_value=False)
    def test_owner_role_can_manage_bot_commands(self, _premium_admin, _roles):
        owner_role = object()
        ctx = make_context(author_id=200, owner_id=100, roles=[owner_role], role_map={501: owner_role})
        self.assertTrue(can_manage_bot(ctx))

    @patch("cogs.admin.get_guild_management_role_ids", return_value=(501, 502))
    @patch("cogs.admin.is_bot_admin", return_value=False)
    def test_limited_admin_role_does_not_gain_server_management_access(self, _premium_admin, _roles):
        admin_role = object()
        ctx = make_context(author_id=200, owner_id=100, roles=[admin_role], role_map={502: admin_role})
        self.assertFalse(can_manage_bot(ctx))


if __name__ == "__main__":
    unittest.main()
