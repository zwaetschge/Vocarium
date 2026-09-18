import os
import unittest
from unittest.mock import AsyncMock, patch


os.environ.setdefault("KIKIRI_TTS_URL", "http://kikiri-tts:8881")

import main


def _kikiri(name: str, group: str = "kikiri", backend: str = "kokoro") -> dict[str, str]:
    return {"name": name, "group": group, "gender": "neutral", "backend": backend, "notes": ""}


KIKIRI_MODELS = {
    "kikiri-bobiverse": _kikiri("kikiri-bobiverse"),
    "david": _kikiri("David Nathan"),
    # The Piper voice bank is the OmniVoice fallback; routing must treat it like
    # any other Kikiri model.
    "fallback-de-thorsten": _kikiri("Thorsten", group="fallback", backend="piper"),
}

# VibeVoice was retired together with Qwen; `main` still carries the branch but
# no service backs it, so the harness leaves the listing empty.
VIBEVOICE_MODELS: dict[str, str] = {}

OMNIVOICE_MODELS = {
    "David": "David",
    "Simon": "Simon",
}


class patch_models:
    """Patch the engine listings; kikiri overridable, the rest default."""

    def __init__(self, models=None, vibevoice=None):
        self._kikiri = patch.object(
            main, "_kikiri_models",
            AsyncMock(return_value=KIKIRI_MODELS if models is None else models),
        )
        self._vibevoice = patch.object(
            main, "_vibevoice_models",
            AsyncMock(return_value=VIBEVOICE_MODELS if vibevoice is None else vibevoice),
        )
        self._omnivoice = patch.object(
            main, "_omnivoice_models", AsyncMock(return_value=OMNIVOICE_MODELS),
        )

    def __enter__(self):
        self._kikiri.__enter__()
        self._vibevoice.__enter__()
        self._omnivoice.__enter__()
        return self

    def __exit__(self, *exc):
        self._omnivoice.__exit__(*exc)
        self._vibevoice.__exit__(*exc)
        self._kikiri.__exit__(*exc)
        return False


class KikiriRoutingTest(unittest.IsolatedAsyncioTestCase):
    async def test_voice_naming_a_finetune_routes_to_the_cpu_backend(self):
        with patch_models():
            self.assertEqual(
                await main._select_tts_backend(None, None, voice_id="david"),
                (main.KIKIRI_TTS_URL, "kikiri", "david"),
            )

    async def test_explicit_engine_still_wins(self):
        with patch_models():
            self.assertEqual(
                await main._select_tts_backend("kikiri-bobiverse", "kikiri"),
                (main.KIKIRI_TTS_URL, "kikiri", "kikiri-bobiverse"),
            )
            self.assertEqual(
                await main._select_tts_backend(None, "omnivoice", voice_id="David"),
                (main.OMNIVOICE_TTS_URL, "omnivoice", "David"),
            )

    async def test_the_fallback_bank_routes_like_any_other_kikiri_voice(self):
        """The Piper bank is only there for when OmniVoice cannot run — but it is
        reached through the same CPU backend, not a separate engine."""
        with patch_models():
            self.assertEqual(
                await main._select_tts_backend(None, None, voice_id="fallback-de-thorsten"),
                (main.KIKIRI_TTS_URL, "kikiri", "fallback-de-thorsten"),
            )

    async def test_requests_no_live_engine_can_serve_say_so(self):
        """Qwen used to absorb these. It is retired, so they must 503 loudly
        rather than post to an empty URL."""
        with patch_models():
            for kwargs in (
                # A cloned voice has no Kikiri or OmniVoice counterpart.
                {"voice_id": "my-clone", "voice_source": "clone"},
                # Kikiri and OmniVoice emit WAV only.
                {"voice_id": "david", "response_format": "mp3"},
                # A custom voice must not be served by a fine-tune with the same id.
                {"voice_id": "david", "voice_source": "custom"},
            ):
                with self.subTest(**kwargs):
                    with self.assertRaises(main.HTTPException) as ctx:
                        await main._select_tts_backend(None, None, **kwargs)
                    self.assertEqual(ctx.exception.status_code, 503)

    async def test_unreachable_kikiri_does_not_silently_serve_another_voice(self):
        with patch_models({}):
            with self.assertRaises(main.HTTPException) as ctx:
                await main._select_tts_backend(None, None, voice_id="david")
            self.assertEqual(ctx.exception.status_code, 503)

    async def test_forcing_kikiri_for_an_unsupported_request_is_an_error(self):
        with patch_models():
            with self.assertRaises(main.HTTPException):
                await main._select_tts_backend(
                    None, "kikiri", voice_id="david", response_format="mp3"
                )
            with self.assertRaises(main.HTTPException):
                await main._select_tts_backend(None, "kikiri", voice_id="unknown-voice")

    async def test_omnivoice_voice_routes_to_gpu1_engine(self):
        with patch_models():
            self.assertEqual(
                await main._select_tts_backend(None, None, voice_id="David"),
                (main.OMNIVOICE_TTS_URL, "omnivoice", "David"),
            )
            self.assertEqual(
                await main._select_tts_backend(None, "omnivoice", voice_id="Simon"),
                (main.OMNIVOICE_TTS_URL, "omnivoice", "Simon"),
            )
            # Exact ids only: the Kikiri fine-tune "david" and the OmniVoice
            # voice "David" are different voices and must not cross over.
            self.assertEqual(
                (await main._select_tts_backend(None, None, voice_id="david"))[1],
                "kikiri",
            )

    async def test_the_builtin_default_voice_lands_on_the_main_engine(self):
        """OmniVoice is the main engine; Kikiri only catches "default" when
        OmniVoice has no voices at all."""
        with patch_models():
            url, engine, _ = await main._select_tts_backend(None, None, voice_id="default")
            self.assertEqual((url, engine), (main.OMNIVOICE_TTS_URL, "omnivoice"))

        with patch.object(main, "_kikiri_models", AsyncMock(return_value=KIKIRI_MODELS)), \
             patch.object(main, "_vibevoice_models", AsyncMock(return_value={})), \
             patch.object(main, "_omnivoice_models", AsyncMock(return_value={})):
            url, engine, _ = await main._select_tts_backend(None, None, voice_id="default")
            self.assertEqual((url, engine), (main.KIKIRI_TTS_URL, "kikiri"))

    async def test_cpu_backend_bypasses_gpu_queue(self):
        calls = []

        async def work(url):
            calls.append(url)
            return "cpu-result"

        with patch.object(
            main.gpu_queue,
            "submit",
            new=AsyncMock(side_effect=AssertionError("GPU queue must not be called")),
        ):
            result = await main._run_tts_job(
                "CPU smoke",
                work,
                tts_url=main.KIKIRI_TTS_URL,
                engine="kikiri",
            )

        self.assertEqual(result, "cpu-result")
        self.assertEqual(calls, [main.KIKIRI_TTS_URL])

    async def test_openai_model_listing_advertises_every_finetune(self):
        with patch_models():
            response = await main.openai_models()
        ids = {model["id"] for model in response["data"]}
        self.assertIn("kikiri-bobiverse", ids)
        self.assertIn("david", ids)


if __name__ == "__main__":
    unittest.main()
