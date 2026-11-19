import ast
import os
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from PIL import Image

from type1_ocr import gemini_solver


class _Response:
    def __init__(self, payload):
        import json
        self.body = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self.body


class GeminiSolverTests(unittest.TestCase):
    def setUp(self):
        self.image = Image.new("RGB", (30, 20), "white")

    def test_groq_vision_is_first_provider(self):
        response = _Response({"choices": [{"message": {"content": "answer: 12345"}}]})
        with patch.dict(os.environ, {"GROQ_API_KEY": "groq-key"}, clear=True), patch.object(
            gemini_solver.urllib.request, "urlopen", return_value=response
        ) as urlopen:
            self.assertEqual(gemini_solver.solve_with_gemini_flash(self.image), "12345")
        request = urlopen.call_args.args[0]
        self.assertIn("api.groq.com", request.full_url)
        self.assertIn("Bearer groq-key", request.headers.get("Authorization"))

    def test_gemini_flash_lite_then_2_fallback(self):
        bad = HTTPError("https://example", 400, "bad model", {}, None)
        good = _Response({"candidates": [{"content": {"parts": [{"text": "67890"}]}}]})
        with patch.dict(os.environ, {"GEMINI_API_KEY": "gemini-key"}, clear=True), patch.object(
            gemini_solver.urllib.request, "urlopen", side_effect=[bad, good]
        ) as urlopen:
            self.assertEqual(gemini_solver.solve_with_gemini_flash(self.image), "67890")
        self.assertEqual(urlopen.call_count, 2)
        self.assertIn("gemini-2.0-flash", urlopen.call_args.args[0].full_url)

    def test_http_errors_timeout_and_invalid_output_return_none(self):
        timeout = TimeoutError("timeout")
        with patch.dict(os.environ, {"GROQ_API_KEY": "key"}, clear=True), patch.object(
            gemini_solver.urllib.request, "urlopen", side_effect=timeout
        ):
            self.assertIsNone(gemini_solver.solve_with_gemini_flash(self.image))
        bad = _Response({"choices": [{"message": {"content": "not five digits"}}]})
        with patch.dict(os.environ, {"GROQ_API_KEY": "key"}, clear=True), patch.object(
            gemini_solver.urllib.request, "urlopen", return_value=bad
        ):
            self.assertIsNone(gemini_solver.solve_with_gemini_flash(self.image))

    def test_disable_flags_short_circuit_without_network(self):
        with patch.dict(os.environ, {"TYPE1_DISABLE_CLOUD": "1"}, clear=True), patch.object(
            gemini_solver.urllib.request, "urlopen"
        ) as urlopen:
            self.assertIsNone(gemini_solver.solve_with_gemini_flash(self.image))
            urlopen.assert_not_called()

    def test_solver_has_no_internal_type1_ocr_imports(self):
        path = Path(gemini_solver.__file__)
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            self.assertFalse(any(name.startswith("type1_ocr") for name in names))


if __name__ == "__main__":
    unittest.main()
