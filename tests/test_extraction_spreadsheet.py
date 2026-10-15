import io
from decimal import Decimal

import openpyxl
import pytest

from listaya.extraction.rows import ListFormat
from listaya.extraction.spreadsheet import SpreadsheetError, read_table, rows_from_table, table_as_text

D = Decimal


def xlsx_bytes(*sheets: list[list]) -> bytes:
    workbook = openpyxl.Workbook()
    workbook.remove(workbook.active)
    for index, rows in enumerate(sheets):
        sheet = workbook.create_sheet(f"Hoja{index + 1}")
        for row in rows:
            sheet.append(row)
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


class TestReadTable:
    def test_reads_semicolon_csv_in_windows_1252(self):
        content = "Código;Descripción;Precio\nA1;Caño ½;1.234,56\n".encode("cp1252")
        table, notes = read_table(content, "csv")
        assert table == [["Código", "Descripción", "Precio"], ["A1", "Caño ½", "1.234,56"]]
        assert notes == []

    def test_reads_comma_csv_in_utf8(self):
        table, _ = read_table("cod,desc,precio\nA1,Tornillo,85\n".encode("utf-8-sig"), "csv")
        assert table[1] == ["A1", "Tornillo", "85"]

    def test_reads_first_xlsx_sheet_with_data_and_notes_the_rest(self):
        content = xlsx_bytes([], [["Codigo", "Producto", "Precio"], ["A1", "Tornillo", 85.5]], [["otra"]])
        table, notes = read_table(content, "xlsx")
        assert table == [["Codigo", "Producto", "Precio"], ["A1", "Tornillo", 85.5]]
        assert notes == ["only sheet 'Hoja2' was read; ignored: Hoja3"]

    def test_unreadable_xlsx_raises(self):
        with pytest.raises(SpreadsheetError):
            read_table(b"not a zip", "xlsx")


class TestRowsFromTable:
    def test_maps_columns_from_a_header_found_below_title_rows(self):
        table = [
            ["FERRETERIA EL TORNILLO - LISTA OCTUBRE"],
            [],
            ["Cód.", "Artículo", "Presentación", "Unidad", "Precio Lista + IVA"],
            ["TOR-61", "Tornillo 6x1", "100", "caja", "1.234,56"],
            [None, None, None, None, None],
            ["TAR-8", "Tarugo 8mm", None, "unidad", 40],
        ]
        rows = rows_from_table(table)
        assert [r.source_row for r in rows] == [4, 6]
        first, second = rows
        assert first.supplier_code == "TOR-61"
        assert first.description == "Tornillo 6x1"
        assert first.pack_size == D("100")
        assert first.unit == "caja"
        assert first.unit_cost == D("1234.56")
        assert first.includes_vat is False
        assert first.currency is None
        assert first.confidence == 1.0
        assert second.unit_cost == D("40")

    @pytest.mark.parametrize(
        "header, includes_vat, currency",
        [
            ("Precio c/IVA", True, None),
            ("Precio IVA incluido", True, None),
            ("Precio s/IVA", False, None),
            ("Precio sin IVA", False, None),
            ("Precio U$S", None, "USD"),
            ("Precio $", None, "ARS"),
            ("Precio", None, None),
        ],
    )
    def test_reads_vat_and_currency_from_the_price_header(self, header, includes_vat, currency):
        [row] = rows_from_table([["Descripcion", header], ["Tornillo", "10"]])
        assert row.includes_vat is includes_vat
        assert row.currency == currency

    def test_returns_none_when_no_header_is_recognized(self):
        assert rows_from_table([["foo", "bar"], ["Tornillo", "10"]]) is None

    def test_skips_section_title_rows_without_price(self):
        rows = rows_from_table([["Descripcion", "Precio"], ["TORNILLERIA", None], ["Tornillo", "10"]])
        assert [r.description for r in rows] == ["Tornillo"]


def test_table_as_text_joins_non_empty_cells():
    # Real numbers use a decimal comma so the Argentine parser reads 1.234 as one peso, not 1234.
    assert table_as_text([["a", None, 1.5], [], [None, "b", 1.234, 1234]]) == "a | 1,5\n\nb | 1,234 | 1234"


class TestEnglishLists:
    US = ListFormat(decimal_separator=".", local_currency="USD")

    def test_english_header_and_dollar_sign_mean_the_merchant_currency(self):
        table = [
            ["SOUTHERN FASTENERS - PRICE LIST"],
            ["Item code", "Description", "Pack", "Unit price $ (excl. VAT)"],
            ["SF-6100", "Self-drilling screw 6x1", "100", "$1,250.00"],
        ]
        [row] = rows_from_table(table, self.US)
        assert (row.supplier_code, row.description, row.pack_size) == ("SF-6100", "Self-drilling screw 6x1", D("100"))
        assert row.unit_cost == D("1250.00")
        assert row.currency == "USD"
        assert row.includes_vat is False

    @pytest.mark.parametrize(
        "header, includes_vat",
        [("Price incl. VAT", True), ("Price VAT included", True), ("Price ex VAT", False), ("Net price", False)],
    )
    def test_english_vat_wording(self, header, includes_vat):
        [row] = rows_from_table([["Product", header], ["Hammer", "10.50"]], self.US)
        assert row.includes_vat is includes_vat

    def test_table_as_text_uses_the_list_decimal_separator(self):
        assert table_as_text([["Hammer", 10.5]], self.US) == "Hammer | 10.5"
