import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

class BrainBotProtocolTests(unittest.TestCase):
    def test_validation_rejects_non_owner(self):
        from brain_bot.protocol import validate_request
        ok, reason = validate_request({"action": "restart", "target": "second_bot", "requester_id": 7}, expected_owner_id=8)
        self.assertFalse(ok)
        self.assertIn("owner", reason)

    def test_enqueue_claim_finish_uses_directories(self):
        import brain_bot.protocol as protocol
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            with patch.object(protocol,"REQUESTS_DIR",root/"requests"), patch.object(protocol,"INBOX_DIR",root/"requests"/"inbox"), patch.object(protocol,"PROCESSING_DIR",root/"requests"/"processing"), patch.object(protocol,"DONE_DIR",root/"requests"/"done"), patch.object(protocol,"FAILED_DIR",root/"requests"/"failed"), patch.object(protocol,"STATE_DIR",root/"state"), patch.object(protocol,"STATUS_PATH",root/"state"/"status.json"), patch.object(protocol,"LOCK_PATH",root/"requests"/".lock"):
                request=protocol.enqueue("start","second_bot",8)
                self.assertTrue((root/"requests"/"inbox"/(request["id"]+".json")).exists())
                claimed=protocol.claim_next(); self.assertEqual(claimed["id"],request["id"])
                protocol.finish(request["id"],status="done")
                self.assertTrue((root/"requests"/"done"/(request["id"]+".json")).exists())
                for action in ("start", "restart", "shutdown"):
                    queued = protocol.enqueue(action, "primary", 8)
                    self.assertTrue((root/"requests"/"inbox"/(queued["id"]+".json")).exists())
                self.assertEqual(len(list((root/"requests"/"inbox").glob("*.json"))), 3)

    def test_concurrent_requests_never_overwrite(self):
        import brain_bot.protocol as protocol
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            patches=[patch.object(protocol,"REQUESTS_DIR",root/"requests"),patch.object(protocol,"INBOX_DIR",root/"requests"/"inbox"),patch.object(protocol,"PROCESSING_DIR",root/"requests"/"processing"),patch.object(protocol,"DONE_DIR",root/"requests"/"done"),patch.object(protocol,"FAILED_DIR",root/"requests"/"failed"),patch.object(protocol,"STATE_DIR",root/"state"),patch.object(protocol,"STATUS_PATH",root/"state"/"status.json"),patch.object(protocol,"LOCK_PATH",root/"requests"/".lock")]
            for item in patches: item.start()
            try:
                with ThreadPoolExecutor(max_workers=8) as pool:
                    requests=list(pool.map(lambda i: protocol.enqueue("start","primary",42,source=str(i)),range(24)))
                self.assertEqual(len({r["id"] for r in requests}),24)
                files=list((root/"requests"/"inbox").glob("*.json"))
                self.assertEqual(len(files),24)
                self.assertEqual({json.loads(f.read_text())["source"] for f in files},{str(i) for i in range(24)})
            finally:
                for item in reversed(patches): item.stop()

    def test_connection_status_is_atomic_json(self):
        import brain_bot.protocol as protocol
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            with patch.object(protocol,"REQUESTS_DIR",root/"requests"),patch.object(protocol,"STATE_DIR",root/"state"),patch.object(protocol,"STATUS_PATH",root/"state"/"status.json"),patch.object(protocol,"LOCK_PATH",root/"requests"/".lock"):
                protocol.write_connection("primary","connected",user_id=10)
                status=protocol.read_status()
                self.assertEqual(status["connections"]["primary"]["state"],"connected")
                self.assertEqual(status["connections"]["primary"]["user_id"],10)
                self.assertIsInstance(json.loads((root/"state"/"status.json").read_text()),dict)

    def test_primary_manual_self_controls_removed_but_auto_delivery_retained(self):
        root=Path(__file__).resolve().parent.parent
        primary=(root/"main.py").read_text(encoding="utf-8")
        economy=(root/"cogs"/"economy.py").read_text(encoding="utf-8")
        self.assertNotIn("queue_self_command",primary)
        self.assertNotIn("control_second_bot",primary)
        self.assertNotIn('@bot.command(name="self"',primary)
        self.assertIn("start_second_bot_for_delivery",economy)
        self.assertIn("build_handoff_message",economy)

    def test_second_bot_required_dollar_commands_remain_registered(self):
        source=(Path(__file__).resolve().parent.parent/"second_bot"/"main.py").read_text(encoding="utf-8")
        for name in ("say","ping","numbers","tc","ta","tr","wallet","set_wallet","guilds","invite_link","join_guild","leave_guild","auto_delivery","change_model"):
            self.assertIn(f'name="{name}"',source)
        self.assertIn('COMMAND_PREFIX = "$"',source)
        self.assertIn("process_pending_currency_confirmation(message)",source)
        self.assertIn("process_auto_delivery_handoff(message)",source)

    def test_brain_bot_uses_standard_discord_py_and_separate_token_setting(self):
        root=Path(__file__).resolve().parent.parent
        requirements=(root/"brain_bot"/"requirements.txt").read_text(encoding="utf-8").lower()
        source=(root/"brain_bot"/"main.py").read_text(encoding="utf-8")
        self.assertIn("discord.py",requirements)
        self.assertNotIn("discord.py-self",requirements)
        self.assertIn('os.getenv("BRAIN_BOT_TOKEN"',source)
        self.assertIn("must be unique",source)

    def test_supervisor_starts_all_three_and_has_request_lifecycle(self):
        script=(Path(__file__).resolve().parent.parent/"run_bots.sh").read_text(encoding="utf-8")
        for name in ("primary","second_bot","brain_bot"):
            self.assertIn(f"launch_bot \"{name}\"",script)
        for folder in ("inbox","processing","done","failed"):
            self.assertIn(folder,script)
        self.assertIn("mv -f \"$request_file\" \"$processing_file\"",script)

if __name__ == "__main__": unittest.main()
