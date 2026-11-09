"""The batch report: one self-contained HTML file and the review CSV.

Both artifacts are rendered from models this module is handed. The numeric
limit shown beside each breach comes from the verdicts; the manifest supplies
``limits_version`` and the calibration provenance rather than a copy of the
limit table, so the rules file stays the sole authority for what a limit means.

The HTML inlines its stylesheet and uses no script or external asset, so it can
be archived beside the manifest and opened on an offline machine.
"""

from __future__ import annotations

import csv
import datetime as dt
import html
import io
from collections.abc import Mapping, Sequence
from typing import Any

from .delta import DECISION_REVIEW, FieldChange, StructuralChange, Verdict
from .gate import REVIEW_COLUMNS, Manifest, Record


def review_csv(verdicts: Sequence[Verdict], records: Mapping[str, Record]) -> str:
    """Write the review template ``accept`` consumes.

    Only flagged records get a row. ``decision`` and ``reason`` stay empty so a
    reviewer has to fill them in; an unfilled row makes ``accept`` refuse the
    file instead of releasing it silently.
    """

    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(REVIEW_COLUMNS)
    for verdict in sorted(verdicts, key=lambda item: item.record_id):
        if verdict.decision != DECISION_REVIEW:
            continue
        record = records[verdict.record_id]
        writer.writerow(
            [verdict.record_id, record.output_type, "", verdict.limits_version, ""]
        )
    return buffer.getvalue()


def render_report(
    *,
    manifest: Manifest,
    verdicts: Sequence[Verdict],
    records: Mapping[str, Record],
    generated_at: dt.datetime,
) -> str:
    """Render the batch's single-file HTML report."""

    ordered = sorted(verdicts, key=lambda item: item.record_id)
    provenance = manifest.calibration
    source = "calibrated" if provenance.source == "calibrated" else "defaults"

    return "\n".join(
        [
            "<!doctype html>",
            '<html lang="zh-CN">',
            "<head>",
            '<meta charset="utf-8">',
            '<meta name="viewport" content="width=device-width, initial-scale=1">',
            f"<title>DeltaGate · {_e(manifest.batch_id)}</title>",
            f"<style>{_CSS}</style>",
            "</head>",
            "<body>",
            '<main class="sheet">',
            _header(manifest),
            _summary(manifest),
            _provenance(manifest, source),
            _changes_section(ordered, records),
            _decisions_section(ordered, records),
            _footer(manifest, generated_at),
            "</main>",
            "</body>",
            "</html>",
        ]
    )


def _header(manifest: Manifest) -> str:
    return f"""
<header class="head">
  <div>
    <p class="eyebrow">DeltaGate · 批次放行报告</p>
    <h1>{_e(manifest.batch_id)}</h1>
    <p class="sub">参考快照 <code>{_e(manifest.accepted_snapshot_id)}</code>
      · 限值版本 <code>{_e(manifest.limits_version)}</code></p>
  </div>
  <span class="chip chip-{'review' if manifest.counts.review else 'ok'}">
    {manifest.counts.review} 条待复查
  </span>
</header>
"""


def _summary(manifest: Manifest) -> str:
    counts = manifest.counts
    return f"""
<section class="summary" aria-label="批次汇总">
  <div class="stat"><span class="label">记录</span><strong>{counts.total}</strong></div>
  <div class="stat ok"><span class="label">RELEASE</span><strong>{counts.release}</strong></div>
  <div class="stat review"><span class="label">REVIEW</span><strong>{counts.review}</strong></div>
  <div class="stat"><span class="label">flag_rate</span><strong>{manifest.flag_rate:.1%}</strong></div>
  <div class="stat"><span class="label">budget</span><strong>{manifest.budget:.1%}</strong></div>
</section>
"""


def _provenance(manifest: Manifest, source: str) -> str:
    provenance = manifest.calibration
    holdout = (
        f"{provenance.holdout_flag_rate:.1%}"
        if provenance.holdout_flag_rate is not None
        else "—"
    )
    reason = (
        f'<p class="note">{_e(provenance.reason)}</p>' if provenance.reason else ""
    )
    return f"""
<section class="card" aria-label="限值来源">
  <h2>限值 provenance</h2>
  <dl class="grid">
    <div><dt>source</dt><dd>{_e(source)}</dd></div>
    <div><dt>budget</dt><dd>{provenance.budget:.1%}</dd></div>
    <div><dt>quantile</dt><dd>{provenance.quantile:.2f}</dd></div>
    <div><dt>history</dt><dd>{provenance.history_snapshots} snapshots</dd></div>
    <div><dt>calibrated fields</dt><dd>{provenance.calibrated_fields}</dd></div>
    <div><dt>holdout flag rate</dt><dd>{holdout}</dd></div>
    <div><dt>holdout snapshot</dt><dd>{_e(provenance.holdout_snapshot_id or "—")}</dd></div>
    <div><dt>holdout records</dt><dd>{provenance.holdout_records}</dd></div>
  </dl>
  {reason}
</section>
"""


def _changes_section(
    verdicts: Sequence[Verdict], records: Mapping[str, Record]
) -> str:
    rows: list[str] = []
    for verdict in verdicts:
        if verdict.decision != DECISION_REVIEW:
            continue
        record = records[verdict.record_id]
        for structural in verdict.structural:
            rows.append(_structural_row(verdict.record_id, record.output_type, structural))
        for change in verdict.changes:
            if change.breached:
                rows.append(_change_row(verdict.record_id, record.output_type, change))

    if not rows:
        body = '<p class="empty">本批次没有需要复查的字段。</p>'
    else:
        body = f"""
<table>
  <thead>
    <tr><th>记录</th><th>类型</th><th>字段</th><th>变化</th><th>delta</th><th>limit</th><th>判定</th></tr>
  </thead>
  <tbody>
    {''.join(rows)}
  </tbody>
</table>
"""
    return f"""
<section class="card" aria-label="越限与结构变化">
  <h2>越限与结构变化</h2>
  {body}
</section>
"""


def _decisions_section(
    verdicts: Sequence[Verdict], records: Mapping[str, Record]
) -> str:
    rows = []
    for verdict in verdicts:
        record = records[verdict.record_id]
        flagged = len(verdict.structural) + sum(
            1 for change in verdict.changes if change.breached
        )
        css = "review" if verdict.decision == DECISION_REVIEW else "release"
        detail = f"{flagged} 处变化" if flagged else "无变化"
        rows.append(
            f'<tr class="{css}"><td><code>{_e(verdict.record_id)}</code></td>'
            f"<td>{_e(record.output_type)}</td>"
            f'<td><span class="dot {css}"></span>{_e(verdict.decision)}</td>'
            f"<td>{detail}</td></tr>"
        )
    return f"""
<section class="card" aria-label="逐条判定">
  <h2>逐条判定</h2>
  <table>
    <thead><tr><th>记录</th><th>类型</th><th>判定</th><th>说明</th></tr></thead>
    <tbody>{''.join(rows)}</tbody>
  </table>
</section>
"""


def _change_row(record_id: str, output_type: str, change: FieldChange) -> str:
    delta = "—" if change.delta is None else f"{change.delta:+.4f}"
    limit = "—" if change.limit is None else f"{change.limit:g}"
    return (
        f'<tr class="review"><td><code>{_e(record_id)}</code></td>'
        f"<td>{_e(output_type)}</td>"
        f"<td><code>{_e(change.path)}</code></td>"
        f"<td>{_fmt(change.before)} → {_fmt(change.after)}</td>"
        f"<td><code>{_e(delta)}</code></td>"
        f"<td><code>{_e(limit)}</code></td>"
        '<td><span class="tag">BREACH</span></td></tr>'
    )


def _structural_row(
    record_id: str, output_type: str, change: StructuralChange
) -> str:
    if change.kind == "length_changed":
        detail = f"length {change.before} → {change.after}"
    elif change.kind == "added":
        detail = f"added {_fmt(change.after)}"
    else:
        detail = f"removed {_fmt(change.before)}"
    return (
        f'<tr class="review"><td><code>{_e(record_id)}</code></td>'
        f"<td>{_e(output_type)}</td>"
        f"<td><code>{_e(change.path)}</code></td>"
        f"<td>{detail}</td><td>—</td><td>—</td>"
        '<td><span class="tag">STRUCTURAL</span></td></tr>'
    )


def _footer(manifest: Manifest, generated_at: dt.datetime) -> str:
    return f"""
<footer class="foot">
  <span>生成于 {generated_at.astimezone(dt.UTC).strftime("%Y-%m-%d %H:%M:%S UTC")}</span>
  <span>批次数 {manifest.counts.total} · verdicts {_e(manifest.verdicts_sha256[:12])}
    · limits {_e(manifest.limits_version)}</span>
</footer>
"""


def _fmt(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return _e(f"{value:,}")
    if isinstance(value, float):
        return _e(f"{value:,.2f}")
    return _e(str(value))


def _e(value: str) -> str:
    return html.escape(value, quote=True)


_CSS = """
:root{
  --ink:#131a2a;--muted:#5d6779;--line:#e2e7f0;--bg:#f4f6fa;--panel:#fff;
  --accent:#1f5eff;--review:#b3261e;--review-bg:#fdf1f0;--ok:#176b45;--ok-bg:#eef8f2;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
  font:14px/1.6 ui-sans-serif,-apple-system,"Segoe UI","PingFang SC","Hiragino Sans GB",sans-serif}
.sheet{max-width:1060px;margin:0 auto;padding:40px 24px 64px}
.head{display:flex;align-items:flex-start;justify-content:space-between;gap:24px;margin-bottom:24px}
.eyebrow{margin:0 0 6px;color:var(--accent);font-size:12px;font-weight:700;letter-spacing:.08em;text-transform:uppercase}
h1{margin:0;font-size:28px;line-height:1.2;letter-spacing:-.01em}
.sub{margin:8px 0 0;color:var(--muted)}
h2{margin:0 0 16px;font-size:15px;letter-spacing:.01em}
code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12.5px;color:#243049}
.chip{display:inline-block;padding:6px 12px;border-radius:999px;font-size:13px;font-weight:700;white-space:nowrap}
.chip-review{background:var(--review-bg);color:var(--review);border:1px solid #f2c6c2}
.chip-ok{background:var(--ok-bg);color:var(--ok);border:1px solid #c6e7d5}
.summary{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:12px;margin-bottom:20px}
.stat{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px 16px}
.stat .label{display:block;color:var(--muted);font-size:11.5px;letter-spacing:.06em;text-transform:uppercase}
.stat strong{display:block;margin-top:4px;font-size:22px;font-variant-numeric:tabular-nums}
.stat.review strong{color:var(--review)}.stat.ok strong{color:var(--ok)}
.card{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:20px;margin-bottom:20px}
.grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:14px 20px;margin:0}
.grid div{min-width:0}
dt{color:var(--muted);font-size:11.5px;letter-spacing:.05em;text-transform:uppercase}
dd{margin:2px 0 0;font-variant-numeric:tabular-nums;overflow-wrap:anywhere}
.note{margin:14px 0 0;color:var(--muted);font-size:13px}
table{width:100%;border-collapse:collapse}
th{text-align:left;color:var(--muted);font-size:11.5px;letter-spacing:.05em;text-transform:uppercase;border-bottom:1px solid var(--line);padding:8px 10px}
td{padding:9px 10px;border-bottom:1px solid var(--line);vertical-align:top}
tr.review td{background:var(--review-bg)}
tbody tr:last-child td{border-bottom:0}
.tag{display:inline-block;padding:2px 7px;border-radius:6px;background:#fff;border:1px solid #f2c6c2;color:var(--review);font-size:11px;font-weight:700;letter-spacing:.04em}
.dot{display:inline-block;width:7px;height:7px;border-radius:50%;margin-right:7px;background:var(--ok)}
.dot.review{background:var(--review)}
.empty{margin:0;color:var(--muted)}
.foot{display:flex;flex-wrap:wrap;gap:8px 24px;justify-content:space-between;color:var(--muted);font-size:12px;padding-top:8px}
@media (max-width:760px){
  .summary{grid-template-columns:repeat(2,minmax(0,1fr))}
  .grid{grid-template-columns:repeat(2,minmax(0,1fr))}
  .head{flex-direction:column}
}
"""
