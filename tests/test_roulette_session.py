import unittest
from unittest.mock import Mock, patch

from cogs.games import RouletteSession


class RouletteSessionTests(unittest.TestCase):
    def test_stop_refunds_once_and_prevents_a_winner(self):
        session = RouletteSession(123, 500)
        session.current_view = Mock()
        with patch("cogs.games.refund_server_spending") as refund:
            self.assertTrue(session.stop())
            self.assertFalse(session.refund())
            self.assertFalse(session.finish())

        refund.assert_called_once_with(123, 500)
        session.current_view.stop.assert_called_once()
        self.assertTrue(session.is_stopped)
        self.assertTrue(session.closed)

    def test_finalization_prevents_later_stop_or_refund(self):
        session = RouletteSession(456, 700)
        with patch("cogs.games.refund_server_spending") as refund:
            self.assertTrue(session.finish())
            self.assertFalse(session.stop())
            self.assertFalse(session.refund())

        refund.assert_not_called()
        self.assertTrue(session.closed)
        self.assertFalse(session.is_stopped)


if __name__ == "__main__":
    unittest.main()
