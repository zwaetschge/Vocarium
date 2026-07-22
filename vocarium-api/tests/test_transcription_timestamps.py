import ast
import re
import unittest
from pathlib import Path


API_MAIN = Path(__file__).resolve().parents[1] / "main.py"


def _load_timestamp_helpers() -> dict:
    source = API_MAIN.read_text(encoding="utf-8")
    tree = ast.parse(source)
    names = {
        "_clean_asr_text",
        "_parse_asr_result",
        "_normalize_timestamp_words",
        "_join_timestamp_token",
        "_group_timestamp_words",
    }
    nodes = [
        node for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names
    ]
    namespace = {"re": re}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(API_MAIN), "exec"), namespace)
    return namespace


class TranscriptionTimestampTest(unittest.TestCase):
    def test_qwen_language_prefix_is_parsed_and_removed(self):
        helpers = _load_timestamp_helpers()

        language, text = helpers["_parse_asr_result"](
            "language German<asr_text>Guten Morgen.</asr_text><|endoftext|>"
        )

        self.assertEqual(language, "German")
        self.assertEqual(text, "Guten Morgen.")


    def test_word_timestamps_are_normalized_and_grouped_into_phrases(self):
        helpers = _load_timestamp_helpers()
        words = helpers["_normalize_timestamp_words"]([
            {"text": "Guten", "start": 0, "end": 0.6},
            {"text": "Morgen.", "start": 0.7, "end": 2.2},
            {"text": "Willkommen", "start": 2.5, "end": 3.4},
            {"text": "bei", "start": 3.5, "end": 3.8},
            {"text": "Vocarium.", "start": 3.9, "end": 5.2},
        ])

        self.assertEqual(helpers["_group_timestamp_words"](words), [
            {"start": 0.0, "end": 2.2, "text": "Guten Morgen."},
            {"start": 2.5, "end": 5.2, "text": "Willkommen bei Vocarium."},
        ])


if __name__ == "__main__":
    unittest.main()
