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

La clave de AMD Token Factory se define en la variable de entorno `RADEON_API_KEY` (por ejemplo, en un archivo `.env` local que no se versiona).

## Licencia

Ver el archivo `LICENSE`.
