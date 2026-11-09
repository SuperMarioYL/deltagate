"""Calibrate per-field delta limits from the accepted snapshot history.

The limits a fresh ``init`` writes are deliberate guesses. Calibration replaces
each guess with a number measured from history: for every field it takes the
``1 - budget`` quantile of the deltas that field showed across consecutive
accepted snapshots. ``budget`` is therefore the share of a stable batch the
limits are allowed to flag, and it moves the queue the way a reviewer expects:
a 5% budget puts the limit near the 95th percentile, a 30% budget near the
70th, which tightens the limit and flags more records.

That works because :func:`deltagate.delta.compare_field` reports every delta in
the unit its limit is written in -- relative change for money and numeric,
whole days for dates, edit ratio for text -- so a quantile of observed deltas is
directly comparable with the limit. Money, numeric and date deltas are signed
while their limits bound the magnitude, so those fields are fitted on
``|delta|``. A fitted limit is only adopted when it is positive: a limit of zero
or less would collapse the tolerance to the floor and flag every later change.
Code fields are never calibrated: their limit is exactness after normalization,
which no distribution can widen.

This module never writes a file. It returns the refined table plus a
:class:`CalibrationProvenance` that ``rules`` stores in the rules file (the
model round-trips through ``model_dump`` / ``model_validate``) and that ``gate``
copies into the release manifest, so a report can always say where a limit came
from. ``limits_version`` also belongs to ``rules``: it is the hash of the
serialized rules file and does not exist until that file is written.

Inputs are read structurally (:class:`SnapshotView`), so ``store`` keeps
ownership of how snapshots are laid out on disk.
"""

from __future__ import annotations

import datetime as dt
import math
from collections.abc import Iterator, Mapping, Sequence
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict

from .delta import (
    DECISION_REVIEW,
    FieldKind,
    FieldLimit,
    compare_fields,
    compare_record,
    path_template,
)

# Below this many observations a quantile is noise, so the field keeps the
# limit the rules file already declared.
MIN_OBSERVATIONS = 5

# A held-out flag rate exists only once the split leaves one pair for it, which
# takes three snapshots (two pairs).
MIN_SNAPSHOTS = 3

# compare_field reports these deltas signed but breaches on their magnitude
# (``|difference|`` and ``|whole days|``), so the fitted limit must be a
# magnitude. Text deltas are edit ratios and already non-negative.
_MAGNITUDE_KINDS = frozenset({FieldKind.MONEY, FieldKind.NUMERIC, FieldKind.DATE})


class RecordView(Protocol):
    """The parts of a stored record that calibration reads."""

    output_type: str
    fields: Mapping[str, Any]


class SnapshotView(Protocol):
    """The parts of a stored snapshot that calibration reads.

    The concrete snapshot type is ``store``'s; declaring the view structurally
    keeps calibration out of the storage layer.
    """

    snapshot_id: str
    accepted_at: dt.datetime
    records: Mapping[str, RecordView]


class CalibrationProvenance(BaseModel):
    """Where a rules file's limits came from, carried into manifest and report."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source: Literal["defaults", "calibrated"]
    budget: float
    quantile: float
    history_snapshots: int
    calibrated_fields: int
    holdout_snapshot_id: str | None = None
    holdout_records: int = 0
    holdout_flag_rate: float | None = None
    reason: str | None = None


class Calibration(BaseModel):
    """A calibrated ``output_type -> path -> FieldLimit`` table and its provenance."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    limits: dict[str, dict[str, FieldLimit]]
    provenance: CalibrationProvenance


def calibrate(
    history: Sequence[SnapshotView],
    *,
    base: Mapping[str, Mapping[str, FieldLimit]],
    budget: float,
    min_observations: int = MIN_OBSERVATIONS,
) -> Calibration:
    """Fit per-field limits from ``history`` and measure a held-out flag rate.

    ``base`` is the rules table ``init`` wrote; it is the schema authority.
    Calibration only revises the ``limit`` number of paths it declares, keeping
    each field's kind, unit, floor and normalization. Snapshots are ordered by
    ``accepted_at``, so callers need not sort them. The newest pair is held out:
    limits are fitted on the earlier pairs and the flag rate is measured by
    re-deciding that pair.

    With fewer than :data:`MIN_SNAPSHOTS` snapshots there is no pair to hold
    out, and the returned table is ``base`` unchanged with
    ``provenance.source == "defaults"``.
    """

    quantile = 1.0 - _validated_budget(budget)
    ordered = sorted(history, key=lambda snapshot: snapshot.accepted_at)
    if len(ordered) < MIN_SNAPSHOTS:
        return Calibration(
            limits=_copy_table(base),
            provenance=CalibrationProvenance(
                source="defaults",
                budget=budget,
                quantile=quantile,
                history_snapshots=len(ordered),
                calibrated_fields=0,
                reason=(
                    f"calibration needs {MIN_SNAPSHOTS} snapshots to fit and "
                    f"still hold one pair out; history has {len(ordered)}"
                ),
            ),
        )

    pairs = list(zip(ordered, ordered[1:]))
    fitted_pairs, holdout_pair = pairs[:-1], pairs[-1]

    observations: dict[tuple[str, str], list[float]] = {}
    for before, after in fitted_pairs:
        for output_type, template, delta in _scalar_deltas(before, after, base):
            observations.setdefault((output_type, template), []).append(delta)

    limits = _copy_table(base)
    calibrated_fields = 0
    for (output_type, template), deltas in sorted(observations.items()):
        table = limits.get(output_type)
        if table is None or template not in table:
            continue
        declared = table[template]
        if declared.kind is FieldKind.CODE:
            continue
        if len(deltas) < min_observations:
            continue
        fitted = _quantile(sorted(_magnitudes(declared.kind, deltas)), quantile)
        if fitted <= 0.0:
            # A quantile of zero means no change was observed, so adopting it
            # would flag every later change; keep the declared limit instead.
            continue
        table[template] = declared.model_copy(update={"limit": fitted})
        calibrated_fields += 1

    holdout_records, holdout_flagged = _flag_rate(holdout_pair, limits)
    reason = None
    if not calibrated_fields:
        reason = (
            f"no field reached {min_observations} observations across "
            f"{len(fitted_pairs)} fitted pair(s)"
        )
    return Calibration(
        limits=limits,
        provenance=CalibrationProvenance(
            source="calibrated" if calibrated_fields else "defaults",
            budget=budget,
            quantile=quantile,
            history_snapshots=len(ordered),
            calibrated_fields=calibrated_fields,
            holdout_snapshot_id=holdout_pair[1].snapshot_id,
            holdout_records=holdout_records,
            holdout_flag_rate=(
                holdout_flagged / holdout_records if holdout_records else None
            ),
            reason=reason,
        ),
    )


def _scalar_deltas(
    before: SnapshotView,
    after: SnapshotView,
    base: Mapping[str, Mapping[str, FieldLimit]],
) -> Iterator[tuple[str, str, float]]:
    """Yield ``(output_type, path template, delta)`` for every compared field."""

    for _record_id, previous, current in _shared_records(before, after):
        limits = base.get(previous.output_type)
        if limits is None:
            continue
        changes, _structural = compare_fields(previous.fields, current.fields, limits)
        for change in changes:
            # A shape change reports no delta; it is already a REVIEW and says
            # nothing about a field's tolerance.
            if change.delta is None:
                continue
            yield previous.output_type, path_template(change.path), float(change.delta)


def _flag_rate(
    pair: tuple[SnapshotView, SnapshotView],
    limits: Mapping[str, Mapping[str, FieldLimit]],
) -> tuple[int, int]:
    """Count ``(compared records, records the limits would flag)`` on one pair."""

    before, after = pair
    compared = flagged = 0
    for record_id, previous, current in _shared_records(before, after):
        table = limits.get(previous.output_type)
        if table is None:
            continue
        verdict = compare_record(
            record_id,
            previous.fields,
            current.fields,
            table,
            # The rules file is not serialized yet, so no limits_version exists;
            # the flag rate only reads the decision.
            limits_version="",
        )
        compared += 1
        flagged += verdict.decision == DECISION_REVIEW
    return compared, flagged


def _shared_records(
    before: SnapshotView, after: SnapshotView
) -> Iterator[tuple[str, RecordView, RecordView]]:
    """Yield records present in both snapshots with the same output type.

    Only those records have two versions to compare; a record that appears with
    a different output type is a batch-level change, not a field delta.
    """

    for record_id in sorted(before.records):
        previous = before.records.get(record_id)
        current = after.records.get(record_id)
        if previous is None or current is None:
            continue
        if previous.output_type != current.output_type:
            continue
        yield record_id, previous, current


def _magnitudes(kind: FieldKind, deltas: Sequence[float]) -> list[float]:
    """Return the deltas a limit bounds, as positive magnitudes where needed."""

    if kind in _MAGNITUDE_KINDS:
        return [abs(delta) for delta in deltas]
    return list(deltas)


def _quantile(values: Sequence[float], q: float) -> float:
    """Linear-interpolated quantile of an ascending, non-empty sequence."""

    if len(values) == 1:
        return values[0]
    position = (len(values) - 1) * q
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return values[lower]
    weight = position - lower
    return values[lower] + (values[upper] - values[lower]) * weight


def _validated_budget(budget: float) -> float:
    if not 0.0 < budget < 1.0:
        raise ValueError(f"budget must be a fraction in (0, 1), got {budget!r}")
    return budget


def _copy_table(
    base: Mapping[str, Mapping[str, FieldLimit]],
) -> dict[str, dict[str, FieldLimit]]:
    return {output_type: dict(paths) for output_type, paths in base.items()}
