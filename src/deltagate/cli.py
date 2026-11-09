"""The ``deltagate`` command line.

``check`` is the contract: it prints one header, one batch line, the exit line,
one row per flagged field, and the artifact paths -- in that order -- then exits
0 when everything released, 3 when a record needs review, 4 when more than 2% of
the candidate records are malformed, and 2 on a usage or state error.

``init`` and ``calibrate`` are the two ways a rules file is produced;
``accept`` is the loop back into the accepted set.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any, Optional

import typer

from . import report, rules as rules_file, store
from .calibrate import calibrate as calibrate_limits
from .delta import (
    DECISION_REVIEW,
    EXIT_MALFORMED,
    EXIT_OK,
    EXIT_REVIEW,
    EXIT_USAGE,
    FieldChange,
    FieldKind,
    FieldLimit,
    LimitUnit,
    MalformedValue,
    StructuralChange,
    Verdict,
    compare_record,
    exit_code,
    resolve_limit,
)
from .gate import (
    BatchOutcome,
    GateError,
    Manifest,
    Record,
    accept as accept_review,
    gate_batch,
    read_review,
)

# More malformed records than this share of the batch is an input problem, not
# something a reviewer can adjudicate.
MALFORMED_BUDGET = 0.02

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Record-by-record release gate for re-extracted documents.",
)


@app.command()
def init(
    accepted: Path = typer.Option(
        ...,
        "--accepted",
        exists=True,
        file_okay=False,
        readable=True,
        help="Directory holding the last accepted records.",
    ),
    out: Path = typer.Option(
        ...,
        "--out",
        help="Rules file to write.",
    ),
    budget: float = typer.Option(
        rules_file.DEFAULT_BUDGET,
        "--budget",
        help="False-flag budget recorded in the rules file.",
    ),
) -> None:
    """Infer conservative default limits from an accepted snapshot."""

    try:
        snapshot = store.load_snapshot(accepted)
        if not snapshot.records:
            raise store.StoreError(f"{accepted}: no records found")
        rules = rules_file.from_snapshot(
            snapshot.records.values(),
            snapshot_id=snapshot.snapshot_id,
            budget=budget,
        )
        rules = rules_file.write(rules, out)
    except (store.StoreError, rules_file.RulesError) as exc:
        _fail(str(exc))

    fields = sum(len(table) for table in rules.limits.values())
    typer.echo(
        f"wrote {out} · limits {rules.limits_version} · "
        f"{len(rules.limits)} output type(s) · {fields} field(s) · "
        f"defaults (budget {rules.budget:.1%})"
    )


@app.command()
def check(
    baseline: Path = typer.Option(
        ...,
        "--baseline",
        exists=True,
        file_okay=False,
        readable=True,
        help="Accepted snapshot the candidate is compared against.",
    ),
    candidate: Path = typer.Option(
        ...,
        "--candidate",
        exists=True,
        file_okay=False,
        readable=True,
        help="Re-run records to gate.",
    ),
    rules_path: Path = typer.Option(
        ...,
        "--rules",
        exists=True,
        dir_okay=False,
        readable=True,
        help="Rules file that supplies the field limits.",
    ),
    out: Path = typer.Option(
        Path("out"),
        "--out",
        help="Directory for decisions.jsonl, review.csv, report.html and manifest.json.",
    ),
) -> None:
    """Gate every candidate record and write the batch artifacts."""

    try:
        rules = rules_file.load(rules_path)
        baseline_snapshot = store.load_snapshot(baseline)
        candidate_snapshot = store.load_snapshot(candidate)
    except (rules_file.RulesError, store.StoreError) as exc:
        _fail(str(exc))

    malformed = dict(candidate_snapshot.malformed)
    gated: dict[str, Record] = {}
    verdicts: list[Verdict] = []
    try:
        for record in sorted(
            candidate_snapshot.records.values(), key=lambda item: item.record_id
        ):
            reference = baseline_snapshot.records.get(record.record_id)
            before = (
                reference.fields
                if reference is not None
                and reference.output_type == record.output_type
                else {}
            )
            table = rules.limits.get(record.output_type, {})
            try:
                verdict = compare_record(
                    record.record_id,
                    before,
                    record.fields,
                    table,
                    rules.limits_version,
                )
            except MalformedValue as exc:
                malformed[record.record_id] = str(exc)
                continue
            verdicts.append(verdict)
            gated[record.record_id] = record
    except ValueError as exc:
        # A kind/unit pair the rules file cannot express is a rules problem,
        # not a bad record; compare_field reports it the same way either way.
        _fail(f"{rules_path}: {exc}")

    total = len(candidate_snapshot.records) + len(candidate_snapshot.malformed)
    if total == 0:
        _fail(f"{candidate}: no records found")
    if malformed and len(malformed) / total > MALFORMED_BUDGET:
        typer.echo(
            f"error: {len(malformed)} of {total} records are malformed "
            f"(> {MALFORMED_BUDGET:.0%}); fix the input before gating",
            err=True,
        )
        for name, detail in sorted(malformed.items())[:5]:
            typer.echo(f"  {name}: {detail}", err=True)
        raise typer.Exit(EXIT_MALFORMED)
    if malformed:
        typer.echo(
            f"warning: skipped {len(malformed)} malformed record(s) of {total}",
            err=True,
        )
    if not verdicts:
        _fail(f"{candidate}: no usable records to gate")

    batch_id = (
        f"{candidate_snapshot.snapshot_id}-{dt.datetime.now(dt.UTC):%Y%m%dT%H%M%SZ}"
    )
    try:
        outcome = gate_batch(
            verdicts,
            batch_id=batch_id,
            accepted_snapshot_id=baseline_snapshot.snapshot_id,
            budget=rules.budget,
            calibration=rules.calibration,
        )
        generated_at = dt.datetime.now(dt.UTC)
        store.write_batch(
            out,
            records=gated,
            verdicts=outcome.verdicts,
            manifest=outcome.manifest,
            review_csv=report.review_csv(outcome.verdicts, gated),
            report_html=report.render_report(
                manifest=outcome.manifest,
                verdicts=outcome.verdicts,
                records=gated,
                generated_at=generated_at,
            ),
        )
    except (GateError, store.StoreError) as exc:
        _fail(str(exc))

    code = exit_code(outcome.verdicts)
    _print_header(rules)
    _print_summary(outcome.manifest, rules)
    _print_exit(code)
    _print_reviews(outcome.verdicts, gated, rules)
    typer.echo(
        f"{out / store.DECISIONS_FILE}   {out / store.REPORT_FILE}   "
        f"{out / store.MANIFEST_FILE}"
    )
    raise typer.Exit(code)


@app.command()
def calibrate(
    history: Path = typer.Option(
        ...,
        "--history",
        exists=True,
        file_okay=False,
        readable=True,
        help="Directory of accepted snapshots, or one snapshot directory.",
    ),
    rules_path: Path = typer.Option(
        ...,
        "--rules",
        exists=True,
        dir_okay=False,
        readable=True,
        help="Rules file to calibrate in place.",
    ),
    budget: float = typer.Option(
        rules_file.DEFAULT_BUDGET,
        "--budget",
        help="Share of a stable batch the limits may flag.",
    ),
) -> None:
    """Fit per-field limits from history and record the held-out flag rate."""

    try:
        rules = rules_file.load(rules_path)
        snapshots = store.load_history(history)
        fitted = calibrate_limits(snapshots, base=rules.limits, budget=budget)
        rules = rules_file.write(
            rules_file.apply_calibration(rules, fitted), rules_path
        )
    except (rules_file.RulesError, store.StoreError, ValueError) as exc:
        _fail(str(exc))

    provenance = rules.calibration
    if provenance.source == "calibrated":
        holdout = (
            f"{provenance.holdout_flag_rate:.1%}"
            if provenance.holdout_flag_rate is not None
            else "no holdout records"
        )
        typer.echo(
            f"calibrated {provenance.calibrated_fields} field(s) over "
            f"{provenance.history_snapshots} snapshot(s) · limits "
            f"{rules.limits_version} · holdout {holdout} vs budget "
            f"{rules.budget:.1%}"
        )
    else:
        typer.echo(
            f"kept defaults · {provenance.reason} · limits {rules.limits_version}"
        )


@app.command()
def accept(
    manifest: Path = typer.Option(
        ...,
        "--manifest",
        exists=True,
        dir_okay=False,
        readable=True,
        help="manifest.json written by check.",
    ),
    review: Path = typer.Option(
        ...,
        "--review",
        exists=True,
        dir_okay=False,
        readable=True,
        help="review.csv with a filled-in decision per flagged record.",
    ),
    into: Path = typer.Option(
        ...,
        "--into",
        help="Accepted-snapshot root the new snapshot is archived under.",
    ),
    rules_path: Optional[Path] = typer.Option(
        None,
        "--rules",
        exists=True,
        dir_okay=False,
        readable=True,
        help="Current rules file; also refuses a review written against other limits.",
    ),
) -> None:
    """Turn a review into the next accepted snapshot."""

    try:
        archived = store.read_manifest(manifest)
        records, verdicts = store.read_decisions(
            manifest.parent / store.DECISIONS_FILE
        )
        review_file = read_review(review)
        current_version = (
            rules_file.load(rules_path).limits_version
            if rules_path is not None
            else archived.limits_version
        )
        batch = BatchOutcome(manifest=archived, verdicts=tuple(verdicts))
        snapshot = accept_review(
            batch,
            candidate=records,
            review=review_file,
            current_limits_version=current_version,
            existing=store.load_manifests(into),
        )
        target = store.write_snapshot(snapshot, into)
    except (store.StoreError, rules_file.RulesError, GateError) as exc:
        _fail(str(exc))

    typer.echo(
        f"accepted {len(snapshot.records)} record(s) into {target} · limits "
        f"{snapshot.limits_version} · manifest {snapshot.manifest_sha256[:12]}"
    )


def _print_header(rules: rules_file.Rules) -> None:
    provenance = rules.calibration
    if provenance.source == "calibrated":
        source = "source calibrated"
        if provenance.holdout_flag_rate is not None:
            source += f" (holdout {provenance.holdout_flag_rate:.1%})"
    else:
        source = "source defaults"
    typer.echo(
        f"DeltaGate · limits {rules.limits_version} · "
        f"budget {rules.budget:.1%} · {source}"
    )


def _print_summary(manifest: Manifest, rules: rules_file.Rules) -> None:
    counts = manifest.counts
    typer.echo(
        f"{counts.total} records  →  {counts.release} RELEASE   "
        f"{counts.review} REVIEW   flag_rate {manifest.flag_rate:.1%} vs "
        f"budget {rules.budget:.1%}"
    )


def _print_exit(code: int) -> None:
    label = {
        EXIT_OK: "all released",
        EXIT_REVIEW: "review required",
        EXIT_MALFORMED: "malformed input",
    }.get(code, "usage error")
    typer.echo(f"exit {code}  ({label})")


def _print_reviews(
    verdicts: list[Verdict],
    records: dict[str, Record],
    rules: rules_file.Rules,
) -> None:
    for verdict in sorted(verdicts, key=lambda item: item.record_id):
        if verdict.decision != DECISION_REVIEW:
            continue
        record = records[verdict.record_id]
        table = rules.limits.get(record.output_type, {})
        for structural in verdict.structural:
            typer.echo(
                _structural_row(verdict.record_id, record.output_type, structural)
            )
        for change in verdict.changes:
            if change.breached:
                typer.echo(
                    _change_row(
                        verdict.record_id,
                        record.output_type,
                        change,
                        resolve_limit(table, change.path, change.before),
                    )
                )


def _change_row(
    record_id: str,
    output_type: str,
    change: FieldChange,
    limit: FieldLimit,
) -> str:
    prefix = _row_prefix(record_id, output_type, change.path)
    detail = (
        f"{_format_value(change.before, limit.kind)} → "
        f"{_format_value(change.after, limit.kind)}  "
        f"{_format_delta(change.delta, limit)}  {_format_limit(limit)}"
    )
    return f"{prefix}{detail}  BREACH"


def _structural_row(
    record_id: str, output_type: str, change: StructuralChange
) -> str:
    if change.kind == "length_changed":
        detail = f"length {change.before} → {change.after}"
    elif change.kind == "added":
        detail = f"added {change.after}"
    else:
        detail = f"removed {change.before}"
    # The fixed width keeps STRUCTURAL in the verdict column for row lengths the
    # demo uses; longer details simply push it right.
    return f"{_row_prefix(record_id, output_type, change.path)}{detail:<44}STRUCTURAL"


def _row_prefix(record_id: str, output_type: str, path: str) -> str:
    return f"  {record_id:<10}{output_type:<11}{path:<13}  "


def _format_value(value: Any, kind: FieldKind) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "true" if value else "false"
    if kind in (FieldKind.MONEY, FieldKind.NUMERIC):
        number = _as_number(value)
        if number is not None:
            if kind is FieldKind.MONEY:
                return f"¥{number:,.2f}"
            return f"{number:,.2f}" if number % 1 else f"{int(number):,}"
    return str(value)


def _format_delta(delta: Optional[float], limit: FieldLimit) -> str:
    if delta is None:
        return "Δ —"
    if limit.unit is LimitUnit.RELATIVE:
        return f"Δ {_format_percent(delta)}"
    if limit.unit is LimitUnit.ABSOLUTE:
        return f"Δ {delta:+,.2f}"
    if limit.unit is LimitUnit.DAYS:
        return f"Δ {delta:+.0f} day(s)"
    if limit.unit is LimitUnit.EXACT:
        return "Δ changed"
    return f"Δ {delta:+.2f}"


def _format_limit(limit: FieldLimit) -> str:
    if limit.unit is LimitUnit.RELATIVE:
        return f"limit {limit.limit:.2%}"
    if limit.unit is LimitUnit.ABSOLUTE:
        return f"limit {limit.limit:,.2f}"
    if limit.unit is LimitUnit.DAYS:
        return f"limit {limit.limit:g} day(s)"
    if limit.unit is LimitUnit.EXACT:
        return "limit exact"
    return f"limit {limit.limit:g}"


def _format_percent(delta: float) -> str:
    """Percent with two decimals below 10% and one at or above it.

    Keeps a 0.79% change readable without dropping the second digit, while wide
    changes do not pretend to precision the reviewer does not have.
    """

    percent = delta * 100.0
    # The tolerance keeps an exact 10.0% from losing its single-decimal form to
    # a float that lands at 9.999999999999998.
    digits = 1 if abs(percent) >= 10.0 - 1e-9 else 2
    return f"{percent:+.{digits}f}%"


def _as_number(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _fail(message: str, *, code: int = EXIT_USAGE) -> None:
    typer.echo(f"error: {message}", err=True)
    raise typer.Exit(code)


if __name__ == "__main__":
    app()
