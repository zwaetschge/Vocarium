import asyncio
import io
import os
import py_compile
import re
import ast
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import wave
from pathlib import Path


API_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = API_ROOT.parent

# API replicas the Compose file actually ships. The dual-GPU `vocarium-api-2`
# replica was retired together with the second Qwen worker; keeping this as a
# tuple means the bytecode-cache invariants still apply to whatever set of API
# services exists, without hard-coding a replica that no longer does.
API_SERVICES = ("vocarium-api",)
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

    def test_performance_schema_indexes_and_columns_exist(self):
        import database

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "vocarium.db"
            database.init_db(db_path)
            try:
                db = database.get_db()
                tables = {
                    row[0]
                    for row in db.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }
                self.assertIn("music_task_files", tables)

                chunk_cols = {
                    row[1]
                    for row in db.execute("PRAGMA table_info(podcast_chunks)").fetchall()
                }
                self.assertIn("embedding_blob", chunk_cols)
                host_cols = {
                    row[1]
                    for row in db.execute("PRAGMA table_info(hosts)").fetchall()
                }
                self.assertTrue(
                    {
                        "persona_tagline",
                        "persona_humour",
                        "persona_warmth",
                        "persona_script_length",
                    }.issubset(host_cols)
                )

                expected = {
                    "idx_voices_user_source_created_at",
                    "idx_voices_user_created_at_desc",
                    "idx_hosts_user_name_nocase",
                    "idx_hosts_user_created_at",
                    "idx_podcasts_user_created_at_desc",
                    "idx_podcast_chunks_podcast_position",
                    "idx_podcast_sources_podcast_created_at",
                    "idx_benchmarks_user_created_at_desc",
                    "idx_music_task_files_user_path",
                }
                indexes = {
                    row[0]
                    for row in db.execute(
                        "SELECT name FROM sqlite_master WHERE type='index'"
                    ).fetchall()
                }
                self.assertTrue(expected.issubset(indexes))
            finally:
                database.get_db().close()
                database._db = None

    def test_music_task_files_backfilled_from_legacy_json(self):
        import database

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "vocarium.db"
            database.init_db(db_path)
            db = database.get_db()
            db.execute("INSERT INTO users (username) VALUES ('u')")
            user_id = db.execute("SELECT id FROM users WHERE username='u'").fetchone()[0]
            db.execute(
                "INSERT INTO music_tasks (task_id, user_id, status, file_paths) "
                "VALUES ('task-1', ?, 'completed', '[\"out/a.wav\", \"out/b.wav\"]')",
                (user_id,),
            )
            db.commit()
            db.close()
            database._db = None

            database.init_db(db_path)
            try:
                rows = database.get_db().execute(
                    "SELECT path FROM music_task_files WHERE user_id=? ORDER BY path",
                    (user_id,),
                ).fetchall()
                self.assertEqual([row[0] for row in rows], ["out/a.wav", "out/b.wav"])
            finally:
                database.get_db().close()
                database._db = None

    def test_gpu_queue_jobs_are_persisted_and_interrupted_on_startup(self):
        import database

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "vocarium.db"
            database.init_db(db_path)
            try:
                database.upsert_gpu_queue_job(
                    {
                        "job_id": "job-1",
                        "worker_id": "api-1",
                        "service_type": "tts",
                        "description": "test",
                        "status": "running",
                        "position": 0,
                        "created_at": time.time() - 10,
                        "started_at": time.time() - 5,
                        "finished_at": None,
                        "error": None,
                        "updated_at": time.time(),
                    }
                )
                database.upsert_gpu_queue_job(
                    {
                        "job_id": "job-2",
                        "worker_id": "api-2",
                        "service_type": "tts",
                        "description": "other",
                        "status": "running",
                        "position": 0,
                        "created_at": time.time() - 10,
                        "started_at": time.time() - 5,
                        "finished_at": None,
                        "error": None,
                        "updated_at": time.time(),
                    }
                )
                interrupted = database.mark_interrupted_gpu_jobs(
                    "test restart", worker_id="api-1"
                )
                self.assertEqual(interrupted, 1)
                job = database.get_gpu_queue_job("job-1")
                self.assertIsNotNone(job)
                self.assertEqual(job["status"], "interrupted")
                self.assertEqual(job["error"], "test restart")
                other = database.get_gpu_queue_job("job-2")
                self.assertEqual(other["status"], "running")
            finally:
                database.get_db().close()
                database._db = None

    def test_gpu_queue_jobs_migrate_cancel_trace_and_quota_columns(self):
        import database

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "vocarium.db"
            raw = sqlite3.connect(db_path)
            raw.execute(
                "CREATE TABLE gpu_queue_jobs ("
                "job_id TEXT PRIMARY KEY,"
                "worker_id TEXT NOT NULL DEFAULT 'default',"
                "service_type TEXT NOT NULL,"
                "description TEXT NOT NULL DEFAULT '',"
                "status TEXT NOT NULL DEFAULT 'queued',"
                "position INTEGER NOT NULL DEFAULT 0,"
                "error TEXT,"
                "created_at REAL NOT NULL,"
                "started_at REAL,"
                "finished_at REAL,"
                "updated_at REAL NOT NULL)"
            )
            raw.commit()
            raw.close()

            database.init_db(db_path)
            try:
                cols = {
                    row[1]
                    for row in database.get_db()
                    .execute("PRAGMA table_info(gpu_queue_jobs)")
                    .fetchall()
                }
                expected = {
                    "user_id",
                    "request_id",
                    "heartbeat_at",
                    "attempt_count",
                    "max_attempts",
                    "last_error",
                    "cancel_requested",
                }
                self.assertTrue(expected.issubset(cols))
            finally:
                database.get_db().close()
                database._db = None

    def test_gpu_queue_cancel_and_quota_are_user_scoped(self):
        import database

        old_active = os.environ.get("GPU_QUEUE_MAX_ACTIVE_PER_USER")
        old_queued = os.environ.get("GPU_QUEUE_MAX_QUEUED_PER_USER")
        os.environ["GPU_QUEUE_MAX_ACTIVE_PER_USER"] = "1"
        os.environ["GPU_QUEUE_MAX_QUEUED_PER_USER"] = "1"
        try:
            with tempfile.TemporaryDirectory() as tmp:
                db_path = Path(tmp) / "vocarium.db"
                database.init_db(db_path)
                try:
                    now = time.time()
                    database.upsert_gpu_queue_job(
                        {
                            "job_id": "job-1",
                            "worker_id": "api-1",
                            "user_id": 7,
                            "request_id": "req-1",
                            "service_type": "tts",
                            "description": "test",
                            "status": "queued",
                            "position": 1,
                            "created_at": now,
                            "started_at": None,
                            "finished_at": None,
                            "heartbeat_at": now,
                            "error": None,
                            "last_error": None,
                            "attempt_count": 0,
                            "max_attempts": 1,
                            "cancel_requested": False,
                            "updated_at": now,
                        }
                    )
                    self.assertFalse(
                        database.gpu_queue_quota_decision(7, "tts")["allowed"]
                    )
                    self.assertTrue(
                        database.gpu_queue_quota_decision(8, "tts")["allowed"]
                    )
                    self.assertIsNone(
                        database.request_cancel_gpu_queue_job("job-1", user_id=8)
                    )
                    cancelled = database.request_cancel_gpu_queue_job("job-1", user_id=7)
                    self.assertIsNotNone(cancelled)
                    self.assertTrue(cancelled["cancel_requested"])
                    self.assertTrue(database.is_gpu_queue_cancel_requested("job-1"))
                finally:
                    database.get_db().close()
                    database._db = None
        finally:
            if old_active is None:
                os.environ.pop("GPU_QUEUE_MAX_ACTIVE_PER_USER", None)
            else:
                os.environ["GPU_QUEUE_MAX_ACTIVE_PER_USER"] = old_active
            if old_queued is None:
                os.environ.pop("GPU_QUEUE_MAX_QUEUED_PER_USER", None)
            else:
                os.environ["GPU_QUEUE_MAX_QUEUED_PER_USER"] = old_queued

    def test_podcast_audio_integrity_columns_exist(self):
        import database

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "vocarium.db"
            database.init_db(db_path)
            try:
                cols = {
                    row[1]
                    for row in database.get_db()
                    .execute("PRAGMA table_info(podcasts)")
                    .fetchall()
                }
                self.assertIn("audio_size", cols)
                self.assertIn("audio_sha256", cols)
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
                "TTS_URL_2": "http://qwen3-tts-2:8880",
            }
        )
        self.assertEqual(
            gpu_queue._coexisting_services("music"),
            {"tts", "asr", "music"},
        )

    def test_gpu_guard_denial_retries_after_self_unload(self):
        import gpu_queue

        calls: list[tuple[str, str]] = []

        async def fake_require(service_type: str, need_mib: int | None = None) -> None:
            calls.append(("require", service_type))
            if calls.count(("require", service_type)) == 1:
                raise gpu_queue.GpuResourceError(
                    service_type,
                    {
                        "service_type": service_type,
                        "allowed": False,
                        "selected": {"reasons": ["not enough VRAM"]},
                    },
                )

        async def fake_unload() -> None:
            calls.append(("unload", "tts"))

        original_require = gpu_queue.require_gpu_resources
        original_callbacks = dict(gpu_queue._unload_callbacks)
        try:
            gpu_queue.require_gpu_resources = fake_require
            gpu_queue._unload_callbacks.clear()
            gpu_queue._unload_callbacks["tts"] = fake_unload

            asyncio.run(
                gpu_queue.GpuQueue()._require_resources_with_self_unload("tts")
            )

            self.assertEqual(
                calls,
                [("require", "tts"), ("unload", "tts"), ("require", "tts")],
            )
        finally:
            gpu_queue.require_gpu_resources = original_require
            gpu_queue._unload_callbacks.clear()
            gpu_queue._unload_callbacks.update(original_callbacks)


class SourceSecurityTest(unittest.TestCase):
    def test_podcast_voice_queries_are_user_scoped(self):
        """Every read of the shared voices table must carry the tenant filter.

        Pinning one literal statement went stale as soon as the queries moved,
        which hid the actual rule: voices are per-user, so a query without
        ``user_id=?`` leaks another tenant's presets.
        """
        routes = (API_ROOT / "podcast" / "routes.py").read_text()
        queries = re.findall(r'"(SELECT [^"]*FROM voices[^"]*)"', routes)
        self.assertTrue(queries, "expected at least one voices query to guard")
        for query in queries:
            self.assertIn("user_id=?", query, f"unscoped voices query: {query}")

    def test_prod_overlay_hides_gateway_port_and_fails_closed(self):
        prod = (REPO_ROOT / "docker-compose.prod.yml").read_text()
        self.assertIn("vocarium-api:", prod)
        self.assertIn("ports: !reset []", prod)
        self.assertIn("ALLOW_ANONYMOUS: ${ALLOW_ANONYMOUS:-false}", prod)
        self.assertIn("CORS_ORIGINS: ${CORS_ORIGINS:?", prod)

    def test_api_cache_mounts_avoid_read_only_source_bind_mountpoints(self):
        compose = (REPO_ROOT / "docker-compose.yml").read_text()
        strategies: dict[str, tuple[str, tuple[str, ...]]] = {}

        for service in API_SERVICES:
            match = re.search(
                rf"(?ms)^  {re.escape(service)}:\n(?P<body>.*?)(?=^  [a-zA-Z0-9_-]+:\n|\Z)",
                compose,
            )
            self.assertIsNotNone(match, f"missing Compose service {service}")
            block = match.group("body")
            read_only_targets = re.findall(
                r"(?m)^      - [^:\n]+:(/[^:\n]+):ro$", block
            )
            tmpfs_targets = tuple(
                re.findall(
                    r"(?m)^      - type: tmpfs\n        target: (/\S+)$", block
                )
            )
            nested_targets = [
                (tmpfs_target, source_target)
                for tmpfs_target in tmpfs_targets
                for source_target in read_only_targets
                if tmpfs_target == source_target
                or tmpfs_target.startswith(source_target.rstrip("/") + "/")
            ]
            self.assertEqual(
                nested_targets,
                [],
                f"{service} tmpfs targets must not require mountpoints below "
                "read-only sources",
            )

            prefix_match = re.search(
                r"(?m)^      PYTHONPYCACHEPREFIX:[ \t]*(\S+)[ \t]*$", block
            )
            self.assertIsNotNone(
                prefix_match, f"{service} must configure PYTHONPYCACHEPREFIX"
            )
            prefix = prefix_match.group(1).strip("\"'")
            self.assertTrue(prefix.startswith("/"))
            self.assertFalse(prefix == "/app" or prefix.startswith("/app/"))
            self.assertIn(prefix, tmpfs_targets)
            self.assertFalse(
                any("__pycache__" in Path(target).parts for target in tmpfs_targets)
            )
            strategies[service] = (prefix, tmpfs_targets)

        self.assertEqual(len(set(strategies.values())), 1)

    def test_api_cache_prefix_ignores_colocated_stale_bytecode(self):
        compose = (REPO_ROOT / "docker-compose.yml").read_text()
        prefixes: list[str] = []
        for service in API_SERVICES:
            match = re.search(
                rf"(?ms)^  {re.escape(service)}:\n(?P<body>.*?)(?=^  [a-zA-Z0-9_-]+:\n|\Z)",
                compose,
            )
            self.assertIsNotNone(match, f"missing Compose service {service}")
            prefix_match = re.search(
                r"(?m)^      PYTHONPYCACHEPREFIX:[ \t]*(\S+)[ \t]*$",
                match.group("body"),
            )
            self.assertIsNotNone(
                prefix_match, f"{service} must configure PYTHONPYCACHEPREFIX"
            )
            prefixes.append(prefix_match.group(1).strip("\"'"))
        self.assertEqual(len(set(prefixes)), 1)

        with tempfile.TemporaryDirectory() as tmp:
            source_root = Path(tmp) / "source"
            source_root.mkdir()
            module_path = source_root / "cache_probe.py"
            module_path.write_text("VALUE = 'stale'\n")
            colocated_cache = (
                module_path.parent
                / "__pycache__"
                / f"{module_path.stem}.{sys.implementation.cache_tag}.pyc"
            )
            colocated_cache.parent.mkdir()
            py_compile.compile(
                str(module_path),
                cfile=str(colocated_cache),
                doraise=True,
                invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH,
            )
            module_path.write_text("VALUE = 'fresh'\n")

            probe = (
                "import importlib.util, pathlib, sys\n"
                f"source_root = pathlib.Path({str(source_root)!r})\n"
                "sys.path.insert(0, str(source_root))\n"
                "import cache_probe\n"
                "print(cache_probe.VALUE)\n"
                "print(sys.pycache_prefix)\n"
                "print(importlib.util.cache_from_source(str(source_root / 'cache_probe.py')))\n"
            )
            baseline_env = os.environ.copy()
            baseline_env.pop("PYTHONPYCACHEPREFIX", None)
            baseline_env["PYTHONDONTWRITEBYTECODE"] = "1"
            baseline = subprocess.run(
                [sys.executable, "-c", probe],
                capture_output=True,
                check=False,
                env=baseline_env,
                text=True,
            )
            self.assertEqual(baseline.returncode, 0, baseline.stderr)
            baseline_output = baseline.stdout.splitlines()
            self.assertEqual(baseline_output[0], "stale")
            self.assertEqual(baseline_output[1], "None")
            self.assertEqual(Path(baseline_output[2]), colocated_cache)

            prefixed_env = baseline_env.copy()
            prefixed_env["PYTHONPYCACHEPREFIX"] = prefixes[0]
            prefixed = subprocess.run(
                [sys.executable, "-c", probe],
                capture_output=True,
                check=False,
                env=prefixed_env,
                text=True,
            )
            self.assertEqual(prefixed.returncode, 0, prefixed.stderr)
            prefixed_output = prefixed.stdout.splitlines()
            self.assertEqual(prefixed_output[0], "fresh")
            self.assertEqual(prefixed_output[1], prefixes[0])
            redirected_cache = Path(prefixed_output[2])
            self.assertTrue(redirected_cache.is_relative_to(Path(prefixes[0])))
            self.assertNotEqual(redirected_cache, colocated_cache)

    def test_local_auth_default_allows_shared_api_user(self):
        main_source = (API_ROOT / "main.py").read_text()
        self.assertIn(
            'ALLOW_ANONYMOUS = os.environ.get("ALLOW_ANONYMOUS", "false")',
            main_source,
        )
        self.assertIn(
            "def get_current_user(request: Request, allow_anonymous: bool = True) -> dict:",
            main_source,
        )
        self.assertIn(
            "if not username and allow_anonymous and ALLOW_ANONYMOUS:",
            main_source,
        )

    def test_ui_nginx_forwards_identity_headers(self):
        nginx = (REPO_ROOT / "vocarium-ui" / "nginx.conf.template").read_text()
        self.assertIn("proxy_set_header Remote-User $http_remote_user;", nginx)
        self.assertIn(
            "proxy_set_header X-Forwarded-User $http_x_forwarded_user;",
            nginx,
        )
        self.assertNotIn('proxy_set_header Remote-User "";', nginx)
        self.assertNotIn('proxy_set_header X-Forwarded-User "";', nginx)

class ApiContractHardeningTest(unittest.TestCase):
    def test_sse_event_helper_emits_json_event_frame(self):
        main_source = (API_ROOT / "main.py").read_text()
        self.assertIn("def _sse_event(event: str, data: dict) -> str:", main_source)
        self.assertIn("ensure_ascii=False", main_source)
        self.assertIn(r'"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"', main_source)

    def test_llm_base_url_is_normalized_and_http_only(self):
        main_source = (API_ROOT / "main.py").read_text()
        url_security_source = (API_ROOT / "url_security.py").read_text()
        self.assertIn("def _normalize_http_base_url", main_source)
        self.assertIn("normalize_http_base_url(", main_source)
        self.assertIn('parsed.scheme not in ("http", "https")', url_security_source)
        self.assertIn(".strip().rstrip", url_security_source)
        self.assertIn("not ip.is_global", url_security_source)

    def test_set_active_provider_update_stays_user_scoped(self):
        main_source = (API_ROOT / "main.py").read_text()
        self.assertIn(
            "UPDATE llm_providers SET is_active=1, updated_at=? WHERE id=? AND user_id=?",
            main_source,
        )

    def test_streaming_endpoint_uses_queue_backed_live_sse(self):
        main_source = (API_ROOT / "main.py").read_text()
        self.assertIn("event_queue: asyncio.Queue[str | None] = asyncio.Queue()", main_source)
        self.assertIn('yield _sse_event(', main_source)

    def test_metrics_cleanup_and_persisted_queue_endpoints_exist(self):
        main_source = (API_ROOT / "main.py").read_text()
        self.assertIn('@app.get("/api/metrics")', main_source)
        self.assertIn('@app.post("/api/admin/artifacts/cleanup")', main_source)
        self.assertIn('@app.get("/api/queue/jobs")', main_source)
        self.assertIn('@app.post("/api/queue/jobs/{job_id}/cancel")', main_source)
        self.assertIn("register_job_recorder(record_gpu_job)", main_source)
        self.assertIn("register_quota_checker(gpu_queue_quota_decision)", main_source)
        self.assertIn("register_cancel_checker(is_gpu_queue_cancel_requested)", main_source)
        self.assertIn("mark_interrupted_gpu_jobs(worker_id=API_INSTANCE_ID)", main_source)

    def test_request_id_is_propagated_to_responses_and_queue_jobs(self):
        main_source = (API_ROOT / "main.py").read_text()
        queue_source = (API_ROOT / "gpu_queue.py").read_text()
        self.assertIn('TRACE_HEADER = "X-Request-ID"', main_source)
        self.assertIn("response.headers[TRACE_HEADER] = request_id", main_source)
        self.assertIn("request_id_var.set(request_id)", main_source)
        self.assertIn("current_request_id()", queue_source)
        self.assertIn('"request_id": self.request_id', queue_source)

    def test_openai_personas_are_separate_from_voice_selection(self):
        main_source = (API_ROOT / "main.py").read_text()
        self.assertIn('@app.get("/v1/personas")', main_source)
        self.assertIn('@app.post("/v1/personas")', main_source)
        self.assertIn('"voice": "default"', main_source)
        self.assertIn('"soul": row[2] or ""', main_source)
        self.assertIn('"tagline": row[3] or ""', main_source)
        self.assertIn('"humour": row[4] or ""', main_source)
        self.assertIn('"warmth": row[5] or row[7] or ""', main_source)
        self.assertIn('"scriptLength": row[6] or "medium"', main_source)
        self.assertIn("persona_script_length", (API_ROOT / "database.py").read_text())
        self.assertNotIn('"voice_id": "michael-scott"', main_source)

    def test_observability_metrics_are_recorded_in_hot_paths(self):
        queue_source = (API_ROOT / "gpu_queue.py").read_text()
        main_source = (API_ROOT / "main.py").read_text()
        llm_source = (API_ROOT / "podcast" / "llm_client.py").read_text()
        self.assertIn("vocarium_gpu_queue_wait_seconds", queue_source)
        self.assertIn("vocarium_gpu_job_run_seconds", queue_source)
        self.assertIn("vocarium_gpu_guard_blocks_total", main_source)
        self.assertIn("vocarium_db_query_seconds", (API_ROOT / "database.py").read_text())
        self.assertIn("vocarium_llm_seconds", llm_source)


class PodcastProductionHardeningTest(unittest.TestCase):
    def test_segment_numeric_coercion_calls_pass_field_name(self):
        tree = ast.parse((API_ROOT / "podcast" / "routes.py").read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Name) and func.id in {"_coerce_int", "_coerce_float"}:
                self.assertGreaterEqual(len(node.args), 2)
                self.assertIsInstance(node.args[1], ast.Constant)
                self.assertIsInstance(node.args[1].value, str)

    def test_podcast_generator_branding_is_vocarium_only(self):
        source = (API_ROOT / "podcast" / "script_generator.py").read_text()
        self.assertIn("Vocarium-Label-Drop", source)
        self.assertIn("Vocarium-Sign-off", source)
        self.assertNotIn("Pod" + "Forge", source)
        self.assertNotIn("]\n```\n]\n```", source)

    def test_docling_fallback_works_without_service_url(self):
        """Ohne Docling muessen Quellen trotzdem verarbeitet werden.

        Dieser Stack bringt keinen Docling-Dienst mit, also darf ein PDF-Upload
        nicht daran scheitern: der lokale Parser des Hoerbuch-Bereichs springt
        ein, und eine URL wird direkt geholt.
        """
        source = (API_ROOT / "podcast" / "docling_client.py").read_text()
        self.assertIn("return {\"status\": \"success\", \"text\": content}", source)
        for ext in ('".pdf": "pdf"', '".epub": "epub"', '".docx": "docx"'):
            self.assertIn(ext, source)
        self.assertIn("from audiobooks.text_pipeline import parse_document", source)
        self.assertIn("_fetch_url_text", source)

    def test_the_single_api_owns_the_podcast_database_volume(self):
        """One API service, one data volume, and no orphaned second volume.

        This replaces the dual-replica sharing test: `vocarium-api-2` and the
        `dual-gpu` profile are gone, so what still needs guarding is that the
        remaining API mounts the podcast database and nobody introduced a
        parallel `vocarium-data-2` alongside it.
        """
        compose = (REPO_ROOT / "docker-compose.yml").read_text()
        self.assertNotIn("vocarium-data" + "-2", compose)
        self.assertEqual(
            compose.count("- vocarium-data:/app/data"), len(API_SERVICES)
        )

    def test_podcast_tts_never_round_robins_to_emergency_gpu(self):
        tree = ast.parse((API_ROOT / "podcast" / "routes.py").read_text())
        generator_class = next(
            node for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "VocariumTTSGenerator"
        )
        order_method = next(
            node for node in generator_class.body
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "_ordered_tts_urls"
        )
        namespace: dict[str, object] = {}
        exec(
            compile(ast.Module(body=[order_method], type_ignores=[]), "routes.py", "exec"),
            namespace,
        )

        class GeneratorStub:
            _tts_urls = ["http://qwen3-tts:8880", "http://qwen3-tts-2:8880"]

        expected = ["http://qwen3-tts:8880", "http://qwen3-tts-2:8880"]
        ordered_urls = namespace["_ordered_tts_urls"]
        self.assertEqual(asyncio.run(ordered_urls(GeneratorStub())), expected)
        self.assertEqual(asyncio.run(ordered_urls(GeneratorStub())), expected)

    def test_webui_speech_generation_keeps_cloned_voices_available(self):
        main_source = (API_ROOT / "main.py").read_text()

        generate_start = main_source.index("@app.post(\"/api/generate\")")
        generate_end = main_source.index("# ---------------------------------------------------------------------------\n# Benchmark", generate_start)
        generate_source = main_source[generate_start:generate_end]
        stream_start = main_source.index("@app.post(\"/api/generate/stream\")")
        stream_end = main_source.index("@app.post(\"/api/generate\")", stream_start)
        stream_source = main_source[stream_start:stream_end]

        self.assertNotIn("_require_custom_generation_voice(source)", main_source)
        self.assertNotIn("Speech generation only supports custom voices", main_source)
        self.assertNotIn("_require_custom_generation_voice(source)", generate_source)
        self.assertNotIn("_require_custom_generation_voice(source)", stream_source)

    def test_f5_experiment_is_not_in_stack(self):
        compose = (REPO_ROOT / "docker-compose.yml").read_text()
        env_example = (REPO_ROOT / ".env.example").read_text()
        main_source = (API_ROOT / "main.py").read_text()
        e2e_source = (REPO_ROOT / "vocarium-ui" / "e2e" / "runtime.spec.ts").read_text()
        combined = "\n".join([compose, env_example, main_source, e2e_source])
        self.assertNotIn("f5", combined.lower())
        self.assertFalse((REPO_ROOT / "f5-tts" / "Dockerfile").exists())
        self.assertFalse((REPO_ROOT / "f5-tts" / "server.py").exists())

    def test_neutts_experiment_is_not_in_stack(self):
        compose = (REPO_ROOT / "docker-compose.yml").read_text()
        main_source = (API_ROOT / "main.py").read_text()
        self.assertNotIn("neutts-tts", compose)
        self.assertNotIn("NEUTTS_URL", main_source)
        self.assertFalse((REPO_ROOT / "neutts-tts" / "Dockerfile").exists())

    def test_chunk_text_uses_requested_overlap(self):
        from podcast.helpers import chunk_text

        chunks = chunk_text("a" * 120, max_chars=50, overlap=10)
        self.assertGreaterEqual(len(chunks), 3)
        self.assertEqual(chunks[0][-10:], chunks[1][:10])

    def test_music_output_is_not_mounted_from_a_retired_worker(self):
        """ACE-Step left the stack; the API must not claim its output volume.

        The route code still knows how to serve a local music file (see
        `test_music_download_serves_local_files_without_proxying`), but there is
        no longer a worker producing one, and a stale read-only mount of a
        removed volume would fail the whole API on `up`.
        """
        compose = (REPO_ROOT / "docker-compose.yml").read_text()
        self.assertNotIn("acestep-output:", compose)
        self.assertNotIn("  acestep:", compose)

    def test_audio_generation_benchmark_script_documents_preload_costs(self):
        script = REPO_ROOT / "scripts" / "benchmark-audio-generation.py"
        self.assertTrue(script.exists())
        source = script.read_text()
        self.assertIn("MODEL_WEIGHT_HINTS", source)
        self.assertIn("--kind", source)
        self.assertIn("--preload", source)
        self.assertIn("--json", source)
        self.assertIn("--force-generate", source)
        self.assertIn("Remote-User", source)

    def test_openai_tts_persona_aliases_route_to_default_voice(self):
        source = (API_ROOT / "main.py").read_text()
        self.assertIn("def _resolve_openai_tts_voice", source)
        self.assertIn("def _openai_voice_lookup_key", source)
        self.assertIn("SELECT id, name FROM voices WHERE user_id=?", source)
        self.assertIn("SELECT id, name FROM voices WHERE user_id IS NULL", source)
        self.assertIn("WHERE id=? AND (user_id=? OR user_id IS NULL)", source)
        self.assertIn("Stored user voices win over persona aliases", source)
        self.assertIn('"michael scott"', source)
        self.assertIn('if r[0] == "default" and source in (None, "clone"):', source)
        self.assertIn("voice = _resolve_openai_tts_voice(req.voice, user_id=user[\"id\"])", source)
        self.assertIn("voice_override=voice", source)
        self.assertIn('"voice": voice', source)

    def test_podcast_audio_downloads_are_path_and_size_checked(self):
        source = (API_ROOT / "podcast" / "routes.py").read_text()
        self.assertIn("def _safe_audio_path", source)
        self.assertIn("root not in candidate.parents", source)
        self.assertIn("Audio artifact integrity check failed", source)
        self.assertIn("VERIFY_ARTIFACT_HASH_ON_READ", source)

    def test_podcast_tts_batch_runs_outside_the_gpu_queue(self):
        """Speech renders in one batch, and that batch does not queue.

        The invariant inverted when podcasts moved to OmniVoice and Kikiri:
        both are resident (GPU slice / CPU), so there is nothing for the queue
        to evict and taking a `tts` slot would only serialise against music.
        What still matters is that the assembler hands the whole foreground
        pass to a single `run_batch` call rather than per segment.
        """
        routes = (API_ROOT / "podcast" / "routes.py").read_text()
        assembler = (API_ROOT / "podcast" / "audio_assembler.py").read_text()
        self.assertIn("async def run_batch", routes)
        self.assertIn('batch_runner(f"Podcast TTS {project_id}", _run_foreground_tts)', assembler)

        batch = routes.split("async def run_batch", 1)[1].split("\n    async def", 1)[0]
        self.assertNotIn("_gpu_submit", batch)
        # Music is the one podcast phase that still needs a GPU slot.
        self.assertIn('await self._gpu_submit("music", "Podcast Music", work)', routes)

    def test_tts_segment_cache_reuses_identical_audio(self):
        from podcast.audio_assembler import AssemblyOptions, AudioAssembler
        from podcast.disfluency import ScriptSegment

        class FakeTTS:
            def __init__(self):
                self.calls = 0

            async def synthesize_to_file(self, text, voice, output_path, output_format, **kwargs):
                self.calls += 1
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_bytes(_tiny_wav())
                return 0.05

            async def default_voice_for_speaker(self, speaker, *, user_id=None):
                return "voice-1"

        async def run_case():
            with tempfile.TemporaryDirectory() as tmp:
                fake = FakeTTS()
                assembler = AudioAssembler(tts=fake, output_dir=tmp)
                opts = AssemblyOptions(output_format="wav")
                first = ScriptSegment(
                    id="seg-1",
                    script_id=None,
                    speaker="A",
                    text="Hallo Test",
                    type="speech",
                    voice="voice-1",
                )
                second = ScriptSegment(
                    id="seg-2",
                    script_id=None,
                    speaker="A",
                    text="Hallo Test",
                    type="speech",
                    voice="voice-1",
                )
                await assembler._synthesize_segments([first], "pod-1", opts, None, user_id=1)
                await assembler._synthesize_segments([second], "pod-2", opts, None, user_id=1)
                self.assertEqual(fake.calls, 1)
                self.assertTrue((Path(tmp) / "pod-2" / "seg-2.wav").exists())

        asyncio.run(run_case())

    def test_segment_manifest_prevents_stale_project_audio_reuse(self):
        from podcast.audio_assembler import AssemblyOptions, AudioAssembler
        from podcast.disfluency import ScriptSegment

        class FakeTTS:
            def __init__(self):
                self.calls = 0

            async def synthesize_to_file(self, text, voice, output_path, output_format, **kwargs):
                self.calls += 1
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_bytes(_tiny_wav())
                return 0.05

            async def default_voice_for_speaker(self, speaker, *, user_id=None):
                return "voice-1"

        async def run_case():
            with tempfile.TemporaryDirectory() as tmp:
                fake = FakeTTS()
                assembler = AudioAssembler(tts=fake, output_dir=tmp)
                opts = AssemblyOptions(output_format="wav")
                first = ScriptSegment(
                    id="seg-1",
                    script_id=None,
                    speaker="A",
                    text="first text",
                    type="speech",
                    voice="voice-1",
                )
                second = ScriptSegment(
                    id="seg-1",
                    script_id=None,
                    speaker="A",
                    text="changed text",
                    type="speech",
                    voice="voice-1",
                )
                await assembler._synthesize_segments([first], "pod-1", opts, None, user_id=1)
                await assembler._synthesize_segments([second], "pod-1", opts, None, user_id=1)
                self.assertEqual(fake.calls, 2)
                self.assertTrue((Path(tmp) / "pod-1" / "seg-1.wav.json").exists())

        asyncio.run(run_case())


class ArtifactCleanupTest(unittest.TestCase):
    def test_cleanup_removes_only_unreferenced_local_artifacts(self):
        import database
        from artifact_cleanup import cleanup_artifacts

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            voices_dir = root / "voices"
            audio_dir = data_dir / "podcast_audio"
            upload_dir = data_dir / "podcast_uploads"
            old_audio_env = os.environ.get("PODCAST_AUDIO_PATH")
            old_upload_env = os.environ.get("PODCAST_UPLOAD_PATH")
            os.environ["PODCAST_AUDIO_PATH"] = str(audio_dir)
            os.environ["PODCAST_UPLOAD_PATH"] = str(upload_dir)
            for path in (audio_dir, upload_dir, voices_dir):
                path.mkdir(parents=True, exist_ok=True)

            db_path = data_dir / "vocarium.db"
            database.init_db(db_path)
            try:
                db = database.get_db()
                db.execute("INSERT INTO users (username) VALUES ('u')")
                user_id = db.execute("SELECT id FROM users WHERE username='u'").fetchone()[0]
                kept_audio = audio_dir / "pod-keep.mp3"
                kept_audio.write_bytes(b"keep")
                orphan_audio = audio_dir / "orphan.mp3"
                orphan_audio.write_bytes(b"drop")
                kept_upload = upload_dir / "src-keep.md"
                kept_upload.write_text("keep")
                orphan_upload = upload_dir / "orphan.md"
                orphan_upload.write_text("drop")
                kept_voice = voices_dir / "voice-keep"
                kept_voice.mkdir()
                orphan_voice = voices_dir / "voice-orphan"
                orphan_voice.mkdir()

                old = time.time() - 3600
                for path in (orphan_audio, orphan_upload, orphan_voice):
                    os.utime(path, (old, old))

                db.execute(
                    "INSERT INTO voices (id, user_id, name, source) VALUES ('voice-keep', ?, 'Keep', 'clone')",
                    (user_id,),
                )
                db.execute(
                    "INSERT INTO podcasts (id, user_id, topic, audio_path) VALUES ('pod-keep', ?, 'Keep', ?)",
                    (user_id, str(kept_audio)),
                )
                db.execute(
                    "INSERT INTO podcast_sources (id, podcast_id, type, title, content, status) "
                    "VALUES ('src-keep', 'pod-keep', 'file', 'Keep', ?, 'processed')",
                    (str(kept_upload),),
                )
                db.commit()

                report = cleanup_artifacts(
                    db, data_dir, voices_dir, max_age_hours=0.01, dry_run=False
                )
                deleted = {Path(item["path"]).name for item in report["deleted"]}
                self.assertIn("orphan.mp3", deleted)
                self.assertIn("orphan.md", deleted)
                self.assertIn("voice-orphan", deleted)
                self.assertTrue(kept_audio.exists())
                self.assertTrue(kept_upload.exists())
                self.assertTrue(kept_voice.exists())
            finally:
                if old_audio_env is None:
                    os.environ.pop("PODCAST_AUDIO_PATH", None)
                else:
                    os.environ["PODCAST_AUDIO_PATH"] = old_audio_env
                if old_upload_env is None:
                    os.environ.pop("PODCAST_UPLOAD_PATH", None)
                else:
                    os.environ["PODCAST_UPLOAD_PATH"] = old_upload_env
                database.get_db().close()
                database._db = None


class RuntimeScriptTest(unittest.TestCase):
    def test_stack_smoke_and_loadtest_scripts_exist(self):
        smoke = REPO_ROOT / "scripts" / "e2e-smoke.py"
        loadtest = REPO_ROOT / "scripts" / "tts-loadtest.py"
        self.assertTrue(smoke.exists())
        self.assertTrue(loadtest.exists())
        self.assertIn("/api/admin/artifacts/cleanup", smoke.read_text())
        self.assertIn("Skipping TTS load test", loadtest.read_text())

    def test_playwright_runtime_checks_exist(self):
        package_json = (REPO_ROOT / "vocarium-ui" / "package.json").read_text()
        spec = REPO_ROOT / "vocarium-ui" / "e2e" / "runtime.spec.ts"
        self.assertIn('"test:e2e": "playwright test"', package_json)
        self.assertTrue(spec.exists())
        self.assertIn("loads core routes without a blank screen", spec.read_text())


def _tiny_wav() -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(24000)
        wav.writeframes(b"\x00\x00" * 1200)
    return buf.getvalue()


if __name__ == "__main__":
    unittest.main()
