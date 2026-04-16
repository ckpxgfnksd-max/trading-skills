"""Signal expression parser and evaluator.

Supports a minimal expression language:
  Variables: main_net, retail_net, price, ma5, ma10, ma20, ma60
  Operators: >, <, >=, <=, ==, and, or
  Literals:  integers and floats

Example: "main_net > 0 and price > ma20"
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional


VALID_VARS = {"main_net", "retail_net", "price", "ma5", "ma10", "ma20", "ma60"}

# Tokenizer: split on whitespace but keep operators together
_TOKEN_RE = re.compile(r">=|<=|==|>|<|and|or|[a-z_][a-z_0-9]*|[0-9]+(?:\.[0-9]+)?")


@dataclass
class SignalResult:
    triggered: bool
    expression: str
    variables: dict[str, Optional[float]]


def tokenize(expr: str) -> list[str]:
    return _TOKEN_RE.findall(expr.lower())


def parse_required_mas(expr: str) -> set[int]:
    """Extract which MA periods are needed by the expression."""
    tokens = tokenize(expr)
    periods = set()
    for tok in tokens:
        if tok.startswith("ma") and tok[2:].isdigit():
            periods.add(int(tok[2:]))
    return periods


def evaluate(expr: str, variables: dict[str, Optional[float]]) -> SignalResult:
    """Evaluate a signal expression against provided variables.

    Returns SignalResult with triggered=False if any referenced variable is None.
    """
    tokens = tokenize(expr)
    if not tokens:
        return SignalResult(triggered=False, expression=expr, variables=variables)

    # Check all referenced variables are available
    for tok in tokens:
        if tok in VALID_VARS and variables.get(tok) is None:
            return SignalResult(triggered=False, expression=expr, variables=variables)

    def _resolve(tok: str) -> float:
        if tok in VALID_VARS:
            return variables[tok]
        try:
            return float(tok)
        except ValueError:
            raise ValueError(f"unknown token in signal expression: {tok!r}")

    # Simple recursive descent: expr = or_expr
    # or_expr  = and_expr ("or" and_expr)*
    # and_expr = cmp_expr ("and" cmp_expr)*
    # cmp_expr = value op value
    # value    = variable | number

    pos = 0

    def _peek() -> Optional[str]:
        nonlocal pos
        return tokens[pos] if pos < len(tokens) else None

    def _advance() -> str:
        nonlocal pos
        tok = tokens[pos]
        pos += 1
        return tok

    def _cmp_expr() -> bool:
        left = _resolve(_advance())
        op = _advance()
        right = _resolve(_advance())
        if op == ">":
            return left > right
        if op == "<":
            return left < right
        if op == ">=":
            return left >= right
        if op == "<=":
            return left <= right
        if op == "==":
            return left == right
        raise ValueError(f"unknown operator: {op!r}")

    def _and_expr() -> bool:
        result = _cmp_expr()
        while _peek() == "and":
            _advance()
            result = _cmp_expr() and result
        return result

    def _or_expr() -> bool:
        result = _and_expr()
        while _peek() == "or":
            _advance()
            result = _and_expr() or result
        return result

    try:
        triggered = _or_expr()
    except (ValueError, IndexError) as e:
        raise ValueError(f"invalid signal expression: {expr!r} ({e})")

    return SignalResult(triggered=triggered, expression=expr, variables=variables)


def compute_ma(closes: list[float], period: int) -> Optional[float]:
    """Compute simple moving average from a list of closing prices.

    Returns None if not enough data.
    """
    if len(closes) < period:
        return None
    return sum(closes[-period:]) / period
