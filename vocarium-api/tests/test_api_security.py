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
