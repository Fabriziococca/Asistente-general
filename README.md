# Asistente General de Discord

Bot de Python/discord.py para atender tickets de Ticket Tool, leer comprobantes con Gemini, entregar rangos permanentes y registrar ventas en PostgreSQL/Neon. Los descargadores de imágenes y videos son aplicaciones independientes.

## Comprobantes y entrega

- Lee todas las imágenes/PDF de cada mensaje. Acepta diferencias de diseño, recortes, banco receptor y ausencia de fecha o número de operación; Wilobank es válido. Requiere un importe legible en ARS/USDT y coincidencia del titular o cuenta de destino.
- Gemini extrae datos. Python comprueba precios, combinaciones y saldo con Decimal. Las órdenes textuales del modelo no entregan roles.
- Registra los comprobantes antes de asignar acceso. Acumula pagos del mismo comprador y ticket, también en mensajes separados; no mezcla ARS y USDT.
- Conserva todos los rangos pendientes desde antes de llamar a Gemini. Si se pidieron Diamante y Oro, escribir solamente “Oro” junto al segundo comprobante no cancela Diamante. 4,50 USDT + 4 USDT completan ambos; si falta registrar parte del pago, identifica el pedido y los rangos que faltan cubrir. El combo se entrega cuando alcanza el total.
- Detecta reenvíos por huella del archivo y por operación/importe/moneda, cuando hay operación. La protección empieza con esta versión: los pagos históricos no contienen huellas.
- Un comprobante repetido también revisa el acceso en Discord. Si falta un rango que ya tiene una entrega registrada, recupera esa entrega sin sumar ingresos. Si el pedido no coincide con los rangos registrados, conserva el ticket y pide revisión, en lugar de confirmar que está resuelto. No reconstruye automáticamente las compras antiguas que quedaron mal clasificadas.
- Prepara la entrega en Neon, asigna roles, consulta Discord para verificar todos los rangos y recién entonces informa éxito.
- Completar una entrega quita únicamente esos rangos del pedido pendiente. Otros rangos o comprobantes pendientes mantienen el ticket abierto; la protección de conservación no cambia.
- Recupera entregas interrumpidas cada cinco minutos, hasta tres intentos; después interviene administración. El estado sobrevive a reinicios. `/manual` también pausa los reintentos del ticket.
- Ante incertidumbre o fallo técnico avisa al administrador desde el primer problema y conserva el ticket. No acusa de fraude ni pide otro pago por fallas de procesamiento.

No hay API bancaria ni confirmación de acreditación. La aprobación se basa en el comprobante, según la política permisiva del propietario. Se mantiene el pool existente: Gemini 3.8 Flash, 3.7 Flash y 3.6 Flash. No se habilita facturación ni se agregan servicios pagos.

## Conservación

`/conservar_ticket` es exclusivo para administradores. Guarda una protección permanente en Neon, respetada por limpieza periódica, `/purgar_tickets`, compra completada y salida del cliente del servidor. Reiniciar o completar la venta no quita la marca. El ticket queda para borrado manual.

La protección no controla botones ni automatizaciones de Ticket Tool u otros bots. `/manual` pausa la atención y los reintentos de entrega. `/automatico` los reanuda sin quitar la protección de conservación.

Sin protección, los tickets sin conversación pueden cerrarse a las tres horas y los demás a las 24 horas de inactividad. Se comprueba también el historial reciente de Discord. Un fallo al leerlo o una entrega pendiente impiden el borrado. Se eliminó el barrido basado solo en la edad del canal. Las sugerencias con conversación conservan su protección previa.

## Configuración y ejecución

Variables privadas: `DISCORD_TOKEN`, `DATABASE_URL`, `GEMINI_API_KEY_1`, `GEMINI_API_KEY_2` y `PORT` (opcional, 8080). Nunca guardar sus valores en Git.

```sh
pip install -r requirements.txt
python main.py
```

El bot necesita intents de miembros y contenido de mensajes, permiso para administrar roles y un rol superior a todos los rangos vendidos. Identificadores de roles: `cogs/tickets.py`. Destinos públicos y precios: `payment_policy.py`, compartidos por mensajes y cálculo y contrastados con tests.

El inicio aplica `payment_store.SCHEMA`: columnas e índices adicionales, sin borrar pagos ni reconstruir datos históricos. En un rollback se conservan las columnas; una versión anterior no respeta las protecciones ni entregas nuevas.

## Pruebas

```sh
python -X utf8 -m unittest discover -s tests -v
```

La suite normal simula Discord y Gemini. Para la integración PostgreSQL configurar `BOT_TEST_DATABASE_URL`: usa el endpoint directo, una transacción y tablas TEMP con `search_path=pg_temp`; revierte la transacción y cierra la conexión al terminar. No escribe filas de negocio.

Al desplegar, comprobar commit en Render, carga de cogs, sincronización de `/conservar_ticket`, esquema actualizado y jerarquía de roles. HTTP 200 del servidor de salud no demuestra conexión con Discord ni Neon.

## Atención y recuperación

Las consultas frecuentes (precios, cobro, duración, diferencias, comprobantes y acceso) se responden localmente sin Gemini. Las demás usan historial cronológico con autores identificados por Discord y estado real de la compra. No se infiere acreditación ni análisis en curso a partir de frases del cliente. Las respuestas largas se dividen según el límite de Discord y las menciones de la IA no generan pings.

`ticket_inbox` guarda IDs y estados, no el texto de las conversaciones. Los mensajes en trámite tienen una reserva temporal; si se reinicia el proceso, se recuperan al vencerla. Los eventos duplicados ya completados no se procesan nuevamente. La revisión de canales y bienvenidas corre al reconectar y cada cinco minutos, en lotes limitados. Un ticket con respuesta pendiente no se borra automáticamente.

La recuperación comienza con el despliegue de esta versión; no reproduce conversaciones ni comprobantes históricos anteriores. Un archivo eliminado de Discord o un canal sin permisos requiere intervención humana. La recuperación depende de que Discord mantenga el mensaje y el servicio vuelva a ejecutarse.

El modo manual se respeta durante la recuperación. Las consultas demoradas tienen un aviso de recepción y, si falla Google, una respuesta útil y derivación a administración. Los modelos con errores quedan temporalmente en espera para no agotar cuota repitiendo llamadas. El presupuesto incluye la espera por la IA: 35 segundos para texto y 90 para comprobantes.

El aviso semanal conserva su fecha en Neon; reiniciar o reconectar ya no dispara otro @everyone. La primera ejecución de esta versión inicia ese calendario sin emitir un broadcast.

## Operación gratuita y salud

El supervisor recrea el cliente después de fallos transitorios, cierra el pool anterior y conserva los trabajos en Neon. Un módulo esencial que no carga aborta ese cliente; no se sincroniza un árbol incompleto de comandos. Las dependencias directas se fijan a versiones verificadas.

`/` y `/readyz` devuelven 200 solo con conexión Discord, base comprobada, tickets cargados y comandos sincronizados; en caso contrario devuelven 503. `/healthz` comprueba la actividad del proceso/event loop. La base se comprueba cada cinco minutos, sin consultas por cada health check HTTP. Los endpoints exponen únicamente estados, sin credenciales ni conversaciones.

Render Free puede suspender servicios por inactividad o al consumir sus horas incluidas; Gemini gratuito también tiene cuotas y disponibilidad variable. Estas mejoras recuperan trabajo al volver a estar disponible el servicio y reducen silencios; no ofrecen disponibilidad continua garantizada. No se agregan servicios pagos ni mecanismos artificiales para impedir la suspensión. Referencias: https://render.com/docs/free y https://ai.google.dev/gemini-api/docs/rate-limits.

## Medios de cobro vigentes

Solo transferencia en pesos argentinos a CBU/CVU y USDT por Binance Pay. El Pay ID confirmado por el propietario es `552346130`; alias local `LENGUA.LUJOSA.TELAR`, CBU/CVU `3840200500000026286680`.

Precios internacionales sin descuento: Diamante 4,50 USDT, Oro 4 USDT, Plata 2 USDT y petición 4 USDT. No se aceptan conversiones de USD ni de otras criptomonedas. El destinatario se valida por ID cuando aparece, sin depender de que el apodo público permanezca igual.

Si un comprobante Binance/USDT legible solo muestra un apodo nuevo, la política permisiva permite continuar y avisa a administración para auditar el destinatario. Un ID de destino explícitamente diferente no se aprueba. La falta total de destinatario sigue requiriendo revisión.

La política se controla al extraer, guardar, preparar y entregar. Un método anterior no vuelve a habilitarse por un mensaje repetido ni por la recuperación de pendientes. El contexto y las respuestas automáticas omiten instrucciones de cobro obsoletas.

El titular bancario permanece en el contexto interno para leer comprobantes y responder consultas explícitas sobre el titular. No se publica en bienvenidas, datos generales de pago ni respuestas espontáneas. El historial que recibe la IA omite el nombre y el apodo anterior, y una guardia de salida impide que el modelo los vuelva a ofrecer sin consulta. Los identificadores necesarios para cobrar (alias, CBU/CVU y Pay ID) siguen visibles. Esto controla los mensajes del asistente, no las pantallas de las plataformas de pago ni mensajes ajenos.

Los pagos históricos se conservan con su moneda y método originales. `/finanzas` muestra importes registrados brutos, separa ARS, USDT y USD históricos y no aplica descuentos, conversiones ni comisiones supuestas. No se modifica retroactivamente el historial ni se revocan rangos comprados antes del cambio.
