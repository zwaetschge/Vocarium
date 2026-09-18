"""The embedding backend is a local llama.cpp server (bge-m3, CPU).

``EMBEDDING_API_URL`` is read by two modules with different conventions:
``audiobooks.semantics`` appends ``/embeddings`` to it, while
``podcast.embedding_client`` POSTs to it directly. Both spellings therefore have
to end up at the same endpoint -- otherwise one of the two 404s silently.
"""

import importlib
import os
import unittest
from unittest.mock import patch

from podcast.embedding_client import _default_config, _embeddings_endpoint

DEFAULT_URL = "http://embeddings:8080/v1"


class EmbeddingsEndpointTest(unittest.TestCase):
    def test_base_url_gets_the_path_appended(self):
        self.assertEqual(
            _embeddings_endpoint(DEFAULT_URL), "http://embeddings:8080/v1/embeddings"
        )

    def test_full_endpoint_is_left_alone(self):
        full = "http://embeddings:8080/v1/embeddings"
        self.assertEqual(_embeddings_endpoint(full), full)

    def test_trailing_slash_and_whitespace_are_ignored(self):
        self.assertEqual(
            _embeddings_endpoint("  http://embeddings:8080/v1/  "),
            "http://embeddings:8080/v1/embeddings",
        )

    def test_empty_stays_empty(self):
        """An unset URL must not become the string "/embeddings"."""
        self.assertEqual(_embeddings_endpoint(""), "")


class DefaultConfigTest(unittest.TestCase):
    def test_unset_env_falls_back_to_the_local_service(self):
        with patch.dict(os.environ, {"EMBEDDING_API_URL": "", "EMBEDDING_MODEL": ""}):
            cfg = _default_config()
        self.assertEqual(cfg.base_url, "http://embeddings:8080/v1/embeddings")
        self.assertEqual(cfg.model, "bge-m3")

    def test_dimensions_match_bge_m3(self):
        """bge-m3 is 1024-dim -- the same shape the retired backend produced, so
        embedding caches written back then stay valid."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("EMBEDDING_DIMENSIONS", None)
            self.assertEqual(_default_config().dimensions, 1024)


class SemanticsDefaultTest(unittest.TestCase):
    def test_semantics_defaults_to_the_same_service(self):
        with patch.dict(os.environ, {"EMBEDDING_API_URL": "", "EMBEDDING_MODEL": ""}):
            import audiobooks.semantics as semantics

            importlib.reload(semantics)
            self.assertEqual(semantics.EMBED_URL, DEFAULT_URL)
            self.assertEqual(semantics.EMBED_MODEL, "bge-m3")


if __name__ == "__main__":
    unittest.main()
