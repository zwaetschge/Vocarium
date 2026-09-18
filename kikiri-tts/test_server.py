import asyncio
import io
import json
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

import numpy as np
import torch
from fastapi import HTTPException

import server


class FakePipeline:
    def __call__(self, text, *, voice, speed):
        del text, voice, speed
        yield "Hallo", "hˈaloː", np.zeros(2_400, dtype=np.float32)


def write_model_dir(root: Path, name: str, display_name: str | None = None) -> Path:
    directory = root / name
    directory.mkdir(parents=True)
    (directory / "model.pth").write_bytes(b"weights")
    (directory / "default.pt").write_bytes(b"voicepack")
    (directory / "config.json").write_text("{}", encoding="utf-8")
    if display_name:
        (directory / "meta.json").write_text(
            json.dumps({"display_name": display_name}), encoding="utf-8"
        )
    return directory


class RegistryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self._models_dir = server.MODELS_DIR
        self._legacy = server.LEGACY_MODEL_PATH
        server.MODELS_DIR = self.root
        # Keep the legacy flat layout out of the way unless a test opts in.
        server.LEGACY_MODEL_PATH = self.root / "does-not-exist.pth"
        server.registry = {}
        server.loaded.clear()

    def tearDown(self):
        server.MODELS_DIR = self._models_dir
        server.LEGACY_MODEL_PATH = self._legacy
        server.registry = {}
        server.loaded.clear()
        self.tmp.cleanup()

    def test_discovers_one_entry_per_complete_model_directory(self):
        write_model_dir(self.root, "kikiri-bobiverse")
        write_model_dir(self.root, "david", display_name="David Nathan")
        (self.root / "half-written").mkdir()
        (self.root / "half-written" / "model.pth").write_bytes(b"weights")

        entries = server.refresh_registry()

        self.assertEqual(sorted(entries), ["david", "kikiri-bobiverse"])
        self.assertEqual(entries["david"].display_name, "David Nathan")
        self.assertNotIn("half-written", entries)

    def test_dropped_model_directory_is_unloaded(self):
        write_model_dir(self.root, "david")
        server.refresh_registry()
        server.loaded["david"] = server.LoadedModel(
            pipeline=FakePipeline(), voice=torch.zeros((510, 1, 256))
        )

        for path in (self.root / "david").iterdir():
            path.unlink()
        (self.root / "david").rmdir()
        server.refresh_registry()

        self.assertNotIn("david", server.loaded)

    def test_eviction_keeps_only_the_recent_models_resident(self):
        write_model_dir(self.root, "one")
        write_model_dir(self.root, "two")
        write_model_dir(self.root, "three")
        server.refresh_registry()

        def fake_load(model_id: str):
            server.loaded[model_id] = server.LoadedModel(
                pipeline=FakePipeline(), voice=torch.zeros((510, 1, 256))
            )
            server.loaded.move_to_end(model_id)
            while len(server.loaded) > server.MAX_RESIDENT_MODELS:
                server.loaded.popitem(last=False)

        for model_id in ("one", "two", "three"):
            fake_load(model_id)

        self.assertLessEqual(len(server.loaded), server.MAX_RESIDENT_MODELS)
        self.assertIn("three", server.loaded)
        self.assertNotIn("one", server.loaded)


class RequestContractTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self._models_dir = server.MODELS_DIR
        self._legacy = server.LEGACY_MODEL_PATH
        server.MODELS_DIR = self.root
        server.LEGACY_MODEL_PATH = self.root / "does-not-exist.pth"
        write_model_dir(self.root, "kikiri-bobiverse")
        write_model_dir(self.root, "david", display_name="David Nathan")
        server.refresh_registry()

    def tearDown(self):
        server.MODELS_DIR = self._models_dir
        server.LEGACY_MODEL_PATH = self._legacy
        server.registry = {}
        server.loaded.clear()
        self.tmp.cleanup()

    def test_model_id_selects_the_finetune(self):
        model_id, text = server.resolve_request(
            server.SpeechRequest(input="Hallo", model="david")
        )
        self.assertEqual(model_id, "david")
        self.assertEqual(text, "Hallo")

    def test_generic_kikiri_model_falls_back_to_the_default(self):
        model_id, _ = server.resolve_request(
            server.SpeechRequest(input="Hallo", model="kikiri")
        )
        self.assertEqual(model_id, server.default_model_id())

    def test_voice_may_name_the_model_or_its_display_name(self):
        for voice in ("default", "david", "David Nathan"):
            model_id, _ = server.resolve_request(
                server.SpeechRequest(input="Hallo", model="david", voice=voice)
            )
            self.assertEqual(model_id, "david")

    def test_unknown_model_and_voice_are_rejected(self):
        with self.assertRaises(HTTPException):
            server.resolve_request(server.SpeechRequest(input="Hallo", model="nope"))
        with self.assertRaises(HTTPException):
            server.resolve_request(
                server.SpeechRequest(input="Hallo", model="david", voice="clone")
            )

    def test_non_wav_output_is_rejected(self):
        with self.assertRaises(HTTPException):
            server.resolve_request(
                server.SpeechRequest(input="Hallo", model="david", response_format="mp3")
            )

    def test_synthesize_returns_pcm16_24khz_wav(self):
        server.loaded["david"] = server.LoadedModel(
            pipeline=FakePipeline(), voice=torch.zeros((510, 1, 256))
        )
        payload, duration = server.synthesize("david", "Hallo", 1.0)

        with wave.open(io.BytesIO(payload), "rb") as wav:
            self.assertEqual(wav.getnchannels(), 1)
            self.assertEqual(wav.getsampwidth(), 2)
            self.assertEqual(wav.getframerate(), 24_000)
            self.assertEqual(wav.getnframes(), 2_400)
        self.assertAlmostEqual(duration, 0.1)

    def test_stream_emits_chunk_and_done_events(self):
        request = server.SpeechRequest(input="Hallo", model="david")
        with patch.object(server, "synthesize", return_value=(b"RIFFaudio", 1.25)):
            response = asyncio.run(server.speech_stream(request))

            async def collect() -> str:
                parts = []
                async for part in response.body_iterator:
                    parts.append(part.decode() if isinstance(part, bytes) else part)
                return "".join(parts)

            body = asyncio.run(collect())
        self.assertIn("event: chunk", body)
        self.assertIn("event: done", body)
        self.assertIn('"model": "david"', body)


if __name__ == "__main__":
    unittest.main()
