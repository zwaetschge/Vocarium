"""Contract tests for the Whisper transcription path.

Qwen-ASR is gone, and with it the `<asr_text>` prefix parsing and the manual
word-grouping helpers this file used to cover. faster-whisper returns text,
language and word/segment timestamps in one response, so what is worth pinning
now is the normalisation `main` applies on top of that response and the way the
route reports an unreachable engine.
"""

import asyncio
import os
import unittest
from unittest.mock import patch


os.environ.setdefault("WHISPER_URL", "http://whisper-stt:8000")

import main


class _FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self):
        return self._payload

    async def read(self):
        return b"boom"


class _FakeSession:
    def __init__(self, response):
        self._response = response
        self.calls: list[str] = []

    def post(self, url, **kwargs):
        self.calls.append(url)
        return self._response


def _run(coro):
    return asyncio.run(coro)


class WhisperTranscriptionTest(unittest.TestCase):
    def test_whisper_response_is_normalized_to_the_api_shape(self):
        session = _FakeSession(_FakeResponse({
            "text": "Guten Morgen. Willkommen bei Vocarium.",
            "language": "de",
            "words": [
                {"word": "Guten", "start": 0.0, "end": 0.6},
                # A word without timings must not blow up the mapping.
                {"word": "Morgen."},
            ],
            "segments": [{"start": 0.0, "end": 2.2, "text": "Guten Morgen."}],
        }))

        with patch.object(main, "_http_session", lambda: session):
            result = _run(main._transcribe_wav_whisper(b"RIFF"))

        self.assertEqual(result["text"], "Guten Morgen. Willkommen bei Vocarium.")
        self.assertEqual(result["language"], "de")
        self.assertEqual(result["words"], [
            {"word": "Guten", "start": 0.0, "end": 0.6},
            {"word": "Morgen.", "start": 0.0, "end": 0.0},
        ])
        self.assertEqual(result["segments"][0]["text"], "Guten Morgen.")
        self.assertTrue(session.calls[0].endswith("/v1/audio/transcriptions"))

    def test_language_defaults_to_german_when_whisper_omits_it(self):
        session = _FakeSession(_FakeResponse({"text": "Hallo"}))

        with patch.object(main, "_http_session", lambda: session):
            result = _run(main._transcribe_wav_whisper(b"RIFF"))

        self.assertEqual(result["language"], "de")
        self.assertEqual(result["words"], [])
        self.assertEqual(result["segments"], [])

    def test_whisper_http_error_surfaces_the_status(self):
        session = _FakeSession(_FakeResponse({}, status=500))

        with patch.object(main, "_http_session", lambda: session):
            with self.assertRaises(RuntimeError) as ctx:
                _run(main._transcribe_wav_whisper(b"RIFF"))

        self.assertIn("500", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
