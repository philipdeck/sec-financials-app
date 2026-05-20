"""Load and validate the maintained items list (`config/items.yaml`).

Each item in the file produces one column in the output CSV. An item is
defined by EITHER an `xbrl_tags` fallback list (direct tag lookup) OR a
`derivation` block (sum/subtract of multiple tags) — never both, never
neither. The schema is enforced at load time so a malformed config fails
the app at startup rather than silently producing missing columns.

See REQUIREMENTS.md §5.2 for the full schema spec.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml
from jsonschema import Draft202012Validator

Statement = Literal["income", "balance_sheet", "cash_flow"]
# - flow: quantity over a period, sums across quarters (revenue, expenses, cash flows)
# - stock: point-in-time balance (assets, equity, cash on hand)
# - period_avg: weighted-average over a period, NOT additive (shares outstanding).
#   Q4 cannot be derived from annual − (Q1+Q2+Q3) for these; the M1 extractor
#   leaves Q4 blank and records the reason in `notes`.
FlowOrStock = Literal["flow", "stock", "period_avg"]


# JSON Schema for items.yaml. Enforces:
# - all required fields present
# - exactly one of xbrl_tags / derivation
# - tag lists are non-empty strings of plausible XBRL concept names
_ITEMS_SCHEMA: dict = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": ["items"],
    "additionalProperties": False,
    "properties": {
        "items": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "required": [
                    "key",
                    "display_name",
                    "statement",
                    "flow_or_stock",
                    "unit",
                ],
                "additionalProperties": False,
                "properties": {
                    "key": {
                        "type": "string",
                        "pattern": "^[a-z][a-z0-9_]*$",
                        "description": "snake_case identifier used as CSV column header",
                    },
                    "display_name": {"type": "string", "minLength": 1},
                    "statement": {
                        "enum": ["income", "balance_sheet", "cash_flow"],
                    },
                    "flow_or_stock": {"enum": ["flow", "stock", "period_avg"]},
                    "unit": {"type": "string", "minLength": 1},
                    "xbrl_tags": {
                        "type": "array",
                        "minItems": 1,
                        "items": {"type": "string", "minLength": 1},
                    },
                    "derivation": {
                        "type": "object",
                        "required": ["add"],
                        "additionalProperties": False,
                        "properties": {
                            "add": {
                                "type": "array",
                                "minItems": 1,
                                "items": {"type": "string", "minLength": 1},
                            },
                            "subtract": {
                                "type": "array",
                                "items": {"type": "string", "minLength": 1},
                            },
                        },
                    },
                },
                # Exactly one of xbrl_tags / derivation must be present.
                "oneOf": [
                    {"required": ["xbrl_tags"], "not": {"required": ["derivation"]}},
                    {"required": ["derivation"], "not": {"required": ["xbrl_tags"]}},
                ],
            },
        },
    },
}


class ItemsConfigError(ValueError):
    """Raised when items.yaml fails to load or validate."""


@dataclass(frozen=True)
class Derivation:
    """A linear combination of XBRL tag values: sum(add) − sum(subtract)."""

    add: tuple[str, ...]
    subtract: tuple[str, ...]

    @classmethod
    def from_dict(cls, raw: dict) -> Derivation:
        return cls(
            add=tuple(raw["add"]),
            subtract=tuple(raw.get("subtract") or ()),
        )


@dataclass(frozen=True)
class Item:
    """One column in the output CSV.

    Exactly one of `xbrl_tags` or `derivation` is set (enforced by schema).
    """

    key: str
    display_name: str
    statement: Statement
    flow_or_stock: FlowOrStock
    unit: str
    xbrl_tags: tuple[str, ...] | None = None
    derivation: Derivation | None = None

    @property
    def is_derived(self) -> bool:
        return self.derivation is not None

    @property
    def all_tags(self) -> tuple[str, ...]:
        """Every XBRL tag referenced by this item, regardless of mode.

        Useful for prefetching companyfacts data and for the sources sidecar.
        """
        if self.xbrl_tags is not None:
            return self.xbrl_tags
        assert self.derivation is not None  # schema guarantees this
        return self.derivation.add + self.derivation.subtract


@dataclass(frozen=True)
class ItemsConfig:
    """The loaded, validated items list."""

    items: tuple[Item, ...]

    def by_key(self, key: str) -> Item:
        for item in self.items:
            if item.key == key:
                return item
        raise KeyError(f"No item with key {key!r}")

    def by_statement(self, statement: Statement) -> tuple[Item, ...]:
        return tuple(i for i in self.items if i.statement == statement)


def _validate(raw: object) -> None:
    """Run JSON Schema validation, raising ItemsConfigError on failure."""
    validator = Draft202012Validator(_ITEMS_SCHEMA)
    errors = sorted(validator.iter_errors(raw), key=lambda e: list(e.absolute_path))
    if not errors:
        return
    lines = ["items.yaml failed schema validation:"]
    for err in errors:
        path = ".".join(str(p) for p in err.absolute_path) or "<root>"
        lines.append(f"  - at {path}: {err.message}")
    raise ItemsConfigError("\n".join(lines))


def _build_item(raw: dict) -> Item:
    derivation = (
        Derivation.from_dict(raw["derivation"]) if "derivation" in raw else None
    )
    xbrl_tags = tuple(raw["xbrl_tags"]) if "xbrl_tags" in raw else None
    return Item(
        key=raw["key"],
        display_name=raw["display_name"],
        statement=raw["statement"],
        flow_or_stock=raw["flow_or_stock"],
        unit=raw["unit"],
        xbrl_tags=xbrl_tags,
        derivation=derivation,
    )


def load_items(path: str | Path) -> ItemsConfig:
    """Load and validate `items.yaml`.

    Args:
        path: Path to the YAML file.

    Returns:
        ItemsConfig with all items as frozen dataclasses.

    Raises:
        ItemsConfigError: if the file does not parse or fails schema validation.
        FileNotFoundError: if the file does not exist.
    """
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"items.yaml not found at {path}")

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise ItemsConfigError(f"items.yaml is not valid YAML: {e}") from e

    if not isinstance(raw, dict):
        raise ItemsConfigError(
            f"items.yaml must be a mapping at the top level, got {type(raw).__name__}"
        )

    _validate(raw)

    items = tuple(_build_item(d) for d in raw["items"])

    # Duplicate-key check (not expressible cleanly in JSON Schema).
    seen: set[str] = set()
    for item in items:
        if item.key in seen:
            raise ItemsConfigError(f"Duplicate item key: {item.key!r}")
        seen.add(item.key)

    return ItemsConfig(items=items)
