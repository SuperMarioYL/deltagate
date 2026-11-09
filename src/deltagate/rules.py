"""The rules file: the single authority for limits and their version.

A rules file declares, per output type, one :class:`~deltagate.delta.FieldLimit`
per field path. Those definitions -- the field kinds, the defaults and the
template form used for array elements -- belong to :mod:`deltagate.delta`, so
this module imports them and only adds what a file needs: validation, canonical
YAML, and ``limits_version``.

``limits_version`` is ``v`` plus the first 12 hex characters of the SHA-256 of
the rules file's bytes. It is content-derived on purpose: the report and the
manifest both quote it, and ``accept`` refuses a review written against a
different one. Because the hash covers the whole file, editing any limit changes
the version, which is the point.

Calibration provenance is stored beside the limits and is the one place the
false-flag budget lives; :attr:`Rules.budget` reads it rather than keeping a
second copy in the file.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError

from .calibrate import Calibration, CalibrationProvenance
from .delta import FieldLimit, infer_limits

DEFAULT_BUDGET = 0.05
_VERSION_CHARS = 12

_HEADER = (
    "# DeltaGate rules. Edit limits below; limits_version hashes this whole file.\n"
    "# kind: money | numeric | date | code | text\n"
)


class RulesError(ValueError):
    """A rules file cannot be read, validated or written."""


class Rules(BaseModel):
    """A loaded rules file and the limits version it was read at."""

    model_config = ConfigDict(extra="forbid")

    limits: dict[str, dict[str, FieldLimit]]
    calibration: CalibrationProvenance
    limits_version: str

    @property
    def budget(self) -> float:
        return self.calibration.budget


def version_of(data: bytes) -> str:
    """Return the ``v<12 hex>`` limit version of rules file content."""

    return "v" + hashlib.sha256(data).hexdigest()[:_VERSION_CHARS]


def load(path: Path) -> Rules:
    """Read and validate the rules file at ``path``."""

    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise RulesError(f"{path}: {exc.strerror or exc}") from exc

    try:
        payload = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise RulesError(f"{path}: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise RulesError(f"{path}: expected a mapping at the top level")

    limits_payload = payload.get("limits")
    if not isinstance(limits_payload, Mapping) or not limits_payload:
        raise RulesError(f"{path}: 'limits' must name at least one output type")
    calibration_payload = payload.get("calibration")
    if not isinstance(calibration_payload, Mapping):
        raise RulesError(f"{path}: 'calibration' is missing")

    try:
        limits = {
            output_type: {
                field_path: FieldLimit.model_validate(entry)
                for field_path, entry in table.items()
            }
            for output_type, table in limits_payload.items()
            if isinstance(table, Mapping)
        }
        calibration = CalibrationProvenance.model_validate(calibration_payload)
    except ValidationError as exc:
        raise RulesError(f"{path}: {_first_line(exc)}") from exc
    if len(limits) != len(limits_payload):
        raise RulesError(f"{path}: every output type needs a field table")
    _check_budget(calibration.budget, path=path)

    return Rules(
        limits=limits,
        calibration=calibration,
        limits_version=version_of(raw),
    )


def from_snapshot(
    records: Iterable[Any],
    *,
    snapshot_id: str,
    budget: float = DEFAULT_BUDGET,
) -> Rules:
    """Build the conservative default rules ``init`` writes for one snapshot."""

    _check_budget(budget)
    limits = infer_limits(
        (record.output_type, record.fields) for record in records
    )
    if not limits:
        raise RulesError("the accepted snapshot has no scalar fields to describe")
    calibration = CalibrationProvenance(
        source="defaults",
        budget=budget,
        quantile=1.0 - budget,
        history_snapshots=1,
        calibrated_fields=0,
        reason=f"default limits inferred from snapshot {snapshot_id}",
    )
    return Rules(limits=limits, calibration=calibration, limits_version="")


def apply_calibration(base: Rules, calibration: Calibration) -> Rules:
    """Return ``base`` with the calibrated limits and their provenance."""

    unknown = sorted(set(calibration.limits) - set(base.limits))
    if unknown:
        raise RulesError(
            "calibration introduced output types the rules file does not declare: "
            + ", ".join(unknown)
        )
    return Rules(
        limits=calibration.limits,
        calibration=calibration.provenance,
        limits_version="",
    )


def render(rules: Rules) -> str:
    """Canonical YAML for ``rules``; sorting keeps the version stable."""

    payload = {
        "limits": {
            output_type: {
                field_path: limit.model_dump(mode="json")
                for field_path, limit in sorted(table.items())
            }
            for output_type, table in sorted(rules.limits.items())
        },
        "calibration": rules.calibration.model_dump(mode="json"),
    }
    body = yaml.safe_dump(
        payload,
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=True,
        width=88,
    )
    return _HEADER + body


def write(rules: Rules, path: Path) -> Rules:
    """Write ``rules`` to ``path`` and return them with their new version."""

    data = render(rules).encode("utf-8")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_bytes(data)
        os.replace(temporary, path)
    except OSError as exc:
        raise RulesError(f"{path}: {exc.strerror or exc}") from exc
    return rules.model_copy(update={"limits_version": version_of(data)})


def _check_budget(budget: float, *, path: Path | None = None) -> None:
    if not 0.0 < budget < 1.0:
        where = f"{path}: " if path is not None else ""
        raise RulesError(f"{where}budget must be a fraction in (0, 1), got {budget!r}")


def _first_line(exc: BaseException) -> str:
    if isinstance(exc, ValidationError):
        errors = exc.errors()
        if errors:
            first = errors[0]
            location = ".".join(str(part) for part in first.get("loc", ())) or "rules"
            return f"{location}: {first.get('msg', 'invalid value')}"
    message = str(exc).strip()
    return message.splitlines()[0] if message else exc.__class__.__name__
