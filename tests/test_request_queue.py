import json
import multiprocessing
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import second_bot.command_queue as command_queue


def _enqueue_from_process(queue_path, lock_path, marker):
    command_queue.QUEUE_PATH = Path(queue_path)
    command_queue.LOCK_PATH = Path(lock_path)
    return command_queue.enqueue_command({"marker": marker})


class RequestQueueTests(unittest.TestCase):
    def test_concurrent_process_enqueues_preserve_all_commands(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            queue_path = root / "command_queue.json"
            lock_path = root / "command_queue.lock"
            context = multiprocessing.get_context("fork")
            with patch.object(command_queue, "QUEUE_PATH", queue_path), patch.object(command_queue, "LOCK_PATH", lock_path):
                with context.Pool(processes=4) as pool:
                    ids = pool.starmap(
                        _enqueue_from_process,
                        [(queue_path, lock_path, str(index)) for index in range(32)],
                    )

            self.assertEqual(len(ids), len(set(ids)))
            payload = json.loads(queue_path.read_text(encoding="utf-8"))
            self.assertEqual(len(payload), 32)
            self.assertEqual({item["marker"] for item in payload}, {str(index) for index in range(32)})
            self.assertTrue(all(item["status"] == "pending" for item in payload))

    def test_queue_write_failure_releases_lock_and_cleans_temporary_file(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            queue_path = root / "command_queue.json"
            lock_path = root / "command_queue.lock"
            with patch.object(command_queue, "QUEUE_PATH", queue_path), patch.object(command_queue, "LOCK_PATH", lock_path):
                with patch.object(command_queue.os, "replace", side_effect=OSError("simulated replace failure")):
                    with self.assertRaises(OSError):
                        command_queue.enqueue_command({"marker": "failed"})
                self.assertFalse(list(root.glob(".command_queue.json.*.tmp")))
                command_queue.enqueue_command({"marker": "succeeds"})

            payload = json.loads(queue_path.read_text(encoding="utf-8"))
            self.assertEqual([item["marker"] for item in payload], ["succeeds"])

    def test_missing_interprocess_lock_support_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with patch.object(command_queue, "QUEUE_PATH", root / "command_queue.json"), patch.object(command_queue, "LOCK_PATH", root / "command_queue.lock"), patch.object(command_queue, "fcntl", None):
                with self.assertRaisesRegex(RuntimeError, "inter-process command queue locking"):
                    command_queue.enqueue_command({"marker": "rejected"})

    def test_claim_and_finish_keep_queue_json_valid(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            queue_path = root / "command_queue.json"
            lock_path = root / "command_queue.lock"
            with patch.object(command_queue, "QUEUE_PATH", queue_path), patch.object(command_queue, "LOCK_PATH", lock_path):
                request_id = command_queue.enqueue_command({"action": "start"})
                claimed = command_queue.claim_next_command()
                self.assertEqual(claimed["id"], request_id)
                self.assertEqual(claimed["status"], "processing")
                command_queue.finish_command(request_id, status="completed")
                payload = json.loads(queue_path.read_text(encoding="utf-8"))

            self.assertEqual(payload[0]["status"], "completed")
            self.assertIsInstance(payload, list)


if __name__ == "__main__":
    unittest.main()
