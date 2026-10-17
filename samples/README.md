# Sample data

**Fictional** data for a demo hardware store and four made-up suppliers, in Argentine pesos. They do not belong to any real store or supplier; they exist to test ListaYa end to end until real anonymized lists are available. The files themselves are in Spanish, as a real Argentine merchant would have them.

| File | What it is | What it exercises |
|---|---|---|
| `ferreteria/catalogo.csv` | The merchant's catalog: 25 products with cost and current price, without VAT | CSV catalog |
| `ferreteria/lista_bulonera_sur.csv` | A supplier list as a spreadsheet, `;`-separated, prices without VAT | Code-only path (no model): header below a title, section rows, price increases, margin alerts, an ambiguous line ("TORNILLO AUTOPERFORANTE" without a size) and a new product (12 mm wall plug) |
| `ferreteria/lista_distribuidora_norte.txt` | A list pasted as text, prices including 21% VAT | Text model path: messy layout, VAT to remove, thousands separator (`$ 189.000`), a "consultar" (ask for price) row and a new product (bench grinder) |
| `ferreteria/lista_pintureria_oeste.pdf` | A PDF with a text layer, prices "+ IVA" (without VAT) | Text PDF path: the text goes to the model and code decides VAT from what the list states. Includes a new product (synthetic enamel) |
| `ferreteria/foto_corralon_san_martin.jpg` | A photo of a printed list, slightly skewed, prices including VAT | Vision model path. Includes a new product (adjustable wrench) |
| `ferreteria/escaneo_corralon_san_martin.pdf` | The same list as a scanned PDF (image only) | Scanned PDF path: each page is turned into an image for the vision model |
| `ferreteria/esperado.json` | Hand-labeled expected rows for each list | Ground truth for `python -m listaya.benchmark` |

The PDF, the photo and the scan are generated with `python scripts/make_sample_documents.py`.

To try extraction on these lists:

```bash
python -m listaya.extraction_check samples/ferreteria/lista_bulonera_sur.csv
python -m listaya.extraction_check samples/ferreteria/lista_distribuidora_norte.txt
python -m listaya.extraction_check samples/ferreteria/foto_corralon_san_martin.jpg
```

Approving a list in the bot rewrites the catalog file: use a copy if you want to keep the original.
