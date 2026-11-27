import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class BrainBotProtocolTests(unittest.TestCase):
    def test_validation_rejects_non_owner(self):
        from brain_bot.protocol import validate_request

        ok, reason = validate_request(
            {"action": "restart", "target": "second_bot", "requester_id": 7},
            expected_owner_id=8,
        )
        self.assertFalse(ok)
        self.assertIn("owner", reason)

    def test_enqueue_claim_finish_uses_directories(self):
        import brain_bot.protocol as protocol

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with patch.object(protocol, "REQUESTS_DIR", root / "requests"), \
                 patch.object(protocol, "INBOX_DIR", root / "requests" / "inbox"), \
                 patch.object(protocol, "PROCESSING_DIR", root / "requests" / "processing"), \
                 patch.object(protocol, "DONE_DIR", root / "requests" / "done"), \
                 patch.object(protocol, "FAILED_DIR", root / "requests" / "failed"), \
                 patch.object(protocol, "STATE_DIR", root / "state"), \
                 patch.object(protocol, "STATUS_PATH", root / "state" / "status.json"), \
                 patch.object(protocol, "LOCK_PATH", root / "requests" / ".lock"):
                request = protocol.enqueue("start", "second_bot", 8)
                self.assertTrue((root / "requests" / "inbox").joinpath(request["id"] + ".json").exists())
                claimed = protocol.claim_next()
                self.assertEqual(claimed["id"], request["id"])
                self.assertTrue((root / "requests" / "processing").joinpath(request["id"] + ".json").exists())
                protocol.finish(request["id"], status="done")
                self.assertTrue((root / "requests" / "done").joinpath(request["id"] + ".json").exists())

    def test_status_and_connection_are_atomic_json(self):
        import brain_bot.protocol as protocol

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with patch.object(protocol, "REQUESTS_DIR", root / "requests"), \
                 patch.object(protocol, "STATE_DIR", root / "state"), \
                 patch.object(protocol, "STATUS_PATH", root / "state" / "status.json"), \
                 patch.object(protocol, "LOCK_PATH", root / "requests" / ".lock"):
                protocol.write_connection("primary", "connected", user_id=10)
                status = protocol.read_status()
                self.assertEqual(status["connections"]["primary"]["state"], "connected")
                self.assertEqual(status["connections"]["primary"]["user_id"], 10)
                self.assertIsInstance(json.loads((root / "state" / "status.json").read_text()), dict)


if __name__ == "__main__":
    unittest.main()
