from decimal import Decimal

import pytest

from listaya.catalog import CatalogProduct
from listaya.extraction.rows import ExtractedRow
from listaya.matching import MatchResult, MatchStatus
from listaya.pricing import MarginRule, PricingConfig
from listaya.summary import (
    alerts_text,
    format_money,
    format_percent,
    price_items,
    summarize,
    summary_text,
)

D = Decimal
RULE = MarginRule(target_margin=D("0.30"), min_margin=D("0.20"))
CONFIG = PricingConfig()

PRODUCTS = {
    "1001": CatalogProduct("1001", "Tornillo autoperforante 6x1", "caja", D("100"), D("4200"), D("6000"), None),
    "1002": CatalogProduct("1002", "Tornillo autoperforante 6x2", "caja", D("100"), D("4900"), D("7000"), None),
    "1101": CatalogProduct("1101", "Tarugo nylon 6 mm", "bolsa", D("100"), D("1500"), D("2150"), None),
    "1301": CatalogProduct("1301", "Martillo carpintero", "unidad", None, D("9800"), D("14000"), None),
}


def row(description, cost, n, pack=None, vat=False, currency=None, raw=None):
    return ExtractedRow(None, description, pack, None, cost, currency, vat, n, 0.9, raw or str(cost))


def matched(r, sku):
    return MatchResult(r, MatchStatus.MATCHED, sku, None, "fuzzy", 0.95)


def unmatched(r):
    return MatchResult(r, MatchStatus.UNMATCHED, None, None, "none", 0.3)


def items(supplier_vat=None):
    results = [
        matched(row("TORNILLO 6X1", D("5180"), 1, pack=D("100")), "1001"),  # 6000 -> 7400, alert (13.7%)
        matched(row("TORNILLO 6X2", D("5390"), 2, pack=D("100")), "1002"),  # 7000 -> 7700, alert (23%? no)
        matched(row("TARUGO 6MM", D("1590"), 3, pack=D("100")), "1101"),  # 2150 -> 2270
        matched(row("MARTILLO", None, 4, raw="consultar"), "1301"),  # no cost
        unmatched(row("AMOLADORA DE BANCO", D("189000"), 5)),
    ]
    return price_items(results, PRODUCTS, RULE, CONFIG, supplier_includes_vat=supplier_vat, default_currency="ARS")


class TestFormatting:
    @pytest.mark.parametrize(
        "value, text",
        [(D("1200"), "$1.200"), (D("1416.5"), "$1.416,50"), (D("85"), "$85"), (D("1234567.891"), "$1.234.567,89")],
    )
    def test_money_uses_argentine_format(self, value, text):
        assert format_money(value) == text

    @pytest.mark.parametrize(
        "value, text", [(D("0.18"), "+18%"), (D("0.1234"), "+12%"), (D("-0.05"), "-5%"), (D("0.004"), "+0,4%")]
    )
    def test_percent(self, value, text):
        assert format_percent(value) == text


class TestPriceItems:
    def test_matched_rows_get_a_quote_with_the_catalog_prices(self):
        first = items()[0]
        assert first.product.sku == "1001"
        assert first.quote.suggested_price == D("7400")  # 5180 / 0.7
        assert first.quote.current_price_below_min_margin is True

    def test_row_without_cost_is_not_priced(self):
        martillo = items()[3]
        assert martillo.quote is None
        assert "missing_cost" in martillo.flags

    def test_unmatched_rows_have_no_product(self):
        assert items()[4].product is None and items()[4].quote is None

    def test_unknown_vat_uses_the_suppliers_remembered_answer(self):
        r = row("TORNILLO 6X1", D("6267.80"), 1, pack=D("100"), vat=None)
        [item] = price_items([matched(r, "1001")], PRODUCTS, RULE, CONFIG, supplier_includes_vat=True, default_currency="ARS")
        assert item.quote.new_cost == D("5180")  # 6267.80 / 1.21

    def test_list_without_currency_is_taken_in_the_merchants_currency(self):
        [item] = price_items(
            [matched(row("TARUGO", D("1590"), 1, pack=D("100")), "1101")], PRODUCTS, RULE, CONFIG,
            supplier_includes_vat=None, default_currency="ARS",
        )
        assert item.quote.priced

    def test_different_pack_sizes_are_not_priced(self):
        [item] = price_items(
            [matched(row("TARUGO X 50", D("800"), 1, pack=D("50")), "1101")], PRODUCTS, RULE, CONFIG,
            supplier_includes_vat=None, default_currency="ARS",
        )
        assert item.quote is None
        assert "pack_mismatch" in item.flags


class TestSummary:
    def test_counts(self):
        summary = summarize("Bulonera Sur", items(), problems=[])
        assert summary.total == 5
        assert [i.product.sku for i in summary.increased] == ["1001", "1002", "1101"]  # by variation, desc
        assert [i.product.sku for i in summary.alerts] == ["1001"]
        assert len(summary.unmatched) == 1
        assert len(summary.unpriced) == 1
        assert len(summary.updatable) == 3

    def test_summary_text_matches_the_prd_shape(self):
        text = summary_text(summarize("Bulonera Sur", items(), problems=[]))
        assert text.startswith("Listo, procesé la lista de Bulonera Sur (5 productos).")
        assert "Subieron 3. Los que más subieron:" in text
        assert "1. Tornillo autoperforante 6x1: +23% (de $6.000 a $7.400)" in text
        assert "⚠ 1 producto quedaría por debajo de tu margen mínimo." in text
        assert "❓ 1 no pude emparejar con tu catálogo." in text
        assert "ℹ 1 sin precio para calcular" in text
        assert 'Tocá Aprobar para actualizar 3 precios en tu catálogo, o respondé "ok".' in text

    def test_reading_problems_are_mentioned(self):
        text = summary_text(summarize("Bulonera Sur", items(), problems=["lines 1-60: the model did not return valid JSON"]))
        assert "No pude leer una parte de la lista" in text

    def test_nothing_to_update(self):
        summary = summarize("Bulonera Sur", [items()[4]], problems=[])
        assert "No encontré precios para actualizar." in summary_text(summary)

    def test_alerts_text_explains_each_alert_and_lists_the_rest(self):
        text = alerts_text(summarize("Bulonera Sur", items(), problems=[]))
        assert "Tornillo autoperforante 6x1: hoy lo vendés a $6.000 y con el costo nuevo ($5.180) el margen queda en 14% (tu mínimo es 20%). Precio sugerido: $7.400." in text
        assert "Sin emparejar:\n- AMOLADORA DE BANCO" in text
        assert "Sin precio para calcular:\n- MARTILLO (consultar)" in text


def test_unmatched_row_without_price_counts_as_unpriced():
    no_price = unmatched(row("PINTURA LATEX 4L", None, 9, raw="consultar"))
    summary = summarize("Norte", price_items([no_price], PRODUCTS, RULE, CONFIG, supplier_includes_vat=None, default_currency="ARS"), problems=[])
    assert summary.unmatched == []
    assert len(summary.unpriced) == 1
    assert "Sin precio para calcular:\n- PINTURA LATEX 4L (consultar)" in alerts_text(summary)
