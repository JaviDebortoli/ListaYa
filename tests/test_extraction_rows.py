from decimal import Decimal

import pytest

from listaya.extraction.rows import ExtractedRow, ListFormat, parse_amount, validate_rows

D = Decimal


class TestParseAmount:
    @pytest.mark.parametrize(
        "text, expected",
        [
            ("1.234,56", "1234.56"),
            ("$ 1.234,56", "1234.56"),
            ("$1.234", "1234"),
            ("12.345.678", "12345678"),
            ("85,5", "85.5"),
            ("1234,50", "1234.50"),
            ("1234", "1234"),
            ("12.5", "12.5"),
            ("1,234.56", "1234.56"),
            ("  $  970  ", "970"),
            ("ARS 1.500,00", "1500.00"),
        ],
    )
    def test_argentine_and_plain_formats(self, text, expected):
        assert parse_amount(text) == D(expected)

    @pytest.mark.parametrize("text", ["", "abc", "1,2,3", "1.234,56,7", "--5", "NaN", "consultar", "1e3"])
    def test_unparseable_returns_none(self, text):
        assert parse_amount(text) is None


def raw(**overrides):
    row = {
        "source_row": 3,
        "supplier_code": "TOR-61",
        "description": "Tornillo 6x1",
        "pack_size": 100,
        "unit": "caja",
        "unit_cost_text": "1.234,56",
        "currency": "ARS",
        "includes_vat": False,
        "confidence": 0.95,
    }
    row.update(overrides)
    return row


class TestValidateRows:
    def test_valid_row_matches_the_contract(self):
        [row] = validate_rows([raw()])
        assert row == ExtractedRow(
            supplier_code="TOR-61",
            description="Tornillo 6x1",
            pack_size=D("100"),
            unit="caja",
            unit_cost=D("1234.56"),
            currency="ARS",
            includes_vat=False,
            source_row=3,
            confidence=0.95,
            raw_cost="1.234,56",
            issues=(),
        )

    def test_missing_cost_is_marked_never_guessed(self):
        [row] = validate_rows([raw(unit_cost_text=None)])
        assert row.unit_cost is None
        assert "missing_cost" in row.issues

    def test_unparseable_cost_is_marked(self):
        [row] = validate_rows([raw(unit_cost_text="consultar")])
        assert row.unit_cost is None
        assert row.raw_cost == "consultar"
        assert "unparseable_cost" in row.issues

    def test_non_positive_cost_is_marked(self):
        [row] = validate_rows([raw(unit_cost_text="0")])
        assert "non_positive_cost" in row.issues

    def test_missing_description_is_marked(self):
        [row] = validate_rows([raw(description="  ")])
        assert "missing_description" in row.issues

    @pytest.mark.parametrize("currency, expected", [("usd", "USD"), (" ARS ", "ARS"), ("EUR", None), (None, None)])
    def test_currency_is_normalized_and_unknown_values_dropped(self, currency, expected):
        [row] = validate_rows([raw(currency=currency)])
        assert row.currency == expected

    @pytest.mark.parametrize("value", [True, False, None])
    def test_includes_vat_keeps_true_false_or_unknown(self, value):
        [row] = validate_rows([raw(includes_vat=value)])
        assert row.includes_vat is value

    def test_non_boolean_includes_vat_becomes_unknown(self):
        [row] = validate_rows([raw(includes_vat="si")])
        assert row.includes_vat is None

    @pytest.mark.parametrize("confidence, expected", [(1.5, 0.0), (-1, 0.0), ("x", 0.0), (None, 0.0), (0.4, 0.4)])
    def test_out_of_range_confidence_counts_as_zero(self, confidence, expected):
        [row] = validate_rows([raw(confidence=confidence)])
        assert row.confidence == expected

    @pytest.mark.parametrize("pack_size", [0, -2, "seis", float("nan")])
    def test_invalid_pack_size_becomes_unknown_and_marked(self, pack_size):
        [row] = validate_rows([raw(pack_size=pack_size)])
        assert row.pack_size is None
        assert "invalid_pack_size" in row.issues

    def test_numeric_cost_from_the_model_is_accepted_as_text(self):
        [row] = validate_rows([raw(unit_cost_text=1500)])
        assert row.unit_cost == D("1500")

    def test_skips_entries_that_are_not_objects(self):
        assert validate_rows(["text", 5, None]) == []

    def test_missing_source_row_uses_position(self):
        rows = validate_rows([raw(source_row=None), raw(source_row="x")])
        assert [r.source_row for r in rows] == [1, 2]

    @pytest.mark.parametrize("value, expected", [(1.234, "1.234"), (Decimal("1234.5"), "1234.5"), (85, "85")])
    def test_numeric_cell_values_are_taken_as_numbers_not_reparsed_as_text(self, value, expected):
        # A spreadsheet number 1.234 is one peso and change, not "1.234" written with a thousands dot.
        [row] = validate_rows([raw(unit_cost_text=value)])
        assert row.unit_cost == D(expected)


US = ListFormat(decimal_separator=".", local_currency="USD")


class TestUsFormat:
    @pytest.mark.parametrize(
        "text, expected",
        [
            ("$1,250.00", "1250.00"),
            ("1,250", "1250"),
            ("5.18", "5.18"),
            ("12,345,678", "12345678"),
            ("US$ 790.5", "790.5"),
            ("1.234,56", "1234.56"),
        ],
    )
    def test_dot_decimal_lists(self, text, expected):
        assert parse_amount(text, decimal_separator=".") == D(expected)

    def test_validate_rows_uses_the_list_format(self):
        [row] = validate_rows([raw(unit_cost_text="$1,250.00", currency=None)], US)
        assert row.unit_cost == D("1250.00")

    def test_rejects_unknown_decimal_separator(self):
        with pytest.raises(ValueError):
            ListFormat(decimal_separator=";", local_currency="USD")


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Precios + IVA", False),
        ("Precios más IVA", False),
        ("Precio s/IVA", False),
        ("Precios sin IVA", False),
        ("Precios con IVA incluido", True),
        ("Precio c/IVA", True),
        ("IVA incluido", True),
        ("Lista de precios", None),
        ("Precios con IVA incluido salvo herramientas + IVA", None),
    ],
)
def test_vat_statement(text, expected):
    from listaya.extraction.rows import vat_statement

    assert vat_statement(text) is expected


@pytest.mark.parametrize("text", ["Pintura masiva antióxido", "Kit sin ivas", "Comisión", "conivacion"])
def test_vat_statement_needs_whole_words(text):
    from listaya.extraction.rows import vat_statement

    assert vat_statement(text) is None


@pytest.mark.parametrize(
    "text, expected",
    [
        ("LISTA OCTUBRE\nPrecios + IVA\nTornillo $10", False),
        ("Precios finales\nTornillo $10\nFlete + IVA", None),  # a footnote about freight says nothing about prices
        ("Lista de precios con IVA incluido\nEnvío sin IVA\nTornillo $10", True),
        ("Tornillo masiva $10\nTarugo $5", None),
    ],
)
def test_list_vat_statement_only_reads_lines_about_prices(text, expected):
    from listaya.extraction.rows import list_vat_statement

    assert list_vat_statement(text) is expected


@pytest.mark.parametrize(
    "text, expected",
    [
        ("IVA incluido\nTornillo $10", True),
        ("Valores expresados + IVA\nTornillo $10", False),
        ("LISTA DE PRECIOS\n+ IVA\nTornillo $10", False),
        ("Precios finales\nTornillo $10\nEnvío sin IVA", None),
        ("Tornillo autoperforante caja x 100 unidades + IVA bonificado $10", None),  # long product line
    ],
)
def test_list_vat_statement_also_reads_short_standalone_lines(text, expected):
    from listaya.extraction.rows import list_vat_statement

    assert list_vat_statement(text) is expected
