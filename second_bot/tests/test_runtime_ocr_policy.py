import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MAIN_SOURCE = (ROOT / "main.py").read_text(encoding="utf-8")
LOCAL_OCR_SOURCE = (ROOT / "local_ocr.py").read_text(encoding="utf-8")


class RuntimeOcrPolicyTests(unittest.TestCase):
    def test_second_bot_requirements_drop_numpy_and_python_dotenv(self):
        requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
        lock = (ROOT / "cosmo_second_requirements.lock").read_text(encoding="utf-8")
        for content in (requirements, lock):
            self.assertNotIn("numpy", content.lower())
            self.assertNotIn("python-dotenv", content.lower())

    def test_main_has_no_type1_local_or_dotenv_import(self):
        tree = ast.parse(MAIN_SOURCE)
        imported = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.append(node.module or "")
        self.assertFalse(any(name == "numpy" or name.startswith("numpy.") for name in imported))
        self.assertFalse(any(name == "dotenv" or name.startswith("dotenv.") for name in imported))
        self.assertFalse(any(name.endswith("type1_local_ocr") or name.endswith("type1_ocr") for name in imported))

    def test_type1_runtime_function_calls_gemini_only(self):
        tree = ast.parse(MAIN_SOURCE)
        function = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef)
                        and node.name == "extract_type1_hybrid")
        source = ast.get_source_segment(MAIN_SOURCE, function)
        self.assertIn("extract_numbers_from_image", source)
        self.assertNotIn("extract_numbers_with_ocr_space", source)
        self.assertNotIn("extract_numbers_with_veryfi", source)
        self.assertNotIn("extract_numbers_with_openrouter", source)
        self.assertNotIn("recognize_type1", source)

    def test_type2_module_is_independent_of_gemini_and_numpy(self):
        self.assertNotIn("numpy", LOCAL_OCR_SOURCE.lower())
        self.assertNotIn("gemini", LOCAL_OCR_SOURCE.lower())
        self.assertNotIn("extract_numbers_from_image", LOCAL_OCR_SOURCE)
        self.assertIn("from PIL", LOCAL_OCR_SOURCE)


if __name__ == "__main__":
    unittest.main()
