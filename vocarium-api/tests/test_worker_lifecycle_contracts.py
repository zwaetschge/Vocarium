import ast
import unittest
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
FUNCTION_NODES = (ast.FunctionDef, ast.AsyncFunctionDef)
RUNTIME_SCOPE_NODES = (*FUNCTION_NODES, ast.Lambda, ast.ClassDef)
DIRECT_EXECUTION_STATEMENTS = (
    ast.Expr,
    ast.Assign,
    ast.AnnAssign,
    ast.AugAssign,
    ast.Return,
    ast.Raise,
)


@dataclass(frozen=True)
class ParsedSource:
    name: str
    tree: ast.Module


def runtime_children(scope: ast.AST, node: ast.AST) -> tuple[ast.AST, ...]:
    if isinstance(node, RUNTIME_SCOPE_NODES):
        if node is not scope:
            return ()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            return tuple(node.body)
        assert isinstance(node, ast.Lambda)
        return (node.body,)
    return tuple(ast.iter_child_nodes(node))


def runtime_nodes(scope: ast.AST) -> Iterator[ast.AST]:
    stack = [scope]
    while stack:
        node = stack.pop()
        yield node
        stack.extend(reversed(runtime_children(scope, node)))


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
    nodes = source.tree.body if within is None else runtime_nodes(within)
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
        (node for node in runtime_nodes(scope) if isinstance(node, node_type)),
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
    return any(node is target for node in runtime_nodes(scope))


def block_contains(statements: list[ast.stmt], target: ast.AST) -> bool:
    return any(contains(statement, target) for statement in statements)


def find_try_containing(
    source: ParsedSource,
    function: ast.FunctionDef | ast.AsyncFunctionDef,
    target: ast.AST,
) -> ast.Try:
    matches = [
        node
        for node in runtime_nodes(function)
        if isinstance(node, ast.Try) and block_contains(node.body, target)
    ]
    if not matches:
        raise AssertionError(
            f"{source.name}:{function.name}: proxied/work call must be inside a try body"
        )
    return min(matches, key=lambda node: node.end_lineno - node.lineno)


def unconditional_statement_index(
    source: ParsedSource,
    function: ast.FunctionDef | ast.AsyncFunctionDef,
    statements: list[ast.stmt],
    target: ast.AST,
    *,
    context: str,
) -> int:
    sequential: list[ast.stmt] = []

    def append_sequential(block: list[ast.stmt]) -> None:
        for statement in block:
            sequential.append(statement)
            if isinstance(statement, (ast.With, ast.AsyncWith)):
                append_sequential(statement.body)
            elif isinstance(statement, ast.Try):
                append_sequential(statement.body)

    append_sequential(statements)
    for index, statement in enumerate(sequential):
        if statement is target or (
            isinstance(statement, DIRECT_EXECUTION_STATEMENTS)
            and contains(statement, target)
        ):
            return index
    raise AssertionError(
        f"{source.name}:{function.name}: expected {context} on an unconditional "
        "executable statement path"
    )


def direct_statement_index(
    source: ParsedSource,
    function: ast.FunctionDef | ast.AsyncFunctionDef,
    target: ast.AST,
) -> int:
    return unconditional_statement_index(
        source,
        function,
        function.body,
        target,
        context="lifecycle action in the direct function body",
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
    for node in runtime_nodes(scope):
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
    for node in runtime_nodes(scope):
        if (
            isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "last_used" for target in node.targets)
            and isinstance(node.value, ast.Call)
            and expression_name(node.value.func) == "time.time"
        ):
            return node
    return None


def comparison_in(scope: ast.AST, name: str, operator: type[ast.cmpop]) -> bool:
    if (
        operator is ast.Eq
        and isinstance(scope, ast.BoolOp)
        and isinstance(scope.op, ast.And)
    ):
        return any(comparison_in(value, name, operator) for value in scope.values)
    return (
        isinstance(scope, ast.Compare)
        and isinstance(scope.left, ast.Name)
        and scope.left.id == name
        and len(scope.ops) == 1
        and isinstance(scope.ops[0], operator)
        and len(scope.comparators) == 1
        and isinstance(scope.comparators[0], ast.Constant)
        and scope.comparators[0].value == 0
    )


def parent_map(scope: ast.AST) -> dict[ast.AST, ast.AST]:
    return {
        child: parent
        for parent in runtime_nodes(scope)
        for child in runtime_children(scope, parent)
    }


def guarding_if(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
    action: ast.AST,
    operator: type[ast.cmpop],
) -> ast.If | None:
    parents = parent_map(function)
    current = action
    while current in parents:
        parent = parents[current]
        if isinstance(parent, ast.If):
            in_safe_body = any(statement is current for statement in parent.body)
            if (
                in_safe_body
                and comparison_in(parent.test, "active_requests", operator)
            ):
                return parent
        current = parent
    return None


def guarded_by_zero_check(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
    action: ast.AST,
    operator: type[ast.cmpop],
) -> bool:
    return guarding_if(function, action, operator) is not None


def lifecycle_invocations(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
    target: str,
) -> list[ast.Call]:
    matches: list[ast.Call] = []
    for node in ordered_nodes(function, ast.Call):
        assert isinstance(node, ast.Call)
        if expression_name(node.func) == target:
            matches.append(node)
        elif (
            expression_name(node.func) == "asyncio.to_thread"
            and node.args
            and expression_name(node.args[0]) == target
        ):
            matches.append(node)
    return matches


def find_lifecycle_invocation(
    source: ParsedSource,
    function: ast.FunctionDef | ast.AsyncFunctionDef,
    target: str,
) -> ast.Call:
    matches = lifecycle_invocations(function, target)
    if matches:
        return matches[0]
    raise AssertionError(
        f"{source.name}:{function.name}: expected invocation of lifecycle helper {target!r}"
    )


def guard_precedes_unconditional_action(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
    guard: ast.If,
    action: ast.AST,
) -> bool:
    parents = parent_map(function)
    block_owner = parents.get(guard)
    body = getattr(block_owner, "body", None)
    if not isinstance(body, list) or guard not in body:
        return False

    current = action
    while current in parents and parents[current] is not block_owner:
        current = parents[current]
    if current not in body:
        return False
    if current is not action and not (
        isinstance(current, DIRECT_EXECUTION_STATEMENTS) and contains(current, action)
    ):
        return False
    return body.index(guard) < body.index(current)


def uses_shared_factory(function: ast.FunctionDef | ast.AsyncFunctionDef, call: ast.Call, factory: str) -> bool:
    receiver = call.func.value if isinstance(call.func, ast.Attribute) else None
    if isinstance(receiver, ast.Call) and expression_name(receiver.func) == factory:
        return True
    aliases: set[str] = set()
    for node in runtime_nodes(function):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            if expression_name(node.value.func) == factory:
                aliases.update(target.id for target in node.targets if isinstance(target, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if isinstance(node.value, ast.Call) and expression_name(node.value.func) == factory:
                aliases.add(node.target.id)
    return isinstance(receiver, ast.Name) and receiver.id in aliases


def returned_status(scope: ast.AST, status: str) -> ast.Return | None:
    for node in runtime_nodes(scope):
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


class ASTControlFlowRegressionTest(unittest.TestCase):
    def _function(
        self,
        text: str,
        name: str,
    ) -> tuple[ParsedSource, ast.FunctionDef | ast.AsyncFunctionDef]:
        source = ParsedSource("<synthetic>", ast.parse(text))
        return source, find_function(source, name)

    def test_zero_active_guard_rejects_action_in_else_branch(self):
        source, function = self._function(
            """
def worker():
    if active_requests == 0:
        pass
    else:
        stop_backend()
""",
            "worker",
        )
        action = find_lifecycle_invocation(source, function, "stop_backend")

        self.assertFalse(guarded_by_zero_check(function, action, ast.Eq))

    def test_zero_active_guard_rejects_or_override(self):
        source, function = self._function(
            """
def worker():
    if active_requests == 0 or override:
        stop_backend()
""",
            "worker",
        )
        action = find_lifecycle_invocation(source, function, "stop_backend")

        self.assertFalse(guarded_by_zero_check(function, action, ast.Eq))

    def test_positive_active_guard_rejects_required_override(self):
        source, function = self._function(
            """
def unload():
    if active_requests > 0 and override:
        return {"status": "busy"}
    stop_backend()
""",
            "unload",
        )
        busy = returned_status(function, "busy")
        assert busy is not None

        self.assertFalse(guarded_by_zero_check(function, busy, ast.Gt))

    def test_runtime_lookup_ignores_action_in_uncalled_nested_function(self):
        source, function = self._function(
            """
def worker():
    def never_called():
        stop_backend()
    return None
""",
            "worker",
        )

        with self.assertRaisesRegex(AssertionError, "expected invocation"):
            find_lifecycle_invocation(source, function, "stop_backend")

    def test_conditionally_skipped_acquisition_is_not_direct(self):
        source, function = self._function(
            """
async def proxy():
    if enabled:
        await asyncio.to_thread(acquire_backend)
    try:
        await _client().request()
    finally:
        pass
""",
            "proxy",
        )
        acquisition = find_thread_offload(source, function, "acquire_backend")

        with self.assertRaisesRegex(AssertionError, "unconditional"):
            direct_statement_index(source, function, acquisition)

    def test_conditionally_skipped_step_assignment_is_not_direct(self):
        source, function = self._function(
            """
def generate_audio():
    if apply_steps:
        fm.num_steps = req.num_steps
    audios = generate()
""",
            "generate_audio",
        )
        assignment = next(
            node
            for node in ordered_nodes(function, ast.Assign)
            if expression_name(node.targets[0]) == "fm.num_steps"
        )

        with self.assertRaisesRegex(AssertionError, "unconditional"):
            direct_statement_index(source, function, assignment)


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
        work_try = find_try_containing(source, wrapper, work)
        ownership_lock = find_lock_block(
            source,
            wrapper,
            "model_lock",
            preparation,
            increment,
        )
        self.assertLess(
            unconditional_statement_index(
                source,
                wrapper,
                ownership_lock.body,
                preparation,
                context="model preparation under model_lock",
            ),
            unconditional_statement_index(
                source,
                wrapper,
                ownership_lock.body,
                increment,
                context="active_requests increment under model_lock",
            ),
        )
        self.assertLess(
            direct_statement_index(source, wrapper, ownership_lock),
            direct_statement_index(source, wrapper, work_try),
        )
        unconditional_statement_index(
            source,
            wrapper,
            work_try.body,
            work,
            context="work invocation in the protected try body",
        )

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
            for node in runtime_nodes(wrapper)
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
            any(
                isinstance(node, ast.Return) and contains(node, work)
                for node in runtime_nodes(wrapper)
            ),
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
        release_lock = find_lock_block(
            source,
            wrapper,
            "model_lock",
            decrement,
            last_used,
        )
        unconditional_statement_index(
            source,
            wrapper,
            work_try.finalbody,
            release_lock,
            context="model_lock release block in finally",
        )
        self.assertLess(
            unconditional_statement_index(
                source,
                wrapper,
                release_lock.body,
                decrement,
                context="active_requests decrement under model_lock",
            ),
            unconditional_statement_index(
                source,
                wrapper,
                release_lock.body,
                last_used,
                context="last_used update under model_lock",
            ),
        )

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
            acquire_lock = find_lock_block(source, acquire, "lock", increment)
            unconditional_statement_index(
                source,
                acquire,
                acquire_lock.body,
                increment,
                context="active_requests increment under lock",
            )
        with self.subTest(helper="release_backend"):
            release = find_function(source, "release_backend", is_async=False)
            decrement = next(
                (
                    node
                    for node in runtime_nodes(release)
                    if isinstance(node, ast.Assign)
                    and any(isinstance(target, ast.Name) and target.id == "active_requests" for target in node.targets)
                    and any(
                        isinstance(child, ast.BinOp)
                        and isinstance(child.left, ast.Name)
                        and child.left.id == "active_requests"
                        and isinstance(child.op, ast.Sub)
                        for child in runtime_nodes(node.value)
                    )
                ),
                None,
            )
            self.assertIsNotNone(
                decrement,
                f"{self.SOURCE}:release_backend: expected a bounded active_requests decrement",
            )
            release_lock = find_lock_block(source, release, "lock", decrement)
            unconditional_statement_index(
                source,
                release,
                release_lock.body,
                decrement,
                context="bounded active_requests decrement under lock",
            )

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
        unconditional_statement_index(
            source,
            proxy,
            request_try.finalbody,
            release,
            context="release_backend offload in the proxied request finally block",
        )

    def test_idle_stop_requires_zero_active_requests(self):
        source = parsed_source(self.SOURCE)
        scheduler = find_function(source, "schedule_unload", is_async=False)
        check = find_function(source, "_check", is_async=False, within=scheduler)
        stop = find_lifecycle_invocation(source, check, "stop_backend")
        guard = guarding_if(check, stop, ast.Eq)
        self.assertIsNotNone(
            guard,
            f"{self.SOURCE}:schedule_unload._check: stop_backend must be guarded by active_requests == 0",
        )
        assert guard is not None
        unconditional_statement_index(
            source,
            check,
            guard.body,
            stop,
            context="stop_backend in the zero-active guard body",
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
                for node in runtime_nodes(proxy)
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
            acquire_lock = find_lock_block(source, acquire, "lock", increment)
            unconditional_statement_index(
                source,
                acquire,
                acquire_lock.body,
                increment,
                context="active_requests increment under lock",
            )
        with self.subTest(helper="_release_backend"):
            release = find_function(source, "_release_backend", is_async=False)
            decrement = next(
                (
                    node
                    for node in runtime_nodes(release)
                    if isinstance(node, ast.Assign)
                    and any(isinstance(target, ast.Name) and target.id == "active_requests" for target in node.targets)
                    and any(
                        isinstance(child, ast.BinOp)
                        and isinstance(child.left, ast.Name)
                        and child.left.id == "active_requests"
                        and isinstance(child.op, ast.Sub)
                        for child in runtime_nodes(node.value)
                    )
                ),
                None,
            )
            self.assertIsNotNone(
                decrement,
                f"{self.SOURCE}:_release_backend: expected a bounded active_requests decrement",
            )
            release_lock = find_lock_block(source, release, "lock", decrement)
            unconditional_statement_index(
                source,
                release,
                release_lock.body,
                decrement,
                context="bounded active_requests decrement under lock",
            )

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
                unconditional_statement_index(
                    source,
                    endpoint,
                    request_try.finalbody,
                    release,
                    context="_release_backend in the proxied request finally block",
                )

    def test_idle_and_manual_unload_refuse_active_work(self):
        source = parsed_source(self.SOURCE)
        with self.subTest(path="idle"):
            watcher = find_function(source, "idle_watcher", is_async=True)
            stop = find_lifecycle_invocation(source, watcher, "stop_backend")
            guard = guarding_if(watcher, stop, ast.Eq)
            self.assertIsNotNone(
                guard,
                f"{self.SOURCE}:idle_watcher: stop_backend must be guarded by active_requests == 0",
            )
            assert guard is not None
            unconditional_statement_index(
                source,
                watcher,
                guard.body,
                stop,
                context="stop_backend in the zero-active guard body",
            )
        with self.subTest(path="manual"):
            helper = find_function(source, "_unload_if_idle", is_async=False)
            busy = returned_status(helper, "busy")
            self.assertIsNotNone(
                busy,
                f"{self.SOURCE}:_unload_if_idle: expected busy response while active",
            )
            assert busy is not None
            busy_guard = guarding_if(helper, busy, ast.Gt)
            self.assertIsNotNone(
                busy_guard,
                f"{self.SOURCE}:_unload_if_idle: busy response must be guarded by active_requests > 0",
            )
            assert busy_guard is not None
            unconditional_statement_index(
                source,
                helper,
                busy_guard.body,
                busy,
                context="busy early return in the active-work guard",
            )
            stops = lifecycle_invocations(helper, "stop_backend")
            self.assertTrue(
                stops,
                f"{self.SOURCE}:_unload_if_idle: expected invocation of stop_backend",
            )
            for stop in stops:
                self.assertTrue(
                    guard_precedes_unconditional_action(helper, busy_guard, stop),
                    f"{self.SOURCE}:_unload_if_idle: stop_backend must be reachable "
                    "only after the active-work busy return",
                )
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
                        for node in runtime_nodes(endpoint)
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
        direct_statement_index(source, function, lock_block)
        unconditional_statement_index(
            source,
            function,
            lock_block.body,
            generate_call,
            context="generate() call under the generation lock",
        )
        assigned_to_audios = any(
            isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "audios" for target in node.targets)
            and contains(node.value, generate_call)
            for node in runtime_nodes(lock_block)
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
                for node in runtime_nodes(lock_block)
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
            unconditional_statement_index(
                source,
                function,
                lock_block.body,
                assignment,
                context="fm.num_steps assignment under the generation lock",
            ),
            unconditional_statement_index(
                source,
                function,
                lock_block.body,
                generate_call,
                context="generate() call under the generation lock",
            ),
            f"{source.name}:{function.name}: num_steps assignment must unconditionally "
            "precede generate()",
        )

    def test_cuda_audio_copy_completes_inside_generation_lock(self):
        source, function, generate_call, lock_block = self._generation_context()
        copy_call = next(
            (
                node
                for node in runtime_nodes(lock_block)
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
            unconditional_statement_index(
                source,
                function,
                lock_block.body,
                copy_call,
                context="CUDA-to-CPU copy under the generation lock",
            ),
            unconditional_statement_index(
                source,
                function,
                lock_block.body,
                generate_call,
                context="generate() call under the generation lock",
            ),
            f"{source.name}:{function.name}: CUDA output must be copied "
            "unconditionally after generate() and before unlocking",
        )
