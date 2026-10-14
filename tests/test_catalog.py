import os
import stat
from decimal import Decimal
from pathlib import Path

import pytest

from listaya.catalog import CatalogError, CsvCatalog, PriceUpdate

D = Decimal

HEADER = "sku,name,unit,pack_size,cost,price,category,notes\n"


def write_catalog(path: Path, body: str, header: str = HEADER, encoding: str = "utf-8") -> Path:
    path.write_text(header + body, encoding=encoding)
    return path


@pytest.fixture
def catalog_path(tmp_path: Path) -> Path:
    return write_catalog(
        tmp_path / "catalog.csv",
        "2231,Tornillo 6x1,unidad,1,85,120,tornillos,keep me\n"
        "2232,Tarugo 8mm,unidad,,40,60,tarugos,\n"
        "3001,Producto nuevo,,,,,,\n",
    )


class TestLoad:
    def test_loads_products_with_decimal_amounts(self, catalog_path: Path):
        products = CsvCatalog(catalog_path).load()
        assert [p.sku for p in products] == ["2231", "2232", "3001"]
        first = products[0]
        assert first.name == "Tornillo 6x1"
        assert first.unit == "unidad"
        assert first.pack_size == D("1")
        assert first.cost == D("85")
        assert first.price == D("120")
        assert first.category == "tornillos"

    def test_empty_cells_become_none(self, catalog_path: Path):
        product = CsvCatalog(catalog_path).load()[2]
        assert product.unit is None
        assert product.pack_size is None
        assert product.cost is None
        assert product.price is None
        assert product.category is None

    def test_accepts_excel_utf8_bom(self, tmp_path: Path):
        path = write_catalog(tmp_path / "bom.csv", "1,Martillo,,,10,20,,\n", encoding="utf-8-sig")
        assert CsvCatalog(path).load()[0].sku == "1"

    def test_optional_columns_may_be_missing(self, tmp_path: Path):
        path = write_catalog(tmp_path / "min.csv", "1,Martillo,10,20\n", header="sku,name,cost,price\n")
        product = CsvCatalog(path).load()[0]
        assert product.cost == D("10")
        assert product.category is None

    def test_rejects_missing_required_column(self, tmp_path: Path):
        path = write_catalog(tmp_path / "bad.csv", "1,Martillo,10\n", header="sku,name,cost\n")
        with pytest.raises(CatalogError, match="price"):
            CsvCatalog(path).load()

    @pytest.mark.parametrize("value", ["abc", "NaN", "Infinity", "1.234,56"])
    def test_rejects_invalid_amount_with_line_number(self, tmp_path: Path, value: str):
        path = write_catalog(tmp_path / "bad.csv", f'1,Martillo,,,"{value}",20,,\n')
        with pytest.raises(CatalogError, match="line 2"):
            CsvCatalog(path).load()

    def test_rejects_duplicate_sku(self, tmp_path: Path):
        path = write_catalog(tmp_path / "dup.csv", "1,Martillo,,,10,20,,\n1,Otro,,,10,20,,\n")
        with pytest.raises(CatalogError, match="duplicate"):
            CsvCatalog(path).load()

    def test_rejects_empty_sku(self, tmp_path: Path):
        path = write_catalog(tmp_path / "nosku.csv", " ,Martillo,,,10,20,,\n")
        with pytest.raises(CatalogError, match="sku"):
            CsvCatalog(path).load()


class TestApplyUpdates:
    def test_updates_cost_and_price_and_keeps_everything_else(self, catalog_path: Path):
        catalog = CsvCatalog(catalog_path)
        catalog.apply_updates([PriceUpdate(sku="2231", cost=D("100"), price=D("1430"))])

        products = {p.sku: p for p in catalog.load()}
        assert products["2231"].cost == D("100")
        assert products["2231"].price == D("1430")
        assert products["2232"].cost == D("40")
        text = catalog_path.read_text(encoding="utf-8")
        assert text.splitlines()[0] == HEADER.strip()
        assert "keep me" in text

    def test_unknown_sku_changes_nothing(self, catalog_path: Path):
        before = catalog_path.read_bytes()
        with pytest.raises(CatalogError, match="9999"):
            CsvCatalog(catalog_path).apply_updates(
                [
                    PriceUpdate(sku="2231", cost=D("100"), price=D("1430")),
                    PriceUpdate(sku="9999", cost=D("1"), price=D("2")),
                ]
            )
        assert catalog_path.read_bytes() == before

    def test_rejects_non_positive_values_without_writing(self, catalog_path: Path):
        before = catalog_path.read_bytes()
        with pytest.raises(CatalogError):
            CsvCatalog(catalog_path).apply_updates([PriceUpdate(sku="2231", cost=D("100"), price=D("0"))])
        assert catalog_path.read_bytes() == before

    def test_leaves_no_temporary_files(self, catalog_path: Path):
        CsvCatalog(catalog_path).apply_updates([PriceUpdate(sku="2232", cost=D("50"), price=D("80"))])
        assert sorted(p.name for p in catalog_path.parent.iterdir()) == ["catalog.csv"]


class TestRobustness:
    def test_rejects_row_with_more_cells_than_header(self, tmp_path: Path):
        path = write_catalog(tmp_path / "extra.csv", "1,Martillo,10,20,,\n", header="sku,name,cost,price\n")
        with pytest.raises(CatalogError, match="line 2"):
            CsvCatalog(path).load()

    @pytest.mark.parametrize("bad", [D("Infinity"), D("NaN"), D("-Infinity")])
    def test_rejects_non_finite_update_without_writing(self, catalog_path: Path, bad: Decimal):
        before = catalog_path.read_bytes()
        with pytest.raises(CatalogError):
            CsvCatalog(catalog_path).apply_updates([PriceUpdate(sku="2231", cost=D("100"), price=bad)])
        assert catalog_path.read_bytes() == before

    def test_rejects_duplicate_sku_in_updates_without_writing(self, catalog_path: Path):
        before = catalog_path.read_bytes()
        with pytest.raises(CatalogError, match="2231"):
            CsvCatalog(catalog_path).apply_updates(
                [
                    PriceUpdate(sku="2231", cost=D("100"), price=D("1430")),
                    PriceUpdate(sku="2231", cost=D("101"), price=D("1440")),
                ]
            )
        assert catalog_path.read_bytes() == before

    def test_preserves_bom_and_crlf_line_endings(self, tmp_path: Path):
        path = tmp_path / "excel.csv"
        path.write_bytes(b"\xef\xbb\xbfsku,name,cost,price\r\n1,Martillo,10,20\r\n")
        CsvCatalog(path).apply_updates([PriceUpdate(sku="1", cost=D("12"), price=D("30"))])
        assert path.read_bytes() == b"\xef\xbb\xbfsku,name,cost,price\r\n1,Martillo,12,30\r\n"

    def test_writes_long_decimals_in_plain_notation(self, catalog_path: Path):
        cost = D("826.4462809917355371900826446")
        catalog = CsvCatalog(catalog_path)
        catalog.apply_updates([PriceUpdate(sku="2231", cost=cost, price=D("1.18E+3"))])
        text = catalog_path.read_text(encoding="utf-8")
        assert "826.4462809917355371900826446,1180," in text
        assert {p.sku: p for p in catalog.load()}["2231"].cost == cost

    @pytest.mark.skipif(os.name == "nt", reason="POSIX file permissions")
    def test_preserves_file_permissions(self, catalog_path: Path):
        catalog_path.chmod(0o644)
        CsvCatalog(catalog_path).apply_updates([PriceUpdate(sku="2231", cost=D("100"), price=D("1430"))])
        assert stat.S_IMODE(catalog_path.stat().st_mode) == 0o644

    def test_reads_and_keeps_windows_1252_catalog(self, tmp_path: Path):
        path = tmp_path / "latin.csv"
        path.write_bytes("sku,name,cost,price\n1,Caño PVC ½,10,20\n".encode("cp1252"))
        catalog = CsvCatalog(path)
        assert catalog.load()[0].name == "Caño PVC ½"
        catalog.apply_updates([PriceUpdate(sku="1", cost=D("12"), price=D("30"))])
        assert path.read_bytes() == "sku,name,cost,price\n1,Caño PVC ½,12,30\n".encode("cp1252")

    def test_undecodable_catalog_raises_catalog_error(self, tmp_path: Path):
        path = tmp_path / "binary.csv"
        path.write_bytes(b"sku,name,cost,price\n1,\x81\x8d,10,20\n")
        with pytest.raises(CatalogError, match="encoding"):
            CsvCatalog(path).load()

    def test_keeps_header_spacing_on_update(self, tmp_path: Path):
        path = write_catalog(tmp_path / "spaced.csv", "1, Martillo, 10, 20\n", header="sku, name, cost, price\n")
        CsvCatalog(path).apply_updates([PriceUpdate(sku="1", cost=D("12"), price=D("30"))])
        assert path.read_text(encoding="utf-8").splitlines() == ["sku, name, cost, price", "1, Martillo,12,30"]
