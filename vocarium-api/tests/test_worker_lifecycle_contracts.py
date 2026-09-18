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


