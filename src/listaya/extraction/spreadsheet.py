"""Reading supplier lists that arrive as CSV or Excel files, with code only.

The header row is found by keywords (código, descripción, precio...). When no
header is recognized the caller falls back to the model with table_as_text.
VAT and currency are read from the price header ("Precio + IVA", "Precio U$S")
and are left unknown when the header does not say.
"""

from __future__ import annotations

import csv
import io
import re
import unicodedata
import zipfile
from decimal import Decimal
from typing import Any

from listaya.extraction.rows import ExtractedRow, ListFormat, validate_rows

HEADER_SEARCH_ROWS = 20

# Spanish and English keywords, checked in this order, so "Cód. Artículo" / "Item code" is a
# code and "Precio x unidad" / "Unit price" is a price.
_FIELD_KEYWORDS: list[tuple[str, frozenset[str]]] = [
    ("supplier_code", frozenset({"codigo", "cod", "sku", "ref", "referencia", "code"})),
    (
        "unit_cost_text",
        frozenset({"precio", "costo", "importe", "valor", "lista", "neto", "pvp", "price", "cost", "net"}),
    ),
    (
        "description",
        frozenset(
            {"descripcion", "producto", "articulo", "detalle", "nombre", "item", "desc", "description", "product", "name"}
        ),
    ),
    (
        "pack_size",
        frozenset({"presentacion", "pack", "bulto", "envase", "contenido", "cantidad", "qty", "quantity", "case"}),
    ),
    ("unit", frozenset({"unidad", "um", "medida", "unit", "uom"})),
]


class SpreadsheetError(ValueError):
    """The file could not be read as a spreadsheet."""


def _normalize(text: str) -> str:
    stripped = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    return stripped.lower().strip()


def _cell(value: Any) -> Any:
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        return value.strip() or None
    return value


def _trim(row: list[Any]) -> list[Any]:
    cells = [_cell(value) for value in row]
    while cells and cells[-1] is None:
        cells.pop()
    return cells


def read_table(content: bytes, kind: str) -> tuple[list[list[Any]], list[str]]:
    """Return the rows of a 'csv' or 'xlsx' file and notes about anything skipped."""
    if kind == "csv":
        return _read_csv(content), []
    if kind == "xlsx":
        return _read_xlsx(content)
    raise SpreadsheetError(f"unsupported spreadsheet type: {kind}")


def _read_csv(content: bytes) -> list[list[Any]]:
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = content.decode("cp1252", errors="replace")
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=";,\t|")
        delimiter = dialect.delimiter
    except csv.Error:
        delimiter = ","
    return [_trim(row) for row in csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)]


def _read_xlsx(content: bytes) -> tuple[list[list[Any]], list[str]]:
    import openpyxl
    from openpyxl.utils.exceptions import InvalidFileException

    try:
        workbook = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    except (zipfile.BadZipFile, InvalidFileException, KeyError, OSError) as error:
        raise SpreadsheetError(f"the Excel file could not be read: {error}") from error

    with_data: list[tuple[str, list[list[Any]]]] = []
    try:
        # Read-only workbooks parse sheet XML lazily, so malformed files can also fail here.
        for sheet in workbook.worksheets:
            rows = [_trim(list(row)) for row in sheet.iter_rows(values_only=True)]
            if any(rows):
                with_data.append((sheet.title, rows))
    except Exception as error:
        raise SpreadsheetError(f"the Excel file could not be read: {error}") from error
    finally:
        workbook.close()

    if not with_data:
        return [], []
    (title, rows), rest = with_data[0], with_data[1:]
    notes = [f"only sheet '{title}' was read; ignored: {', '.join(name for name, _ in rest)}"] if rest else []
    return rows, notes


def _header_fields(row: list[Any]) -> dict[str, int]:
    fields: dict[str, int] = {}
    for column, value in enumerate(row):
        if not isinstance(value, str):
            continue
        words = set(re.findall(r"[a-z]+", _normalize(value).replace(".", "")))
        for field, keywords in _FIELD_KEYWORDS:
            if words & keywords:
                fields.setdefault(field, column)
                break
    return fields


def _vat_from_header(header: str) -> bool | None:
    text = _normalize(header)
    if re.search(r"(c/|con)\s*iva|iva\s*incl|incl\.?\s*(vat|tax)|(vat|tax)\s*incl", text):
        return True
    if re.search(r"(s/|sin|\+|mas)\s*iva|(\bex|excl?)\.?\s*(vat|tax)|(\+|plus)\s*(vat|tax)|\bnet\b", text):
        return False
    return None


def _currency_from_header(header: str, local_currency: str) -> str | None:
    text = _normalize(header)
    if re.search(r"u\$s|us\$|usd|dolar", text):
        return "USD"
    if re.search(r"\bars\b|pesos", text):
        return "ARS"
    if "$" in text:
        return local_currency  # a bare "$" is the merchant's own currency
    return None


def rows_from_table(table: list[list[Any]], list_format: ListFormat | None = None) -> list[ExtractedRow] | None:
    """Extract rows using a recognized header, or None when no header is found."""
    fmt = list_format or ListFormat()
    for header_index, header in enumerate(table[:HEADER_SEARCH_ROWS]):
        fields = _header_fields(header)
        if "description" in fields and "unit_cost_text" in fields:
            break
    else:
        return None

    price_header = str(header[fields["unit_cost_text"]])
    includes_vat = _vat_from_header(price_header)
    currency = _currency_from_header(price_header, fmt.local_currency)

    raw_rows: list[dict[str, Any]] = []
    for row_index in range(header_index + 1, len(table)):
        row = table[row_index]
        if sum(value is not None for value in row) < 2:
            continue  # blank lines and section titles such as "TORNILLERIA"
        item: dict[str, Any] = {
            field: row[column] if column < len(row) else None for field, column in fields.items()
        }
        item.update(
            source_row=row_index + 1, includes_vat=includes_vat, currency=currency, confidence=1.0
        )
        raw_rows.append(item)
    return validate_rows(raw_rows, fmt)


def table_as_text(table: list[list[Any]], list_format: ListFormat | None = None) -> str:
    """Render a table as text lines ('cell | cell') for the model when no header was recognized."""
    separator = (list_format or ListFormat()).decimal_separator
    return "\n".join(" | ".join(_cell_text(value, separator) for value in row if value is not None) for row in table)


def _cell_text(value: Any, decimal_separator: str) -> str:
    # Write real numbers with the list's decimal separator, the convention the amount parser expects.
    if isinstance(value, float):
        return format(Decimal(str(value)), "f").replace(".", decimal_separator)
    return str(value)
