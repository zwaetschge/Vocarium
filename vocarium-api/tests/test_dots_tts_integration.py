import ast
import os
import sys
import unittest
from pathlib import Path


API_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = API_ROOT.parent
sys.path.insert(0, str(API_ROOT))


class DotsTtsIntegrationContractTest(unittest.TestCase):
    def setUp(self):
        self._old_env = os.environ.copy()

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._old_env)

    def test_dots_is_a_distinct_gpu0_queue_service(self):
        import gpu_queue

        os.environ.update(
            {
                "GPU_TTS_PRIMARY": "0",
                "GPU_DOTS_TTS": "0",
                "GPU_ASR": "0",
                "GPU_MUSIC": "0",
                "GPU_SFX": "0",
                "DOTS_TTS_URL": "http://dots-tts:8890",
            }
        )

        self.assertEqual(gpu_queue._service_gpus()["dots"], "0")
        self.assertEqual(gpu_queue._service_container_names("dots"), {"dots-tts"})
        self.assertEqual(gpu_queue._coexisting_services("dots"), {"dots"})

    def test_api_routes_explicit_dots_engine_to_dots_worker(self):
        source = (API_ROOT / "main.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        selector = next(
            node for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "_select_tts_backend"
        )
        selector_source = ast.get_source_segment(source, selector) or ""

        self.assertIn('SUPPORTED_TTS_ENGINES = {"qwen", "dots"}', source)
        self.assertIn('return DOTS_TTS_URL, "dots"', selector_source)
        self.assertIn('source != "clone"', source)
        self.assertIn('service_type="dots"', source)

    def test_compose_mounts_shared_clone_voices_into_lazy_dots_worker(self):
        compose = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        dots_block = compose.split("  dots-tts:", 1)[1].split("  # ---", 1)[0]

        self.assertIn("NVIDIA_VISIBLE_DEVICES=${GPU_DOTS_TTS:-0}", dots_block)
        self.assertIn("voices-data:/app/voices:ro", dots_block)
        self.assertIn("IDLE_TIMEOUT=${DOTS_TTS_IDLE_TIMEOUT:-120}", dots_block)
        self.assertIn("dots-studio/dots.tts-soar", dots_block)

    def test_worker_uses_existing_clone_audio_and_transcript(self):
        source = (REPO_ROOT / "dots-tts" / "server.py").read_text(encoding="utf-8")

        self.assertIn('voice_dir / "ref_audio.wav"', source)
        self.assertIn('voice_dir / "metadata.json"', source)
        self.assertIn('metadata.get("ref_text")', source)
        self.assertIn('@app.post("/unload")', source)
        self.assertIn('asyncio.to_thread', source)

    def test_speech_ui_exposes_both_engines_and_sends_selection(self):
        source = (REPO_ROOT / "vocarium-ui" / "src" / "pages" / "SpeechPage.tsx").read_text(
            encoding="utf-8"
        )

        self.assertIn("Qwen3-TTS", source)
        self.assertIn("dots.tts", source)
        self.assertIn("setSelectedEngine", source)
        self.assertIn("engine: selectedEngine", source)
        self.assertIn("Only cloned voices", source)


if __name__ == "__main__":
    unittest.main()
