"""``init`` + ``check`` on the ticket output_type fixture named by m1.

The invoice demo stays the shipped 24-record example; this small fixture keeps a
second output type under test so ``decisions.jsonl`` is proven to carry records
and verdicts for a type other than ``invoice``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from deltagate.cli import app

FIXTURE = Path(__file__).parent / "fixtures" / "tickets"


def test_init_and_check_gate_the_ticket_fixture(tmp_path: Path) -> None:
    runner = CliRunner()
    rules = tmp_path / "rules.yaml"
    out = tmp_path / "out"

    init = runner.invoke(
        app,
        ["init", "--accepted", str(FIXTURE / "accepted"), "--out", str(rules)],
    )
    assert init.exit_code == 0, init.output
    assert "1 output type(s) · 6 field(s)" in init.stdout
    assert "ticket:" in rules.read_text(encoding="utf-8")

    check = runner.invoke(
        app,
        [
            "check",
            "--baseline",
            str(FIXTURE / "accepted"),
            "--candidate",
            str(FIXTURE / "rerun"),
            "--rules",
            str(rules),
            "--out",
            str(out),
        ],
    )
    assert check.exit_code == 3, check.output

    entries = [
        json.loads(line)
        for line in (out / "decisions.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert [entry["record"]["record_id"] for entry in entries] == [
        "T-2001",
        "T-2002",
        "T-2003",
    ]
    assert {entry["record"]["output_type"] for entry in entries} == {"ticket"}

    verdicts = {entry["record"]["record_id"]: entry["verdict"] for entry in entries}
    assert verdicts["T-2001"]["decision"] == "RELEASE"
    assert verdicts["T-2002"]["decision"] == "REVIEW"
    breached = [
        change for change in verdicts["T-2002"]["changes"] if change["breached"]
    ]
    assert [change["path"] for change in breached] == ["refund_amount"]
    assert breached[0]["before"] == 100.0
    assert breached[0]["after"] == 110.0
    assert breached[0]["limit"] == 0.005
    assert breached[0]["delta"] == pytest.approx(0.1)

    assert verdicts["T-2003"]["decision"] == "REVIEW"
    assert [
        (item["kind"], item["path"]) for item in verdicts["T-2003"]["structural"]
    ] == [("added", "assignee")]

    review = (out / "review.csv").read_text(encoding="utf-8").splitlines()
    assert review[0] == "record_id,output_type,decision,limits_version,reason"
    assert [row.split(",")[0] for row in review[1:]] == ["T-2002", "T-2003"]
