"""The bundled sample data must stay readable by the real code paths."""

from decimal import Decimal
from pathlib import Path

from listaya.catalog import CsvCatalog
from listaya.extraction.spreadsheet import read_table, rows_from_table

SAMPLES = Path(__file__).resolve().parent.parent / "samples" / "ferreteria"


def test_sample_catalog_loads():
    products = CsvCatalog(SAMPLES / "catalogo.csv").load()
    assert len(products) == 25
    assert all(p.cost and p.price and p.price > p.cost for p in products)


def test_sample_spreadsheet_list_is_read_without_the_model():
    table, notes = read_table((SAMPLES / "lista_bulonera_sur.csv").read_bytes(), "csv")
    rows = rows_from_table(table)
    assert notes == []
    assert len(rows) == 11
    assert all(row.issues == () for row in rows)
    assert all(row.currency is None and row.includes_vat is False for row in rows)
    first = rows[0]
    assert (first.supplier_code, first.description, first.unit_cost) == (
        "BS-6100",
        "TORNILLO AUTOPERFORANTE 6X1",
        Decimal("5180.00"),
    )
