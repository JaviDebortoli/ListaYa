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
    # The price header ("Precio s/IVA") states VAT but no currency symbol, so currency stays unknown.
    assert all(row.currency is None and row.includes_vat is False for row in rows)
    first = rows[0]
    assert (first.supplier_code, first.description, first.unit_cost) == (
        "BS-6100",
        "TORNILLO AUTOPERFORANTE 6X1",
        Decimal("5180.00"),
    )


def test_sample_pdfs_have_the_expected_text_layers():
    import pdfplumber

    with pdfplumber.open(SAMPLES / "lista_pintureria_oeste.pdf") as pdf:
        text = pdf.pages[0].extract_text()
    assert "Látex interior blanco 4 L" in text and "Precios + IVA" in text
    with pdfplumber.open(SAMPLES / "escaneo_corralon_san_martin.pdf") as pdf:
        assert (pdf.pages[0].extract_text() or "").strip() == ""  # image only: goes to the vision model
    assert (SAMPLES / "foto_corralon_san_martin.jpg").stat().st_size > 10_000
