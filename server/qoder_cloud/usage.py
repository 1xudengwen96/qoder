"""Strict usage normalization. Missing Credits are never treated as free usage."""

from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

MICRO_CREDITS = 1_000_000
MAX_LEDGER_UNITS = 2**63 - 1


class InvalidUsage(ValueError):
    """Contains a field name only, never an untrusted upstream value."""


def _counter(value: Any, field: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidUsage(f"invalid counter: {field}")
    if not 0 <= value <= MAX_LEDGER_UNITS:
        raise InvalidUsage(f"counter out of range: {field}")
    return value


def _credits(value: Any) -> int | None:
    if value is None:
        return None
    # JSON is decoded with parse_float=Decimal. Binary floats are not ledger input.
    if isinstance(value, bool) or not isinstance(value, (int, str, Decimal)):
        raise InvalidUsage("invalid Credits type")
    try:
        number = Decimal(value)
    except (InvalidOperation, ValueError):
        raise InvalidUsage("invalid Credits value") from None
    if not number.is_finite() or number < 0:
        raise InvalidUsage("Credits must be finite and nonnegative")
    # Use the Decimal coefficient directly: arithmetic in the default decimal
    # context could silently round a large or excessively precise upstream value.
    _, digits, exponent = number.as_tuple()
    if not any(digits):
        return 0
    coefficient = "".join(map(str, digits))
    shift = exponent + 6
    if shift < 0:
        trailing_zeros = len(coefficient) - len(coefficient.rstrip("0"))
        removable = min(-shift, trailing_zeros)
        if removable:
            coefficient = coefficient[:-removable]
            shift += removable
        if shift < 0:
            raise InvalidUsage("Credits precision exceeds six decimal places")
    if len(coefficient) + shift > 19:
        raise InvalidUsage("Credits exceed ledger range")
    units = int(coefficient) * 10**shift
    if units > MAX_LEDGER_UNITS:
        raise InvalidUsage("Credits exceed ledger range")
    return units


def _alias(data: dict, names: tuple[str, ...], decoder, field: str):
    values = [decoder(data[name], field) for name in names if data.get(name) is not None]
    if not values:
        return None
    if any(value != values[0] for value in values[1:]):
        raise InvalidUsage(f"conflicting usage aliases: {field}")
    return values[0]


@dataclass(frozen=True)
class UsageSnapshot:
    credits_micro: int | None
    prompt_tokens: int | None
    completion_tokens: int | None
    cached_tokens: int | None
    reasoning_tokens: int | None
    total_tokens: int | None
    billable: bool | None

    def report(self) -> dict:
        return asdict(self)


def normalize_usage(data: Any) -> UsageSnapshot:
    if not isinstance(data, dict):
        raise InvalidUsage("usage must be an object")
    credits = _alias(data, ("credits", "credit"), lambda value, _: _credits(value), "credits")
    prompt = _alias(data, ("prompt_tokens", "input_tokens"), _counter, "prompt_tokens")
    completion = _alias(data, ("completion_tokens", "output_tokens"), _counter, "completion_tokens")
    total = _counter(data.get("total_tokens"), "total_tokens")
    details = []
    for field in ("completion_tokens_details", "prompt_tokens_details", "input_tokens_details"):
        value = data.get(field)
        if value is not None and not isinstance(value, dict):
            raise InvalidUsage("token details must be objects")
        details.append(value or {})
    completion_details, prompt_details, input_details = details
    cache_values = [
        value for value in (
            _counter(data.get("prompt_cache_hit_tokens"), "cached_tokens"),
            _counter(prompt_details.get("cached_tokens"), "cached_tokens"),
            _counter(input_details.get("cached_tokens"), "cached_tokens"),
            _counter(completion_details.get("cached_tokens"), "cached_tokens"),
        ) if value is not None
    ]
    if len(set(cache_values)) > 1:
        raise InvalidUsage("conflicting usage aliases: cached_tokens")
    cached = cache_values[0] if cache_values else None
    reasoning = _counter(completion_details.get("reasoning_tokens"), "reasoning_tokens")
    billable = data.get("billable")
    if billable is not None and not isinstance(billable, bool):
        raise InvalidUsage("billable must be a boolean")
    if billable is False and credits not in (None, 0):
        raise InvalidUsage("non-billable usage contains positive Credits")
    if cached is not None and prompt is not None and cached > prompt:
        raise InvalidUsage("cached tokens exceed prompt tokens")
    return UsageSnapshot(credits, prompt, completion, cached, reasoning, total, billable)


def compare_credits(observed_micro: int | None, official_credits: Any, *, sample_ready: bool) -> dict:
    """离线核对手工提供的同笔官方消耗；数字一致不代表身份或请求归属已验证。"""
    observed = _counter(observed_micro, "observed_credits_micro")
    official = _credits(official_credits)
    if official is None or not isinstance(sample_ready, bool):
        raise InvalidUsage("official comparison requires a value and sample state")
    difference = observed - official if observed is not None else None
    if observed is None:
        state = "credits_missing"
    elif not sample_ready:
        state = "sample_unconfirmed"
    elif difference == 0:
        state = "matched"
    else:
        state = "mismatch"
    return {"state": state, "official_credits_micro": official,
            "observed_credits_micro": observed, "difference_micro": difference,
            "official_value_source": "manual_unverified", "request_correlation_verified": False,
            "diagnostic_only": True}
