"""Tests for the items.yaml loader."""

from __future__ import annotations

from pathlib import Path

import pytest

from sec_financials.config import (
    Derivation,
    Item,
    ItemsConfig,
    ItemsConfigError,
    load_items,
)

PROJECT_ROOT = Path(__file__).parent.parent
REAL_ITEMS_YAML = PROJECT_ROOT / "config" / "items.yaml"


# ──────────────────────────────────────────────────────────────────────────
# Happy path: the real items.yaml in the repo loads cleanly
# ──────────────────────────────────────────────────────────────────────────


def test_real_items_yaml_loads():
    cfg = load_items(REAL_ITEMS_YAML)
    assert isinstance(cfg, ItemsConfig)
    assert len(cfg.items) == 27, "items.yaml is expected to define 27 items in v1"


def test_real_items_yaml_has_expected_top_line_items():
    cfg = load_items(REAL_ITEMS_YAML)
    keys = {i.key for i in cfg.items}
    # Spot-check a handful from each statement
    for key in ("revenue", "net_income", "cash", "total_assets", "capex"):
        assert key in keys


def test_real_items_yaml_statement_split():
    cfg = load_items(REAL_ITEMS_YAML)
    assert len(cfg.by_statement("income")) == 11
    assert len(cfg.by_statement("balance_sheet")) == 8
    assert len(cfg.by_statement("cash_flow")) == 8


def test_derived_items_have_derivation_set():
    cfg = load_items(REAL_ITEMS_YAML)
    for key in ("debt", "debt_change", "equity_change"):
        item = cfg.by_key(key)
        assert item.is_derived, f"{key} should be a derived item"
        assert item.derivation is not None
        assert item.derivation.add  # non-empty


def test_direct_items_have_xbrl_tags_set():
    cfg = load_items(REAL_ITEMS_YAML)
    item = cfg.by_key("revenue")
    assert not item.is_derived
    assert item.xbrl_tags is not None
    assert "Revenues" in item.xbrl_tags


def test_all_tags_includes_both_modes():
    cfg = load_items(REAL_ITEMS_YAML)
    # Direct lookup item: all_tags == xbrl_tags
    rev = cfg.by_key("revenue")
    assert rev.all_tags == rev.xbrl_tags

    # Derived item: all_tags includes both add and subtract sides
    eq_change = cfg.by_key("equity_change")
    assert "ProceedsFromIssuanceOfCommonStock" in eq_change.all_tags
    assert "PaymentsForRepurchaseOfCommonStock" in eq_change.all_tags


# ──────────────────────────────────────────────────────────────────────────
# Error paths
# ──────────────────────────────────────────────────────────────────────────


def _write(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "items.yaml"
    p.write_text(content, encoding="utf-8")
    return p


def test_missing_file_raises_filenotfound(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        load_items(tmp_path / "nope.yaml")


def test_invalid_yaml_raises_items_error(tmp_path: Path):
    p = _write(tmp_path, "items: [ this is: not: valid")
    with pytest.raises(ItemsConfigError, match="not valid YAML"):
        load_items(p)


def test_missing_required_field_fails(tmp_path: Path):
    p = _write(
        tmp_path,
        """
items:
  - key: revenue
    display_name: Revenue
    statement: income
    flow_or_stock: flow
    # unit missing
    xbrl_tags: [Revenues]
""",
    )
    with pytest.raises(ItemsConfigError, match="unit"):
        load_items(p)


def test_both_xbrl_tags_and_derivation_fails(tmp_path: Path):
    p = _write(
        tmp_path,
        """
items:
  - key: revenue
    display_name: Revenue
    statement: income
    flow_or_stock: flow
    unit: USD
    xbrl_tags: [Revenues]
    derivation:
      add: [Foo]
""",
    )
    with pytest.raises(ItemsConfigError):
        load_items(p)


def test_neither_xbrl_tags_nor_derivation_fails(tmp_path: Path):
    p = _write(
        tmp_path,
        """
items:
  - key: revenue
    display_name: Revenue
    statement: income
    flow_or_stock: flow
    unit: USD
""",
    )
    with pytest.raises(ItemsConfigError):
        load_items(p)


def test_duplicate_keys_fail(tmp_path: Path):
    p = _write(
        tmp_path,
        """
items:
  - key: revenue
    display_name: Revenue
    statement: income
    flow_or_stock: flow
    unit: USD
    xbrl_tags: [Revenues]
  - key: revenue
    display_name: Revenue 2
    statement: income
    flow_or_stock: flow
    unit: USD
    xbrl_tags: [SalesRevenueNet]
""",
    )
    with pytest.raises(ItemsConfigError, match="Duplicate item key"):
        load_items(p)


def test_invalid_statement_fails(tmp_path: Path):
    p = _write(
        tmp_path,
        """
items:
  - key: revenue
    display_name: Revenue
    statement: profit_loss
    flow_or_stock: flow
    unit: USD
    xbrl_tags: [Revenues]
""",
    )
    with pytest.raises(ItemsConfigError, match="profit_loss"):
        load_items(p)


def test_derivation_object_constructs_correctly():
    d = Derivation.from_dict({"add": ["A", "B"], "subtract": ["C"]})
    assert d.add == ("A", "B")
    assert d.subtract == ("C",)


def test_derivation_without_subtract_defaults_to_empty():
    d = Derivation.from_dict({"add": ["A"]})
    assert d.subtract == ()


def test_item_is_frozen():
    item = Item(
        key="x",
        display_name="X",
        statement="income",
        flow_or_stock="flow",
        unit="USD",
        xbrl_tags=("X",),
    )
    with pytest.raises((AttributeError, TypeError)):
        item.key = "y"  # type: ignore[misc]
