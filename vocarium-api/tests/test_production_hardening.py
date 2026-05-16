import os
import re
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


API_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = API_ROOT.parent
sys.path.insert(0, str(API_ROOT))


class DatabaseMigrationTest(unittest.TestCase):
    def test_benchmarks_are_user_scoped(self):
        import database

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "vocarium.db"
            database.init_db(db_path)
            try:
                cols = {
                    row[1]
                    for row in database.get_db()
                    .execute("PRAGMA table_info(benchmarks)")
                    .fetchall()
                }
                self.assertIn("user_id", cols)
                indexes = {
                    row[1]
                    for row in database.get_db()
                    .execute("PRAGMA index_list(benchmarks)")
                    .fetchall()
                }
                self.assertIn("idx_benchmarks_user_id", indexes)
            finally:
                database.get_db().close()
                database._db = None

    def test_existing_benchmark_tables_are_migrated(self):
        import database

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "vocarium.db"
            raw = sqlite3.connect(db_path)
            raw.execute(
                "CREATE TABLE benchmarks ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT,"
                "voice_id TEXT, model_id TEXT, text TEXT,"
                "audio_duration REAL, generation_time REAL, rtf REAL,"
                "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
            )
            raw.commit()
            raw.close()

            database.init_db(db_path)
            try:
                cols = {
                    row[1]
                    for row in database.get_db()
                    .execute("PRAGMA table_info(benchmarks)")
                    .fetchall()
                }
                self.assertIn("user_id", cols)
            finally:
                database.get_db().close()
                database._db = None


class GpuQueuePlacementTest(unittest.TestCase):
    def setUp(self):
        self._old_env = os.environ.copy()

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._old_env)

    def test_single_gpu_services_conflict(self):
        import gpu_queue

        os.environ.update(
            {
                "GPU_TTS_PRIMARY": "0",
                "GPU_ASR": "0",
                "GPU_MUSIC": "0",
                "GPU_SFX": "0",
                "TTS_URL_2": "",
            }
        )
        self.assertEqual(gpu_queue._coexisting_services("music"), {"music"})

    def test_dual_gpu_music_keeps_gpu0_services_only(self):
        import gpu_queue

        os.environ.update(
            {
                "GPU_TTS_PRIMARY": "0",
                "GPU_TTS_EXTRA": "1",
                "GPU_ASR": "0",
                "GPU_MUSIC": "1",
                "GPU_SFX": "1",
                "TTS_URL_2": "http://qwen3-tts-2:8880",
            }
        )
        self.assertEqual(
            gpu_queue._coexisting_services("music"),
            {"tts", "asr", "music"},
        )


class SourceSecurityTest(unittest.TestCase):
    def test_podcast_voice_queries_are_user_scoped(self):
        routes = (API_ROOT / "podcast" / "routes.py").read_text()
        self.assertIsNone(
            re.search(r'"SELECT id, source FROM voices WHERE id=\?"\s*,', routes)
        )
        self.assertIn("SELECT id, source FROM voices WHERE id=? AND user_id=?", routes)

    def test_prod_overlay_hides_gateway_port_and_fails_closed(self):
        prod = (REPO_ROOT / "docker-compose.prod.yml").read_text()
        self.assertIn("vocarium-api:", prod)
        self.assertIn("ports: !reset []", prod)
        self.assertIn("ALLOW_ANONYMOUS: ${ALLOW_ANONYMOUS:-false}", prod)
        self.assertIn("CORS_ORIGINS: ${CORS_ORIGINS:?", prod)


if __name__ == "__main__":
    unittest.main()
