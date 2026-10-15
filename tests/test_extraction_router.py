import asyncio
from decimal import Decimal

import pytest

from listaya.extraction.model import ExtractionResult
from listaya.extraction.router import MAX_PDF_PAGES, ListExtractor, detect_kind
from listaya.extraction.rows import ExtractedRow


def pdf_bytes(*page_texts: str) -> bytes:
    """Build a minimal PDF with one page per text (an empty text gives a page with no text layer)."""
    objects = ["<< /Type /Catalog /Pages 2 0 R >>", ""]
    page_ids = []
    font_id = 3
    objects.append("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    for text in page_texts:
        stream = f"BT /F1 12 Tf 50 750 Td ({text}) Tj ET" if text else ""
        objects.append(f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream")
        content_id = len(objects)
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 {font_id} 0 R >> >> /Contents {content_id} 0 R >>"
        )
        page_ids.append(len(objects))
    kids = " ".join(f"{i} 0 R" for i in page_ids)
    objects[1] = f"<< /Type /Pages /Kids [{kids}] /Count {len(page_ids)} >>"

    out = b"%PDF-1.4\n"
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n{body}\nendobj\n".encode("latin-1")
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += "".join(f"{offset:010d} 00000 n \n" for offset in offsets).encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return out


def a_row(n: int) -> ExtractedRow:
    return ExtractedRow(None, f"item {n}", None, None, Decimal("10"), "ARS", None, n, 0.9, "10")


class FakeModelExtractor:
    def __init__(self) -> None:
        self.texts: list[str] = []
        self.images: list[list[tuple[bytes, str]]] = []

    async def extract_text(self, text: str) -> ExtractionResult:
        self.texts.append(text)
        return ExtractionResult([a_row(1)])

    async def extract_images(self, images) -> ExtractionResult:
        self.images.append(list(images))
        return ExtractionResult([a_row(n) for n in range(1, len(images) + 1)])


def run(coro):
    return asyncio.run(coro)


@pytest.mark.parametrize(
    "file_name, mime, kind",
    [
        ("lista.csv", None, "csv"),
        ("LISTA.XLSX", None, "xlsx"),
        ("lista.xls", None, "xls"),
        ("lista.pdf", None, "pdf"),
        (None, "application/pdf", "pdf"),
        ("foto.jpg", None, "image"),
        (None, "image/jpeg", "image"),
        ("lista.txt", None, "text"),
        ("raro.zip", "application/zip", None),
    ],
)
def test_detect_kind(file_name, mime, kind):
    assert detect_kind(file_name, mime) == kind


class TestRoutes:
    def test_csv_with_header_is_parsed_without_the_model(self):
        model = FakeModelExtractor()
        content = "Codigo;Descripcion;Precio\nA1;Tornillo;1.234,56\n".encode()
        result = run(ListExtractor(model).extract_file(content, "lista.csv", None))
        assert [(r.description, r.unit_cost) for r in result.rows] == [("Tornillo", Decimal("1234.56"))]
        assert model.texts == [] and model.images == []

    def test_csv_without_recognized_header_goes_to_the_text_model(self):
        model = FakeModelExtractor()
        result = run(ListExtractor(model).extract_file(b"Tornillo;85\nTarugo;40\n", "lista.csv", None))
        assert model.texts == ["Tornillo | 85\nTarugo | 40"]
        assert len(result.rows) == 1

    def test_old_xls_is_rejected_with_advice(self):
        result = run(ListExtractor(FakeModelExtractor()).extract_file(b"x", "lista.xls", None))
        assert result.rows == []
        assert result.problems == ["old .xls files are not supported: save the list as .xlsx or .csv"]

    def test_unreadable_spreadsheet_is_reported(self):
        result = run(ListExtractor(FakeModelExtractor()).extract_file(b"not a zip", "lista.xlsx", None))
        assert result.rows == []
        assert result.problems[0].startswith("the Excel file could not be read")

    def test_pdf_with_text_sends_its_text_to_the_text_model(self):
        model = FakeModelExtractor()
        content = pdf_bytes("Tornillo 6x1 1.234,56 " * 3, "Tarugo 8mm 40,00 " * 3)
        run(ListExtractor(model).extract_file(content, "lista.pdf", None))
        assert len(model.texts) == 1
        assert "Tornillo 6x1" in model.texts[0] and "Tarugo 8mm" in model.texts[0]
        assert model.images == []

    def test_scanned_pdf_pages_go_to_the_vision_model_as_png(self):
        model = FakeModelExtractor()
        result = run(ListExtractor(model).extract_file(pdf_bytes("", ""), "escaneo.pdf", None))
        assert model.texts == []
        [images] = model.images
        assert len(images) == 2
        assert all(mime == "image/png" and data.startswith(b"\x89PNG") for data, mime in images)
        assert len(result.rows) == 2

    def test_pdf_over_the_page_limit_is_truncated_and_reported(self):
        model = FakeModelExtractor()
        content = pdf_bytes(*([""] * (MAX_PDF_PAGES + 2)))
        result = run(ListExtractor(model).extract_file(content, "largo.pdf", None))
        assert len(model.images[0]) == MAX_PDF_PAGES
        assert result.problems == [f"only the first {MAX_PDF_PAGES} of {MAX_PDF_PAGES + 2} pages were read"]

    def test_invalid_pdf_is_reported(self):
        result = run(ListExtractor(FakeModelExtractor()).extract_file(b"%PDF-broken", "lista.pdf", None))
        assert result.rows == []
        assert result.problems[0].startswith("the PDF could not be read")

    def test_photo_goes_to_the_vision_model(self):
        model = FakeModelExtractor()
        run(ListExtractor(model).extract_file(b"jpeg-bytes", None, "image/jpeg"))
        assert model.images == [[(b"jpeg-bytes", "image/jpeg")]]

    def test_text_file_and_pasted_text_go_to_the_text_model(self):
        model = FakeModelExtractor()
        extractor = ListExtractor(model)
        run(extractor.extract_file("Caño ½ $85".encode("cp1252"), "lista.txt", None))
        run(extractor.extract_pasted_text("Tornillo $10"))
        assert model.texts == ["Caño ½ $85", "Tornillo $10"]

    def test_unsupported_file_is_reported(self):
        result = run(ListExtractor(FakeModelExtractor()).extract_file(b"x", "raro.zip", "application/zip"))
        assert result.problems == ["unsupported file type: send a PDF, Excel/CSV, photo or text"]


class TestMixedAndBrokenFiles:
    def test_mixed_pdf_reads_text_pages_as_text_and_only_sparse_pages_as_images(self):
        model = FakeModelExtractor()
        content = pdf_bytes("Tornillo 6x1 1.234,56 " * 3, "", "Tarugo 8mm 40,00 " * 3)
        result = run(ListExtractor(model).extract_file(content, "mixto.pdf", None))
        assert len(model.texts) == 1 and "Tarugo 8mm" in model.texts[0]
        assert len(model.images) == 1 and len(model.images[0]) == 1
        # Image rows are numbered after the text lines so source rows never collide.
        rows = sorted(r.source_row for r in result.rows)
        assert len(rows) == len(set(rows))

    def test_workbook_failing_while_reading_sheets_is_reported(self, monkeypatch):
        import openpyxl

        class BrokenWorkbook:
            worksheets = property(lambda self: (_ for _ in ()).throw(ValueError("bad sheet xml")))

            def close(self):
                pass

        monkeypatch.setattr(openpyxl, "load_workbook", lambda *a, **k: BrokenWorkbook())
        result = run(ListExtractor(FakeModelExtractor()).extract_file(b"PK", "lista.xlsx", None))
        assert result.rows == []
        assert "bad sheet xml" in result.problems[0]
