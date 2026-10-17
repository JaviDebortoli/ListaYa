# ListaYa

A Telegram assistant that turns a supplier's price list into updated selling prices and flags the products whose margin fell below the minimum.

Built by team YOLO for the AMD Developer Hackathon: ACT III (Reinvent Commerce track).

ListaYa is designed for small Argentine businesses, so the bot talks in Argentine Spanish and works in pesos with Argentine number formats (`1.234,56`).

## What it does

1. The merchant sends the supplier's list over Telegram (PDF, Excel/CSV, photo or pasted text).
2. ListaYa extracts every product with its cost and matches it against the merchant's catalog, asking the merchant when a match is doubtful.
3. It computes suggested selling prices from the merchant's margin rules and flags the products that would fall below the minimum margin.
4. It replies with a summary and updates the catalog only if the merchant taps **Aprobar** (Approve).

## Try it

The bot is deployed at [t.me/ListaYaBot](https://t.me/ListaYaBot):

1. Download one of the sample lists from [`samples/ferreteria/`](samples/ferreteria/), for example `lista_bulonera_sur.csv`, `foto_corralon_san_martin.jpg` or `lista_pintureria_oeste.pdf`.
2. Send it to the bot. If you don't add the supplier's name as a caption, the bot asks for it ("¿De qué proveedor es esta lista?").
3. Answer the matching questions (**Sí** / **No**) and review the summary with the suggested prices and margin alerts.
4. Tap **Aprobar** to update the catalog or **Cancelar** to discard it.

The demo catalog is [`samples/ferreteria/catalogo.csv`](samples/ferreteria/catalogo.csv), and everyone who tries the bot shares it.

## How it uses AMD

Reading lists (photos, scanned PDFs and messy text) and resolving doubtful matches use open models served on AMD GPUs through AMD Token Factory (an OpenAI-compatible API). Prices and margins are computed by deterministic, tested code; the model never computes them.

## Measured results

Benchmark run on 2026-10-16 over the 5 sample lists (spreadsheet, pasted text, text PDF, photo and scanned PDF), with DeepSeek-V4-Flash for text and DeepSeek-V4.1-Flash for images on AMD Token Factory:

| Metric | Result |
|---|---|
| Rows extracted correctly (description, cost and VAT) | 46 of 46 (100%) |
| Model calls | 4 (the spreadsheet is read by code only) |
| Total cost | USD 0.0012 (about USD 0.0003 per list read with AI) |
| Time per list read with AI | 24 to 36 seconds |

Vision models compared on the photo and the scan: DeepSeek-V4.1-Flash 14/14, Qwen3.8-Flash-Next 14/14 (at twice the cost), MiMo-V2.6-Flash 0/14.

## Status

MVP complete for the October 18, 2026 submission. The whole flow works over Telegram: receiving the list, asking about doubtful matches, summarizing and updating the catalog on approval. It runs in a Docker container on an Oracle Cloud virtual machine.

## Configuration

Settings are read from environment variables (for example from a local `.env` file, which is not versioned):

| Variable | Purpose | Default |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | Telegram bot token, created with [@BotFather](https://t.me/BotFather) | (required) |
| `RADEON_API_KEY` | AMD Token Factory key | (required) |
| `TELEGRAM_ALLOWED_CHAT_IDS` | Comma-separated ids of the chats that may use the bot, or `*` to let any chat use it (they all share the same catalog). The bot replies to an unauthorized chat with its id | empty |
| `LISTAYA_CATALOG_PATH` | The merchant's catalog as CSV; approving a list updates it | `data/catalogo.csv` |
| `LISTAYA_DB_PATH` | SQLite database with confirmed aliases, per-supplier VAT and metrics | `data/listaya.db` |
| `LISTAYA_TARGET_MARGIN` / `LISTAYA_MIN_MARGIN` | Target and minimum margin, on the selling price | `0.30` / `0.20` |
| `LISTAYA_ROUNDING_STEP` | Rounding of the suggested price | `10` |
| `LISTAYA_PRICES_INCLUDE_VAT` / `LISTAYA_VAT_RATE` | Whether catalog prices include VAT, and the VAT rate | `false` / `0.21` |
| `LISTAYA_CURRENCY` / `LISTAYA_DECIMAL_SEPARATOR` | The store's currency and the lists' decimal separator | `ARS` / `,` |
| `LISTAYA_TEXT_MODEL` / `LISTAYA_VISION_MODEL` | Token Factory models for text and images | `DeepSeek-V4-Flash` / `DeepSeek-V4.1-Flash` |
| `TOKEN_FACTORY_BASE_URL` | API base URL | `https://developer.amd.com.cn/radeon/api/v1` |

To try it with the sample data, copy the catalog to the default location (the `data/` folder is not versioned):

```bash
mkdir data
cp samples/ferreteria/catalogo.csv data/catalogo.csv
```

On startup the bot checks that the configured models exist on Token Factory. If `TELEGRAM_ALLOWED_CHAT_IDS` is empty, message the bot: it replies with your chat id so you can add it.

## Run locally

Requires Python 3.12 or later.

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -e ".[dev]"   # on Linux/macOS: .venv/bin/python
.venv/Scripts/python -m pytest                    # tests
.venv/Scripts/python -m listaya                   # starts the bot
.venv/Scripts/python -m listaya.token_factory_check   # checks access to AMD Token Factory
.venv/Scripts/python -m listaya.extraction_check lista.pdf   # extracts a list and shows rows, tokens and time
.venv/Scripts/python -m listaya.benchmark          # accuracy, time and cost on the sample lists
.venv/Scripts/python -m listaya.metrics            # metrics for every list the bot processed
```

`benchmark` compares each sample list with its expected result (`samples/ferreteria/esperado.json`): a row is correct when its description, cost and VAT all match. `metrics` reads the bot's SQLite database; the seconds for each list run from when it arrives until the summary is sent, so they include the time the merchant takes to answer questions.

`extraction_check` accepts PDF, Excel/CSV, photos and `.txt`, or text with `--text`. Choose the models with `--text-model` and `--vision-model` (by default `DeepSeek-V4-Flash` and `DeepSeek-V4.1-Flash`). It assumes Argentine lists by default (pesos and decimal comma); for dollar lists with a decimal point, add `--currency USD --decimal-separator .`.

## Run with Docker

```bash
docker build -t listaya .
mkdir data
cp samples/ferreteria/catalogo.csv data/catalogo.csv   # or your own catalog
docker run --rm --env-file .env -v "$(pwd)/data:/app/data" listaya
```

The container runs as an unprivileged user and keeps the catalog and the SQLite database in `/app/data` (the mounted volume), so approved prices and learned aliases survive restarts.

## Sample data

The `samples/` folder holds fictional data for an Argentine hardware store, in pesos: the catalog, five supplier lists (spreadsheet, pasted text, text PDF, photo and scanned PDF) and their expected extraction results. See `samples/README.md`.

## License

See the `LICENSE` file.
