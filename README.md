# ListaYa

Asistente por Telegram que convierte la lista de precios de un proveedor en precios de venta actualizados y avisa qué productos quedaron con el margen por debajo del mínimo.

Proyecto del equipo YOLO para el AMD Developer Hackathon: ACT III (track Reinvent Commerce).

## Qué hace

1. El comerciante envía por Telegram la lista del proveedor (PDF, Excel/CSV, foto o texto).
2. ListaYa extrae cada producto con su costo y lo empareja con el catálogo del comerciante, consultándole cuando hay dudas.
3. Calcula los precios de venta sugeridos según sus reglas de margen y señala los productos que quedarían por debajo del margen mínimo.
4. Responde con un resumen y, solo si el comerciante toca **Aprobar**, actualiza el catálogo.

## Cómo usa AMD

La lectura de listas (fotos, PDFs escaneados y texto desordenado) y la resolución de emparejamientos dudosos usan modelos abiertos servidos en GPUs de AMD mediante AMD Token Factory (API compatible con OpenAI). Los precios y márgenes los calcula código determinístico con pruebas automáticas; el modelo nunca los calcula.

## Resultados medidos

Benchmark del 16/10/2026 sobre las 5 listas de ejemplo (planilla, texto pegado, PDF con texto, foto y PDF escaneado), con DeepSeek-V4-Flash para texto y DeepSeek-V4.1-Flash para imágenes en AMD Token Factory:

| Métrica | Resultado |
|---|---|
| Filas extraídas correctamente (descripción, costo e IVA) | 46 de 46 (100%) |
| Llamadas al modelo | 4 (la planilla se lee solo con código) |
| Costo total | USD 0,0012 (unos USD 0,0003 por lista leída con IA) |
| Tiempo por lista leída con IA | entre 24 y 36 segundos |

Comparación de modelos de visión sobre la foto y el escaneo: DeepSeek-V4.1-Flash 14/14, Qwen3.8-Flash-Next 14/14 (el doble de costo), MiMo-V2.6-Flash 0/14.

## Estado

En desarrollo para la entrega del 18 de octubre de 2026. El flujo completo funciona por Telegram: recibir la lista, preguntar las dudas, resumir y actualizar el catálogo al aprobar.

## Configuración

La configuración se lee de variables de entorno (por ejemplo, de un archivo `.env` local que no se versiona):

| Variable | Para qué sirve | Por defecto |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | Token del bot de Telegram, creado con [@BotFather](https://t.me/BotFather) | (obligatoria) |
| `RADEON_API_KEY` | Clave de AMD Token Factory | (obligatoria) |
| `TELEGRAM_ALLOWED_CHAT_IDS` | Ids de los chats que pueden usar el bot, separados por coma. A un chat no autorizado el bot le responde con su id | vacío |
| `LISTAYA_CATALOG_PATH` | Catálogo del comerciante en CSV; aprobar una lista lo actualiza | `data/catalogo.csv` |
| `LISTAYA_DB_PATH` | Base SQLite con alias confirmados, IVA por proveedor y métricas | `data/listaya.db` |
| `LISTAYA_TARGET_MARGIN` / `LISTAYA_MIN_MARGIN` | Margen objetivo y mínimo, sobre el precio de venta | `0.30` / `0.20` |
| `LISTAYA_ROUNDING_STEP` | Redondeo del precio sugerido | `10` |
| `LISTAYA_PRICES_INCLUDE_VAT` / `LISTAYA_VAT_RATE` | Si el catálogo tiene precios con IVA, y la alícuota | `false` / `0.21` |
| `LISTAYA_CURRENCY` / `LISTAYA_DECIMAL_SEPARATOR` | Moneda del comercio y separador decimal de las listas | `ARS` / `,` |
| `LISTAYA_TEXT_MODEL` / `LISTAYA_VISION_MODEL` | Modelos de Token Factory para texto e imágenes | `DeepSeek-V4-Flash` / `DeepSeek-V4.1-Flash` |
| `TOKEN_FACTORY_BASE_URL` | Base URL de la API | `https://developer.amd.com.cn/radeon/api/v1` |

Para probar con los datos de ejemplo, copiá el catálogo a la ubicación por defecto (la carpeta `data/` no se versiona):

```bash
mkdir data
cp samples/ferreteria/catalogo.csv data/catalogo.csv
```

Al arrancar, el bot verifica que los modelos configurados existan en Token Factory. Si `TELEGRAM_ALLOWED_CHAT_IDS` está vacío, escribile al bot: te responde con el id de tu chat para agregarlo.

## Ejecutar en local

Requiere Python 3.12 o superior.

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -e ".[dev]"   # en Linux/macOS: .venv/bin/python
.venv/Scripts/python -m pytest                    # pruebas
.venv/Scripts/python -m listaya                   # inicia el bot
.venv/Scripts/python -m listaya.token_factory_check   # verifica el acceso a AMD Token Factory
.venv/Scripts/python -m listaya.extraction_check lista.pdf   # extrae una lista y muestra filas, tokens y tiempo
.venv/Scripts/python -m listaya.benchmark          # precisión, tiempo y costo sobre las listas de ejemplo
.venv/Scripts/python -m listaya.metrics            # métricas de cada lista procesada por el bot
```

`benchmark` compara cada lista de ejemplo con su resultado esperado (`samples/ferreteria/esperado.json`): una fila es correcta si coinciden la descripción, el costo y el IVA. `metrics` lee la base SQLite del bot; los segundos de cada lista van desde que llega hasta que se envía el resumen, así que incluyen el tiempo que el comerciante tarda en contestar las preguntas.

`extraction_check` acepta PDF, Excel/CSV, fotos y `.txt`, o texto con `--text`. Los modelos se eligen con `--text-model` y `--vision-model` (por defecto `DeepSeek-V4-Flash` y `DeepSeek-V4.1-Flash`). Por defecto asume listas argentinas (pesos y coma decimal); para listas en dólares con punto decimal, agregá `--currency USD --decimal-separator .`.

## Ejecutar con Docker

```bash
docker build -t listaya .
mkdir data
cp samples/ferreteria/catalogo.csv data/catalogo.csv   # o tu propio catálogo
docker run --rm --env-file .env -v "$(pwd)/data:/app/data" listaya
```

El contenedor corre como usuario sin privilegios y guarda el catálogo y la base SQLite en `/app/data` (el volumen montado), así que los precios aprobados y lo aprendido persisten entre reinicios.

## Datos de ejemplo

La carpeta `samples/` tiene datos ficticios de una ferretería argentina, en pesos: el catálogo y dos listas de proveedor (planilla y texto pegado). Ver `samples/README.md`.

## Licencia

Ver el archivo `LICENSE`.
