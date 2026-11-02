"""Hand-written schema validation for raw provider records.

Validation is explicit (not pydantic) because it runs on every one of the
hundreds of thousands of raw lines per day. Each validator raises
:class:`SchemaError` with the source line; :class:`ValidationReport` decides
whether that aborts the run (strict) or is counted and skipped (lenient).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from bybit_demo.errors import SchemaError

BYBIT_OB_TYPES = frozenset({"snapshot", "delta"})
BYBIT_TRADE_COLUMNS = ("timestamp", "symbol", "side", "size", "price")
TARDIS_BOOK_COLUMNS = (
    "exchange",
    "symbol",
    "timestamp",
    "local_timestamp",
    "is_snapshot",
    "side",
    "price",
    "amount",
)
TARDIS_TRADE_COLUMNS = (
    "exchange",
    "symbol",
    "timestamp",
    "local_timestamp",
    "id",
    "side",
    "price",
    "amount",
)


@dataclass(slots=True)
class ValidationReport:
    """Counts valid / malformed records; keeps the first ``max_examples`` errors."""

    source: str
    strict: bool = False
    max_examples: int = 20
    records_ok: int = 0
    records_malformed: int = 0
    examples: list[str] = field(default_factory=list)

    def ok(self) -> None:
        self.records_ok += 1

    def malformed(self, err: SchemaError) -> None:
        if self.strict:
            raise err
        self.records_malformed += 1
        if len(self.examples) < self.max_examples:
            self.examples.append(str(err))

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "records_ok": self.records_ok,
            "records_malformed": self.records_malformed,
            "error_examples": self.examples,
        }


def parse_decimal(value: Any, name: str, *, source: str, line: int, positive: bool) -> Decimal:
    """Parse a non-negative (or strictly positive) finite decimal string; raise SchemaError."""
    if type(value) is not str:
        raise SchemaError(
            f"{name} must be a decimal string, got {type(value).__name__}", source=source, line=line
        )
    try:
        d = Decimal(value)
    except InvalidOperation as exc:
        raise SchemaError(f"{name}={value!r} is not a decimal", source=source, line=line) from exc
    if not d.is_finite() or d < 0 or (positive and not d):
        raise SchemaError(f"{name}={value!r} out of range", source=source, line=line)
    return d


def validate_bybit_ob_message(msg: Any, *, source: str, line: int) -> None:
    """Structure check for one Bybit orderbook archive / WebSocket message."""

    def need(cond: bool, what: str) -> None:
        if not cond:
            raise SchemaError(what, source=source, line=line)

    need(isinstance(msg, dict), "message is not a JSON object")
    for key in ("topic", "type", "ts", "data"):
        need(key in msg, f"missing field {key!r}")
    need(msg["type"] in BYBIT_OB_TYPES, f"unknown type {msg['type']!r}")
    need(isinstance(msg["ts"], int) and not isinstance(msg["ts"], bool), "ts must be int ms")
    need("cts" not in msg or isinstance(msg["cts"], int), "cts must be int ms")
    data = msg["data"]
    need(isinstance(data, dict), "data is not an object")
    for key in ("s", "b", "a", "u"):
        need(key in data, f"missing field data.{key}")
    need(isinstance(data["u"], int), "data.u must be int")
    need("seq" not in data or isinstance(data["seq"], int), "data.seq must be int")
    # Per-level [price, qty] shape is enforced while parsing (normalization._levels).
    need(type(data["b"]) is list and type(data["a"]) is list, "data.b / data.a must be lists")


def require_columns(header: list[str], required: tuple[str, ...], *, source: str) -> None:
    missing = [c for c in required if c not in header]
    if missing:
        raise SchemaError(f"missing CSV columns {missing}; header={header}", source=source, line=1)
