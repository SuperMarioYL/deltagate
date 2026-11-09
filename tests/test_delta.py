"""Field-level delta semantics: kinds, limits, structure and the record verdict.

These tests drive the public functions only. They are the contract m1
(``init`` + ``check``) depends on: which limit a field gets, how one delta is
measured in that limit's unit, when it breaches, what counts as a structural
change, and how a record becomes RELEASE or REVIEW.
"""

from __future__ import annotations

import pytest

from deltagate.delta import (
    DECISION_RELEASE,
    DECISION_REVIEW,
    DEFAULT_LIMITS,
    EXIT_OK,
    EXIT_REVIEW,
    FieldKind,
    FieldLimit,
    LimitUnit,
    MalformedValue,
    compare_field,
    compare_fields,
    compare_record,
    exit_code,
    infer_kind,
    infer_limits,
    normalize,
    path_template,
    resolve_limit,
)

CODE = FieldLimit(
    kind=FieldKind.CODE,
    unit=LimitUnit.EXACT,
    normalize=("nfkc", "strip_ws", "cf_upper"),
)
MONEY = FieldLimit(
    kind=FieldKind.MONEY, unit=LimitUnit.RELATIVE, limit=0.005, floor=0.01
)
NUMERIC = FieldLimit(kind=FieldKind.NUMERIC, unit=LimitUnit.RELATIVE, limit=0.01)
DATE = FieldLimit(kind=FieldKind.DATE, unit=LimitUnit.DAYS)
TEXT = FieldLimit(
    kind=FieldKind.TEXT, unit=LimitUnit.EDIT_RATIO, limit=0.2, normalize=("nfkc",)
)


# --- money and numeric -----------------------------------------------------


def test_money_breaches_only_above_max_of_floor_and_relative_limit() -> None:
    # 1,284.00 → 1,294.20 is +0.79%, over the 0.50% limit.
    change = compare_field("total_amount", 1284.0, 1294.2, MONEY)
    assert change.breached is True
    assert change.delta == pytest.approx(1294.2 / 1284.0 - 1.0)
    assert change.limit == 0.005


def test_money_inside_limit_is_a_release() -> None:
    change = compare_field("total_amount", 1284.0, 1289.0, MONEY)
    assert change.delta == pytest.approx(5.0 / 1284.0)
    assert change.breached is False


def test_money_floor_keeps_a_tiny_amount_from_flagging() -> None:
    # The relative limit allows only 0.5% of 0.50 (0.0025), below the ¥0.01 floor.
    within = compare_field("fee", 0.50, 0.508, MONEY)
    assert within.breached is False
    beyond = compare_field("fee", 0.50, 0.52, MONEY)
    assert beyond.breached is True


def test_money_relative_delta_is_undefined_from_zero() -> None:
    change = compare_field("discount", 0.0, 12.0, MONEY)
    assert change.delta is None
    assert change.breached is True


def test_numeric_limit_is_strict_about_the_boundary() -> None:
    # 2% limit on 200.00 allows exactly 2.00, so 202.00 is still inside.
    at_limit = compare_field("quantity", 200.0, 202.0, NUMERIC)
    assert at_limit.breached is False
    over_limit = compare_field("quantity", 200.0, 203.0, NUMERIC)
    assert over_limit.breached is True


def test_absolute_unit_bounds_the_difference_itself() -> None:
    absolute = FieldLimit(kind=FieldKind.MONEY, unit=LimitUnit.ABSOLUTE, limit=5.0)
    change = compare_field("discount", 200.0, 204.0, absolute)
    assert change.delta == pytest.approx(4.0)
    assert change.breached is False
    assert compare_field("discount", 200.0, 206.0, absolute).breached is True


def test_money_rejects_a_value_it_cannot_read() -> None:
    with pytest.raises(MalformedValue):
        compare_field("total_amount", 1284.0, "待补录", MONEY)


def test_a_limit_unit_that_does_not_fit_the_kind_is_an_error() -> None:
    wrong = FieldLimit(kind=FieldKind.MONEY, unit=LimitUnit.DAYS, limit=1.0)
    with pytest.raises(ValueError):
        compare_field("total_amount", 1.0, 2.0, wrong)


# --- dates, codes, text ----------------------------------------------------


def test_date_uses_whole_days() -> None:
    change = compare_field("invoiceDate", "2026-09-08", "2026-09-10", DATE)
    assert change.delta == pytest.approx(2.0)
    assert change.breached is True
    same = compare_field("invoiceDate", "2026-09-08", "2026-09-08", DATE)
    assert same.delta == 0.0
    assert same.breached is False


def test_date_accepts_compact_and_iso_spellings() -> None:
    change = compare_field("issue_date", 20261110, "2026-11-11", DATE)
    assert change.delta == pytest.approx(1.0)


def test_code_ignores_case_width_and_spacing_but_flags_a_real_change() -> None:
    cosmetic = compare_field("invoiceNum", "ａｂ-1007 ", "AB-1007", CODE)
    assert cosmetic.delta == 0.0
    assert cosmetic.breached is False
    # An integral float is the same identifier as its integer spelling.
    assert compare_field("invoiceNum", 1007, 1007.0, CODE).breached is False
    real = compare_field("invoiceNum", 1007, 1008, CODE)
    assert real.delta == 1.0
    assert real.breached is True


def test_text_delta_is_a_ratio_in_the_unit_range() -> None:
    # fuzz.ratio("abcd", "abce") is 75, so the edit ratio is 0.25, not 25.
    change = compare_field("remark", "abcd", "abce", TEXT)
    assert change.delta == pytest.approx(0.25)
    assert change.breached is True
    assert compare_field("remark", "abcd", "abce", TEXT).delta <= 1.0
    assert compare_field("remark", "same text", "same text", TEXT).delta == 0.0


def test_text_limit_lets_a_small_rewording_through() -> None:
    loose = TEXT.model_copy(update={"limit": 0.3})
    assert compare_field("remark", "abcd", "abce", loose).breached is False


# --- structure -------------------------------------------------------------


def test_array_length_change_is_structural_and_compares_the_shared_prefix() -> None:
    before = {"line_items": [{"amount": 700.0}, {"amount": 300.0}, {"amount": 211.32}]}
    after = {"line_items": [{"amount": 700.0}, {"amount": 300.0}]}
    changes, structural = compare_fields(before, after, {"line_items[].amount": MONEY})

    assert [change.path for change in structural] == ["line_items"]
    assert structural[0].kind == "length_changed"
    assert (structural[0].before, structural[0].after) == (3, 2)
    # The two surviving elements are still compared field by field.
    assert {change.path for change in changes} == {
        "line_items[0].amount",
        "line_items[1].amount",
    }


def test_array_elements_are_compared_by_index() -> None:
    before = {"line_items": [{"amount": 88.0}, {"amount": 120.0}]}
    after = {"line_items": [{"amount": 88.0}, {"amount": 132.0}]}
    changes, structural = compare_fields(before, after, {"line_items[].amount": MONEY})

    assert structural == []
    breached = [change for change in changes if change.breached]
    assert [change.path for change in breached] == ["line_items[1].amount"]


def test_added_and_removed_fields_are_structural() -> None:
    _, added = compare_fields({"seller": "甲"}, {"seller": "甲", "remark": "已核对"}, {})
    assert [(item.kind, item.path) for item in added] == [("added", "remark")]

    _, removed = compare_fields({"seller": "甲", "remark": "已核对"}, {"seller": "甲"}, {})
    assert [(item.kind, item.path) for item in removed] == [("removed", "remark")]


def test_nested_mappings_keep_their_dotted_path() -> None:
    before = {"meta": {"issuer": {"name": "甲公司"}}}
    after = {"meta": {"issuer": {"name": "乙公司"}}}
    changes, structural = compare_fields(before, after, {})
    assert structural == []
    assert [change.path for change in changes] == ["meta.issuer.name"]


def test_a_field_that_changes_shape_needs_a_human() -> None:
    changes, structural = compare_fields({"note": "text"}, {"note": {"text": "text"}}, {})
    assert structural == []
    assert len(changes) == 1
    assert changes[0].delta is None
    assert changes[0].limit is None
    assert changes[0].breached is True


# --- the record verdict ----------------------------------------------------


def test_record_releases_when_every_field_is_inside_its_limit() -> None:
    verdict = compare_record(
        "INV-1002",
        {"total_amount": 500.0, "remark": "当月服务费"},
        {"total_amount": 501.0, "remark": "当月服务费"},
        {"total_amount": MONEY, "remark": TEXT},
        "v123456789abc",
    )
    assert verdict.decision == DECISION_RELEASE
    assert verdict.limits_version == "v123456789abc"


def test_record_reviews_when_one_field_breaches() -> None:
    verdict = compare_record(
        "INV-1007",
        {"total_amount": 1284.0, "remark": "当月服务费"},
        {"total_amount": 1294.2, "remark": "当月服务费"},
        {"total_amount": MONEY, "remark": TEXT},
        "v123456789abc",
    )
    assert verdict.decision == DECISION_REVIEW
    assert [change.path for change in verdict.changes if change.breached] == [
        "total_amount"
    ]


def test_record_reviews_on_a_structural_change_even_without_a_breach() -> None:
    verdict = compare_record(
        "INV-1009",
        {"line_items": [{"amount": 10.0}, {"amount": 20.0}]},
        {"line_items": [{"amount": 10.0}]},
        {"line_items[].amount": MONEY},
        "v1",
    )
    assert verdict.decision == DECISION_REVIEW
    assert [item.kind for item in verdict.structural] == ["length_changed"]


def test_a_record_absent_from_the_baseline_is_all_added_fields() -> None:
    # ``check`` passes an empty reference when the baseline has no such record;
    # every field then counts as added, which is a REVIEW by invariant 1.
    verdict = compare_record("INV-1099", {}, {"total_amount": 42.0}, {}, "v1")
    assert verdict.decision == DECISION_REVIEW
    assert [item.kind for item in verdict.structural] == ["added"]


def test_exit_code_is_zero_only_when_nothing_needs_review() -> None:
    released = compare_record("A", {"x": 1}, {"x": 1}, {"x": NUMERIC}, "v1")
    flagged = compare_record("B", {"x": 1}, {"x": 2}, {"x": NUMERIC}, "v1")

    assert exit_code([released]) == EXIT_OK
    assert exit_code([released, flagged]) == EXIT_REVIEW


# --- limit selection and inference ----------------------------------------


@pytest.mark.parametrize(
    ("path", "value", "kind"),
    [
        ("total_amount", 1284.0, FieldKind.MONEY),
        ("tax_amount", 72.68, FieldKind.MONEY),
        ("line_items[].unit_price", 88.0, FieldKind.MONEY),
        ("vat_rate", 0.06, FieldKind.NUMERIC),
        ("numItems", 4, FieldKind.NUMERIC),
        ("invoiceDate", "2026-09-08", FieldKind.DATE),
        ("issue_date", 20261110, FieldKind.DATE),
        ("invoiceNum", 1007, FieldKind.CODE),
        ("invoice_no", "A-1007", FieldKind.CODE),
        ("invoiceNum", "A-1007", FieldKind.CODE),
        ("remark", "已核对", FieldKind.TEXT),
        ("currency", "CNY", FieldKind.TEXT),
    ],
)
def test_kind_inference(path: str, value: object, kind: FieldKind) -> None:
    assert infer_kind(path, value) is kind


def test_identifier_hints_beat_money_and_date_hints() -> None:
    # ``tax_id`` is an identifier even though it contains a money word.
    assert infer_kind("tax_id", 91340100) is FieldKind.CODE
    assert infer_kind("sellerAccountNo", 6222) is FieldKind.CODE


def test_init_writes_the_conservative_defaults_per_kind() -> None:
    table = infer_limits(
        [
            (
                "invoice",
                {
                    "invoiceNum": 1007,
                    "invoiceDate": "2026-09-08",
                    "total_amount": 1284.0,
                    "vat_rate": 0.06,
                    "remark": "已核对",
                    "line_items": [{"amount": 700.0, "quantity": 1}],
                },
            )
        ]
    )

    assert set(table) == {"invoice"}
    limits = table["invoice"]
    assert set(limits) == {
        "invoiceNum",
        "invoiceDate",
        "total_amount",
        "vat_rate",
        "remark",
        "line_items[].amount",
        "line_items[].quantity",
    }
    # Array elements are keyed by their template, not by an index.
    assert limits["line_items[].amount"] == DEFAULT_LIMITS[FieldKind.MONEY]
    assert limits["invoiceNum"].unit is LimitUnit.EXACT
    assert limits["invoiceDate"].limit == 0.0
    assert limits["remark"].unit is LimitUnit.EDIT_RATIO


def test_array_indexes_collapse_to_one_rule_key() -> None:
    assert path_template("line_items[1].amount") == "line_items[].amount"
    assert path_template("line_items[0].sub[12].code") == "line_items[].sub[].code"
    assert path_template("total_amount") == "total_amount"


def test_resolve_limit_prefers_exact_then_template_then_root_then_default() -> None:
    exact = MONEY.model_copy(update={"limit": 0.5})
    template = MONEY.model_copy(update={"limit": 0.05})
    root = MONEY.model_copy(update={"limit": 0.005})
    limits = {
        "line_items[0].amount": exact,
        "line_items[].amount": template,
        "line_items": root,
    }

    assert resolve_limit(limits, "line_items[0].amount", 10.0) is exact
    assert resolve_limit(limits, "line_items[3].amount", 10.0) is template
    # An unindexed nested path falls back to the array root's rule.
    assert resolve_limit(limits, "line_items.amount", 10.0) is root
    # A path the file never mentions gets its kind's default.
    assert resolve_limit(limits, "vat_rate", 0.06) == DEFAULT_LIMITS[
        FieldKind.NUMERIC
    ]


def test_normalize_steps_are_applied_in_order() -> None:
    assert normalize("  Ａ Ｂ  ", ("nfkc", "strip_ws")) == "A B"
    assert normalize("ab", ("cf_upper",)) == "AB"
    with pytest.raises(ValueError):
        normalize("x", ("unknown",))
