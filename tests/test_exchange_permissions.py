from types import SimpleNamespace
import unittest
from unittest.mock import patch

from cogs.economy import can_manage_exchange_request
from database import PRIMARY_OWNER_ID


class ExchangeRequestPermissionTests(unittest.TestCase):
    def context(self, author_id, guild_id=123, owner_id=1, administrator=False, manage_guild=False):
        author = SimpleNamespace(
            id=author_id,
            guild_permissions=SimpleNamespace(
                administrator=administrator,
                manage_guild=manage_guild,
            ),
        )
        guild = SimpleNamespace(id=guild_id, owner_id=owner_id)
        return SimpleNamespace(author=author, guild=guild)

    def test_non_premium_server_owner_can_process_its_request(self):
        ctx = self.context(author_id=1, owner_id=1)
        with patch("cogs.economy.is_bot_admin", return_value=False):
            self.assertTrue(can_manage_exchange_request(ctx, 123))

    def test_non_premium_server_admins_can_process_its_request(self):
        manager = self.context(author_id=2, manage_guild=True)
        administrator = self.context(author_id=3, administrator=True)
        with patch("cogs.economy.is_bot_admin", return_value=False):
            self.assertTrue(can_manage_exchange_request(manager, 123))
            self.assertTrue(can_manage_exchange_request(administrator, 123))

    def test_other_guild_members_cannot_process_request(self):
        ctx = self.context(author_id=2, guild_id=456, owner_id=1, administrator=True)
        with patch("cogs.economy.is_bot_admin", return_value=True):
            self.assertFalse(can_manage_exchange_request(ctx, 123))

    def test_primary_bot_owner_can_process_request_from_anywhere(self):
        ctx = self.context(author_id=PRIMARY_OWNER_ID, guild_id=456)
        self.assertTrue(can_manage_exchange_request(ctx, 123))

    def test_existing_premium_bot_admin_access_remains(self):
        ctx = self.context(author_id=2, owner_id=1)
        with patch("cogs.economy.is_bot_admin", return_value=True):
            self.assertTrue(can_manage_exchange_request(ctx, 123))


if __name__ == "__main__":
    unittest.main()
