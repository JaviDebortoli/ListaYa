"""Choosing the extraction route for each kind of supplier list (PRD section 8.1).

- CSV / Excel: parsed with code; the text model is used only when no header is recognized.
- PDF with a text layer: its text goes to the text model in batches.
- Scanned PDF pages: rendered to PNG for the vision model; text pages of the same PDF
  still go to the text model.
- Photo: read by the vision model.
- Text file or pasted text: text model.
"""

from __future__ import annotations

import dataclasses
import io
from pathlib import PurePath
from typing import Any, Protocol, Sequence

from listaya.extraction.model import ExtractionResult
from listaya.extraction.rows import ExtractedRow
from listaya.extraction.spreadsheet import SpreadsheetError, read_table, rows_from_table, table_as_text

MAX_PDF_PAGES = 20
# A page with fewer visible characters than this is treated as scanned.
MIN_TEXT_CHARS_PER_PAGE = 20
RENDER_SCALE = 2.0  # about 144 dpi: legible for the vision model without huge images

_EXTENSIONS = {
    ".csv": "csv",
    ".xlsx": "xlsx",
    ".xlsm": "xlsx",
    ".xls": "xls",
    ".pdf": "pdf",
    ".jpg": "image",
    ".jpeg": "image",
    ".png": "image",
    ".webp": "image",
    ".txt": "text",
}
_IMAGE_MIME = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}


class ModelExtractorPort(Protocol):
    async def extract_text(self, text: str) -> ExtractionResult: ...

    async def extract_images(self, images: Sequence[tuple[bytes, str]]) -> ExtractionResult: ...


def detect_kind(file_name: str | None, mime_type: str | None) -> str | None:
    if file_name:
        kind = _EXTENSIONS.get(PurePath(file_name).suffix.lower())
        if kind:
            return kind
    mime = (mime_type or "").lower()
    if mime == "application/pdf":
        return "pdf"
    if mime.startswith("image/"):
        return "image"
    if mime in {"text/csv", "text/comma-separated-values"}:
        return "csv"
    if mime == "text/plain":
        return "text"
    return None


def _decode_text(content: bytes) -> str:
    try:
        return content.decode("utf-8-sig")
    except UnicodeDecodeError:
        return content.decode("cp1252", errors="replace")


def _with_problems(result: ExtractionResult, notes: list[str]) -> ExtractionResult:
    return ExtractionResult(result.rows, notes + result.problems) if notes else result


class ListExtractor:
    def __init__(self, model: ModelExtractorPort) -> None:
        self._model = model

    async def extract_pasted_text(self, text: str) -> ExtractionResult:
        return await self._model.extract_text(text)

    async def extract_file(self, content: bytes, file_name: str | None, mime_type: str | None) -> ExtractionResult:
        kind = detect_kind(file_name, mime_type)
        if kind in {"csv", "xlsx"}:
            return await self._spreadsheet(content, kind)
        if kind == "xls":
            return ExtractionResult([], ["old .xls files are not supported: save the list as .xlsx or .csv"])
        if kind == "pdf":
            return await self._pdf(content)
        if kind == "image":
            suffix = PurePath(file_name).suffix.lower() if file_name else ""
            mime = mime_type if (mime_type or "").startswith("image/") else _IMAGE_MIME.get(suffix, "image/jpeg")
            return await self._model.extract_images([(content, mime)])
        if kind == "text":
            return await self._model.extract_text(_decode_text(content))
        return ExtractionResult([], ["unsupported file type: send a PDF, Excel/CSV, photo or text"])

    async def _spreadsheet(self, content: bytes, kind: str) -> ExtractionResult:
        try:
            table, notes = read_table(content, kind)
        except SpreadsheetError as error:
            return ExtractionResult([], [str(error)])
        rows = rows_from_table(table)
        if rows is not None:
            return ExtractionResult(rows, notes)
        # No recognizable header: let the model map the columns (PRD 8.1).
        return _with_problems(await self._model.extract_text(table_as_text(table)), notes)

    async def _pdf(self, content: bytes) -> ExtractionResult:
        import pdfplumber

        try:
            with pdfplumber.open(io.BytesIO(content)) as pdf:
                total_pages = len(pdf.pages)
                pages = pdf.pages[:MAX_PDF_PAGES]
                texts = [page.extract_text() or "" for page in pages]
        except Exception as error:  # pdfminer raises several unrelated types for broken files
            return ExtractionResult([], [f"the PDF could not be read: {error}"])

        notes = []
        if total_pages > MAX_PDF_PAGES:
            notes.append(f"only the first {MAX_PDF_PAGES} of {total_pages} pages were read")

        # Pages with a text layer keep their exact text; only sparse (scanned) pages become images.
        sparse = [i for i, text in enumerate(texts) if len("".join(text.split())) < MIN_TEXT_CHARS_PER_PAGE]
        dense_text = "\n".join(text for i, text in enumerate(texts) if i not in sparse)

        rows: list[ExtractedRow] = []
        problems = list(notes)
        if dense_text.strip():
            text_result = await self._model.extract_text(dense_text)
            rows.extend(text_result.rows)
            problems.extend(text_result.problems)
        if sparse:
            try:
                images = _render_pages(content, sparse)
            except Exception as error:
                return ExtractionResult(rows, problems + [f"the PDF pages could not be rendered: {error}"])
            image_result = await self._model.extract_images(images)
            # Number image rows after the text lines so source rows never collide.
            offset = len(dense_text.splitlines())
            rows.extend(dataclasses.replace(row, source_row=offset + row.source_row) for row in image_result.rows)
            problems.extend(image_result.problems)
        return ExtractionResult(rows, problems)


def _render_pages(content: bytes, page_indexes: Sequence[int]) -> list[tuple[bytes, str]]:
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(content)
    try:
        images = []
        for index in page_indexes:
            image: Any = pdf[index].render(scale=RENDER_SCALE).to_pil()
            buffer = io.BytesIO()
            image.save(buffer, format="PNG")
            images.append((buffer.getvalue(), "image/png"))
        return images
    finally:
        pdf.close()
