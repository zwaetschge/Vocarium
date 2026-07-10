import ast
import unittest
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
FUNCTION_NODES = (ast.FunctionDef, ast.AsyncFunctionDef)


@dataclass(frozen=True)
class ParsedSource:
    name: str
    tree: ast.Module


def parsed_source(name: str) -> ParsedSource:
    path = REPO_ROOT / name
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise AssertionError(f"{name}: unable to read worker source: {exc}") from exc
    try:
        tree = ast.parse(text, filename=name)
    except SyntaxError as exc:
        raise AssertionError(f"{name}: worker source does not parse: {exc}") from exc
    return ParsedSource(name, tree)


def find_function(
    source: ParsedSource,
    name: str,
    *,
    is_async: bool | None = None,
    within: ast.AST | None = None,
) -> ast.FunctionDef | ast.AsyncFunctionDef:
    nodes = source.tree.body if within is None else ast.walk(within)
    matches = [
        node
        for node in nodes
        if isinstance(node, FUNCTION_NODES) and node.name == name
    ]
    location = "top-level " if within is None else "nested "
    if not matches:
        raise AssertionError(f"{source.name}: expected {location}function {name!r}")
    function = matches[0]
    if is_async is not None and isinstance(function, ast.AsyncFunctionDef) != is_async:
        kind = "async " if is_async else "blocking "
        raise AssertionError(
            f"{source.name}:{name}: expected a {kind}function definition"
        )
    return function


def expression_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = expression_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    if isinstance(node, ast.Call):
        return f"{expression_name(node.func)}()"
    return ""


def ordered_nodes(scope: ast.AST, node_type: type[ast.AST]) -> list[ast.AST]:
    return sorted(
        (node for node in ast.walk(scope) if isinstance(node, node_type)),
        key=lambda node: (getattr(node, "lineno", -1), getattr(node, "col_offset", -1)),
    )


def find_call(
    source: ParsedSource,
    function: ast.FunctionDef | ast.AsyncFunctionDef,
    callee: str,
    *,
    within: ast.AST | None = None,
    context: str | None = None,
) -> ast.Call:
    scope = within or function
    for node in ordered_nodes(scope, ast.Call):
        assert isinstance(node, ast.Call)
        if expression_name(node.func) == callee:
            return node
    where = context or f"function {function.name!r}"
    raise AssertionError(
        f"{source.name}:{function.name}: expected call to {callee!r} in {where}"
    )


def find_method_call(
    source: ParsedSource,
    function: ast.FunctionDef | ast.AsyncFunctionDef,
    methods: set[str],
) -> ast.Call:
    for node in ordered_nodes(function, ast.Call):
        assert isinstance(node, ast.Call)
        if isinstance(node.func, ast.Attribute) and node.func.attr in methods:
            return node
    expected = "/".join(sorted(methods))
    raise AssertionError(
        f"{source.name}:{function.name}: expected proxied HTTP {expected} call"
    )


def find_thread_offload(
    source: ParsedSource,
    function: ast.FunctionDef | ast.AsyncFunctionDef,
    target: str,
    *,
    within: ast.AST | None = None,
    context: str | None = None,
) -> ast.Await:
    scope = within or function
    for node in ordered_nodes(scope, ast.Await):
        assert isinstance(node, ast.Await)
        call = node.value
        if (
            isinstance(call, ast.Call)
            and expression_name(call.func) == "asyncio.to_thread"
            and call.args
            and expression_name(call.args[0]) == target
        ):
            return node
    where = context or f"function {function.name!r}"
    raise AssertionError(
        f"{source.name}:{function.name}: expected awaited asyncio.to_thread"
        f"({target}, ...) in {where}"
    )


def find_awaited_call(
    source: ParsedSource,
    function: ast.FunctionDef | ast.AsyncFunctionDef,
    target: str,
) -> ast.Await:
    for node in ordered_nodes(function, ast.Await):
        assert isinstance(node, ast.Await)
        if isinstance(node.value, ast.Call) and expression_name(node.value.func) == target:
            return node
    raise AssertionError(
        f"{source.name}:{function.name}: expected awaited call to {target!r}"
    )


def contains(scope: ast.AST, target: ast.AST) -> bool:
    return any(node is target for node in ast.walk(scope))


def block_contains(statements: list[ast.stmt], target: ast.AST) -> bool:
    return any(contains(statement, target) for statement in statements)


def find_try_containing(
    source: ParsedSource,
    function: ast.FunctionDef | ast.AsyncFunctionDef,
    target: ast.AST,
) -> ast.Try:
    matches = [
        node
        for node in ast.walk(function)
        if isinstance(node, ast.Try) and block_contains(node.body, target)
    ]
    if not matches:
        raise AssertionError(
            f"{source.name}:{function.name}: proxied/work call must be inside a try body"
        )
    return min(matches, key=lambda node: node.end_lineno - node.lineno)


def direct_statement_index(
    source: ParsedSource,
    function: ast.FunctionDef | ast.AsyncFunctionDef,
    target: ast.AST,
) -> int:
    for index, statement in enumerate(function.body):
        if contains(statement, target):
            return index
    raise AssertionError(
        f"{source.name}:{function.name}: expected lifecycle action in direct function body"
    )


def find_lock_block(
    source: ParsedSource,
    function: ast.FunctionDef | ast.AsyncFunctionDef,
    lock_name: str,
    *required_nodes: ast.AST,
) -> ast.With:
    for node in ordered_nodes(function, ast.With):
        assert isinstance(node, ast.With)
        uses_lock = any(expression_name(item.context_expr) == lock_name for item in node.items)
        if uses_lock and all(contains(node, required) for required in required_nodes):
            return node
    raise AssertionError(
        f"{source.name}:{function.name}: expected {lock_name!r} with-block to contain "
        "the required lifecycle operations"
    )


def find_augassign(scope: ast.AST, name: str, operation: type[ast.operator]) -> ast.AugAssign | None:
    for node in ast.walk(scope):
        if (
            isinstance(node, ast.AugAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == name
            and isinstance(node.op, operation)
            and isinstance(node.value, ast.Constant)
            and node.value.value == 1
        ):
            return node
    return None


def find_last_used_update(scope: ast.AST) -> ast.Assign | None:
    for node in ast.walk(scope):
        if (
            isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "last_used" for target in node.targets)
            and isinstance(node.value, ast.Call)
            and expression_name(node.value.func) == "time.time"
        ):
            return node
    return None


def comparison_in(scope: ast.AST, name: str, operator: type[ast.cmpop]) -> bool:
    for node in ast.walk(scope):
        if not isinstance(node, ast.Compare) or len(node.ops) != 1:
            continue
        if (
            isinstance(node.left, ast.Name)
            and node.left.id == name
            and isinstance(node.ops[0], operator)
            and len(node.comparators) == 1
            and isinstance(node.comparators[0], ast.Constant)
            and node.comparators[0].value == 0
        ):
            return True
    return False


def parent_map(scope: ast.AST) -> dict[ast.AST, ast.AST]:
    return {child: parent for parent in ast.walk(scope) for child in ast.iter_child_nodes(parent)}


def guarded_by_zero_check(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
    action: ast.AST,
    operator: type[ast.cmpop],
) -> bool:
    parents = parent_map(function)
    current = action
    while current in parents:
        current = parents[current]
        if isinstance(current, ast.If) and comparison_in(current.test, "active_requests", operator):
            return True
    return False


def find_lifecycle_invocation(
    source: ParsedSource,
    function: ast.FunctionDef | ast.AsyncFunctionDef,
    target: str,
) -> ast.Call:
    for node in ordered_nodes(function, ast.Call):
        assert isinstance(node, ast.Call)
        if expression_name(node.func) == target:
            return node
        if (
            expression_name(node.func) == "asyncio.to_thread"
            and node.args
            and expression_name(node.args[0]) == target
        ):
            return node
    raise AssertionError(
        f"{source.name}:{function.name}: expected invocation of lifecycle helper {target!r}"
    )


def uses_shared_factory(function: ast.FunctionDef | ast.AsyncFunctionDef, call: ast.Call, factory: str) -> bool:
    receiver = call.func.value if isinstance(call.func, ast.Attribute) else None
    if isinstance(receiver, ast.Call) and expression_name(receiver.func) == factory:
        return True
    aliases: set[str] = set()
    for node in ast.walk(function):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            if expression_name(node.value.func) == factory:
                aliases.update(target.id for target in node.targets if isinstance(target, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if isinstance(node.value, ast.Call) and expression_name(node.value.func) == factory:
                aliases.add(node.target.id)
    return isinstance(receiver, ast.Name) and receiver.id in aliases


def returned_status(scope: ast.AST, status: str) -> ast.Return | None:
    for node in ast.walk(scope):
        if not isinstance(node, ast.Return) or not isinstance(node.value, ast.Dict):
            continue
        for key, value in zip(node.value.keys, node.value.values):
            if (
                isinstance(key, ast.Constant)
                and key.value == "status"
                and isinstance(value, ast.Constant)
                and value.value == status
            ):
                return node
    return None


class TTSLifecycleContractTest(unittest.TestCase):
    SOURCE = "qwen3-tts/server.py"

    def test_wrapper_prepares_model_and_increments_before_work(self):
        source = parsed_source(self.SOURCE)
        wrapper = find_function(source, "_run_with_model", is_async=False)
        preparation = find_call(source, wrapper, "_ensure_model_locked")
        increment = find_augassign(wrapper, "active_requests", ast.Add)
        self.assertIsNotNone(
            increment,
            f"{self.SOURCE}:_run_with_model: expected active_requests += 1",
        )
        work = find_call(source, wrapper, "work")
        find_lock_block(source, wrapper, "model_lock", preparation, increment)
        self.assertLess(preparation.lineno, increment.lineno)
        self.assertLess(increment.lineno, work.lineno)

    def test_wrapper_passes_one_immutable_model_id_to_returned_work(self):
        source = parsed_source(self.SOURCE)
        wrapper = find_function(source, "_run_with_model", is_async=False)
        work = find_call(source, wrapper, "work")
        self.assertEqual(
            [expression_name(argument) for argument in work.args],
            ["used_model_id"],
            f"{self.SOURCE}:_run_with_model: work must receive the captured used_model_id",
        )
        stores = [
            node
            for node in ast.walk(wrapper)
            if isinstance(node, ast.Name)
            and node.id == "used_model_id"
            and isinstance(node.ctx, ast.Store)
        ]
        self.assertEqual(
            len(stores),
            1,
            f"{self.SOURCE}:_run_with_model: used_model_id must be assigned exactly once",
        )
        self.assertTrue(
            any(isinstance(node, ast.Return) and contains(node, work) for node in ast.walk(wrapper)),
            f"{self.SOURCE}:_run_with_model: expected return work(used_model_id)",
        )

    def test_wrapper_releases_ownership_and_updates_last_used_in_locked_finally(self):
        source = parsed_source(self.SOURCE)
        wrapper = find_function(source, "_run_with_model", is_async=False)
        work = find_call(source, wrapper, "work")
        work_try = find_try_containing(source, wrapper, work)
        decrement = find_augassign(work_try, "active_requests", ast.Sub)
        self.assertIsNotNone(
            decrement,
            f"{self.SOURCE}:_run_with_model: expected active_requests -= 1 in finally",
        )
        last_used = find_last_used_update(work_try)
        self.assertIsNotNone(
            last_used,
            f"{self.SOURCE}:_run_with_model: expected last_used = time.time() in finally",
        )
        self.assertTrue(block_contains(work_try.finalbody, decrement))
        self.assertTrue(block_contains(work_try.finalbody, last_used))
        find_lock_block(source, wrapper, "model_lock", decrement, last_used)

    def test_explicit_model_load_is_offloaded(self):
        source = parsed_source(self.SOURCE)
        endpoint = find_function(source, "load_model_endpoint", is_async=True)
        offload = find_thread_offload(source, endpoint, "ensure_model")
        call = offload.value
        assert isinstance(call, ast.Call)
        self.assertGreaterEqual(len(call.args), 2)
        self.assertEqual(expression_name(call.args[1]), "request.model_id")

    def test_explicit_model_unload_is_offloaded(self):
        source = parsed_source(self.SOURCE)
        endpoint = find_function(source, "unload", is_async=True)
        find_thread_offload(source, endpoint, "_unload_if_idle")


class ASRLifecycleContractTest(unittest.TestCase):
    SOURCE = "asr_proxy.py"

    def test_active_counter_helpers_are_lock_scoped(self):
        source = parsed_source(self.SOURCE)
        with self.subTest(helper="acquire_backend"):
            acquire = find_function(source, "acquire_backend", is_async=False)
            increment = find_augassign(acquire, "active_requests", ast.Add)
            self.assertIsNotNone(
                increment,
                f"{self.SOURCE}:acquire_backend: expected active_requests += 1",
            )
            find_lock_block(source, acquire, "lock", increment)
        with self.subTest(helper="release_backend"):
            release = find_function(source, "release_backend", is_async=False)
            decrement = next(
                (
                    node
                    for node in ast.walk(release)
                    if isinstance(node, ast.Assign)
                    and any(isinstance(target, ast.Name) and target.id == "active_requests" for target in node.targets)
                    and any(
                        isinstance(child, ast.BinOp)
                        and isinstance(child.left, ast.Name)
                        and child.left.id == "active_requests"
                        and isinstance(child.op, ast.Sub)
                        for child in ast.walk(node.value)
                    )
                ),
                None,
            )
            self.assertIsNotNone(
                decrement,
                f"{self.SOURCE}:release_backend: expected a bounded active_requests decrement",
            )
            find_lock_block(source, release, "lock", decrement)

    def test_acquisition_is_offloaded_before_proxied_request(self):
        source = parsed_source(self.SOURCE)
        proxy = find_function(source, "proxy", is_async=True)
        request_call = find_method_call(source, proxy, {"request"})
        acquisition = find_thread_offload(source, proxy, "acquire_backend")
        request_try = find_try_containing(source, proxy, request_call)
        self.assertLess(
            direct_statement_index(source, proxy, acquisition),
            direct_statement_index(source, proxy, request_try),
            f"{self.SOURCE}:proxy: acquire_backend must complete before the request try",
        )

    def test_release_is_offloaded_from_proxied_request_finally(self):
        source = parsed_source(self.SOURCE)
        proxy = find_function(source, "proxy", is_async=True)
        request_call = find_method_call(source, proxy, {"request"})
        request_try = find_try_containing(source, proxy, request_call)
        find_thread_offload(
            source,
            proxy,
            "release_backend",
            within=request_try,
            context="the proxied request finally block",
        )
        release = find_thread_offload(source, proxy, "release_backend", within=request_try)
        self.assertTrue(
            block_contains(request_try.finalbody, release),
            f"{self.SOURCE}:proxy: release_backend must execute from finally",
        )

    def test_idle_stop_requires_zero_active_requests(self):
        source = parsed_source(self.SOURCE)
        scheduler = find_function(source, "schedule_unload", is_async=False)
        check = find_function(source, "_check", is_async=False, within=scheduler)
        stop = find_lifecycle_invocation(source, check, "stop_backend")
        self.assertTrue(
            guarded_by_zero_check(check, stop, ast.Eq),
            f"{self.SOURCE}:schedule_unload._check: stop_backend must be guarded by active_requests == 0",
        )

    def test_proxy_reuses_shared_http_client(self):
        source = parsed_source(self.SOURCE)
        find_function(source, "_client", is_async=False)
        proxy = find_function(source, "proxy", is_async=True)
        request_call = find_method_call(source, proxy, {"request"})
        self.assertTrue(
            uses_shared_factory(proxy, request_call, "_client"),
            f"{self.SOURCE}:proxy: proxied HTTP request must use _client()",
        )
        self.assertFalse(
            any(
                isinstance(node, ast.Call) and expression_name(node.func) == "httpx.AsyncClient"
                for node in ast.walk(proxy)
            ),
            f"{self.SOURCE}:proxy: must not construct a per-request httpx.AsyncClient",
        )


class AceLifecycleContractTest(unittest.TestCase):
    SOURCE = "acestep/proxy.py"
    ENDPOINTS = ("release_task", "query_result", "get_audio", "format_input", "list_models")

    def test_active_counter_helpers_are_lock_scoped(self):
        source = parsed_source(self.SOURCE)
        with self.subTest(helper="_acquire_backend"):
            acquire = find_function(source, "_acquire_backend", is_async=True)
            increment = find_augassign(acquire, "active_requests", ast.Add)
            self.assertIsNotNone(
                increment,
                f"{self.SOURCE}:_acquire_backend: expected active_requests += 1",
            )
            find_lock_block(source, acquire, "lock", increment)
        with self.subTest(helper="_release_backend"):
            release = find_function(source, "_release_backend", is_async=False)
            decrement = next(
                (
                    node
                    for node in ast.walk(release)
                    if isinstance(node, ast.Assign)
                    and any(isinstance(target, ast.Name) and target.id == "active_requests" for target in node.targets)
                    and any(
                        isinstance(child, ast.BinOp)
                        and isinstance(child.left, ast.Name)
                        and child.left.id == "active_requests"
                        and isinstance(child.op, ast.Sub)
                        for child in ast.walk(node.value)
                    )
                ),
                None,
            )
            self.assertIsNotNone(
                decrement,
                f"{self.SOURCE}:_release_backend: expected a bounded active_requests decrement",
            )
            find_lock_block(source, release, "lock", decrement)

    def test_each_proxy_endpoint_acquires_before_its_http_request(self):
        source = parsed_source(self.SOURCE)
        for endpoint_name in self.ENDPOINTS:
            with self.subTest(endpoint=endpoint_name, invariant="acquire-before-request"):
                endpoint = find_function(source, endpoint_name, is_async=True)
                request_call = find_method_call(source, endpoint, {"get", "post"})
                acquisition = find_awaited_call(source, endpoint, "_acquire_backend")
                request_try = find_try_containing(source, endpoint, request_call)
                self.assertLess(
                    direct_statement_index(source, endpoint, acquisition),
                    direct_statement_index(source, endpoint, request_try),
                    f"{self.SOURCE}:{endpoint_name}: acquire must complete before request try",
                )

    def test_each_proxy_endpoint_releases_from_finally(self):
        source = parsed_source(self.SOURCE)
        for endpoint_name in self.ENDPOINTS:
            with self.subTest(endpoint=endpoint_name, invariant="release-from-finally"):
                endpoint = find_function(source, endpoint_name, is_async=True)
                request_call = find_method_call(source, endpoint, {"get", "post"})
                request_try = find_try_containing(source, endpoint, request_call)
                release = find_call(
                    source,
                    endpoint,
                    "_release_backend",
                    within=request_try,
                    context="the proxied request finally block",
                )
                self.assertTrue(
                    block_contains(request_try.finalbody, release),
                    f"{self.SOURCE}:{endpoint_name}: _release_backend must execute from finally",
                )

    def test_idle_and_manual_unload_refuse_active_work(self):
        source = parsed_source(self.SOURCE)
        with self.subTest(path="idle"):
            watcher = find_function(source, "idle_watcher", is_async=True)
            stop = find_lifecycle_invocation(source, watcher, "stop_backend")
            self.assertTrue(
                guarded_by_zero_check(watcher, stop, ast.Eq),
                f"{self.SOURCE}:idle_watcher: stop_backend must be guarded by active_requests == 0",
            )
        with self.subTest(path="manual"):
            helper = find_function(source, "_unload_if_idle", is_async=False)
            busy = returned_status(helper, "busy")
            self.assertIsNotNone(
                busy,
                f"{self.SOURCE}:_unload_if_idle: expected busy response while active",
            )
            self.assertTrue(
                guarded_by_zero_check(helper, busy, ast.Gt),
                f"{self.SOURCE}:_unload_if_idle: busy response must be guarded by active_requests > 0",
            )
            find_lifecycle_invocation(source, helper, "stop_backend")
            endpoint = find_function(source, "unload", is_async=True)
            find_thread_offload(source, endpoint, "_unload_if_idle")

    def test_proxy_endpoints_reuse_shared_http_session(self):
        source = parsed_source(self.SOURCE)
        find_function(source, "_session", is_async=False)
        for endpoint_name in self.ENDPOINTS:
            with self.subTest(endpoint=endpoint_name, invariant="shared-session"):
                endpoint = find_function(source, endpoint_name, is_async=True)
                request_call = find_method_call(source, endpoint, {"get", "post"})
                self.assertTrue(
                    uses_shared_factory(endpoint, request_call, "_session"),
                    f"{self.SOURCE}:{endpoint_name}: HTTP call must use _session()",
                )
                self.assertFalse(
                    any(
                        isinstance(node, ast.Call)
                        and expression_name(node.func) == "aiohttp.ClientSession"
                        for node in ast.walk(endpoint)
                    ),
                    f"{self.SOURCE}:{endpoint_name}: must not create a per-request ClientSession",
                )


class MMAudioLifecycleContractTest(unittest.TestCase):
    SOURCE = "mmaudio/server.py"

    def _generation_context(self) -> tuple[ParsedSource, ast.FunctionDef, ast.Call, ast.With]:
        source = parsed_source(self.SOURCE)
        function = find_function(source, "_generate_sfx_blocking", is_async=False)
        generate_call = find_call(source, function, "generate")
        lock_block = find_lock_block(source, function, "lock", generate_call)
        assigned_to_audios = any(
            isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "audios" for target in node.targets)
            and contains(node.value, generate_call)
            for node in ast.walk(lock_block)
        )
        self.assertTrue(
            assigned_to_audios,
            f"{self.SOURCE}:_generate_sfx_blocking: generate() result must bind audios",
        )
        return source, function, generate_call, lock_block

    def test_requested_steps_are_assigned_before_generation_under_lock(self):
        source, function, generate_call, lock_block = self._generation_context()
        assignment = next(
            (
                node
                for node in ast.walk(lock_block)
                if isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and expression_name(node.targets[0]) == "fm.num_steps"
                and expression_name(node.value) == "req.num_steps"
            ),
            None,
        )
        self.assertIsNotNone(
            assignment,
            f"{source.name}:{function.name}: expected exact fm.num_steps = req.num_steps under lock",
        )
        self.assertLess(
            assignment.lineno,
            generate_call.lineno,
            f"{source.name}:{function.name}: num_steps assignment must precede generate()",
        )

    def test_cuda_audio_copy_completes_inside_generation_lock(self):
        source, function, generate_call, lock_block = self._generation_context()
        copy_call = next(
            (
                node
                for node in ast.walk(lock_block)
                if isinstance(node, ast.Call)
                and expression_name(node.func) == "audios.float().detach().cpu"
            ),
            None,
        )
        self.assertIsNotNone(
            copy_call,
            f"{source.name}:{function.name}: expected audios.float().detach().cpu() inside with lock",
        )
        self.assertGreater(
            copy_call.lineno,
            generate_call.lineno,
            f"{source.name}:{function.name}: CUDA output must be copied after generate() and before unlocking",
        )
