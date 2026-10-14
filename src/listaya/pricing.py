"""Deterministic pricing engine.

The language model never computes prices or margins: everything here is plain
arithmetic on Decimal values so results match a manual calculation exactly.

Conventions (configurable, pending validation with the demo merchant):
- Margin is measured over the selling price: (price - cost) / price.
- Suggested price = unit cost / (1 - target margin), rounded to a step.
- Prices follow one VAT convention (default: without VAT); supplier costs are
  converted to it before pricing, and a cost with unknown VAT is never priced.
"""

from __future__ import annotations

import functools
from dataclasses import dataclass, field
from decimal import ROUND_CEILING, ROUND_HALF_EVEN, ROUND_HALF_UP, Context, Decimal, localcontext
from typing import Callable, Mapping, ParamSpec, TypeVar

PRICED_CURRENCY = "ARS"

# Flags that leave a product unpriced; the rest are informational.
BLOCKING_FLAGS = frozenset({"invalid_new_cost", "unknown_vat", "unknown_currency", "foreign_currency"})

# Fixed arithmetic context so results never depend on the caller's decimal settings.
_DECIMAL_CONTEXT = Context(prec=28, rounding=ROUND_HALF_EVEN)

_P = ParamSpec("_P")
_R = TypeVar("_R")


def _deterministic(func: Callable[_P, _R]) -> Callable[_P, _R]:
    @functools.wraps(func)
    def wrapper(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        with localcontext(_DECIMAL_CONTEXT):
            return func(*args, **kwargs)

    return wrapper


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

    def __post_init__(self) -> None:
        if self.rounding_step <= 0:
            raise ValueError("rounding_step must be positive")
        if self.vat_rate < 0:
            raise ValueError("vat_rate cannot be negative")


@dataclass(frozen=True)
class PriceQuote:
    """Pricing result for one product.

    new_cost is in the merchant's VAT convention whenever the supplier stated
    whether it includes VAT; with unknown VAT it is the raw supplier cost. The current_price_* fields
    describe the price the merchant charges today measured against the new
    cost; they are what triggers the margin alert. The suggested price never
    falls below the minimum margin by construction.
    """

    new_cost: Decimal
    current_price: Decimal | None
    suggested_price: Decimal | None
    variation: Decimal | None
    cost_change: Decimal | None
    current_price_margin: Decimal | None
    current_price_below_min_margin: bool | None
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


@_deterministic
def margin(price: Decimal, cost: Decimal) -> Decimal:
    return (price - cost) / price


@_deterministic
def round_price(value: Decimal, step: Decimal) -> Decimal:
    """Round to the nearest multiple of step (half up)."""
    if step <= 0:
        raise ValueError("rounding step must be positive")
    return (value / step).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * step


@_deterministic
def _round_up(value: Decimal, step: Decimal) -> Decimal:
    return (value / step).quantize(Decimal("1"), rounding=ROUND_CEILING) * step


@_deterministic
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


@_deterministic
def quote(
    new_cost: Decimal,
    cost_includes_vat: bool | None,
    current_price: Decimal | None,
    current_cost: Decimal | None,
    currency: str | None,
    rule: MarginRule,
    config: PricingConfig,
) -> PriceQuote:
    """Price one catalog product given the supplier's new unit cost.

    current_price and current_cost come from the catalog and are already in the
    merchant's VAT convention. Anything the engine cannot price without
    assuming (invalid cost, unknown VAT, other or unknown currency) is returned
    unpriced, with every blocking problem flagged so the summary can report it.
    """
    flags: set[str] = set()
    has_current_price = current_price is not None and current_price > 0
    if current_price is None:
        flags.add("no_current_price")
    elif not has_current_price:
        flags.add("invalid_current_price")
    if current_cost is None:
        flags.add("no_previous_cost")
    elif current_cost <= 0:
        flags.add("invalid_previous_cost")

    cost = new_cost
    try:
        cost = convert_vat(new_cost, cost_includes_vat, config.prices_include_vat, config.vat_rate)
    except UnknownVatError:
        flags.add("unknown_vat")

    if new_cost <= 0:
        flags.add("invalid_new_cost")

    normalized_currency = (currency or "").strip().upper()
    if not normalized_currency:
        flags.add("unknown_currency")
    elif normalized_currency != PRICED_CURRENCY:
        flags.add("foreign_currency")

    if flags & BLOCKING_FLAGS:
        return PriceQuote(
            new_cost=cost,
            current_price=current_price,
            suggested_price=None,
            variation=None,
            cost_change=None,
            current_price_margin=None,
            current_price_below_min_margin=None,
            flags=frozenset(flags),
        )

    cost_change = None
    if current_cost is not None and current_cost > 0:
        cost_change = (cost - current_cost) / current_cost

    raw_price = cost / (1 - rule.target_margin)
    suggested = round_price(raw_price, config.rounding_step)
    if suggested <= 0 or margin(suggested, cost) < rule.min_margin:
        # Rounding to the nearest step must never eat into the minimum margin.
        suggested = _round_up(raw_price, config.rounding_step)

    variation = current_price_margin = below_min = None
    if has_current_price:
        variation = (suggested - current_price) / current_price
        current_price_margin = margin(current_price, cost)
        below_min = current_price_margin < rule.min_margin

    return PriceQuote(
        new_cost=cost,
        current_price=current_price,
        suggested_price=suggested,
        variation=variation,
        cost_change=cost_change,
        current_price_margin=current_price_margin,
        current_price_below_min_margin=below_min,
        flags=frozenset(flags),
    )
