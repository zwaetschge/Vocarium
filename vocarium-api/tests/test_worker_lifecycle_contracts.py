import ast
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def source(name: str) -> str:
    return (REPO_ROOT / name).read_text(encoding="utf-8")


class TTSLifecycleContractTest(unittest.TestCase):
    def test_active_count_is_owned_by_blocking_model_wrapper(self):
        text = source("qwen3-tts/server.py")
        tree = ast.parse(text)
        wrapper = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "_run_with_model"
        )
        body = ast.get_source_segment(text, wrapper) or ""
        self.assertIn("active_requests += 1", body)
        self.assertIn("active_requests -= 1", body)
        self.assertIn("used_model_id", body)

    def test_async_load_and_unload_use_threads(self):
        text = source("qwen3-tts/server.py")
        self.assertIn(
            "await asyncio.to_thread(ensure_model, request.model_id)", text
        )
        self.assertIn("await asyncio.to_thread(_unload_if_idle)", text)


class ASRLifecycleContractTest(unittest.TestCase):
    def test_proxy_tracks_active_requests_around_http_call(self):
        text = source("asr_proxy.py")
        self.assertIn("await asyncio.to_thread(acquire_backend)", text)
        self.assertIn("await asyncio.to_thread(release_backend)", text)
        self.assertIn("active_requests == 0", text)
        self.assertIn("def _client() -> httpx.AsyncClient:", text)


class AceLifecycleContractTest(unittest.TestCase):
    def test_proxy_prevents_unload_while_requests_are_active(self):
        text = source("acestep/proxy.py")
        self.assertIn("active_requests", text)
        self.assertIn('"status": "busy"', text)
        self.assertIn("def _session() -> aiohttp.ClientSession:", text)


class MMAudioLifecycleContractTest(unittest.TestCase):
    def test_requested_steps_are_applied_before_generation(self):
        text = source("mmaudio/server.py")
        self.assertIn("fm.num_steps = req.num_steps", text)

    def test_cuda_output_is_copied_while_generation_lock_is_held(self):
        text = source("mmaudio/server.py")
        tree = ast.parse(text)
        fn = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "_generate_sfx_blocking"
        )
        with_nodes = [node for node in ast.walk(fn) if isinstance(node, ast.With)]
        lock_block = next(
            node
            for node in with_nodes
            if any(
                isinstance(item.context_expr, ast.Name)
                and item.context_expr.id == "lock"
                for item in node.items
            )
        )
        lock_text = ast.get_source_segment(text, lock_block) or ""
        self.assertIn(".cpu()", lock_text)
