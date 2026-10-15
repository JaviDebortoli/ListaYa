# Sample data

**Fictional** data for a demo hardware store and two made-up suppliers, in English and US dollars. They do not belong to any real business; they exist to try ListaYa end to end.

| File | What it is | What it exercises |
|---|---|---|
| `hardware-store/catalog.csv` | The merchant's catalog: 25 products with current cost and selling price, excluding VAT | Catalog in CSV |
| `hardware-store/southern_fasteners.csv` | A supplier list as a spreadsheet, prices excluding VAT | Code-only route (no model): header below a title, section rows, price increases, margin alerts, an ambiguous line ("SELF-DRILLING SCREW" with no size) and a new product (12 mm anchor) |
| `hardware-store/northside_distributors.txt` | A supplier list pasted as text, prices including 21% VAT | Text-model route: messy formatting, VAT to remove, a thousands separator (`$ 1,089.00`), a "call for price" line and a new product (bench grinder) |

Settings for this sample (see the main README):

```
LISTAYA_CATALOG_PATH=samples/hardware-store/catalog.csv
LISTAYA_CURRENCY=USD
LISTAYA_DECIMAL_SEPARATOR=.
LISTAYA_ROUNDING_STEP=0.05
```

Approving a list rewrites the catalog file, so use a copy if you want to keep the original.
