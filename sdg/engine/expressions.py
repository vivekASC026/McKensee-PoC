"""A restricted expression language for derived fields.

Users need to write rules without getting arbitrary code execution. The
expression is parsed with Python's own parser and then walked against a
whitelist: only the node types below are allowed, only the functions below can
be called, and names resolve to row values, never to Python objects. There is
no attribute access on values, no subscripting, no comprehensions, no lambdas,
no imports, and no dunder anything. Anything outside the whitelist is a parse
error, raised before the expression ever runs.

Names come in two shapes:

    CDML_CHG_AMT                      a field in the same row
    dbo.CMC_MEME_MEMBER.MEME_EFF_DT   a field in a parent row

Aggregates over a child table use the same dotted form inside a call:

    sum(dbo.CMC_CDML_CL_LINE.CDML_CHG_AMT)
"""

from __future__ import annotations

import ast
import datetime as dt
from decimal import Decimal
from typing import Any, Callable

AGGREGATES = frozenset({"sum", "count", "avg", "min_of", "max_of"})

# Bounds that keep a short expression from hanging or exhausting a worker. A
# derived-field rule is a line or two; these are far above any real one.
MAX_SOURCE = 2000          # characters in an expression
MAX_NODES = 400            # parsed nodes
MAX_INT_BITS = 4096        # an integer result, about 1,200 digits
MAX_SEQUENCE = 10_000      # characters (or items) a repetition may build


class ExpressionError(ValueError):
    """An expression that is not allowed, or cannot be evaluated."""


ALLOWED_NODES = (
    ast.Expression, ast.BinOp, ast.UnaryOp, ast.BoolOp, ast.Compare, ast.IfExp,
    ast.Call, ast.Name, ast.Attribute, ast.Constant, ast.Load,
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow,
    ast.USub, ast.UAdd, ast.Not, ast.And, ast.Or,
    ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.In, ast.NotIn,
    ast.Tuple, ast.List,
)


def _concat(*parts: Any) -> str:
    return "".join("" if p is None else str(p) for p in parts)


def _coalesce(*parts: Any) -> Any:
    for part in parts:
        if part is not None:
            return part
    return None


def _round(value: Any, digits: int = 0) -> Any:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value.quantize(Decimal(1).scaleb(-int(digits)))
    return round(float(value), int(digits))

def _years_between(start: Any, end: Any) -> int | None:
    if start is None or end is None:
        return None
    start, end = _as_date(start), _as_date(end)
    years = end.year - start.year
    if (end.month, end.day) < (start.month, start.day):
        years -= 1
    return years


def _days_between(start: Any, end: Any) -> int | None:
    if start is None or end is None:
        return None
    return (_as_date(end) - _as_date(start)).days


def _as_date(value: Any) -> dt.date:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    return dt.date.fromisoformat(str(value)[:10])


FUNCTIONS: dict[str, Callable[..., Any]] = {
    "concat": _concat,
    "coalesce": _coalesce,
    "round": _round,
    "abs": lambda v: None if v is None else abs(v),
    "len": lambda v: 0 if v is None else len(str(v)),
    "upper": lambda v: None if v is None else str(v).upper(),
    "lower": lambda v: None if v is None else str(v).lower(),
    "substr": lambda v, start, length=None: (
        None if v is None
        else (str(v)[int(start):] if length is None else str(v)[int(start):int(start) + int(length)])
    ),
    "int": lambda v: None if v is None else int(v),
    "float": lambda v: None if v is None else float(v),
    "decimal": lambda v: None if v is None else Decimal(str(v)),
    "min": lambda *v: min((x for x in v if x is not None), default=None),
    "max": lambda *v: max((x for x in v if x is not None), default=None),
    "years_between": _years_between,
    "days_between": _days_between,
}


def dotted_name(node: ast.AST) -> str | None:
    """Flatten `a.b.c` into 'a.b.c'. Returns None for anything else."""
    parts: list[str] = []
    current = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if not isinstance(current, ast.Name):
        return None
    parts.append(current.id)
    return ".".join(reversed(parts))


class Expression:
    """A parsed, whitelisted expression."""

    __slots__ = ("source", "_tree", "names", "aggregates")

    def __init__(self, source: str) -> None:
        self.source = source
        if len(source) > MAX_SOURCE:
            raise ExpressionError(
                f"the expression is {len(source)} characters; the most allowed is "
                f"{MAX_SOURCE}. Split it across fields."
            )
        try:
            tree = ast.parse(source, mode="eval")
        except SyntaxError as exc:
            raise ExpressionError(f"cannot parse {source!r}: {exc.msg}") from exc
        except (RecursionError, MemoryError) as exc:
            raise ExpressionError(
                f"the expression is nested too deeply to read: {source!r}"
            ) from exc
        _check_nodes(tree, source)
        self._tree = tree
        self.names, self.aggregates = _collect_names(tree)

    def evaluate(self, resolve: Callable[[str], Any], aggregate: Callable[[str, str], Any]) -> Any:
        """Run the expression.

        resolve    given a name, return the row value behind it
        aggregate  given a function name and a dotted child column, return the
                   aggregate over the child rows of the current row
        """
        return _Evaluator(resolve, aggregate, self.source).visit(self._tree.body)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Expression({self.source!r})"


def _check_nodes(tree: ast.AST, source: str) -> None:
    nodes = list(ast.walk(tree))
    if len(nodes) > MAX_NODES:
        raise ExpressionError(
            f"the expression has {len(nodes)} parts; the most allowed is {MAX_NODES}. "
            f"Split it across fields."
        )
    for node in nodes:
        if not isinstance(node, ALLOWED_NODES):
            raise ExpressionError(
                f"{type(node).__name__} is not allowed in an expression: {source!r}"
            )
        if isinstance(node, ast.Call):
            name = node.func.id if isinstance(node.func, ast.Name) else None
            if name is None or (name not in FUNCTIONS and name not in AGGREGATES):
                shown = name or ast.dump(node.func)
                raise ExpressionError(f"unknown function {shown!r} in {source!r}")
            if node.keywords:
                raise ExpressionError(f"keyword arguments are not allowed in {source!r}")
        if isinstance(node, ast.Attribute):
            dotted = dotted_name(node)
            if dotted is None:
                raise ExpressionError(f"attribute access is not allowed in {source!r}")
            # A dotted name is a field reference, never Python attribute access,
            # so no segment of it may look like a dunder.
            for segment in dotted.split("."):
                if segment.startswith("__"):
                    raise ExpressionError(f"name {dotted!r} is not allowed")
        if isinstance(node, ast.Name) and node.id.startswith("__"):
            raise ExpressionError(f"name {node.id!r} is not allowed")


def _collect_names(tree: ast.AST) -> tuple[set[str], set[tuple[str, str]]]:
    """Return plain names and (function, dotted column) aggregate pairs.

    Only whole references count. The segments inside a dotted name, and
    everything inside an aggregate's argument, are part of a larger reference
    and must not be reported as dependencies of their own.
    """
    names: set[str] = set()
    aggregates: set[tuple[str, str]] = set()
    consumed: set[int] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in AGGREGATES:
                if not node.args:
                    raise ExpressionError(f"{node.func.id}() needs a table.column argument")
                target = dotted_name(node.args[0])
                if target is None:
                    raise ExpressionError(f"{node.func.id}() needs a table.column argument")
                aggregates.add((node.func.id, target))
                for inner in ast.walk(node.args[0]):
                    consumed.add(id(inner))

    # Everything below an attribute is a segment of that attribute's dotted name.
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            for inner in ast.walk(node.value):
                consumed.add(id(inner))

    for node in ast.walk(tree):
        if id(node) in consumed:
            continue
        if isinstance(node, ast.Attribute):
            name = dotted_name(node)
            if name:
                names.add(name)
        elif isinstance(node, ast.Name) and node.id not in FUNCTIONS and node.id not in AGGREGATES:
            names.add(node.id)

    return names, aggregates


class _Evaluator:
    def __init__(self, resolve, aggregate, source: str) -> None:
        self.resolve = resolve
        self.aggregate = aggregate
        self.source = source

    def visit(self, node: ast.AST) -> Any:
        method = getattr(self, f"_{type(node).__name__}", None)
        if method is None:
            raise ExpressionError(
                f"{type(node).__name__} is not allowed in an expression: {self.source!r}"
            )
        return method(node)

    def _Constant(self, node: ast.Constant) -> Any:
        return node.value

    def _Name(self, node: ast.Name) -> Any:
        return self.resolve(node.id)

    def _Attribute(self, node: ast.Attribute) -> Any:
        name = dotted_name(node)
        if name is None:
            raise ExpressionError(f"attribute access is not allowed in {self.source!r}")
        return self.resolve(name)

    def _Tuple(self, node: ast.Tuple) -> tuple:
        return tuple(self.visit(e) for e in node.elts)

    def _List(self, node: ast.List) -> list:
        return [self.visit(e) for e in node.elts]

    def _Call(self, node: ast.Call) -> Any:
        name = node.func.id
        if name in AGGREGATES:
            target = dotted_name(node.args[0])
            return self.aggregate(name, target)
        args = [self.visit(a) for a in node.args]
        try:
            return FUNCTIONS[name](*args)
        except (TypeError, ValueError, ArithmeticError) as exc:
            raise ExpressionError(f"{name}() failed in {self.source!r}: {exc}") from exc

    def _UnaryOp(self, node: ast.UnaryOp) -> Any:
        value = self.visit(node.operand)
        try:
            if isinstance(node.op, ast.USub):
                return -value
            if isinstance(node.op, ast.UAdd):
                return +value
        except TypeError as exc:
            raise ExpressionError(
                f"{type(node.op).__name__} failed in {self.source!r}: {exc}"
            ) from exc
        return not value

    def _BinOp(self, node: ast.BinOp) -> Any:
        left, right = self.visit(node.left), self.visit(node.right)
        if left is None or right is None:
            return None
        left, right = _align(left, right)
        op = type(node.op)
        try:
            if op is ast.Add:
                return left + right
            if op is ast.Sub:
                return left - right
            if op is ast.Mult:
                _check_repetition(left, right)
                return left * right
            if op is ast.Div:
                return None if right == 0 else left / right
            if op is ast.FloorDiv:
                return None if right == 0 else left // right
            if op is ast.Mod:
                return None if right == 0 else left % right
            if op is ast.Pow:
                if abs(float(right)) > 64:
                    raise ExpressionError("exponent is too large")
                _check_power(left, right)
                return left**right
        except (TypeError, ArithmeticError) as exc:
            # ArithmeticError covers OverflowError and every decimal signal,
            # InvalidOperation included.
            raise ExpressionError(f"{op.__name__} failed in {self.source!r}: {exc}") from exc
        raise ExpressionError(f"operator {op.__name__} is not allowed")

    def _BoolOp(self, node: ast.BoolOp) -> Any:
        values = [self.visit(v) for v in node.values]
        if isinstance(node.op, ast.And):
            result = True
            for value in values:
                result = result and value
            return result
        result = False
        for value in values:
            result = result or value
        return result

    def _Compare(self, node: ast.Compare) -> Any:
        left = self.visit(node.left)
        for op, comparator in zip(node.ops, node.comparators):
            right = self.visit(comparator)
            try:
                if isinstance(op, ast.In):
                    outcome = right is not None and left in right
                elif isinstance(op, ast.NotIn):
                    outcome = right is None or left not in right
                elif left is None or right is None:
                    outcome = (left is right) if isinstance(op, ast.Eq) else (left is not right)
                else:
                    pair = _align(left, right)
                    outcome = _COMPARISONS[type(op)](*pair)
            except (TypeError, ArithmeticError) as exc:
                raise ExpressionError(
                    f"{type(op).__name__} failed in {self.source!r}: {exc}"
                ) from exc
            if not outcome:
                return False
            left = right
        return True

    def _IfExp(self, node: ast.IfExp) -> Any:
        return self.visit(node.body) if self.visit(node.test) else self.visit(node.orelse)


_COMPARISONS = {
    ast.Eq: lambda a, b: a == b,
    ast.NotEq: lambda a, b: a != b,
    ast.Lt: lambda a, b: a < b,
    ast.LtE: lambda a, b: a <= b,
    ast.Gt: lambda a, b: a > b,
    ast.GtE: lambda a, b: a >= b,
}


def _check_repetition(left: Any, right: Any) -> None:
    """Refuse a string or list repeated into something enormous."""
    for sequence, count in ((left, right), (right, left)):
        if isinstance(sequence, (str, list, tuple)) and isinstance(count, int):
            if len(sequence) * max(count, 0) > MAX_SEQUENCE:
                raise ExpressionError(
                    f"repeating a value {count} times builds more than {MAX_SEQUENCE} "
                    f"characters"
                )
    if isinstance(left, int) and isinstance(right, int):
        if left.bit_length() + right.bit_length() > MAX_INT_BITS:
            raise ExpressionError("the result is too large a number")


def _check_power(base: Any, exponent: Any) -> None:
    """Refuse a power whose integer result would be astronomically large.

    The exponent alone is capped at 64, but powers nest: ((9**64)**64)**64.
    The size of an integer power is the base's bits times the exponent.
    """
    if isinstance(base, int) and isinstance(exponent, int) and exponent > 0:
        if base.bit_length() * exponent > MAX_INT_BITS:
            raise ExpressionError("the result is too large a number")


def _align(left: Any, right: Any) -> tuple[Any, Any]:
    """Make a Decimal and a float comparable without losing the Decimal."""
    if isinstance(left, Decimal) and isinstance(right, float):
        return left, Decimal(repr(right))
    if isinstance(right, Decimal) and isinstance(left, float):
        return Decimal(repr(left)), right
    return left, right


def compile_expression(source: str) -> Expression:
    return Expression(source)
