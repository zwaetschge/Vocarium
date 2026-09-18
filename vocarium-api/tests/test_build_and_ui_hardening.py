import unittest
import re
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

    def test_ui_dependencies_and_build_outputs_are_excluded(self):
        patterns = self._patterns("vocarium-ui")
        self.assertTrue({"node_modules/", "dist/", "test-results/"} <= patterns)

