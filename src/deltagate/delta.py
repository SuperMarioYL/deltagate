"""Per-field delta semantics shared by init, calibrate and check.

This module is the single definition of how re-extracted fields are compared,
so ``rules``, ``calibrate``, ``gate`` and ``report`` import these definitions
instead of re-declaring them:

* how a value is normalized before comparison,
* which :class:`FieldKind` a field has, inferred from its name and a
  representative value, and the conservative :data:`DEFAULT_LIMITS` a fresh
  rules file starts from,
* how the default rules table for a snapshot is built (:func:`infer_limits`),
* how the difference between two values becomes a ``delta`` measured against a
  :class:`FieldLimit`, and
* whether that delta breaches the limit, or whether the record's shape changed
  (a field added, removed or an array resized).

Kind inference and the default limits are not incidental helpers: ``init``
writes them into the rules file and ``calibrate`` refines their numbers, so
keeping them here is what lets a limit, the comparison that measured it and
``limits_version`` (the rules file hash, owned by ``rules``) describe one set of
semantics.

It reads no files and knows no directory layout. Callers pass a mapping of
``dotted_path -> value`` per record and a ``path -> FieldLimit`` table per
output type.
"""

from __future__ import annotations

import datetime as dt
import math
import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from rapidfuzz import fuzz

DECISION_RELEASE = "RELEASE"
DECISION_REVIEW = "REVIEW"
Decision = Literal["RELEASE", "REVIEW"]

# Exit codes are part of the CLI contract and are defined here once so the
# command layer can import them instead of repeating the numbers.
EXIT_OK = 0
EXIT_USAGE = 2
EXIT_REVIEW = 3
EXIT_MALFORMED = 4


class MalformedValue(ValueError):
    """A value cannot be interpreted under its declared field kind."""


class FieldKind(StrEnum):
    MONEY = "money"
    NUMERIC = "numeric"
    DATE = "date"
    CODE = "code"
    TEXT = "text"


class LimitUnit(StrEnum):
    RELATIVE = "relative"
    ABSOLUTE = "absolute"
    DAYS = "days"
    EDIT_RATIO = "edit_ratio"
    EXACT = "exact"


class FieldLimit(BaseModel):
    """One field's tolerance, as written in the rules file."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: FieldKind
    unit: LimitUnit
    limit: float = 0.0
    floor: float = 0.0
    normalize: tuple[str, ...] = ()


class FieldChange(BaseModel):
    """The measured difference for one field path."""

    model_config = ConfigDict(frozen=True)

    path: str
    before: Any
    after: Any
    delta: float | None
    limit: float | None
    breached: bool


class StructuralChange(BaseModel):
    """A shape change, which can never be released without review.

    For ``length_changed`` the ``before``/``after`` values are the array
    lengths; for ``added``/``removed`` they are the field values.
    """

    model_config = ConfigDict(frozen=True)

    kind: Literal["added", "removed", "length_changed"]
    path: str
    before: Any = None
    after: Any = None


class Verdict(BaseModel):
    """The per-record decision; there is no batch-level scoring path."""

    record_id: str
    decision: Decision
    changes: list[FieldChange] = Field(default_factory=list)
    structural: list[StructuralChange] = Field(default_factory=list)
    limits_version: str


_INDEX = re.compile(r"\[\d+\]")
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# Boundaries a field name uses instead of an underscore: ``invoiceNumber``
# splits into ``invoice`` + ``Number``, ``docNo`` into ``doc`` + ``No``, and an
# acronym before a word (``SKUCode``) splits before the word.
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")

_MONEY_HINTS = (
    "amount",
    "total",
    "subtotal",
    "price",
    "fee",
    "tax",
    "cost",
    "balance",
    "discount",
    "金额",
    "合计",
    "价",
    "税",
)
_DATE_HINTS = ("date", "time", "deadline", "due", "日期", "时间")
_CODE_HINTS = ("id", "code", "no", "num", "number", "sn", "sku", "ref", "编号", "号")
# Short abbreviations that name an identifier only when they qualify another
# word ("invoiceNum", "order_no"). Leading them they usually count something
# ("numItems"), so they must not turn a count into an exact-match code.
_TRAILING_CODE_HINTS = frozenset({"num", "no", "sn"})

# Conservative starting points written by ``init``. They are deliberately
# quiet for money and numeric fields and exact for codes and dates, so a fresh
# rules file does not bury the reviewer in rewording noise.
DEFAULT_LIMITS: dict[FieldKind, FieldLimit] = {
    FieldKind.MONEY: FieldLimit(
        kind=FieldKind.MONEY, unit=LimitUnit.RELATIVE, limit=0.005, floor=0.01
    ),
    FieldKind.NUMERIC: FieldLimit(
        kind=FieldKind.NUMERIC, unit=LimitUnit.RELATIVE, limit=0.01
    ),
    FieldKind.DATE: FieldLimit(kind=FieldKind.DATE, unit=LimitUnit.DAYS),
    FieldKind.CODE: FieldLimit(
        kind=FieldKind.CODE,
        unit=LimitUnit.EXACT,
        normalize=("nfkc", "strip_ws", "cf_upper"),
    ),
    FieldKind.TEXT: FieldLimit(
        kind=FieldKind.TEXT,
        unit=LimitUnit.EDIT_RATIO,
        limit=0.2,
        normalize=("nfkc", "strip_ws"),
    ),
}


def normalize(value: Any, steps: Sequence[str]) -> str:
    """Apply the rule's normalization steps to a value before comparison."""

    text = "" if value is None else str(value)
    for step in steps:
        if step == "nfkc":
            text = unicodedata.normalize("NFKC", text)
        elif step == "strip_ws":
            text = " ".join(text.split())
        elif step == "cf_upper":
            text = text.casefold().upper()
        else:
            raise ValueError(f"unknown normalize step: {step!r}")
    return text


def infer_kind(path: str, value: Any) -> FieldKind:
    """Pick a field kind from the field name and a representative value."""

    leaf = path.rsplit(".", 1)[-1].split("[", 1)[0]

    if isinstance(value, bool):
        return FieldKind.CODE
    if isinstance(value, (int, float)):
        # Identifiers are frequently numeric (``invoice_no: 1001``); a code is
        # exact after normalization, so the numeric test must not swallow them.
        if _has_hint(leaf, _CODE_HINTS):
            return FieldKind.CODE
        # ``issue_date: 20261110`` is a date spelled as an integer; comparing
        # it as a numeric would hide a one-day change behind a relative limit.
        if _has_hint(leaf, _DATE_HINTS) and _yyyymmdd(value) is not None:
            return FieldKind.DATE
        if _has_hint(leaf, _MONEY_HINTS):
            return FieldKind.MONEY
        return FieldKind.NUMERIC
    if isinstance(value, dt.date):
        return FieldKind.DATE

    text = "" if value is None else str(value).strip()
    if _ISO_DATE.match(text):
        return FieldKind.DATE
    if _has_hint(leaf, _DATE_HINTS) and _number(text) is not None:
        return FieldKind.DATE
    # As for numbers, an identifier stays a code even when its name also
    # carries a money word (``tax_id``); otherwise the same field would be
    # exact as a number and tolerant as a string.
    if _has_hint(leaf, _CODE_HINTS):
        return FieldKind.CODE
    if _has_hint(leaf, _MONEY_HINTS) and _number(text) is not None:
        return FieldKind.MONEY
    if _number(text) is not None:
        return FieldKind.NUMERIC
    return FieldKind.TEXT


def default_limit_for(path: str, value: Any) -> FieldLimit:
    """Return the default limit for a path inferred from its value."""

    return DEFAULT_LIMITS[infer_kind(path, value)]


def infer_limits(
    records: Iterable[tuple[str, Mapping[str, Any]]],
) -> dict[str, dict[str, FieldLimit]]:
    """Build the default rules table ``init`` writes.

    ``records`` yields ``(output_type, fields)`` pairs from the accepted
    snapshot. Every scalar leaf becomes one entry; arrays contribute their
    element paths as ``name[]`` templates because array length changes are
    structural and need no limit of their own.
    """

    samples: dict[str, dict[str, Any]] = {}
    for output_type, fields in records:
        collected = samples.setdefault(output_type, {})
        _collect_samples(fields, "", collected)

    return {
        output_type: {
            path: default_limit_for(path, value)
            for path, value in sorted(collected.items())
        }
        for output_type, collected in sorted(samples.items())
    }


def path_template(path: str) -> str:
    """Collapse array indexes so a path can key the rules table.

    ``line_items[1].amount`` and ``line_items[0].amount`` both become
    ``line_items[].amount``, the form ``init`` writes for array elements.
    """

    return _INDEX.sub("[]", path)


def resolve_limit(
    limits: Mapping[str, FieldLimit], path: str, sample: Any
) -> FieldLimit:
    """Find the limit for an exact path, an index template, or its root.

    A path that the rules file does not mention falls back to the default for
    its inferred kind. ``init`` writes every field present in the accepted
    snapshot, so this fallback only covers hand-edited rules and records that
    add nested fields under an already known array.
    """

    template = path_template(path)
    for key in (path, template):
        if key in limits:
            return limits[key]

    root = template.split("[", 1)[0].split(".", 1)[0]
    if root in limits:
        return limits[root]
    return default_limit_for(template, sample)


def compare_field(
    path: str, before: Any, after: Any, limit: FieldLimit
) -> FieldChange:
    """Measure one scalar field against its limit.

    money and numeric use ``|after - before| > max(floor, limit * |before|)``
    for relative limits and ``max(floor, limit)`` for absolute ones; dates use
    whole days; codes must match after normalization; text uses
    ``1 - fuzz.ratio(a, b) / 100``.
    """

    if limit.kind is FieldKind.MONEY or limit.kind is FieldKind.NUMERIC:
        if limit.unit not in (LimitUnit.RELATIVE, LimitUnit.ABSOLUTE):
            raise ValueError(f"{path}: {limit.kind} cannot use unit {limit.unit}")
        before_number = _number(before, path)
        after_number = _number(after, path)
        difference = after_number - before_number
        if limit.unit is LimitUnit.ABSOLUTE:
            delta: float | None = difference
            allowed = max(limit.floor, limit.limit)
        else:
            delta = None if before_number == 0 else difference / before_number
            allowed = max(limit.floor, limit.limit * abs(before_number))
        return FieldChange(
            path=path,
            before=before,
            after=after,
            delta=delta,
            limit=limit.limit,
            breached=abs(difference) > allowed,
        )

    if limit.kind is FieldKind.DATE:
        if limit.unit is not LimitUnit.DAYS:
            raise ValueError(f"{path}: date cannot use unit {limit.unit}")
        day_delta = float((_date(after, path) - _date(before, path)).days)
        allowed = max(limit.floor, limit.limit)
        return FieldChange(
            path=path,
            before=before,
            after=after,
            delta=day_delta,
            limit=limit.limit,
            breached=abs(day_delta) > allowed,
        )

    if limit.kind is FieldKind.CODE:
        if limit.unit is not LimitUnit.EXACT:
            raise ValueError(f"{path}: code cannot use unit {limit.unit}")
        changed = _code_key(before, limit.normalize) != _code_key(
            after, limit.normalize
        )
        return FieldChange(
            path=path,
            before=before,
            after=after,
            delta=1.0 if changed else 0.0,
            limit=limit.limit,
            breached=changed,
        )

    if limit.kind is FieldKind.TEXT:
        if limit.unit is not LimitUnit.EDIT_RATIO:
            raise ValueError(f"{path}: text cannot use unit {limit.unit}")
        delta = 1.0 - fuzz.ratio(
            normalize(before, limit.normalize), normalize(after, limit.normalize)
        ) / 100.0
        allowed = max(limit.floor, limit.limit)
        return FieldChange(
            path=path,
            before=before,
            after=after,
            delta=delta,
            limit=limit.limit,
            breached=delta > allowed,
        )

    raise ValueError(f"{path}: unknown field kind {limit.kind}")


def compare_fields(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    limits: Mapping[str, FieldLimit],
) -> tuple[list[FieldChange], list[StructuralChange]]:
    """Compare two records field by field.

    Every comparable field yields one :class:`FieldChange`, breached or not,
    so a report can show the full field table. Added, removed and resized
    fields yield :class:`StructuralChange` entries instead.
    """

    changes: list[FieldChange] = []
    structural: list[StructuralChange] = []
    _compare_mapping("", before, after, limits, changes, structural)
    return changes, structural


def compare_record(
    record_id: str,
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    limits: Mapping[str, FieldLimit],
    limits_version: str,
) -> Verdict:
    """Decide one record: REVIEW if any field breaches or the shape changed."""

    changes, structural = compare_fields(before, after, limits)
    review = bool(structural) or any(change.breached for change in changes)
    return Verdict(
        record_id=record_id,
        decision=DECISION_REVIEW if review else DECISION_RELEASE,
        changes=changes,
        structural=structural,
        limits_version=limits_version,
    )


def exit_code(verdicts: Iterable[Verdict]) -> int:
    """Map a batch of verdicts to 0 (all released) or 3 (review required)."""

    if any(verdict.decision == DECISION_REVIEW for verdict in verdicts):
        return EXIT_REVIEW
    return EXIT_OK


def _has_hint(leaf: str, hints: Sequence[str]) -> bool:
    folded = leaf.casefold()
    # CamelCase is only visible before casefolding, so split the original
    # spelling on word boundaries first; that is what turns ``invoiceNum`` into
    # ``invoice`` + ``num`` and lets its identifier hint apply.
    words = [
        token
        for token in re.split(
            r"[^0-9a-z\u4e00-\u9fff]+", _CAMEL_BOUNDARY.sub(" ", leaf).casefold()
        )
        if token
    ]
    for hint in hints:
        if not hint.isascii():
            if hint in folded:
                return True
            continue
        if hint == folded:
            return True
        if hint not in words:
            continue
        if hint in _TRAILING_CODE_HINTS and len(words) > 1 and words[0] == hint:
            continue
        return True
    return False


def _code_key(value: Any, steps: Sequence[str]) -> str:
    """Normalize a code, spelling an integral float as its integer.

    A JSON round-trip can turn ``1001`` into ``1001.0``; that is the same
    identifier, so an exact comparison must not breach on the spelling.
    """

    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return normalize(value, steps)


def _yyyymmdd(value: Any) -> dt.date | None:
    """Interpret an integral ``YYYYMMDD`` value as a calendar date."""

    if isinstance(value, bool):
        return None
    if isinstance(value, float):
        if not value.is_integer():
            return None
        value = int(value)
    if not isinstance(value, int) or not 10000101 <= value <= 99991231:
        return None
    try:
        return dt.date(value // 10000, value // 100 % 100, value % 100)
    except ValueError:
        return None


def _number(value: Any, path: str | None = None) -> float | None:
    """Coerce a finite number, or return None when called as a probe."""

    probe = path is None
    number: float | None = None
    if isinstance(value, bool):
        number = None
    elif isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value.strip())
        except ValueError:
            number = None
    if number is not None and not math.isfinite(number):
        number = None
    if number is None and not probe:
        raise MalformedValue(f"{path}: expected a finite number, got {value!r}")
    return number


def _date(value: Any, path: str) -> dt.date:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    if isinstance(value, str):
        text = value.strip()
        for candidate in (text, text[:10]):
            try:
                return dt.date.fromisoformat(candidate)
            except ValueError:
                continue
    parsed = _yyyymmdd(value)
    if parsed is not None:
        return parsed
    raise MalformedValue(f"{path}: expected an ISO date, got {value!r}")


def _compare_mapping(
    prefix: str,
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    limits: Mapping[str, FieldLimit],
    changes: list[FieldChange],
    structural: list[StructuralChange],
) -> None:
    for key in sorted(before.keys() | after.keys()):
        path = f"{prefix}{key}"
        if key not in after:
            structural.append(
                StructuralChange(kind="removed", path=path, before=before[key])
            )
        elif key not in before:
            structural.append(
                StructuralChange(kind="added", path=path, after=after[key])
            )
        else:
            _compare_value(
                path, before[key], after[key], limits, changes, structural
            )


def _compare_value(
    path: str,
    before: Any,
    after: Any,
    limits: Mapping[str, FieldLimit],
    changes: list[FieldChange],
    structural: list[StructuralChange],
) -> None:
    if isinstance(before, Mapping) and isinstance(after, Mapping):
        _compare_mapping(f"{path}.", before, after, limits, changes, structural)
        return

    if _is_sequence(before) and _is_sequence(after):
        if len(before) != len(after):
            structural.append(
                StructuralChange(
                    kind="length_changed",
                    path=path,
                    before=len(before),
                    after=len(after),
                )
            )
        for index in range(min(len(before), len(after))):
            _compare_value(
                f"{path}[{index}]",
                before[index],
                after[index],
                limits,
                changes,
                structural,
            )
        return

    if _is_sequence(before) or _is_sequence(after) or isinstance(
        before, Mapping
    ) or isinstance(after, Mapping):
        # The field kept its name but changed shape; nothing can be compared
        # field by field, so it needs a human.
        changes.append(
            FieldChange(
                path=path,
                before=before,
                after=after,
                delta=None,
                limit=None,
                breached=True,
            )
        )
        return

    sample = before if before is not None else after
    changes.append(
        compare_field(path, before, after, resolve_limit(limits, path, sample))
    )


def _is_sequence(value: Any) -> bool:
    return isinstance(value, (list, tuple))


def _collect_samples(
    fields: Mapping[str, Any], prefix: str, samples: dict[str, Any]
) -> None:
    for key, value in fields.items():
        path = f"{prefix}{key}"
        if value is None:
            continue
        if isinstance(value, Mapping):
            _collect_samples(value, f"{path}.", samples)
        elif _is_sequence(value):
            for item in value:
                if isinstance(item, Mapping):
                    _collect_samples(item, f"{path}[].", samples)
                elif not _is_sequence(item):
                    samples.setdefault(f"{path}[]", item)
        else:
            samples.setdefault(path, value)
