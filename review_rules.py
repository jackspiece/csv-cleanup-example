"""Small, opt-in local CSV review rules. All values remain exact strings."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path


MAX_RULE_BYTES = 1024 * 1024


class RulesError(ValueError):
    """A rule file is invalid; no cleanup output should be published."""


def _object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise RulesError("Rules JSON contains a duplicate object key.")
        result[key] = value
    return result


def _nonfinite(value: str) -> None:
    raise RulesError("Rules JSON must not contain non-finite numbers.")


def _number(value: str) -> None:
    raise RulesError("Rules JSON values must be strings, not numbers.")


def _valid_string(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        value.encode("utf-8")
    except UnicodeError:
        return False
    return True


def _column_name(value: object) -> bool:
    return _valid_string(value) and bool(value.strip())


@dataclass(frozen=True)
class ReviewRules:
    source: str
    sha256: str
    required_nonblank: tuple[str, ...]
    allowed_values: tuple[tuple[str, frozenset[str]], ...]

    def bind(self, header: list[str]) -> BoundRules:
        """Resolve names against the actual header, after optional trimming."""
        names = set(self.required_nonblank) | {name for name, _ in self.allowed_values}
        if not names.issubset(header):
            raise RulesError("A rule column is missing from the effective CSV header.")
        if any(header.count(name) != 1 for name in names):
            raise RulesError("Rule columns must identify exactly one CSV column.")
        return BoundRules(
            tuple((header.index(name), name) for name in self.required_nonblank),
            tuple((header.index(name), name, values) for name, values in self.allowed_values),
        )

    def metadata(self) -> dict[str, object]:
        return {
            "source": self.source,
            "sha256": self.sha256,
            "required_nonblank": list(self.required_nonblank),
            "allowed_value_columns": [name for name, _ in self.allowed_values],
        }


@dataclass(frozen=True)
class BoundRules:
    required: tuple[tuple[int, str], ...]
    allowed: tuple[tuple[int, str, frozenset[str]], ...]

    def failures(self, row: list[str]) -> list[dict[str, object]]:
        """Validate a structurally sound, optionally trimmed row without editing it."""
        failures: list[dict[str, object]] = []
        for index, name in self.required:
            if not row[index].strip():
                failures.append({"code": "required_nonblank", "column": name,
                                 "column_index": index + 1})
        for index, name, values in self.allowed:
            if row[index] not in values:
                failures.append({"code": "not_allowed_value", "column": name,
                                 "column_index": index + 1})
        return failures


def failure_reason(failures: list[dict[str, object]]) -> str:
    parts = []
    for failure in failures:
        name = json.dumps(failure["column"], ensure_ascii=False)
        if failure["code"] == "required_nonblank":
            parts.append(f"Required nonblank column {name} is blank")
        else:
            parts.append(f"Column {name} is not an allowed exact value")
    return "; ".join(parts)


def load_rules(path: Path | str) -> ReviewRules:
    path = Path(path)
    if not path.is_file():
        raise RulesError("Rules source must be a file.")
    with path.open("rb") as source:
        raw = source.read(MAX_RULE_BYTES + 1)
    if len(raw) > MAX_RULE_BYTES:
        raise RulesError("Rules file exceeds the 1 MiB limit.")
    try:
        config = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_object,
                            parse_constant=_nonfinite, parse_int=_number, parse_float=_number)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise RulesError("Rules file must be complete, valid UTF-8 JSON.") from exc
    if not isinstance(config, dict):
        raise RulesError("Rules JSON must be an object.")
    if set(config) - {"required_nonblank", "allowed_values"}:
        raise RulesError("Rules JSON contains an unknown key.")
    required = config.get("required_nonblank", [])
    if not isinstance(required, list) or any(not _column_name(name) for name in required):
        raise RulesError("required_nonblank must be an array of nonblank column-name strings.")
    if len(set(required)) != len(required):
        raise RulesError("required_nonblank contains a duplicate column.")
    allowed = config.get("allowed_values", {})
    if not isinstance(allowed, dict):
        raise RulesError("allowed_values must be an object mapping column names to string arrays.")
    for name, values in allowed.items():
        if not _column_name(name):
            raise RulesError("allowed_values requires nonblank column names.")
        if (not isinstance(values, list) or not values
                or any(not _valid_string(value) for value in values)):
            raise RulesError("Each allowed_values entry must be a nonempty array of strings.")
        if len(set(values)) != len(values):
            raise RulesError("An allowed_values entry contains a duplicate value.")
    if not required and not allowed:
        raise RulesError("Rules must define at least one check.")
    return ReviewRules(path.name, hashlib.sha256(raw).hexdigest(), tuple(required),
                       tuple((name, frozenset(values)) for name, values in allowed.items()))
