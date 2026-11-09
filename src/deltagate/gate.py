"""Batch gating, the release manifest, and the accept loop.

This module owns the decisions that span a whole batch:

* turning per-record verdicts into counts, a flag rate and a
  :class:`Manifest` whose ``verdicts_sha256`` and limit provenance make the
  batch auditable,
* the ``review.csv`` schema -- the file reviewers edit and ``accept`` consumes,
  so ``report`` writes it from :data:`REVIEW_COLUMNS` instead of repeating the
  column names,
* :func:`accept`, which turns a filled-in review into the next accepted
  snapshot plus its archived manifest.

Nothing here reads or writes paths other than the review file the caller names,
and no layout is assumed: ``store`` loads the candidate records and the
manifests already sitting in the target directory, this module decides, and
``store`` persists what :func:`accept` returns. That keeps the directory
contract in one place.

``accept`` refuses three things rather than guessing:

* the same batch or the same review file accepted twice,
* a review filled in against different limits than the batch used, or limits
  that changed after the review was written (invariant 2),
* a review that leaves a flagged record undecided, decides a record the gate
  released, or names records the candidate batch does not contain.
"""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
import json
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from .calibrate import CalibrationProvenance
from .delta import DECISION_RELEASE, DECISION_REVIEW, Verdict

REVIEW_RELEASE = "release"
REVIEW_REJECT = "reject"
ReviewDecision = Literal["release", "reject"]

# The columns ``report`` writes and ``accept`` requires. ``limits_version`` is
# carried per row because that is what makes a stale review file detectable.
REVIEW_COLUMNS = ("record_id", "output_type", "decision", "limits_version", "reason")
_REVIEW_DECISIONS = (REVIEW_RELEASE, REVIEW_REJECT)


class GateError(ValueError):
    """A batch or a review file cannot be gated or accepted."""


class ReviewFormatError(GateError):
    """A review file does not match the expected CSV schema."""


class RepeatedAccept(GateError):
    """This batch, or this exact review file, is already in the accepted set."""


class LimitsVersionMismatch(GateError):
    """The review, the batch and the current rules do not share one limits version."""


class Record(BaseModel):
    """One extracted document as loaded by ``store``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    record_id: str
    output_type: str
    fields: dict[str, Any]


class Counts(BaseModel):
    """The batch's per-record decisions; there is no score above the record."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    release: int
    review: int

    @property
    def total(self) -> int:
        return self.release + self.review


class Manifest(BaseModel):
    """The batch's release record, written by ``check`` and archived by ``accept``.

    ``accepted_snapshot_id`` is the accepted snapshot the batch was compared
    against -- the reference values. ``review_sha256`` stays ``None`` until
    ``accept`` archives the manifest, and then it is the idempotency key.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    batch_id: str
    limits_version: str
    budget: float
    counts: Counts
    flag_rate: float
    verdicts_sha256: str
    accepted_snapshot_id: str
    calibration: CalibrationProvenance
    review_sha256: str | None = None


class BatchOutcome(BaseModel):
    """A gated batch: its verdicts and the manifest that describes them."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    manifest: Manifest
    verdicts: tuple[Verdict, ...]


class ReviewRow(BaseModel):
    """One reviewer decision, as written in ``review.csv``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    record_id: str
    output_type: str
    decision: ReviewDecision
    limits_version: str
    reason: str = ""


class ReviewFile(BaseModel):
    """A parsed review: its rows, the limits they were written against, its hash.

    ``limits_version`` is ``None`` for a header-only file, which is what a batch
    with nothing flagged produces: it makes no decision, so it has no version to
    disagree with.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    limits_version: str | None
    rows: tuple[ReviewRow, ...]
    sha256: str


class AcceptedSnapshot(BaseModel):
    """The next accepted snapshot, together with the manifest archived beside it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    snapshot_id: str
    accepted_at: dt.datetime
    limits_version: str
    records: dict[str, Record]
    manifest: Manifest
    manifest_sha256: str


def gate_batch(
    verdicts: Sequence[Verdict],
    *,
    batch_id: str,
    accepted_snapshot_id: str,
    budget: float,
    calibration: CalibrationProvenance,
) -> BatchOutcome:
    """Aggregate per-record verdicts into the batch outcome and its manifest."""

    if not verdicts:
        raise GateError("cannot gate a batch with no records")
    versions = {verdict.limits_version for verdict in verdicts}
    if len(versions) != 1:
        raise GateError(
            "verdicts were measured against different limits: "
            + ", ".join(sorted(versions))
        )

    review = sum(1 for verdict in verdicts if verdict.decision == DECISION_REVIEW)
    return BatchOutcome(
        manifest=Manifest(
            batch_id=batch_id,
            limits_version=versions.pop(),
            budget=budget,
            counts=Counts(release=len(verdicts) - review, review=review),
            flag_rate=review / len(verdicts),
            verdicts_sha256=verdicts_digest(verdicts),
            accepted_snapshot_id=accepted_snapshot_id,
            calibration=calibration,
        ),
        verdicts=tuple(verdicts),
    )


def verdicts_digest(verdicts: Iterable[Verdict]) -> str:
    """Content hash of a batch's verdicts, stable under file ordering."""

    payload = [
        verdict.model_dump(mode="json")
        for verdict in sorted(verdicts, key=lambda item: item.record_id)
    ]
    return _digest(payload)


def read_review(path: Path) -> ReviewFile:
    """Parse the review file at ``path``."""

    return parse_review(path.read_text(encoding="utf-8-sig"), origin=str(path))


def parse_review(text: str, *, origin: str = "review.csv") -> ReviewFile:
    """Parse a review CSV and check that it is complete and self-consistent."""

    body = text.lstrip("\ufeff")
    if not body.strip():
        raise ReviewFormatError(f"{origin}: no review rows")

    reader = csv.DictReader(io.StringIO(body))
    columns = reader.fieldnames or []
    missing = [column for column in REVIEW_COLUMNS if column not in columns]
    if missing:
        raise ReviewFormatError(
            f"{origin}: missing column(s) {', '.join(missing)}; "
            f"expected {', '.join(REVIEW_COLUMNS)}"
        )

    rows: list[ReviewRow] = []
    versions: set[str] = set()
    seen: set[str] = set()
    for line, raw in enumerate(reader, start=2):
        record_id = _cell(raw, "record_id")
        decision = _cell(raw, "decision").casefold()
        version = _cell(raw, "limits_version")
        if not any((record_id, decision, version)):
            continue  # blank line
        if not record_id:
            raise ReviewFormatError(f"{origin}:{line}: empty record_id")
        if decision not in _REVIEW_DECISIONS:
            raise ReviewFormatError(
                f"{origin}:{line}: decision must be one of "
                f"{', '.join(_REVIEW_DECISIONS)}; got {decision!r}"
            )
        if not version:
            raise ReviewFormatError(
                f"{origin}:{line}: empty limits_version for {record_id}"
            )
        reason = _cell(raw, "reason")
        if decision == REVIEW_REJECT and not reason:
            raise ReviewFormatError(
                f"{origin}:{line}: rejecting {record_id} drops it from the "
                "release, so it needs a reason"
            )
        if record_id in seen:
            raise ReviewFormatError(f"{origin}:{line}: {record_id} appears twice")
        seen.add(record_id)
        versions.add(version)
        rows.append(
            ReviewRow(
                record_id=record_id,
                output_type=_cell(raw, "output_type"),
                decision=decision,  # type: ignore[arg-type]
                limits_version=version,
                reason=reason,
            )
        )

    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    if not rows:
        return ReviewFile(limits_version=None, rows=(), sha256=digest)
    if len(versions) != 1:
        raise LimitsVersionMismatch(
            f"{origin}: rows disagree on limits_version: "
            + ", ".join(sorted(versions))
        )
    return ReviewFile(limits_version=versions.pop(), rows=tuple(rows), sha256=digest)


def accept(
    batch: BatchOutcome,
    *,
    candidate: Sequence[Record],
    review: ReviewFile,
    current_limits_version: str,
    existing: Iterable[Manifest],
    snapshot_id: str | None = None,
    accepted_at: dt.datetime | None = None,
) -> AcceptedSnapshot:
    """Apply a review to a gated batch and return the next accepted snapshot.

    ``existing`` is the set of manifests already archived in the target
    directory; it is what makes a second accept of the same batch or the same
    review file fail instead of silently duplicating a release. Records the
    gate released and the reviewer released keep their candidate values;
    rejected records are dropped. A batch with nothing flagged takes a
    header-only review and releases everything. ``store`` persists the result.
    """

    manifest = batch.manifest
    for archived in existing:
        if archived.batch_id == manifest.batch_id:
            raise RepeatedAccept(
                f"batch {manifest.batch_id} already has an accepted snapshot"
            )
        if archived.review_sha256 == review.sha256:
            raise RepeatedAccept(
                f"this review file was already accepted for batch "
                f"{archived.batch_id}"
            )

    if review.limits_version is not None and (
        review.limits_version != manifest.limits_version
    ):
        raise LimitsVersionMismatch(
            f"review.csv was filled in against limits {review.limits_version}, "
            f"but batch {manifest.batch_id} used {manifest.limits_version}"
        )
    if manifest.limits_version != current_limits_version:
        raise LimitsVersionMismatch(
            f"limits changed since batch {manifest.batch_id} was checked: "
            f"it used {manifest.limits_version}, the rules file is now "
            f"{current_limits_version}; re-run check"
        )
    if verdicts_digest(batch.verdicts) != manifest.verdicts_sha256:
        raise GateError(
            f"decisions do not match manifest {manifest.batch_id} "
            "(verdicts_sha256); re-run check to rebuild them"
        )

    records = _index_records(candidate)
    verdicts = {verdict.record_id: verdict for verdict in batch.verdicts}
    flagged = {
        record_id
        for record_id, verdict in verdicts.items()
        if verdict.decision == DECISION_REVIEW
    }
    decided = {row.record_id for row in review.rows}

    undecided = sorted(flagged - decided)
    if undecided:
        raise GateError(
            "review is incomplete; no decision for: " + ", ".join(undecided)
        )
    unneeded = sorted(decided - flagged)
    if unneeded:
        raise GateError(
            "review decided records this batch did not flag: "
            + ", ".join(unneeded)
        )

    for row in review.rows:
        record = records.get(row.record_id)
        if record is None:
            raise GateError(
                f"review names {row.record_id}, which is not in the candidate batch"
            )
        if record.output_type != row.output_type:
            raise GateError(
                f"review says {row.record_id} is {row.output_type}; "
                f"the batch has it as {record.output_type}"
            )

    released = {
        record_id
        for record_id, verdict in verdicts.items()
        if verdict.decision == DECISION_RELEASE
    }
    released.update(
        row.record_id for row in review.rows if row.decision == REVIEW_RELEASE
    )
    absent = sorted(released - set(records))
    if absent:
        raise GateError(
            "cannot build a snapshot; records missing from the candidate batch: "
            + ", ".join(absent)
        )

    archived = manifest.model_copy(update={"review_sha256": review.sha256})
    return AcceptedSnapshot(
        snapshot_id=snapshot_id or manifest.batch_id,
        accepted_at=accepted_at or dt.datetime.now(dt.UTC),
        limits_version=manifest.limits_version,
        records={record_id: records[record_id] for record_id in sorted(released)},
        manifest=archived,
        manifest_sha256=_digest(archived.model_dump(mode="json")),
    )


def _cell(raw: Mapping[str, str | None], column: str) -> str:
    value = raw.get(column)
    return "" if value is None else value.strip()


def _index_records(records: Iterable[Record]) -> dict[str, Record]:
    index: dict[str, Record] = {}
    for record in records:
        if record.record_id in index:
            raise GateError(f"duplicate record_id in the batch: {record.record_id}")
        index[record.record_id] = record
    return index


def _digest(payload: Any) -> str:
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
