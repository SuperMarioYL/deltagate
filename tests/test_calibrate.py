"""Calibration semantics: quantile limits, holdout flag rate, and provenance.

m2 is the promise that limits are measured rather than guessed: each field's
limit becomes the ``1 - budget`` quantile of the deltas it showed across the
accepted history, and the newest pair is held out to report what that budget
actually flags. These tests pin that behaviour, including the plan's case that
tightening the fitted limits threefold materially raises the holdout flag rate.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from collections.abc import Mapping, Sequence

import pytest

from deltagate import rules as rules_file
from deltagate.calibrate import MIN_OBSERVATIONS, MIN_SNAPSHOTS, calibrate
from deltagate.rules import version_of
from deltagate.delta import (
    DECISION_REVIEW,
    FieldKind,
    FieldLimit,
    LimitUnit,
    compare_record,
)

MONEY = FieldLimit(
    kind=FieldKind.MONEY, unit=LimitUnit.RELATIVE, limit=0.005, floor=0.01
)
TEXT = FieldLimit(
    kind=FieldKind.TEXT,
    unit=LimitUnit.EDIT_RATIO,
    limit=0.2,
    normalize=("nfkc", "strip_ws"),
)
DATE = FieldLimit(kind=FieldKind.DATE, unit=LimitUnit.DAYS)
CODE = FieldLimit(
    kind=FieldKind.CODE,
    unit=LimitUnit.EXACT,
    normalize=("nfkc", "strip_ws", "cf_upper"),
)

# Six records move 0.1% per snapshot, two move 1%: the 95th percentile of the
# 16 fitted observations lands on the wide records, so a 5% budget fits 1%.
NARROW = tuple(f"R{index}" for index in range(1, 7))
WIDE = ("R7", "R8")
BASE = {"invoice": {"total_amount": MONEY}}


@dataclasses.dataclass(frozen=True)
class FakeRecord:
    output_type: str
    fields: Mapping[str, object]


@dataclasses.dataclass(frozen=True)
class FakeSnapshot:
    snapshot_id: str
    accepted_at: dt.datetime
    records: Mapping[str, FakeRecord]


def _at(month: int) -> dt.datetime:
    return dt.datetime(2026, month, 1, 9, 0, tzinfo=dt.UTC)


def _amounts(holdout_delta: float = 0.006) -> dict[str, tuple[float, ...]]:
    values: dict[str, tuple[float, ...]] = {}
    for record_id in NARROW + WIDE:
        step = 0.001 if record_id in NARROW else 0.01
        first = 100.0
        second = first * (1.0 + step)
        third = second * (1.0 + step)
        fourth = third * (1.0 + holdout_delta)
        values[record_id] = (first, second, third, fourth)
    return values


def _history(
    amounts: Mapping[str, Sequence[float]],
    *,
    extra_fields: Mapping[str, object] | None = None,
    drop_from_last: str | None = None,
) -> list[FakeSnapshot]:
    snapshots: list[FakeSnapshot] = []
    for position in range(4):
        records: dict[str, FakeRecord] = {}
        for record_id, series in amounts.items():
            if position == 3 and record_id == drop_from_last:
                continue
            fields: dict[str, object] = {"total_amount": series[position]}
            if extra_fields:
                fields.update(extra_fields)
            records[record_id] = FakeRecord(output_type="invoice", fields=fields)
        snapshots.append(
            FakeSnapshot(
                snapshot_id=f"hist-{position}",
                accepted_at=_at(7 + position),
                records=records,
            )
        )
    return snapshots


def _holdout_flag_rate(
    history: Sequence[FakeSnapshot],
    limits: Mapping[str, Mapping[str, FieldLimit]],
    field: str = "total_amount",
) -> float:
    """Decide the newest pair under ``limits`` with the public record verdict."""

    ordered = sorted(history, key=lambda snapshot: snapshot.accepted_at)
    before, after = ordered[-2], ordered[-1]
    compared = flagged = 0
    for record_id in sorted(before.records):
        previous = before.records[record_id]
        current = after.records.get(record_id)
        if current is None or previous.output_type != current.output_type:
            continue
        table = limits[previous.output_type]
        # Only the field under test decides here; it keeps the helper honest
        # about which limit moved the flag rate.
        single = {field: table[field]}
        verdict = compare_record(
            record_id, previous.fields, current.fields, single, "vtest"
        )
        compared += 1
        flagged += verdict.decision == DECISION_REVIEW
    return flagged / compared


def test_three_snapshots_calibrate_every_field_and_report_a_holdout_rate() -> None:
    calibration = calibrate(_history(_amounts()), base=BASE, budget=0.05)

    provenance = calibration.provenance
    assert provenance.source == "calibrated"
    assert provenance.history_snapshots == 4
    assert provenance.calibrated_fields == 1
    assert provenance.quantile == pytest.approx(0.95)
    assert provenance.holdout_snapshot_id == "hist-3"
    assert provenance.holdout_records == 8
    assert calibration.limits["invoice"]["total_amount"].limit == pytest.approx(
        0.01, rel=1e-6
    )
    # The 1% fitted limit tolerates the 0.6% held-out change.
    assert provenance.holdout_flag_rate == 0.0


def test_fitting_keeps_the_declared_kind_unit_floor_and_normalization() -> None:
    calibration = calibrate(_history(_amounts()), base=BASE, budget=0.05)
    fitted = calibration.limits["invoice"]["total_amount"]

    assert fitted.kind is FieldKind.MONEY
    assert fitted.unit is LimitUnit.RELATIVE
    assert fitted.floor == MONEY.floor
    assert fitted.normalize == MONEY.normalize
    assert fitted.limit != MONEY.limit


def test_tightening_the_fitted_limit_threefold_more_than_doubles_the_flag_rate() -> None:
    history = _history(_amounts())
    calibration = calibrate(history, base=BASE, budget=0.05)
    fitted = calibration.limits["invoice"]["total_amount"].limit
    loose_rate = _holdout_flag_rate(history, calibration.limits)

    tighter = {
        "invoice": {
            "total_amount": MONEY.model_copy(update={"limit": fitted / 3.0})
        }
    }
    tight_rate = _holdout_flag_rate(history, tighter)

    assert loose_rate == 0.0
    assert tight_rate >= 0.75, "threefold tightening must expose most held-out changes"
    assert tight_rate > loose_rate + 0.5


def test_a_larger_budget_tightens_the_limit_and_flags_more() -> None:
    history = _history(_amounts())
    loose = calibrate(history, base=BASE, budget=0.05)
    tight = calibrate(history, base=BASE, budget=0.30)

    loose_limit = loose.limits["invoice"]["total_amount"].limit
    tight_limit = tight.limits["invoice"]["total_amount"].limit
    assert tight_limit < loose_limit
    assert tight.provenance.quantile == pytest.approx(0.70)
    assert tight.provenance.holdout_flag_rate is not None
    assert loose.provenance.holdout_flag_rate is not None
    assert (
        tight.provenance.holdout_flag_rate > loose.provenance.holdout_flag_rate
    )


def test_one_snapshot_keeps_the_defaults_and_says_why() -> None:
    history = _history(_amounts())[:1]
    calibration = calibrate(history, base=BASE, budget=0.05)

    provenance = calibration.provenance
    assert provenance.source == "defaults"
    assert provenance.calibrated_fields == 0
    assert provenance.history_snapshots == 1
    assert provenance.holdout_flag_rate is None
    assert str(MIN_SNAPSHOTS) in (provenance.reason or "")
    assert calibration.limits["invoice"]["total_amount"] == MONEY


def test_two_snapshots_still_have_nothing_to_hold_out() -> None:
    calibration = calibrate(_history(_amounts())[:2], base=BASE, budget=0.05)
    assert calibration.provenance.source == "defaults"
    assert calibration.provenance.history_snapshots == 2
    assert calibration.limits["invoice"]["total_amount"] == MONEY


def test_too_few_observations_keeps_the_declared_limit() -> None:
    # Two shared records over two fitted pairs is four observations, below the
    # minimum a quantile needs to mean anything.
    sparse = {record_id: series for record_id, series in _amounts().items()}
    sparse = {key: sparse[key] for key in ("R1", "R2")}
    calibration = calibrate(_history(sparse), base=BASE, budget=0.05)

    assert calibration.provenance.source == "defaults"
    assert calibration.provenance.calibrated_fields == 0
    assert calibration.limits["invoice"]["total_amount"] == MONEY
    assert str(MIN_OBSERVATIONS) in (calibration.provenance.reason or "")


def test_snapshots_are_ordered_by_acceptance_time_not_input_order() -> None:
    history = list(reversed(_history(_amounts())))
    calibration = calibrate(history, base=BASE, budget=0.05)

    assert calibration.provenance.holdout_snapshot_id == "hist-3"
    assert calibration.provenance.source == "calibrated"


def test_records_missing_from_the_newest_snapshot_do_not_change_the_rate() -> None:
    history = _history(_amounts(), drop_from_last="R1")
    calibration = calibrate(history, base=BASE, budget=0.05)

    assert calibration.provenance.holdout_records == 7
    assert calibration.provenance.holdout_flag_rate == 0.0


def test_text_and_date_fields_are_fitted_in_their_own_units() -> None:
    amounts = _amounts()
    snapshots: list[FakeSnapshot] = []
    for position in range(4):
        records: dict[str, FakeRecord] = {}
        for record_id, series in amounts.items():
            wide = record_id in WIDE
            # A one-character rewrite of a four-character remark is edit ratio
            # 0.25; the narrow records are unchanged.
            remark = "abcd" if not wide or position < 2 else "abce"
            # The wide records move two days per snapshot, the rest none.
            day = 8 + (2 * position if wide else 0)
            records[record_id] = FakeRecord(
                output_type="invoice",
                fields={
                    "total_amount": series[position],
                    "remark": remark,
                    "invoiceDate": f"2026-09-{day:02d}",
                    "invoiceNum": 1007,
                },
            )
        snapshots.append(
            FakeSnapshot(
                snapshot_id=f"hist-{position}",
                accepted_at=_at(7 + position),
                records=records,
            )
        )

    base = {
        "invoice": {
            "total_amount": MONEY,
            "remark": TEXT,
            "invoiceDate": DATE,
            "invoiceNum": CODE,
        }
    }
    calibration = calibrate(snapshots, base=base, budget=0.05)
    limits = calibration.limits["invoice"]

    assert limits["remark"].limit == pytest.approx(0.25)
    assert limits["remark"].kind is FieldKind.TEXT
    assert limits["invoiceDate"].limit == pytest.approx(2.0)
    assert limits["invoiceDate"].kind is FieldKind.DATE
    # Codes are exact by definition; calibration must not invent a tolerance.
    assert limits["invoiceNum"] == CODE
    assert calibration.provenance.calibrated_fields == 3


def test_calibration_never_revises_a_zero_quantile() -> None:
    # No money change at all in the fitted pairs: a zero limit would flag every
    # later change, so the declared limit stays.
    static = {record_id: (100.0, 100.0, 100.0, 100.0) for record_id in NARROW + WIDE}
    calibration = calibrate(_history(static), base=BASE, budget=0.05)

    assert calibration.limits["invoice"]["total_amount"] == MONEY


def test_output_types_the_rules_file_does_not_declare_are_ignored() -> None:
    history = _history(_amounts())
    mixed = list(history)
    for snapshot in mixed:
        extra = dict(snapshot.records)
        extra["T1"] = FakeRecord(output_type="ticket", fields={"total_amount": 10.0})
        mixed[mixed.index(snapshot)] = dataclasses.replace(snapshot, records=extra)

    calibration = calibrate(mixed, base=BASE, budget=0.05)
    assert set(calibration.limits) == {"invoice"}


def test_calibration_does_not_mutate_the_base_table() -> None:
    base = {"invoice": {"total_amount": MONEY}}
    calibrate(_history(_amounts()), base=base, budget=0.05)
    assert base["invoice"]["total_amount"] == MONEY


def test_budget_outside_the_open_unit_interval_is_rejected() -> None:
    for budget in (0.0, 1.0, -0.1, 1.5):
        with pytest.raises(ValueError):
            calibrate(_history(_amounts()), base=BASE, budget=budget)


def test_calibrated_limits_round_trip_through_the_rules_file(tmp_path) -> None:
    history = _history(_amounts())
    records = [record for record in history[0].records.values()]
    draft = rules_file.from_snapshot(records, snapshot_id="hist-0", budget=0.05)
    calibration = calibrate(history, base=draft.limits, budget=0.05)

    written = rules_file.write(
        rules_file.apply_calibration(draft, calibration), tmp_path / "rules.yaml"
    )
    loaded = rules_file.load(tmp_path / "rules.yaml")

    assert loaded.calibration.source == "calibrated"
    assert loaded.calibration.holdout_snapshot_id == "hist-3"
    assert loaded.calibration.holdout_flag_rate == 0.0
    # limits_version is the hash of the file that now holds these limits.
    assert loaded.limits_version == written.limits_version
    assert loaded.limits_version == version_of((tmp_path / "rules.yaml").read_bytes())

    # Invariant 2: editing any limit changes the version the report quotes.
    edited = loaded.model_copy(
        update={
            "limits": {
                "invoice": {
                    "total_amount": MONEY.model_copy(update={"limit": 0.02})
                }
            }
        }
    )
    rewritten = rules_file.write(edited, tmp_path / "edited.yaml")
    assert rewritten.limits_version != written.limits_version
