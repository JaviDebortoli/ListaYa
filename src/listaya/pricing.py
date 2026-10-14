"""Deterministic pricing engine.

The language model never computes prices or margins: everything here is plain
arithmetic on Decimal values so results match a manual calculation exactly.

Conventions (configurable, pending validation with the demo merchant):
- Margin is measured over the selling price: (price - cost) / price.
- Suggested price = unit cost / (1 - target margin), rounded to a step.
- Costs and prices share one VAT convention (default: without VAT).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal
from typing import Mapping

PRICED_CURRENCY = "ARS"


class UnknownVatError(ValueError):
    """Raised when a cost does not say whether it includes VAT."""


@dataclass(frozen=True)
class MarginRule:
    target_margin: Decimal
    min_margin: Decimal

    def __post_init__(self) -> None:
        if not Decimal("0") <= self.target_margin < Decimal("1"):
            raise ValueError("target_margin must be in [0, 1)")
        if not Decimal("0") <= self.min_margin < Decimal("1"):
            raise ValueError("min_margin must be in [0, 1)")
        if self.min_margin > self.target_margin:
            raise ValueError("min_margin cannot exceed target_margin")


@dataclass(frozen=True)
class PricingConfig:
    rounding_step: Decimal = Decimal("10")
    vat_rate: Decimal = Decimal("0.21")
    prices_include_vat: bool = False


@dataclass(frozen=True)
class PriceQuote:
    new_cost: Decimal
    current_price: Decimal | None
    suggested_price: Decimal | None
    variation: Decimal | None
    cost_change: Decimal | None
    current_margin: Decimal | None
    below_min_margin: bool | None
    flags: frozenset[str] = field(default_factory=frozenset)

    @property
    def priced(self) -> bool:
        return self.suggested_price is not None


def resolve_rule(
    product_id: str,
    category: str | None,
    product_rules: Mapping[str, MarginRule],
    category_rules: Mapping[str, MarginRule],
    default: MarginRule,
) -> MarginRule:
    """Product rule beats category rule, which beats the merchant default."""
    if product_id in product_rules:
        return product_rules[product_id]
    if category is not None and category in category_rules:
        return category_rules[category]
    return default


def margin(price: Decimal, cost: Decimal) -> Decimal:
    return (price - cost) / price


def round_price(value: Decimal, step: Decimal) -> Decimal:
    """Round to the nearest multiple of step (half up)."""
    if step <= 0:
        raise ValueError("rounding step must be positive")
    return (value / step).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * step


def _round_up(value: Decimal, step: Decimal) -> Decimal:
    return (value / step).quantize(Decimal("1"), rounding=ROUND_CEILING) * step


def convert_vat(
    amount: Decimal,
    includes_vat: bool | None,
    target_includes_vat: bool,
    rate: Decimal,
) -> Decimal:
    """Move an amount to the target VAT convention. Unknown VAT is never assumed."""
    if includes_vat is None:
        raise UnknownVatError("cost does not state whether it includes VAT")
    if includes_vat == target_includes_vat:
        return amount
    if includes_vat:
        return amount / (1 + rate)
    return amount * (1 + rate)


def quote(
    new_cost: Decimal,
    current_price: Decimal | None,
    current_cost: Decimal | None,
    currency: str | None,
    rule: MarginRule,
    config: PricingConfig,
) -> PriceQuote:
    """Price one catalog product given its new unit cost (already in the merchant's VAT convention)."""
    if new_cost <= 0:
        raise ValueError("new_cost must be positive")

    flags: set[str] = set()
    if current_cost is None:
        flags.add("no_previous_cost")
    if current_price is None:
        flags.add("no_current_price")
    cost_change = (new_cost - current_cost) / current_cost if current_cost else None

    if currency != PRICED_CURRENCY:
        flags.add("unknown_currency" if currency is None else "foreign_currency")
        return PriceQuote(new_cost, current_price, None, None, cost_change, None, None, frozenset(flags))

    raw_price = new_cost / (1 - rule.target_margin)
    suggested = round_price(raw_price, config.rounding_step)
    if suggested <= 0 or margin(suggested, new_cost) < rule.min_margin:
        # Rounding to the nearest step must never eat into the minimum margin.
        suggested = _round_up(raw_price, config.rounding_step)

    variation = current_margin = below_min = None
    if current_price is not None and current_price > 0:
        variation = (suggested - current_price) / current_price
        current_margin = margin(current_price, new_cost)
        below_min = current_margin < rule.min_margin

    return PriceQuote(
        new_cost=new_cost,
        current_price=current_price,
        suggested_price=suggested,
        variation=variation,
        cost_change=cost_change,
        current_margin=current_margin,
        below_min_margin=below_min,
        flags=frozenset(flags),
    )
