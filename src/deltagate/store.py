"""The on-disk layout, and nothing else.

Every path this project reads or writes is decided here:

* a snapshot is a directory holding ``snapshot.json`` (its id and acceptance
  time) and one JSON record per file under ``records/`` -- fixtures may instead
  keep the record files directly in the directory,
* a history is either one snapshot directory or a directory of those,
* a batch run writes ``decisions.jsonl``, ``review.csv``, ``report.html`` and
  ``manifest.json`` into one output directory,
* ``accept`` adds one new snapshot directory under its target, so the target
  accumulates the snapshots calibration later fits against.

The other modules pass records, verdicts and manifests in and out as models, so
they never need to know a filename. ``decisions.jsonl`` carries the candidate
record beside its verdict: that is what lets ``accept`` rebuild the release
without the candidate directory still being around.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .delta import Verdict
from .gate import AcceptedSnapshot, Manifest, Record

SNAPSHOT_FILE = "snapshot.json"
RECORDS_DIR = "records"
DECISIONS_FILE = "decisions.jsonl"
REVIEW_FILE = "review.csv"
REPORT_FILE = "report.html"
MANIFEST_FILE = "manifest.json"

_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


class StoreError(RuntimeError):
    """A snapshot or batch directory is missing, malformed or unsafe to write."""


class Snapshot(BaseModel):
    """One accepted (or candidate) batch, with the records that could be parsed.

    ``malformed`` maps a file or record name to the reason it could not be read.
    ``check`` counts those instead of failing on the first one; ``calibrate``
    ignores them.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    snapshot_id: str
    accepted_at: dt.datetime
    records: dict[str, Record]
    malformed: dict[str, str] = Field(default_factory=dict)


class _SnapshotMeta(BaseModel):
    """The parts of ``snapshot.json`` this module interprets."""

    model_config = ConfigDict(extra="ignore")

    snapshot_id: str
    accepted_at: dt.datetime


class DecisionLine(BaseModel):
    """One ``decisions.jsonl`` line: the candidate record and its verdict."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    record: Record
    verdict: Verdict


def load_snapshot(path: Path) -> Snapshot:
    """Load one record directory as a snapshot.

    A malformed record is reported in :attr:`Snapshot.malformed` rather than
    aborting the load, so a batch with a few bad files can still be gated and a
    batch with too many can be rejected with a count.
    """

    if not path.is_dir():
        raise StoreError(f"{path}: not a directory")

    meta = _read_meta(path)
    record_files = _record_files(path)
    records: dict[str, Record] = {}
    malformed: dict[str, str] = {}
    for record_file in record_files:
        try:
            record = Record.model_validate_json(
                record_file.read_text(encoding="utf-8-sig")
            )
        except (OSError, json.JSONDecodeError, ValidationError) as exc:
            malformed[record_file.stem] = _first_line(exc)
            continue
        if record.record_id in records:
            malformed[record_file.stem] = (
                f"duplicate record_id {record.record_id!r}"
            )
            continue
        records[record.record_id] = record

    return Snapshot(
        snapshot_id=meta.snapshot_id if meta else path.name,
        accepted_at=meta.accepted_at if meta else _mtime(path),
        records=records,
        malformed=malformed,
    )


def load_history(path: Path) -> list[Snapshot]:
    """Load every snapshot under ``path``, or ``path`` itself when it is one."""

    if not path.is_dir():
        raise StoreError(f"{path}: not a directory")
    if _is_snapshot(path):
        return [load_snapshot(path)]

    children = sorted(
        child for child in path.iterdir() if child.is_dir() and _is_snapshot(child)
    )
    if not children:
        raise StoreError(f"{path}: no snapshots found")
    return [load_snapshot(child) for child in children]


def load_manifests(path: Path) -> list[Manifest]:
    """Load the manifests archived under an accepted-snapshot directory."""

    if not path.is_dir():
        return []
    candidates = [path / MANIFEST_FILE]
    candidates.extend(sorted(path.glob(f"*/{MANIFEST_FILE}")))
    manifests: list[Manifest] = []
    for candidate in candidates:
        if candidate.is_file():
            manifests.append(read_manifest(candidate))
    return manifests


def read_manifest(path: Path) -> Manifest:
    try:
        return Manifest.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValidationError) as exc:
        raise StoreError(f"{path}: {_first_line(exc)}") from exc


def read_decisions(path: Path) -> tuple[list[Record], list[Verdict]]:
    """Read ``decisions.jsonl`` back into the records and verdicts it recorded."""

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise StoreError(f"{path}: {exc.strerror or exc}") from exc

    records: list[Record] = []
    verdicts: list[Verdict] = []
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            entry = DecisionLine.model_validate_json(line)
        except (json.JSONDecodeError, ValidationError) as exc:
            raise StoreError(f"{path}:{number}: {_first_line(exc)}") from exc
        records.append(entry.record)
        verdicts.append(entry.verdict)
    return records, verdicts


def write_batch(
    out: Path,
    *,
    records: Mapping[str, Record],
    verdicts: Sequence[Verdict],
    manifest: Manifest,
    review_csv: str,
    report_html: str,
) -> None:
    """Write the four artifacts of one ``check`` run into ``out``."""

    out.mkdir(parents=True, exist_ok=True)
    ordered = sorted(verdicts, key=lambda verdict: verdict.record_id)
    if len(ordered) != len(records):
        raise StoreError("every gated record must have exactly one verdict")

    lines = [
        DecisionLine(record=records[verdict.record_id], verdict=verdict)
        .model_dump_json(exclude_none=False)
        for verdict in ordered
    ]
    _write_text(out / DECISIONS_FILE, "\n".join(lines) + "\n")
    _write_text(out / REVIEW_FILE, review_csv)
    _write_text(out / REPORT_FILE, report_html)
    _write_json(out / MANIFEST_FILE, manifest.model_dump(mode="json"))


def write_snapshot(snapshot: AcceptedSnapshot, into: Path) -> Path:
    """Archive an accepted snapshot as ``into/<snapshot_id>/`` and return it."""

    name = _safe_name(snapshot.snapshot_id)
    target = into / name
    if target.exists():
        raise StoreError(
            f"{target} already exists; refusing to overwrite an accepted snapshot"
        )

    records_dir = target / RECORDS_DIR
    records_dir.mkdir(parents=True)
    meta = {
        "snapshot_id": snapshot.snapshot_id,
        "accepted_at": snapshot.accepted_at.isoformat(),
        "limits_version": snapshot.limits_version,
        "manifest_sha256": snapshot.manifest_sha256,
    }
    _write_json(target / SNAPSHOT_FILE, meta)

    seen: set[str] = set()
    for record_id in sorted(snapshot.records):
        filename = f"{_safe_name(record_id)}.json"
        if filename in seen:
            raise StoreError(f"record ids collide on disk: {record_id!r}")
        seen.add(filename)
        _write_json(records_dir / filename, _record_payload(snapshot.records[record_id]))

    _write_json(target / MANIFEST_FILE, snapshot.manifest.model_dump(mode="json"))
    return target


def _read_meta(path: Path) -> _SnapshotMeta | None:
    meta_file = path / SNAPSHOT_FILE
    if not meta_file.is_file():
        return None
    try:
        return _SnapshotMeta.model_validate_json(
            meta_file.read_text(encoding="utf-8-sig")
        )
    except (OSError, json.JSONDecodeError, ValidationError) as exc:
        raise StoreError(f"{meta_file}: {_first_line(exc)}") from exc


def _record_files(path: Path) -> list[Path]:
    records_dir = path / RECORDS_DIR
    directory = records_dir if records_dir.is_dir() else path
    return sorted(
        candidate
        for candidate in directory.glob("*.json")
        if candidate.name != SNAPSHOT_FILE
    )


def _is_snapshot(path: Path) -> bool:
    if (path / SNAPSHOT_FILE).is_file() or (path / RECORDS_DIR).is_dir():
        return True
    return any(path.glob("*.json"))


def _record_payload(record: Record) -> dict[str, Any]:
    return record.model_dump(mode="json")


def _mtime(path: Path) -> dt.datetime:
    return dt.datetime.fromtimestamp(path.stat().st_mtime, tz=dt.UTC)


def _safe_name(value: str) -> str:
    name = _SAFE_NAME.sub("_", value).strip("._")
    return name or "snapshot"


def _write_text(path: Path, text: str) -> None:
    path.write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")


def _write_json(path: Path, payload: Any) -> None:
    text = json.dumps(
        payload,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )
    path.write_text(text + "\n", encoding="utf-8")


def _first_line(exc: BaseException) -> str:
    if isinstance(exc, ValidationError):
        errors = exc.errors()
        if errors:
            first = errors[0]
            location = ".".join(str(part) for part in first.get("loc", ())) or "record"
            return f"{location}: {first.get('msg', 'invalid value')}"
    message = str(exc).strip()
    return message.splitlines()[0] if message else exc.__class__.__name__
