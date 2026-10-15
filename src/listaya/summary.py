"""Pricing matched rows and writing the merchant-facing summary.

Every number in the summary comes from code (PRD 6.2), never from the model.
Merchant-facing text is in Argentine Spanish.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Mapping, Sequence

from listaya.catalog import CatalogProduct
from listaya.matching import MatchResult, MatchStatus
from listaya.pricing import MarginRule, PriceQuote, PricingConfig, quote

TOP_INCREASES = 3


@dataclass(frozen=True)
class RunItem:
    match: MatchResult
    product: CatalogProduct | None
    quote: PriceQuote | None
    flags: frozenset[str] = frozenset()
    rule: MarginRule | None = None

    @property
    def row(self):
        return self.match.row

    @property
    def priced(self) -> bool:
        return self.quote is not None and self.quote.priced


def price_items(
    results: Sequence[MatchResult],
    products: Mapping[str, CatalogProduct],
    rule: MarginRule,
    config: PricingConfig,
    *,
    supplier_includes_vat: bool | None,
    default_currency: str,
) -> list[RunItem]:
    """Quote every matched row against its catalog product.

    A row without its own VAT statement uses the supplier's remembered answer, and a
    row without a currency marker is taken in the merchant's currency.
    """
    items = []
    for result in results:
        if result.status != MatchStatus.MATCHED or result.sku not in products:
            items.append(RunItem(result, None, None))
            continue
        product = products[result.sku]
        row = result.row
        if row.unit_cost is None:
            items.append(RunItem(result, product, None, frozenset({"missing_cost"})))
            continue
        if row.pack_size is not None and product.pack_size is not None and row.pack_size != product.pack_size:
            # Different presentations are reported, not converted by assumption (PRD 11).
            items.append(RunItem(result, product, None, frozenset({"pack_mismatch"})))
            continue
        price_quote = quote(
            new_cost=row.unit_cost,
            cost_includes_vat=row.includes_vat if row.includes_vat is not None else supplier_includes_vat,
            current_price=product.price,
            current_cost=product.cost,
            currency=row.currency or default_currency,
            rule=rule,
            config=config,
        )
        items.append(RunItem(result, product, price_quote, price_quote.flags, rule))
    return items


@dataclass(frozen=True)
class RunSummary:
    supplier: str
    total: int
    increased: list[RunItem]
    alerts: list[RunItem]
    unmatched: list[RunItem]
    unpriced: list[RunItem]
    updatable: list[RunItem]
    problems: list[str]


def summarize(supplier: str, items: Sequence[RunItem], problems: Sequence[str]) -> RunSummary:
    priced = [item for item in items if item.priced]
    increased = sorted(
        (item for item in priced if item.quote.variation is not None and item.quote.variation > 0),
        key=lambda item: item.quote.variation,
        reverse=True,
    )
    return RunSummary(
        supplier=supplier,
        total=len(items),
        increased=increased,
        alerts=[item for item in priced if item.quote.current_price_below_min_margin],
        unmatched=[item for item in items if item.product is None],
        unpriced=[item for item in items if item.product is not None and not item.priced],
        updatable=priced,
        problems=list(problems),
    )


def format_money(value: Decimal) -> str:
    """Argentine format: $1.234 for whole pesos, $1.234,50 with cents."""
    cents = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    whole = cents == cents.to_integral_value()
    text = f"{cents:,.0f}" if whole else f"{cents:,.2f}"
    return "$" + text.replace(",", "_").replace(".", ",").replace("_", ".")


def format_percent(value: Decimal) -> str:
    percent = value * 100
    if abs(percent) < 1:
        text = f"{percent.quantize(Decimal('0.1'), rounding=ROUND_HALF_UP)}".replace(".", ",")
    else:
        text = f"{percent.quantize(Decimal('1'), rounding=ROUND_HALF_UP)}"
    return ("+" if value > 0 else "") + text + "%"


def _name(item: RunItem) -> str:
    return item.product.name if item.product else item.row.description


def _plural(count: int, singular: str, plural: str) -> str:
    return singular if count == 1 else plural


def summary_text(summary: RunSummary) -> str:
    lines = [f"Listo, procesé la lista de {summary.supplier} ({summary.total} productos).", ""]

    if summary.increased:
        lines.append(f"Subieron {len(summary.increased)}. Los que más subieron:")
        for position, item in enumerate(summary.increased[:TOP_INCREASES], start=1):
            q = item.quote
            lines.append(
                f"{position}. {_name(item)}: {format_percent(q.variation)} "
                f"(de {format_money(q.current_price)} a {format_money(q.suggested_price)})"
            )
        lines.append("")

    if summary.alerts:
        count = len(summary.alerts)
        lines.append(
            f"⚠ {count} {_plural(count, 'producto quedaría', 'productos quedarían')} "
            "por debajo de tu margen mínimo."
        )
    if summary.unmatched:
        lines.append(f"❓ {len(summary.unmatched)} no pude emparejar con tu catálogo.")
    if summary.unpriced:
        lines.append(
            f"ℹ {len(summary.unpriced)} sin precio para calcular (consultar, otra moneda o presentación distinta)."
        )
    if summary.problems:
        lines.append("⚠ No pude leer una parte de la lista: revisá que no falten productos.")
    if lines[-1] != "":
        lines.append("")

    if summary.updatable:
        count = len(summary.updatable)
        lines.append(
            f"Tocá Aprobar para actualizar {count} {_plural(count, 'precio', 'precios')} en tu catálogo, "
            'o respondé "ok".'
        )
    else:
        lines.append("No encontré precios para actualizar.")
    return "\n".join(lines)


def alerts_text(summary: RunSummary) -> str:
    sections = []
    if summary.alerts:
        lines = ["Por debajo del margen mínimo:"]
        for item in summary.alerts:
            q = item.quote
            minimum = format_percent(item.rule.min_margin).lstrip("+") if item.rule else "el configurado"
            lines.append(
                f"- {_name(item)}: hoy lo vendés a {format_money(q.current_price)} y con el costo nuevo "
                f"({format_money(q.new_cost)}) el margen queda en {format_percent(q.current_price_margin).lstrip('+')} "
                f"(tu mínimo es {minimum}). Precio sugerido: {format_money(q.suggested_price)}."
            )
        sections.append("\n".join(lines))
    if summary.unmatched:
        sections.append("Sin emparejar:\n" + "\n".join(f"- {item.row.description}" for item in summary.unmatched))
    if summary.unpriced:
        sections.append(
            "Sin precio para calcular:\n"
            + "\n".join(f"- {item.row.description}" + (f" ({item.row.raw_cost})" if item.row.raw_cost else "")
                        for item in summary.unpriced)
        )
    return "\n\n".join(sections) if sections else "No hay alertas en esta lista."
