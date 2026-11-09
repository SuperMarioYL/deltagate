# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
semantic versioning.

## [0.1.0] - 2026-11-10

First release: a record-by-record release gate for re-extracted documents.

### Added

- `deltagate init` infers output types and field paths from an accepted
  snapshot and writes conservative per-field limits.
- Field kinds `money`, `numeric`, `date`, `code` and `text`, with relative,
  absolute, whole-day, exact and edit-ratio comparison units.
- `deltagate calibrate` fits limits from accepted history at a configurable
  false-flag budget and measures the flag rate on the held-out snapshot pair;
  a short history falls back to the declared defaults with an explicit reason.
- `deltagate check` compares every candidate record, writes
  `decisions.jsonl`, `report.html`, `manifest.json` and `review.csv`, and
  exits 0, 3 or 4.
- `deltagate accept` applies a filled-in review, archives the new snapshot
  together with its manifest, and rejects a repeated batch or review file.
- `limits_version` derived from the rules file contents, quoted by the report
  and manifest so a stale review cannot release a batch measured against
  different limits.
- A single-file HTML report with the breached-field table, per-record
  decisions and calibration provenance.
- A synthetic 24-invoice fixture, a recorded 45-second terminal demo, and
  Chinese and English documentation.

[0.1.0]: https://github.com/SuperMarioYL/deltagate/releases/tag/v0.1.0
