# Asistente General de Discord

Bot de Python/discord.py para atender tickets de Ticket Tool, leer comprobantes con Gemini, entregar rangos permanentes y registrar ventas en PostgreSQL/Neon. Los descargadores de imágenes y videos son aplicaciones independientes.

## Comprobantes y entrega

- Lee todas las imágenes/PDF de cada mensaje. Acepta diferencias de diseño, recortes, banco receptor y ausencia de fecha o número de operación; Wilobank es válido. Requiere un importe legible en ARS/USD y coincidencia del titular o cuenta de destino.
- Gemini extrae datos. Python comprueba precios, combinaciones y saldo con Decimal. Las órdenes textuales del modelo no entregan roles.
- Registra los comprobantes antes de asignar acceso. Acumula pagos del mismo comprador y ticket, también en mensajes separados; no mezcla ARS y USD.
- Detecta reenvíos por huella del archivo y por operación/importe/moneda, cuando hay operación. La protección empieza con esta versión: los pagos históricos no contienen huellas.
- Prepara la entrega en Neon, asigna roles, consulta Discord para verificar todos los rangos y recién entonces informa éxito.
- Recupera entregas interrumpidas cada cinco minutos, hasta tres intentos; después interviene administración. El estado sobrevive a reinicios. `/manual` también pausa los reintentos del ticket.
- Ante incertidumbre o fallo técnico avisa al administrador desde el primer problema y conserva el ticket. No acusa de fraude ni pide otro pago por fallas de procesamiento.

No hay API bancaria ni confirmación de acreditación. La aprobación se basa en el comprobante, según la política permisiva del propietario. Se mantiene el pool existente: Gemini 3.8 Flash, 3.7 Flash y 3.6 Flash. No se habilita facturación ni se agregan servicios pagos.

## Conservación

`/conservar_ticket` es exclusivo para administradores. Guarda una protección permanente en Neon, respetada por limpieza periódica, `/purgar_tickets`, compra completada y salida del cliente del servidor. Reiniciar o completar la venta no quita la marca. El ticket queda para borrado manual.

La protección no controla botones ni automatizaciones de Ticket Tool u otros bots. `/manual` apaga la atención automática y no equivale a conservar.

Sin protección, los tickets sin conversación pueden cerrarse a las tres horas y los demás a las 24 horas de inactividad. Se comprueba también el historial reciente de Discord. Un fallo al leerlo o una entrega pendiente impiden el borrado. Se eliminó el barrido basado solo en la edad del canal. Las sugerencias con conversación conservan su protección previa.

## Configuración y ejecución

Variables privadas: `DISCORD_TOKEN`, `DATABASE_URL`, `GEMINI_API_KEY_1`, `GEMINI_API_KEY_2` y `PORT` (opcional, 8080). Nunca guardar sus valores en Git.

```sh
pip install -r requirements.txt
python main.py
```

El bot necesita intents de miembros y contenido de mensajes, permiso para administrar roles y un rol superior a todos los rangos vendidos. Identificadores y precios: `cogs/tickets.py`; tarifas de cálculo: `receipts.py`, contrastadas con tests.

El inicio aplica `payment_store.SCHEMA`: columnas e índices adicionales, sin borrar pagos ni reconstruir datos históricos. En un rollback se conservan las columnas; una versión anterior no respeta las protecciones ni entregas nuevas.

## Pruebas

```sh
python -m unittest discover -s tests -v
```

La suite normal simula Discord y Gemini. Para la integración PostgreSQL configurar `BOT_TEST_DATABASE_URL`: usa el endpoint directo, una transacción y tablas TEMP con `search_path=pg_temp`; revierte la transacción y cierra la conexión al terminar. No escribe filas de negocio.

Al desplegar, comprobar commit en Render, carga de cogs, sincronización de `/conservar_ticket`, esquema actualizado y jerarquía de roles. HTTP 200 del servidor de salud no demuestra conexión con Discord ni Neon.
