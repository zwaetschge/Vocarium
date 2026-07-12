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


class MMAudioCachePolicyTest(unittest.TestCase):
    def test_verified_weight_marker_avoids_rehashing_unchanged_files(self):
        source = (REPO_ROOT / "mmaudio" / "server.py").read_text(encoding="utf-8")
        self.assertIn("def _is_verified(", source)
        self.assertIn('data.get("mtime_ns") == stat.st_mtime_ns', source)
        self.assertIn("if _verify_and_mark(path, expected_md5):", source)


class WorkerInitPolicyTest(unittest.TestCase):
    def test_gpu_workers_run_behind_an_init_subreaper(self):
        compose = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        for service in ("qwen3-asr", "qwen3-tts", "qwen3-tts-2", "acestep", "mmaudio"):
            block = re.search(
                rf"^  {re.escape(service)}:\n(?P<body>(?:^    .*\n|^\s*$)*)",
                compose,
                re.MULTILINE,
            )
            self.assertIsNotNone(block, service)
            assert block is not None
            self.assertIn("    init: true\n", block.group("body"), service)


class TTSMetadataCacheTest(unittest.TestCase):
    def test_generation_hotpaths_use_indexed_voice_metadata(self):
        source = (REPO_ROOT / "qwen3-tts" / "server.py").read_text(encoding="utf-8")
        self.assertIn("voice_metadata: dict[str, dict]", source)
        self.assertIn('language = voice_metadata.get(voice_id, {}).get("language", "English")', source)
        create_speech = source[source.index("async def create_speech("):source.index("async def create_speech_stream(")]
        self.assertNotIn("read_text", create_speech)
