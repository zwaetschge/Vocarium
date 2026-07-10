import ast
import json
import sys
import unittest
from pathlib import Path

API_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(API_ROOT))


class PublicHealthTest(unittest.TestCase):
    def test_public_health_redacts_voice_ids_and_gpu_processes(self):
        from health_public import build_public_health

        payload = build_public_health(
            {
                "status": "ok",
                "current_model": "1.7b-base",
                "model_loaded": True,
                "active_requests": 1,
                "voices_loaded": ["tenant-private-voice"],
                "voice_clone_prompt_cache": [
                    {"model": "1.7b-base", "voice": "tenant-private-voice"}
                ],
            },
            {
                "enabled": True,
                "available": True,
                "service_gpus": {"tts": "0"},
                "decisions": {
                    "tts": {
                        "service_type": "tts",
                        "target_gpu": "0",
                        "estimated_need_mib": 8500,
                        "allowed": True,
                        "selected": {
                            "reasons": [],
                            "processes": [
                                {"pid": 123, "container": "private-container"}
                            ],
                        },
                    }
                },
            },
        )

        serialized = json.dumps(payload)
        self.assertNotIn("tenant-private-voice", serialized)
        self.assertNotIn("private-container", serialized)
        self.assertNotIn('"pid"', serialized)
        self.assertEqual(
            payload["tts"],
            {
                "status": "ok",
                "current_model": "1.7b-base",
                "model_loaded": True,
                "active_requests": 1,
                "voices_loaded": 1,
            },
        )
        self.assertEqual(payload["gpu_resources"]["service_gpus"], {"tts": 0})
        self.assertEqual(
            payload["gpu_resources"]["decisions"]["tts"],
            {
                "service_type": "tts",
                "target_gpu": 0,
                "estimated_need_mib": 8500,
                "allowed": True,
                "reasons": [],
            },
        )

    def test_public_health_normalizes_malicious_free_form_values(self):
        from health_public import build_public_health

        marker = "SENSITIVE-MARKER-MUST-NOT-LEAK"
        payload = build_public_health(
            {
                "status": marker,
                "current_model": marker,
                "model_loaded": marker,
                "active_requests": 10**100,
                "voices_loaded": 10**100,
            },
            {
                "enabled": marker,
                "available": marker,
                "error": marker,
                "service_gpus": {marker: marker, "tts": marker},
                "decisions": {
                    marker: {
                        "service_type": marker,
                        "allowed": True,
                    },
                    "tts": {
                        "service_type": marker,
                        "target_gpu": marker,
                        "estimated_need_mib": 10**100,
                        "allowed": marker,
                        "reasons": [marker],
                    },
                },
            },
        )

        serialized = json.dumps(payload, sort_keys=True)
        self.assertNotIn(marker, serialized)
        self.assertEqual(payload["tts"]["status"], "unreachable")
        self.assertIsNone(payload["tts"]["current_model"])
        self.assertFalse(payload["tts"]["model_loaded"])
        self.assertEqual(payload["tts"]["active_requests"], 10_000)
        self.assertEqual(payload["tts"]["voices_loaded"], 10_000)
        self.assertFalse(payload["gpu_resources"]["enabled"])
        self.assertFalse(payload["gpu_resources"]["available"])
        self.assertEqual(payload["gpu_resources"]["service_gpus"], {"tts": None})
        self.assertEqual(set(payload["gpu_resources"]["decisions"]), {"tts"})
        self.assertEqual(
            payload["gpu_resources"]["decisions"]["tts"],
            {
                "service_type": "tts",
                "target_gpu": None,
                "estimated_need_mib": 1_048_576,
                "allowed": False,
                "reasons": ["resource_unavailable"],
            },
        )
        self.assertEqual(
            payload["gpu_resources"]["error"], "resource monitor unavailable"
        )

    def test_public_health_never_stringifies_unknown_reason_objects(self):
        from health_public import build_public_health

        class UnknownReason:
            def __str__(self):
                raise AssertionError("unknown public-health values must not be stringified")

        payload = build_public_health(
            {},
            {
                "decisions": {
                    "tts": {
                        "allowed": False,
                        "reasons": [UnknownReason()],
                    }
                }
            },
        )

        self.assertEqual(
            payload["gpu_resources"]["decisions"]["tts"]["reasons"],
            ["resource_unavailable"],
        )

    def test_public_health_uses_safe_defaults_for_malformed_nested_values(self):
        from health_public import build_public_health

        marker = "NESTED-SENSITIVE-MARKER"
        payload = build_public_health(
            {
                "status": [marker],
                "current_model": {"detail": marker},
                "model_loaded": 1,
                "active_requests": "many",
                "voices_loaded": marker,
            },
            {
                "enabled": 1,
                "available": "yes",
                "error": {"detail": marker},
                "service_gpus": [["tts", marker]],
                "decisions": {
                    "tts": {
                        "selected": marker,
                        "reasons": 7,
                    }
                },
            },
        )

        self.assertNotIn(marker, json.dumps(payload, sort_keys=True))
        self.assertEqual(
            payload["tts"],
            {
                "status": "unreachable",
                "current_model": None,
                "model_loaded": False,
                "active_requests": 0,
                "voices_loaded": 0,
            },
        )
        self.assertEqual(
            payload["gpu_resources"],
            {
                "enabled": False,
                "available": False,
                "service_gpus": {},
                "decisions": {
                    "tts": {
                        "service_type": "tts",
                        "target_gpu": None,
                        "estimated_need_mib": 0,
                        "allowed": False,
                        "reasons": ["resource_unavailable"],
                    }
                },
                "error": "resource monitor unavailable",
            },
        )

    def test_public_health_uses_safe_defaults_for_malformed_top_level_values(self):
        from health_public import build_public_health

        expected = {
            "api": "ok",
            "tts": {
                "status": "unreachable",
                "current_model": None,
                "model_loaded": False,
                "active_requests": 0,
                "voices_loaded": 0,
            },
            "gpu_resources": {
                "enabled": False,
                "available": False,
                "service_gpus": {},
                "decisions": {},
            },
        }
        malformed_inputs = ((None, None), ([], ()), ("private", 42))

        for tts_health, gpu_resources in malformed_inputs:
            with self.subTest(tts_health=tts_health, gpu_resources=gpu_resources):
                self.assertEqual(
                    build_public_health(tts_health, gpu_resources),
                    expected,
                )

    def test_health_route_returns_public_health_builder_output(self):
        source = (API_ROOT / "main.py").read_text()
        module = ast.parse(source)
        imports = {
            alias.name
            for node in module.body
            if isinstance(node, ast.ImportFrom) and node.module == "health_public"
            for alias in node.names
        }
        health_functions = [
            node
            for node in module.body
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "health"
        ]

        self.assertIn("build_public_health", imports)
        self.assertEqual(len(health_functions), 1)
        health_function = health_functions[0]
        self.assertTrue(
            any(
                isinstance(decorator, ast.Call)
                and isinstance(decorator.func, ast.Attribute)
                and decorator.func.attr == "get"
                and decorator.args
                and isinstance(decorator.args[0], ast.Constant)
                and decorator.args[0].value == "/api/health"
                for decorator in health_function.decorator_list
            )
        )
        self.assertTrue(
            any(
                isinstance(node, ast.Return)
                and isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Name)
                and node.value.func.id == "build_public_health"
                and [
                    arg.id for arg in node.value.args if isinstance(arg, ast.Name)
                ]
                == ["tts_health", "gpu_resources"]
                for node in ast.walk(health_function)
            )
        )


class AdminAllowlistTest(unittest.TestCase):
    def test_admin_names_are_exact_casefolded_matches(self):
        from access_control import is_admin_username

        self.assertTrue(is_admin_username("API", "api, ops@example.test"))
        self.assertTrue(
            is_admin_username("ops@example.test", "api, ops@example.test")
        )
        self.assertFalse(is_admin_username("tenant", "api, ops@example.test"))
        self.assertFalse(is_admin_username("", "api"))
        self.assertFalse(is_admin_username("api", ""))


class AdminAuthorizationBoundaryTest(unittest.TestCase):
    @staticmethod
    def _load_admin_guard(current_user: dict, configured: str):
        from access_control import is_admin_username

        source = (API_ROOT / "main.py").read_text()
        module = ast.parse(source)
        guard_functions = [
            node
            for node in module.body
            if isinstance(node, ast.FunctionDef) and node.name == "_require_admin"
        ]
        if len(guard_functions) != 1:
            raise AssertionError("main.py must define exactly one _require_admin guard")

        class StubHTTPException(Exception):
            def __init__(self, status_code: int, detail: str):
                super().__init__(detail)
                self.status_code = status_code
                self.detail = detail

        namespace = {
            "Request": object,
            "HTTPException": StubHTTPException,
            "VOCARIUM_ADMIN_USERS": configured,
            "get_current_user": lambda _request: current_user,
            "is_admin_username": is_admin_username,
        }
        guard_module = ast.Module(body=guard_functions, type_ignores=[])
        exec(compile(guard_module, str(API_ROOT / "main.py"), "exec"), namespace)
        return namespace["_require_admin"], StubHTTPException

    def test_non_admin_is_rejected_with_forbidden(self):
        require_admin, http_exception = self._load_admin_guard(
            {"username": "tenant"}, "api"
        )

        with self.assertRaises(http_exception) as raised:
            require_admin(object())

        self.assertEqual(raised.exception.status_code, 403)
        self.assertEqual(raised.exception.detail, "Administrator access required")

    def test_global_process_routes_require_admin(self):
        source = (API_ROOT / "main.py").read_text()
        module = ast.parse(source)

        for route_name in ("switch_model", "admin_artifact_cleanup"):
            with self.subTest(route_name=route_name):
                route_functions = [
                    node
                    for node in module.body
                    if isinstance(node, ast.AsyncFunctionDef)
                    and node.name == route_name
                ]
                self.assertEqual(len(route_functions), 1)
                calls = [
                    node
                    for node in ast.walk(route_functions[0])
                    if isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                ]
                self.assertTrue(
                    any(
                        call.func.id == "_require_admin"
                        and len(call.args) == 1
                        and isinstance(call.args[0], ast.Name)
                        and call.args[0].id == "request"
                        for call in calls
                    )
                )
                self.assertFalse(
                    any(call.func.id == "get_current_user" for call in calls)
                )


class MetricsCardinalityTest(unittest.TestCase):
    def test_unmatched_routes_share_one_bounded_label(self):
        from metrics import route_path_label

        class Matched:
            path = "/api/voices/{voice_id}"

        self.assertEqual(route_path_label(Matched()), "/api/voices/{voice_id}")
        self.assertEqual(route_path_label(None), "__unmatched__")
        self.assertEqual(route_path_label(object()), "__unmatched__")


class UserCreationRaceTest(unittest.TestCase):
    def test_wal_race_converges_and_only_creator_seeds(self):
        import database
        import tempfile
        import threading

        initial_reads = threading.Barrier(2)

        class BufferedCursor:
            def __init__(self, row):
                self._row = row

            def fetchone(self):
                return self._row

        class SynchronizedConnection:
            def __init__(self, connection):
                self._connection = connection
                self._initial_read_complete = False

            def execute(self, sql, params=()):
                if (
                    sql.startswith("SELECT id, username")
                    and not self._initial_read_complete
                ):
                    self._initial_read_complete = True
                    row = self._connection.execute(sql, params).fetchone()
                    initial_reads.wait(timeout=5)
                    return BufferedCursor(row)
                return self._connection.execute(sql, params)

            def commit(self):
                self._connection.commit()

            def rollback(self):
                self._connection.rollback()

            @property
            def total_changes(self):
                return self._connection.total_changes

        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "user-race.db"
            setup = database.sqlite3.connect(db_path)
            try:
                self.assertEqual(
                    setup.execute("PRAGMA journal_mode=WAL").fetchone()[0], "wal"
                )
                setup.executescript(
                    """
                    CREATE TABLE users (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        username TEXT UNIQUE NOT NULL,
                        display_name TEXT DEFAULT '',
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );
                    CREATE TABLE seed_events (
                        kind TEXT NOT NULL,
                        user_id INTEGER NOT NULL
                    );
                    """
                )
                setup.commit()
            finally:
                setup.close()

            raw_connections = [
                database.sqlite3.connect(
                    db_path, timeout=5, check_same_thread=False
                )
                for _ in range(2)
            ]
            for connection in raw_connections:
                connection.execute("PRAGMA busy_timeout=5000")
            connections = [
                SynchronizedConnection(connection) for connection in raw_connections
            ]
            thread_connection = threading.local()
            creator_calls = []
            creator_calls_lock = threading.Lock()
            results = [None, None]
            errors = [None, None]

            def record_seed(kind):
                def seed(user_id, *, db=None, commit=True):
                    connection = db if db is not None else database.get_db()
                    with creator_calls_lock:
                        creator_calls.append(
                            (threading.get_ident(), kind, user_id)
                        )
                    connection.execute(
                        "INSERT INTO seed_events (kind, user_id) VALUES (?, ?)",
                        (kind, user_id),
                    )
                    if commit:
                        connection.commit()
                    return 1

                return seed

            def create_user(index):
                thread_connection.current = connections[index]
                try:
                    results[index] = database.get_or_create_user("race-user")
                except BaseException as exc:
                    errors[index] = exc

            original_get_db = database.get_db
            original_voices = database.seed_prebuilt_custom_voices
            original_hosts = database.seed_prebuilt_hosts
            workers = []
            try:
                database.get_db = lambda: thread_connection.current
                database.seed_prebuilt_custom_voices = record_seed("voices")
                database.seed_prebuilt_hosts = record_seed("hosts")
                workers = [
                    threading.Thread(target=create_user, args=(index,))
                    for index in range(2)
                ]
                for worker in workers:
                    worker.start()
                for worker in workers:
                    worker.join(timeout=10)
            finally:
                database.get_db = original_get_db
                database.seed_prebuilt_custom_voices = original_voices
                database.seed_prebuilt_hosts = original_hosts

            self.assertFalse(any(worker.is_alive() for worker in workers))
            self.assertEqual(errors, [None, None])

            observer = database.sqlite3.connect(db_path)
            try:
                canonical_row = observer.execute(
                    "SELECT id, username FROM users WHERE username=?",
                    ("race-user",),
                ).fetchone()
                seed_events = observer.execute(
                    "SELECT kind, user_id FROM seed_events ORDER BY rowid"
                ).fetchall()
            finally:
                observer.close()
                for connection in raw_connections:
                    connection.close()

        self.assertIsNotNone(canonical_row)
        canonical_id = canonical_row[0]
        self.assertEqual(canonical_row[1], "race-user")
        self.assertEqual([result["id"] for result in results], [canonical_id] * 2)
        self.assertEqual(seed_events, [("voices", canonical_id), ("hosts", canonical_id)])
        self.assertEqual(
            [(kind, user_id) for _thread_id, kind, user_id in creator_calls],
            [("voices", canonical_id), ("hosts", canonical_id)],
        )
        self.assertEqual(len({call[0] for call in creator_calls}), 1)

    def test_seed_failure_rolls_back_user_partial_seed_and_reuses_connection(self):
        import database
        import tempfile

        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "user-rollback.db"
            connection = database.sqlite3.connect(db_path)
            connection.executescript(
                """
                CREATE TABLE users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT UNIQUE NOT NULL,
                    display_name TEXT DEFAULT '',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE seed_events (
                    kind TEXT NOT NULL,
                    user_id INTEGER NOT NULL
                );
                """
            )
            connection.commit()

            def partial_voice_seed(user_id, *, db=None, commit=True):
                active_db = db if db is not None else connection
                active_db.execute(
                    "INSERT INTO seed_events (kind, user_id) VALUES ('voices', ?)",
                    (user_id,),
                )
                return 1

            def failing_host_seed(user_id, *, db=None, commit=True):
                active_db = db if db is not None else connection
                active_db.execute(
                    "INSERT INTO seed_events (kind, user_id) VALUES ('hosts', ?)",
                    (user_id,),
                )
                raise RuntimeError("host seed failed")

            original_get_db = database.get_db
            original_voices = database.seed_prebuilt_custom_voices
            original_hosts = database.seed_prebuilt_hosts
            try:
                database.get_db = lambda: connection
                database.seed_prebuilt_custom_voices = partial_voice_seed
                database.seed_prebuilt_hosts = failing_host_seed
                with self.assertRaisesRegex(RuntimeError, "host seed failed"):
                    database.get_or_create_user("rollback-user")

                state_after_failure = (
                    connection.in_transaction,
                    connection.execute(
                        "SELECT COUNT(*) FROM users WHERE username=?",
                        ("rollback-user",),
                    ).fetchone()[0],
                    connection.execute(
                        "SELECT COUNT(*) FROM seed_events"
                    ).fetchone()[0],
                )
                self.assertEqual(state_after_failure, (False, 0, 0))

                connection.execute(
                    "INSERT INTO users (username, display_name) VALUES (?, ?)",
                    ("after-rollback", "after-rollback"),
                )
                connection.commit()
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM users WHERE username=?",
                        ("after-rollback",),
                    ).fetchone()[0],
                    1,
                )
            finally:
                database.get_db = original_get_db
                database.seed_prebuilt_custom_voices = original_voices
                database.seed_prebuilt_hosts = original_hosts
                connection.close()
