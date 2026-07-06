import asyncio
import io
import os
import re
import ast
import sqlite3
import sys
import tempfile
import time
import unittest
import wave
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

    def test_gpu_guard_denial_retries_after_self_unload(self):
        import gpu_queue

        calls: list[tuple[str, str]] = []

        async def fake_require(service_type: str) -> None:
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

    def test_local_auth_default_allows_shared_api_user(self):
        main_source = (API_ROOT / "main.py").read_text()
        self.assertIn(
            'ALLOW_ANONYMOUS = os.environ.get("ALLOW_ANONYMOUS", "true")',
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
        nginx = (REPO_ROOT / "vocarium-ui" / "nginx.conf").read_text()
        self.assertIn("proxy_set_header Remote-User $http_remote_user;", nginx)
        self.assertIn(
            "proxy_set_header X-Forwarded-User $http_x_forwarded_user;",
            nginx,
        )
        self.assertNotIn('proxy_set_header Remote-User "";', nginx)
        self.assertNotIn('proxy_set_header X-Forwarded-User "";', nginx)

    def test_file_download_routes_are_tenant_scoped(self):
        main_source = (API_ROOT / "main.py").read_text()
        routes_source = (API_ROOT / "podcast" / "routes.py").read_text()
        self.assertIn("SELECT id FROM voices WHERE id=? AND user_id=?", main_source)
        self.assertIn("_require_owned_music_audio_path(user[\"id\"], path)", main_source)
        self.assertIn("podcast = _get_podcast_or_404(db_getter(), podcast_id, user[\"id\"])", routes_source)
        self.assertIn("path = _locate_audio(podcast)", routes_source)


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

    def test_docling_text_fallback_works_without_service_url(self):
        source = (API_ROOT / "podcast" / "docling_client.py").read_text()
        self.assertIn("DOCLING_API_URL is not configured", source)
        self.assertIn("return {\"status\": \"success\", \"text\": content}", source)

    def test_dual_api_replicas_share_podcast_database_volume(self):
        compose = (REPO_ROOT / "docker-compose.yml").read_text()
        self.assertNotIn("vocarium-data" + "-2", compose)
        self.assertGreaterEqual(compose.count("- vocarium-data:/app/data"), 2)

    def test_mmaudio_generation_runs_off_event_loop(self):
        source = (REPO_ROOT / "mmaudio" / "server.py").read_text()
        self.assertIn("def _generate_sfx_blocking", source)
        self.assertIn("await asyncio.to_thread(\n            _generate_sfx_blocking, req", source)

    def test_qwen_worker_validates_response_formats(self):
        source = (REPO_ROOT / "qwen3-tts" / "server.py").read_text()
        self.assertIn("SUPPORTED_OUTPUT_FORMATS", source)
        self.assertIn("def _validate_response_format", source)
        self.assertIn("Streaming speech returns base64 WAV chunks", source)

    def test_qwen_clone_generation_is_voice_stable_by_default(self):
        source = (REPO_ROOT / "qwen3-tts" / "server.py").read_text()
        self.assertIn("TTS_CLONE_DO_SAMPLE", source)
        self.assertIn('"do_sample": CLONE_DO_SAMPLE', source)
        self.assertIn('"temperature": CLONE_TEMPERATURE', source)
        self.assertIn("TTS_CLONE_XVEC_ONLY", source)
        self.assertIn('xvec_only = CLONE_XVEC_ONLY or not (ref_text or "").strip()', source)
        self.assertIn('"xvec_only": xvec_only', source)
        self.assertIn('"x_vector_only_mode": xvec_only', source)
        self.assertIn("def _normalize_reference_wav", source)
        self.assertIn("ref_audio_normalization", source)
        self.assertIn("TTS_REF_NORMALIZE_PEAK", source)
        self.assertIn("Unknown voice", source)
        self.assertNotIn("voice_id = available[0]", source)
        self.assertLess(source.index("voice_id = request.voice"), source.index("ensure_model(target_model)"))
        self.assertIn("inference_lock = threading.RLock()", source)
        self.assertIn("with inference_lock:\n                for i, chunk in enumerate(chunks):", source)
        self.assertIn("def _run_stream_request", source)
        self.assertIn("loop.run_in_executor(None, _run_stream_request)", source)
        self.assertIn("voice_clone_prompt_cache", source)
        self.assertIn("create_voice_clone_prompt", source)
        self.assertIn("voice_clone_prompt", source)
        self.assertIn("_clear_voice_clone_prompt(voice_id)", source)

    def test_qwen_custom_voice_generation_is_voice_stable_by_default(self):
        source = (REPO_ROOT / "qwen3-tts" / "server.py").read_text()
        custom_start = source.index("@app.post(\"/v1/audio/speech/custom\")")
        custom_end = source.index("return Response(", custom_start)
        custom_source = source[custom_start:custom_end]
        self.assertIn("TTS_CUSTOM_DO_SAMPLE", source)
        self.assertIn("CUSTOM_DO_SAMPLE = _env_bool(\"TTS_CUSTOM_DO_SAMPLE\", False)", source)
        self.assertIn("CUSTOM_TEMPERATURE", source)
        self.assertIn("CUSTOM_TOP_K", source)
        self.assertIn("CUSTOM_TOP_P", source)
        self.assertIn("non_streaming_mode=True", custom_source)
        self.assertIn("do_sample=CUSTOM_DO_SAMPLE", custom_source)
        self.assertIn("temperature=CUSTOM_TEMPERATURE", custom_source)
        self.assertIn("top_k=CUSTOM_TOP_K", custom_source)
        self.assertIn("top_p=CUSTOM_TOP_P", custom_source)
        self.assertIn("repetition_penalty=1.08", custom_source)
        self.assertIn("custom_sampling", source)

    def test_f5_experiment_is_not_in_stack(self):
        compose = (REPO_ROOT / "docker-compose.yml").read_text()
        env_example = (REPO_ROOT / ".env.example").read_text()
        main_source = (API_ROOT / "main.py").read_text()
        speech_page = (REPO_ROOT / "vocarium-ui" / "src" / "pages" / "SpeechPage.tsx").read_text()
        e2e_source = (REPO_ROOT / "vocarium-ui" / "e2e" / "runtime.spec.ts").read_text()
        combined = "\n".join([compose, env_example, main_source, speech_page, e2e_source])
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

    def test_music_audio_ownership_uses_normalized_file_table(self):
        source = (API_ROOT / "main.py").read_text()
        self.assertIn("SELECT 1 FROM music_task_files WHERE user_id=? AND path=? LIMIT 1", source)
        self.assertNotIn("SELECT file_paths FROM music_tasks WHERE user_id=?", source)

    def test_music_generation_exposes_engine_and_client_audio_options(self):
        source = (API_ROOT / "main.py").read_text()
        self.assertIn('SUPPORTED_MUSIC_ENGINES = {"acestep"}', source)
        self.assertIn('engine: str = "acestep"', source)
        self.assertIn('negative_prompt: str = ""', source)
        self.assertIn("instrumental: bool = False", source)
        self.assertIn("loopable: bool = False", source)
        self.assertIn("normalize_lufs: float | None = None", source)
        self.assertIn("fade_ms: int = 0", source)
        self.assertIn("_validate_engine(req.engine, SUPPORTED_MUSIC_ENGINES", source)
        self.assertIn("_validate_lufs(req.normalize_lufs)", source)
        self.assertIn("_validate_fade_ms(req.fade_ms)", source)
        self.assertIn('"negative_prompt": req.negative_prompt', source)
        self.assertIn('"instrumental": req.instrumental', source)
        self.assertIn('"loopable": req.loopable', source)
        self.assertIn('"vocarium_options"', source)

    def test_sfx_generation_exposes_engine_and_negative_prompt_controls(self):
        source = (API_ROOT / "main.py").read_text()
        self.assertIn('SUPPORTED_SFX_ENGINES = {"mmaudio"}', source)
        self.assertIn("_validate_engine(engine, SUPPORTED_SFX_ENGINES", source)
        self.assertIn('no_speech = _coerce_bool(body.get("no_speech", False), "no_speech")', source)
        self.assertIn('no_music = _coerce_bool(body.get("no_music", False), "no_music")', source)
        self.assertIn("_merge_negative_prompt(", source)
        self.assertIn('"speech, voice, vocals, talking"', source)
        self.assertIn('"music, melody, song, vocals"', source)
        self.assertIn("_validate_lufs(normalize_lufs)", source)
        self.assertIn("_validate_fade_ms(fade_ms)", source)
        self.assertIn('"vocarium_options"', source)

    def test_audio_generation_services_report_first_load_download_status(self):
        ace_source = (REPO_ROOT / "acestep" / "proxy.py").read_text()
        mmaudio_source = (REPO_ROOT / "mmaudio" / "server.py").read_text()
        self.assertIn("MODEL_WEIGHT_HINTS", ace_source)
        self.assertIn("backend_starting", ace_source)
        self.assertIn("last_start_error", ace_source)
        self.assertIn('"first_load"', ace_source)
        self.assertIn("MODEL_WEIGHT_HINTS", mmaudio_source)
        self.assertIn("model_loading", mmaudio_source)
        self.assertIn("last_load_error", mmaudio_source)
        self.assertIn('"first_load"', mmaudio_source)

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

    def test_podcast_tts_batch_is_single_gpu_queue_job(self):
        routes = (API_ROOT / "podcast" / "routes.py").read_text()
        assembler = (API_ROOT / "podcast" / "audio_assembler.py").read_text()
        self.assertIn("async def run_batch", routes)
        self.assertIn('await self._gpu_submit("tts", description, queued_work)', routes)
        self.assertIn('batch_runner(f"Podcast TTS {project_id}", _run_foreground_tts)', assembler)

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
