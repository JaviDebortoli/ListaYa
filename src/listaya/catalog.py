"""Merchant catalog stored as a CSV file (decision D-08).

The CSV is the source of truth for the catalog. Required columns: sku, name,
cost, price. Optional columns: unit, pack_size, category. Any other column is
kept untouched when prices are updated. Amounts use a dot as decimal separator
and no thousands separator; empty cells mean "unknown". Files may be UTF-8
(with or without BOM) or Windows-1252, and are written back the same way.
"""

from __future__ import annotations

import csv
import io
import os
import stat
import tempfile
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterable, NamedTuple

REQUIRED_COLUMNS = ("sku", "name", "cost", "price")
_BOM = b"\xef\xbb\xbf"
# Excel in Spanish often saves CSV as Windows-1252 rather than UTF-8.
_FALLBACK_ENCODING = "cp1252"


class CatalogError(ValueError):
    """Raised when the catalog file is malformed or an update is invalid."""


@dataclass(frozen=True)
class CatalogProduct:
    sku: str
    name: str
    unit: str | None
    pack_size: Decimal | None
    cost: Decimal | None
    price: Decimal | None
    category: str | None


@dataclass(frozen=True)
class PriceUpdate:
    sku: str
    cost: Decimal
    price: Decimal


def _text(row: dict[str, str | None], column: str) -> str | None:
    value = (row.get(column) or "").strip()
    return value or None


def _amount(row: dict[str, str | None], column: str, line: int) -> Decimal | None:
    text = _text(row, column)
    if text is None:
        return None
    try:
        value = Decimal(text)
    except InvalidOperation:
        raise CatalogError(f"line {line}: '{column}' is not a valid amount: {text!r}") from None
    if not value.is_finite():
        raise CatalogError(f"line {line}: '{column}' is not a valid amount: {text!r}")
    return value


def _format(value: Decimal) -> str:
    return format(value, "f")


class _ParsedFile(NamedTuple):
    header: list[str]  # as written in the file, used to rewrite it unchanged
    fieldnames: list[str]  # stripped names used to look up columns
    rows: list[tuple[int, dict[str, str | None]]]  # (line number, row)
    raw: bytes
    encoding: str


class CsvCatalog:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def load(self) -> list[CatalogProduct]:
        rows = self._read().rows
        products: list[CatalogProduct] = []
        seen: set[str] = set()
        for line, row in rows:
            sku = _text(row, "sku")
            if sku is None:
                raise CatalogError(f"line {line}: empty sku")
            if sku in seen:
                raise CatalogError(f"line {line}: duplicate sku {sku!r}")
            seen.add(sku)
            products.append(
                CatalogProduct(
                    sku=sku,
                    name=_text(row, "name") or "",
                    unit=_text(row, "unit"),
                    pack_size=_amount(row, "pack_size", line),
                    cost=_amount(row, "cost", line),
                    price=_amount(row, "price", line),
                    category=_text(row, "category"),
                )
            )
        return products

    def apply_updates(self, updates: Iterable[PriceUpdate]) -> None:
        """Write new costs and prices. Validates everything first; on error the file is untouched."""
        pending: dict[str, PriceUpdate] = {}
        for update in updates:
            if update.sku in pending:
                raise CatalogError(f"sku {update.sku!r} appears more than once in the update")
            for value in (update.cost, update.price):
                if not value.is_finite() or value <= 0:
                    raise CatalogError(f"sku {update.sku!r}: cost and price must be positive numbers")
            pending[update.sku] = update

        self.load()  # validate the whole file before rewriting it
        parsed = self._read()
        rows = parsed.rows
        known = {_text(row, "sku") for _, row in rows}
        missing = sorted(set(pending) - known)
        if missing:
            raise CatalogError(f"unknown sku(s): {', '.join(missing)}")

        for _, row in rows:
            update = pending.get(_text(row, "sku") or "")
            if update is not None:
                row["cost"] = _format(update.cost)
                row["price"] = _format(update.price)

        first_line = parsed.raw.split(b"\n", 1)[0]
        lineterminator = "\r\n" if first_line.endswith(b"\r") else "\n"
        buffer = io.StringIO()
        csv.writer(buffer, lineterminator=lineterminator).writerow(parsed.header)  # original spacing
        writer = csv.DictWriter(buffer, fieldnames=parsed.fieldnames, lineterminator=lineterminator)
        writer.writerows(row for _, row in rows)
        self._write_atomically(buffer.getvalue().encode(parsed.encoding))

    @staticmethod
    def _decode(raw: bytes) -> tuple[str, str]:
        if raw.startswith(_BOM):
            return raw.decode("utf-8-sig"), "utf-8-sig"
        for encoding in ("utf-8", _FALLBACK_ENCODING):
            try:
                return raw.decode(encoding), encoding
            except UnicodeDecodeError:
                continue
        raise CatalogError("unsupported file encoding: save the catalog as UTF-8 CSV")

    def _read(self) -> _ParsedFile:
        raw = self.path.read_bytes()
        text, encoding = self._decode(raw)
        reader = csv.DictReader(io.StringIO(text, newline=""))
        header = list(reader.fieldnames or [])
        fieldnames = [name.strip() for name in header]
        missing = [column for column in REQUIRED_COLUMNS if column not in fieldnames]
        if missing:
            raise CatalogError(f"missing required column(s): {', '.join(missing)}")
        reader.fieldnames = fieldnames
        rows = []
        for row in reader:
            # DictReader puts cells beyond the header under the None key.
            if row.get(None):  # type: ignore[call-overload]
                raise CatalogError(f"line {reader.line_num}: more cells than header columns")
            rows.append((reader.line_num, dict(row)))
        return _ParsedFile(header, fieldnames, rows, raw, encoding)

    def _write_atomically(self, data: bytes) -> None:
        mode = stat.S_IMODE(self.path.stat().st_mode)
        fd, temp_name = tempfile.mkstemp(dir=self.path.parent, prefix=f".{self.path.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            # mkstemp creates the file as owner-only; keep the catalog's original permissions.
            os.chmod(temp_name, mode)
            os.replace(temp_name, self.path)
        except BaseException:
            if os.path.exists(temp_name):
                os.remove(temp_name)
            raise
