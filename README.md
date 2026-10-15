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

## Estado

En desarrollo para la entrega del 18 de octubre de 2026. Las instrucciones para ejecutarlo se agregan a este archivo antes de la entrega.

## Configuración

La configuración se lee de variables de entorno (por ejemplo, de un archivo `.env` local que no se versiona):

| Variable | Para qué sirve |
|---|---|
| `RADEON_API_KEY` | Clave de AMD Token Factory |
| `TELEGRAM_BOT_TOKEN` | Token del bot de Telegram, creado con [@BotFather](https://t.me/BotFather) |
| `TOKEN_FACTORY_BASE_URL` | Opcional. Base URL de la API; por defecto `https://developer.amd.com.cn/radeon/api/v1` |

## Ejecutar en local

Requiere Python 3.12 o superior.

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -e ".[dev]"   # en Linux/macOS: .venv/bin/python
.venv/Scripts/python -m pytest                    # pruebas
.venv/Scripts/python -m listaya                   # inicia el bot
.venv/Scripts/python -m listaya.token_factory_check   # verifica el acceso a AMD Token Factory
```

## Licencia

Ver el archivo `LICENSE`.
