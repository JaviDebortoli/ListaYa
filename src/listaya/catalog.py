"""Merchant catalog stored as a CSV file (decision D-08).

The CSV is the source of truth for the catalog. Required columns: sku, name,
cost, price. Optional columns: unit, pack_size, category. Any other column is
kept untouched when prices are updated. Amounts use a dot as decimal separator
and no thousands separator; empty cells mean "unknown".
"""

from __future__ import annotations

import csv
import io
import os
import tempfile
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterable

REQUIRED_COLUMNS = ("sku", "name", "cost", "price")
_BOM = b"\xef\xbb\xbf"


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


class CsvCatalog:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def load(self) -> list[CatalogProduct]:
        _, rows, _ = self._read()
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
        pending = {update.sku: update for update in updates}
        for update in pending.values():
            if update.cost <= 0 or update.price <= 0:
                raise CatalogError(f"sku {update.sku!r}: cost and price must be positive")

        self.load()  # validate the whole file before rewriting it
        fieldnames, rows, raw = self._read()
        known = {_text(row, "sku") for _, row in rows}
        missing = sorted(set(pending) - known)
        if missing:
            raise CatalogError(f"unknown sku(s): {', '.join(missing)}")

        for _, row in rows:
            update = pending.get(_text(row, "sku") or "")
            if update is not None:
                row["cost"] = _format(update.cost)
                row["price"] = _format(update.price)

        lineterminator = "\r\n" if b"\r\n" in raw.split(b"\n", 1)[0] + b"\n" else "\n"
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=fieldnames, lineterminator=lineterminator)
        writer.writeheader()
        writer.writerows(row for _, row in rows)
        encoding = "utf-8-sig" if raw.startswith(_BOM) else "utf-8"
        self._write_atomically(buffer.getvalue().encode(encoding))

    def _read(self) -> tuple[list[str], list[tuple[int, dict[str, str | None]]], bytes]:
        raw = self.path.read_bytes()
        reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig"), newline=""))
        fieldnames = [name.strip() for name in reader.fieldnames or []]
        missing = [column for column in REQUIRED_COLUMNS if column not in fieldnames]
        if missing:
            raise CatalogError(f"missing required column(s): {', '.join(missing)}")
        reader.fieldnames = fieldnames
        # Line 1 is the header, so the first data row is line 2.
        rows = [(reader.line_num, dict(row)) for row in reader]
        return fieldnames, rows, raw

    def _write_atomically(self, data: bytes) -> None:
        fd, temp_name = tempfile.mkstemp(dir=self.path.parent, prefix=f".{self.path.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
            os.replace(temp_name, self.path)
        except BaseException:
            if os.path.exists(temp_name):
                os.remove(temp_name)
            raise
