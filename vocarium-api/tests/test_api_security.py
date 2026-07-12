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


class _A5DatabaseTestCase(unittest.TestCase):
    def setUp(self):
        import database
        import tempfile

        self.database = database
        self._original_db = database._db
        self._db_path_existed = hasattr(database, "_db_path")
        self._original_db_path = getattr(database, "_db_path", None)
        self._temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self._temp_dir.name) / "a5.db"
        database.init_db(self.db_path)
        self.db = database.get_db()

    def tearDown(self):
        current_db = self.database._db
        if current_db is not None and current_db is not self._original_db:
            current_db.close()
        self.database._db = self._original_db
        if self._db_path_existed:
            self.database._db_path = self._original_db_path
        elif hasattr(self.database, "_db_path"):
            del self.database._db_path
        self._temp_dir.cleanup()


class UserCreationRaceTest(_A5DatabaseTestCase):
    def test_wal_race_converges_and_only_creator_seeds(self):
        import threading

        database = self.database
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

            @property
            def row_factory(self):
                return self._connection.row_factory

            @property
            def isolation_level(self):
                return self._connection.isolation_level

        raw_connections = [
            database.sqlite3.connect(
                self.db_path, timeout=5, check_same_thread=False
            )
            for _ in range(2)
        ]
        for connection in raw_connections:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA busy_timeout=5000")
            connection.execute("PRAGMA foreign_keys=ON")
        connections = [
            SynchronizedConnection(connection) for connection in raw_connections
        ]
        thread_connection = threading.local()
        creator_calls = []
        creator_calls_lock = threading.Lock()
        results = [None, None]
        errors = [None, None]

        original_get_db = database.get_db
        original_speakers = database._BUILTIN_SPEAKERS
        original_instructs = database._BUILTIN_SPEAKER_INSTRUCTS
        original_profiles = database._BUILTIN_HOST_PROFILES
        original_uuid4 = database.uuid.uuid4

        class CountingProfiles(dict):
            def get(self, key, default=None):
                with creator_calls_lock:
                    creator_calls.append((threading.get_ident(), "hosts"))
                return super().get(key, default)

        def counted_uuid4():
            with creator_calls_lock:
                creator_calls.append((threading.get_ident(), "voices"))
            return original_uuid4()

        def create_user(index):
            thread_connection.current = connections[index]
            try:
                results[index] = database.get_or_create_user("race-user")
            except BaseException as exc:
                errors[index] = exc

        workers = []
        try:
            database.get_db = lambda: thread_connection.current
            database._BUILTIN_SPEAKERS = [("Race", "female", "German")]
            database._BUILTIN_SPEAKER_INSTRUCTS = {"Race": ""}
            database._BUILTIN_HOST_PROFILES = CountingProfiles(
                {"Race": ("Race personality", "Race style", "host")}
            )
            database.uuid.uuid4 = counted_uuid4
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
            database._BUILTIN_SPEAKERS = original_speakers
            database._BUILTIN_SPEAKER_INSTRUCTS = original_instructs
            database._BUILTIN_HOST_PROFILES = original_profiles
            database.uuid.uuid4 = original_uuid4

        self.assertFalse(any(worker.is_alive() for worker in workers))
        self.assertEqual(errors, [None, None])

        observer = database.sqlite3.connect(self.db_path)
        try:
            canonical_row = observer.execute(
                "SELECT id, username FROM users WHERE username=?",
                ("race-user",),
            ).fetchone()
            voice_rows = observer.execute(
                "SELECT user_id, speaker FROM voices WHERE speaker='Race'"
            ).fetchall()
            host_rows = observer.execute(
                "SELECT user_id, name FROM hosts WHERE name='Race'"
            ).fetchall()
        finally:
            observer.close()
            for connection in raw_connections:
                connection.close()

        self.assertIsNotNone(canonical_row)
        canonical_id = canonical_row[0]
        self.assertEqual(canonical_row[1], "race-user")
        self.assertEqual([result["id"] for result in results], [canonical_id] * 2)
        self.assertEqual(voice_rows, [(canonical_id, "Race")])
        self.assertEqual(host_rows, [(canonical_id, "Race")])
        self.assertEqual(
            [kind for _thread_id, kind in creator_calls],
            ["voices", "hosts"],
        )
        self.assertEqual(len({call[0] for call in creator_calls}), 1)

    def test_other_request_cannot_commit_failed_creators_partial_work(self):
        import threading

        database = self.database
        partial_seeded = threading.Event()
        other_request_complete = threading.Event()
        first_errors = []
        second_errors = []

        original_speakers = database._BUILTIN_SPEAKERS
        original_instructs = database._BUILTIN_SPEAKER_INSTRUCTS
        original_profiles = database._BUILTIN_HOST_PROFILES

        class PausingFailingProfiles(dict):
            def get(self, key, default=None):
                partial_seeded.set()
                if not other_request_complete.wait(timeout=5):
                    raise AssertionError("second request did not complete")
                raise RuntimeError("host seed failed")

        def create_first_user():
            try:
                database.get_or_create_user("interleaved-user")
            except BaseException as exc:
                first_errors.append(exc)

        def complete_other_request():
            try:
                database.get_db().execute("SELECT 1").fetchone()
                database.get_db().commit()
            except BaseException as exc:
                second_errors.append(exc)
            finally:
                other_request_complete.set()

        try:
            database._BUILTIN_SPEAKERS = [("Interleave", "female", "German")]
            database._BUILTIN_SPEAKER_INSTRUCTS = {"Interleave": ""}
            database._BUILTIN_HOST_PROFILES = PausingFailingProfiles(
                {"Interleave": ("Personality", "Style", "host")}
            )
            first = threading.Thread(target=create_first_user)
            first.start()
            self.assertTrue(partial_seeded.wait(timeout=5))

            second = threading.Thread(target=complete_other_request)
            second.start()
            second.join(timeout=5)
            first.join(timeout=5)
        finally:
            database._BUILTIN_SPEAKERS = original_speakers
            database._BUILTIN_SPEAKER_INSTRUCTS = original_instructs
            database._BUILTIN_HOST_PROFILES = original_profiles

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(second_errors, [])
        self.assertEqual(len(first_errors), 1)
        self.assertIsInstance(first_errors[0], RuntimeError)
        self.assertEqual(str(first_errors[0]), "host seed failed")

        observer = database.sqlite3.connect(self.db_path)
        try:
            state = (
                observer.execute(
                    "SELECT COUNT(*) FROM users WHERE username='interleaved-user'"
                ).fetchone()[0],
                observer.execute(
                    "SELECT COUNT(*) FROM voices WHERE speaker='Interleave'"
                ).fetchone()[0],
                observer.execute(
                    "SELECT COUNT(*) FROM hosts WHERE name='Interleave'"
                ).fetchone()[0],
            )
        finally:
            observer.close()
        self.assertEqual(state, (0, 0, 0))

    def test_seed_failure_rolls_back_user_partial_seed_and_reuses_connection(self):
        database = self.database
        original_speakers = database._BUILTIN_SPEAKERS
        original_instructs = database._BUILTIN_SPEAKER_INSTRUCTS
        original_profiles = database._BUILTIN_HOST_PROFILES

        class FailingProfiles(dict):
            def get(self, key, default=None):
                raise RuntimeError("host seed failed")

        try:
            database._BUILTIN_SPEAKERS = [("Rollback", "female", "German")]
            database._BUILTIN_SPEAKER_INSTRUCTS = {"Rollback": ""}
            database._BUILTIN_HOST_PROFILES = FailingProfiles(
                {"Rollback": ("Personality", "Style", "host")}
            )
            with self.assertRaisesRegex(RuntimeError, "host seed failed"):
                database.get_or_create_user("rollback-user")
        finally:
            database._BUILTIN_SPEAKERS = original_speakers
            database._BUILTIN_SPEAKER_INSTRUCTS = original_instructs
            database._BUILTIN_HOST_PROFILES = original_profiles

        observer = database.sqlite3.connect(self.db_path)
        try:
            state_after_failure = (
                observer.execute(
                    "SELECT COUNT(*) FROM users WHERE username='rollback-user'"
                ).fetchone()[0],
                observer.execute(
                    "SELECT COUNT(*) FROM voices WHERE speaker='Rollback'"
                ).fetchone()[0],
                observer.execute(
                    "SELECT COUNT(*) FROM hosts WHERE name='Rollback'"
                ).fetchone()[0],
            )
        finally:
            observer.close()
        self.assertEqual(state_after_failure, (0, 0, 0))

        self.db.execute(
            "INSERT INTO users (username, display_name) VALUES (?, ?)",
            ("after-rollback", "after-rollback"),
        )
        self.db.commit()
        self.assertEqual(
            self.db.execute(
                "SELECT COUNT(*) FROM users WHERE username='after-rollback'"
            ).fetchone()[0],
            1,
        )


class SeedTransactionOwnershipTest(_A5DatabaseTestCase):
    def setUp(self):
        super().setUp()
        cursor = self.db.execute(
            "INSERT INTO users (username, display_name) VALUES (?, ?)",
            ("seed-owner", "seed-owner"),
        )
        self.user_id = cursor.lastrowid
        self.db.commit()

    def test_public_seed_helpers_keep_one_argument_api(self):
        import inspect

        for helper in (
            self.database.seed_prebuilt_custom_voices,
            self.database.seed_prebuilt_hosts,
        ):
            with self.subTest(helper=helper.__name__):
                self.assertEqual(
                    list(inspect.signature(helper).parameters), ["user_id"]
                )

    def test_public_custom_voice_helper_commits_its_work(self):
        inserted = self.database.seed_prebuilt_custom_voices(self.user_id)
        observer = self.database.sqlite3.connect(self.db_path)
        try:
            visible = observer.execute(
                "SELECT COUNT(*) FROM voices WHERE user_id=?",
                (self.user_id,),
            ).fetchone()[0]
        finally:
            observer.close()
        self.assertEqual((inserted, visible), (9, 9))

    def test_public_host_helper_commits_its_work(self):
        self.database.seed_prebuilt_custom_voices(self.user_id)
        inserted = self.database.seed_prebuilt_hosts(self.user_id)
        observer = self.database.sqlite3.connect(self.db_path)
        try:
            visible = observer.execute(
                "SELECT COUNT(*) FROM hosts WHERE user_id=?",
                (self.user_id,),
            ).fetchone()[0]
        finally:
            observer.close()
        self.assertEqual((inserted, visible), (9, 9))

    def test_private_custom_voice_seed_does_not_commit_unrelated_dml(self):
        self.assertTrue(
            hasattr(self.database, "_seed_prebuilt_custom_voices"),
            "missing private non-committing custom-voice seed primitive",
        )
        primitive = self.database._seed_prebuilt_custom_voices
        self.db.execute(
            "INSERT INTO users (username, display_name) VALUES ('unrelated', 'x')"
        )
        inserted = primitive(self.db, self.user_id)
        own_state = (
            self.db.execute(
                "SELECT COUNT(*) FROM users WHERE username='unrelated'"
            ).fetchone()[0],
            self.db.execute(
                "SELECT COUNT(*) FROM voices WHERE user_id=?", (self.user_id,)
            ).fetchone()[0],
        )
        observer = self.database.sqlite3.connect(self.db_path)
        try:
            visible_state = (
                observer.execute(
                    "SELECT COUNT(*) FROM users WHERE username='unrelated'"
                ).fetchone()[0],
                observer.execute(
                    "SELECT COUNT(*) FROM voices WHERE user_id=?", (self.user_id,)
                ).fetchone()[0],
            )
        finally:
            observer.close()
            self.db.rollback()
        self.assertEqual((inserted, own_state, visible_state), (9, (1, 9), (0, 0)))

    def test_private_host_seed_does_not_commit_unrelated_dml(self):
        self.assertTrue(
            hasattr(self.database, "_seed_prebuilt_hosts"),
            "missing private non-committing host seed primitive",
        )
        self.database.seed_prebuilt_custom_voices(self.user_id)
        primitive = self.database._seed_prebuilt_hosts
        self.db.execute(
            "INSERT INTO users (username, display_name) VALUES ('unrelated', 'x')"
        )
        inserted = primitive(self.db, self.user_id)
        own_state = (
            self.db.execute(
                "SELECT COUNT(*) FROM users WHERE username='unrelated'"
            ).fetchone()[0],
            self.db.execute(
                "SELECT COUNT(*) FROM hosts WHERE user_id=?", (self.user_id,)
            ).fetchone()[0],
        )
        observer = self.database.sqlite3.connect(self.db_path)
        try:
            visible_state = (
                observer.execute(
                    "SELECT COUNT(*) FROM users WHERE username='unrelated'"
                ).fetchone()[0],
                observer.execute(
                    "SELECT COUNT(*) FROM hosts WHERE user_id=?", (self.user_id,)
                ).fetchone()[0],
            )
        finally:
            observer.close()
            self.db.rollback()
        self.assertEqual((inserted, own_state, visible_state), (9, (1, 9), (0, 0)))

    def test_owned_creation_connection_matches_main_configuration(self):
        self.assertTrue(
            hasattr(self.database, "_open_user_creation_connection"),
            "missing independently owned user-creation connection",
        )
        self.db.row_factory = self.database.sqlite3.Row
        owned = self.database._open_user_creation_connection(self.db)
        try:
            main_path = self.db.execute("PRAGMA database_list").fetchone()[2]
            owned_path = owned.execute("PRAGMA database_list").fetchone()[2]
            self.assertIsNot(owned, self.db)
            self.assertIsInstance(owned, self.database.InstrumentedConnection)
            self.assertEqual(owned_path, main_path)
            self.assertEqual(
                owned.execute("PRAGMA journal_mode").fetchone()[0], "wal"
            )
            self.assertEqual(
                owned.execute("PRAGMA busy_timeout").fetchone()[0], 5000
            )
            self.assertEqual(
                owned.execute("PRAGMA foreign_keys").fetchone()[0], 1
            )
            self.assertIs(owned.row_factory, self.database.sqlite3.Row)
            self.assertEqual(owned.isolation_level, self.db.isolation_level)
        finally:
            owned.close()

    def test_missing_user_rechecks_owned_connection_and_closes_it(self):
        self.assertTrue(
            hasattr(self.database, "_open_user_creation_connection"),
            "missing independently owned user-creation connection",
        )
        self.db.execute(
            "INSERT INTO users (username, display_name) VALUES (?, ?)",
            ("won-elsewhere", "Canonical Winner"),
        )
        self.db.commit()
        raw_owned = self.database.sqlite3.connect(self.db_path)

        class MissingMain:
            row_factory = None

            def execute(self, sql, params=()):
                if sql.startswith("SELECT id, username"):
                    return type("Cursor", (), {"fetchone": lambda self: None})()
                raise AssertionError(sql)

        class TrackingConnection:
            def __init__(self, connection):
                self._connection = connection
                self.statements = []
                self.closed = False

            def execute(self, sql, params=()):
                self.statements.append(sql)
                return self._connection.execute(sql, params)

            def commit(self):
                self._connection.commit()

            def rollback(self):
                self._connection.rollback()

            def close(self):
                self.closed = True
                self._connection.close()

        tracking = TrackingConnection(raw_owned)
        original_get_db = self.database.get_db
        original_open = self.database._open_user_creation_connection
        try:
            self.database.get_db = lambda: MissingMain()
            self.database._open_user_creation_connection = lambda _main: tracking
            user = self.database.get_or_create_user("won-elsewhere")
        finally:
            self.database.get_db = original_get_db
            self.database._open_user_creation_connection = original_open
            if not tracking.closed:
                tracking.close()

        self.assertEqual(user["display_name"], "Canonical Winner")
        self.assertTrue(tracking.closed)
        self.assertTrue(
            any(sql.startswith("SELECT id, username") for sql in tracking.statements)
        )
        self.assertFalse(
            any(sql.startswith("INSERT") for sql in tracking.statements)
        )
