import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


class DockerContextTest(unittest.TestCase):
    def _patterns(self, directory: str) -> set[str]:
        path = REPO_ROOT / directory / ".dockerignore"
        return {
            line.strip()
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }

    def test_qwen_model_cache_is_not_sent_to_docker(self):
        patterns = self._patterns("qwen3-tts")
        self.assertIn("models/", patterns)
        self.assertNotIn("ref_audio/", patterns)

    def test_ui_dependencies_and_build_outputs_are_excluded(self):
        patterns = self._patterns("vocarium-ui")
        self.assertTrue({"node_modules/", "dist/", "test-results/"} <= patterns)

    def test_python_contexts_exclude_bytecode_and_runtime_data(self):
        for directory in ("vocarium-api", "acestep", "mmaudio"):
            patterns = self._patterns(directory)
            self.assertIn("__pycache__/", patterns)
            self.assertIn("*.pyc", patterns)
