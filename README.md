# Asistente General de Discord

Bot de Python/discord.py para atender tickets de Ticket Tool, leer comprobantes con Gemini, entregar rangos permanentes y registrar ventas en PostgreSQL/Neon. Los descargadores de imágenes y videos son aplicaciones independientes.

## Comprobantes y entrega

- Lee todas las imágenes/PDF de cada mensaje. Acepta diferencias de diseño, recortes, banco receptor y ausencia de fecha o número de operación; Wilobank es válido. Requiere un importe legible en una moneda habilitada y coincidencia del titular o cuenta de destino.
- Gemini extrae datos. Python comprueba precios, combinaciones y saldo con Decimal. Las órdenes textuales del modelo no entregan roles.
- Registra los comprobantes antes de asignar acceso. Acumula pagos del mismo comprador y ticket, también en mensajes separados; no suma monedas distintas entre sí.
- Conserva todos los rangos pendientes desde antes de llamar a Gemini. Si se pidieron Diamante y Oro, escribir solamente “Oro” junto al segundo comprobante no cancela Diamante. Durante la campaña, 2 USD + 1,75 USD completan ambos; si falta registrar parte del pago, identifica el pedido y los rangos que faltan cubrir. El combo se entrega cuando alcanza el total.
- Detecta reenvíos por huella del archivo y por operación/importe/moneda, cuando hay operación. La protección empieza con esta versión: los pagos históricos no contienen huellas.
- Un comprobante repetido también revisa el acceso en Discord. Si falta un rango que ya tiene una entrega registrada, recupera esa entrega sin sumar ingresos. Si el pedido no coincide con los rangos registrados, conserva el ticket y pide revisión, en lugar de confirmar que está resuelto. No reconstruye automáticamente las compras antiguas que quedaron mal clasificadas.
- Prepara la entrega en Neon, asigna roles, consulta Discord para verificar todos los rangos y recién entonces informa éxito.
- Completar una entrega quita únicamente esos rangos del pedido pendiente. Otros rangos o comprobantes pendientes mantienen el ticket abierto; la protección de conservación no cambia.
- Recupera entregas interrumpidas cada cinco minutos, hasta tres intentos; después interviene administración. El estado sobrevive a reinicios. `/manual` también pausa los reintentos del ticket.
- Ante incertidumbre o fallo técnico avisa al administrador desde el primer problema y conserva el ticket. No acusa de fraude ni pide otro pago por fallas de procesamiento.

No hay API bancaria ni confirmación de acreditación. La aprobación se basa en el comprobante, según la política permisiva del propietario. Se mantiene el pool existente: Gemini 3.8 Flash, 3.7 Flash y 3.6 Flash. No se habilita facturación ni se agregan servicios pagos.

## Conservación

`/conservar_ticket` es exclusivo para administradores. Guarda una protección en Neon frente a inactividad, `/purgar_tickets` y compra completada mientras el creador permanezca en el servidor. Reiniciar o completar la venta no quita la marca. Por decisión del propietario del 31/10/2026, la salida del creador elimina el canal incluso si está conservado o tiene comprobantes pendientes; los registros financieros se mantienen en Neon.

La protección no controla botones ni automatizaciones de Ticket Tool u otros bots. `/manual` pausa la atención y los reintentos de entrega. `/automatico` los reanuda sin quitar la protección de conservación.

Sin protección, los tickets sin conversación pueden cerrarse a las tres horas y los demás a las 24 horas de inactividad. Se comprueba también el historial reciente de Discord. Un fallo al leerlo o una entrega pendiente impiden el borrado por inactividad. Se eliminó el barrido basado solo en la edad del canal. Las sugerencias con conversación conservan su protección previa.

## Salida del creador y cierre definitivo

Al recibir la salida del creador, el bot cancela las lecturas pendientes y manuales, marca el ticket como `salio` y elimina el canal sin emitir mensajes ni avisos. Este cierre tiene prioridad sobre `/conservar_ticket`, `/manual`, sugerencias y entregas pendientes. Se conserva la fila de ticket para impedir que un evento atrasado o un reinicio vuelva a activar trabajo. Los pagos, importes, monedas, comprobantes registrados y metadatos de entrega no se eliminan ni se reescriben: quedan para consulta y eventual gestión manual de administración.

El barrido periódico confirma por REST la presencia del creador para recuperar salidas ocurridas mientras el servicio estuvo desconectado. Solo `Unknown Member` confirma que se fue; un fallo de permisos, timeout o caída de Discord no autoriza borrar el canal. Los cierres que fallan por permisos quedan silenciados y se vuelven a intentar sin avisar al ticket. Los canales eliminados manualmente también cancelan su bandeja. Los archivos eliminados de un ticket cuyo creador sigue presente continúan en revisión humana.

Los resultados tardíos de IA, errores de envío y confirmaciones de entrega no reabren estados `salio`/`cerrado` ni reactivan la bandeja `cancelled`. No se espera una lectura IA de hasta 90 segundos para iniciar el cierre. Al salir se cierra el ticket; un eventual regreso del miembro no revive ese canal ni acredita automáticamente pagos antiguos en otra compra.

## Configuración y ejecución

Variables privadas: `DISCORD_TOKEN`, `DATABASE_URL`, `GEMINI_API_KEY_1`, `GEMINI_API_KEY_2` y `PORT` (opcional, 8080). Nunca guardar sus valores en Git.

```sh
pip install -r requirements.txt
python scripts/prepare_ocr.py
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

La conversación de compra, por ejemplo «Quiero adquirir el diamante», continúa con Gemini. El chat prioriza 3.6 Flash y conserva 3.8/3.7 como respaldos: en la comprobación del 03/11/2026, 3.8 y 3.7 agotaban su tiempo mientras 3.6 respondía con ambas claves. La lectura de comprobantes mantiene el orden 3.8/3.7/3.6. Ante `ServiceUnavailable` (503), ese modelo espera 120 segundos antes de volver a probarse con cualquier clave; los respaldos siguen disponibles. Los errores de cuota conservan su espera por clave. Los logs registran modelo, índice de clave, tipo de resultado y duración, sin copiar claves ni conversaciones. Una indisponibilidad de Google todavía puede agotar los respaldos; cambiar claves válidas no garantiza resolverla.

`ticket_inbox` guarda IDs y estados, no el texto de las conversaciones. Los mensajes en trámite tienen una reserva temporal; si se reinicia el proceso, se recuperan al vencerla. Los eventos duplicados ya completados no se procesan nuevamente. La revisión de canales y bienvenidas corre al reconectar y cada cinco minutos, en lotes limitados. Un ticket con respuesta pendiente no se borra por inactividad; la salida confirmada de su creador aplica el cierre definitivo descrito arriba.

Si Gemini falla u omite archivos, un respaldo local Tesseract lee las imágenes y PDF. El modelo de 4 MB se prepara durante el build, desde una revisión fija con SHA-256 verificado. El OCR procesa cada archivo en un proceso con límite de tiempo y usa el mismo validador, registro y entrega. El texto extraído no se publica ni se guarda en logs. Una lectura explícitamente pendiente o de un medio retirado no se aprueba mediante el respaldo.

Cuando fallan técnicamente los lectores, el comprobante permanece `pending` con el error `ReceiptProcessingUnavailable`. Los reintentos se espacian hasta una hora y continúan tras reinicios, sin agotar el límite de tres lecturas ni repetir el aviso a administración en cada intento. El barrido cada cinco minutos retoma el archivo de Discord y actualiza el mismo mensaje. Los errores de lectura no recuperados y archivos que requieren una decisión humana pasan a `manual` al agotar tres intentos. `/manual`, la salida del creador, el cierre y `/conservar_ticket` conservan sus controles. Un archivo eliminado de Discord requiere intervención humana.

La recuperación comienza con el despliegue de esta versión; no reproduce conversaciones ni comprobantes históricos anteriores. Un archivo eliminado de Discord o un canal sin permisos requiere intervención humana. La recuperación depende de que Discord mantenga el mensaje y el servicio vuelva a ejecutarse.

El modo manual se respeta durante la recuperación. Las consultas demoradas tienen un aviso de recepción y, si falla Google, una respuesta útil y derivación a administración. Un pedido claro de rangos recibe su total real y los datos de pago de respaldo. Los modelos con errores quedan temporalmente en espera para no agotar cuota repitiendo llamadas. El presupuesto incluye la espera por la IA: 35 segundos para texto y 45 para comprobantes, más hasta 45 segundos para OCR serializado.

Durante la campaña temporal se suspenden los avisos semanales. No se emiten anuncios masivos ni menciones generales; las respuestas del modelo también eliminan esas menciones.

## Recuperación entre pago, acceso y confirmación

La recuperación comprueba también pagos guardados sin entrega preparada y entregas interrumpidas durante el tercer intento. En ese último caso verifica los roles reales: confirma los que ya existen o deriva a administración, sin hacer una cuarta asignación. Si administración entrega el rango manualmente y el ticket sigue en automático, verifica ese acceso y concilia la entrega sin otro cobro ni reasignación; así otros rangos pagados pueden continuar. Los fallos tardíos no sobrescriben una entrega completada. Los permisos y el modo manual se respetan al retomar cada caso; un ticket con error no frena los demás.

`pagos.aviso_pendiente` conserva las confirmaciones y derivaciones a administración que todavía no pudieron enviarse. El aviso se recupera sin volver a cobrar ni asignar roles. Los pagos pendientes sin rango definido reciben una sola consulta; no se repite cada cinco minutos. Esta marca se activa en operaciones nuevas y no reanuncia el historial previo al despliegue. Un aviso sin confirmar protege el ticket frente al borrado por inactividad.

Una respuesta de IA que omite un archivo o no logra leer sus datos se reintenta conservando los comprobantes ya registrados. Los archivos que ya constan en Neon no vuelven a enviarse a la IA. Si desaparece el original de Discord, queda para revisión humana, nunca como lectura completada. Los importes ARS escritos con miles (`4.700`) se interpretan como 4700; los importes USD y USDT conservan su interpretación decimal. Mercado Pago y Ualá se reconocen como transferencias ARS.

Un pedido de varios rangos puede pagarse con distintas monedas: cada rango debe quedar cubierto íntegramente por su precio en la moneda registrada. No se convierten monedas ni se suman para cubrir un mismo rango. Si la combinación no alcanza o es ambigua, se conservan los importes y se pide revisión sin indicar un faltante inventado.

Ante un reclamo de acceso de una compra con entrega registrada, el bot consulta Discord y recupera únicamente los roles documentados si faltan. Las consultas breves de un cliente que espera reciben el estado real sin reiniciar la venta ni depender de Gemini. La reparación no reconstruye compras históricas sin una entrega identificada ni modifica permisos generales del servidor.

## Operación gratuita y salud

El supervisor recrea el cliente después de fallos transitorios, cierra el pool anterior y conserva los trabajos en Neon. Un módulo esencial que no carga aborta ese cliente; no se sincroniza un árbol incompleto de comandos. Las dependencias directas se fijan a versiones verificadas.

`/` y `/readyz` devuelven 200 solo con conexión Discord, base comprobada, tickets cargados y comandos sincronizados; en caso contrario devuelven 503. `/healthz` comprueba la actividad del proceso/event loop. La base se comprueba cada cinco minutos, sin consultas por cada health check HTTP. Los endpoints exponen únicamente estados, sin credenciales ni conversaciones.

Render Free puede suspender servicios por inactividad o al consumir sus horas incluidas; Gemini gratuito también tiene cuotas y disponibilidad variable. Estas mejoras recuperan trabajo al volver a estar disponible el servicio y reducen silencios; no ofrecen disponibilidad continua garantizada. No se agregan servicios pagos ni mecanismos artificiales para impedir la suspensión. Referencias: https://render.com/docs/free y https://ai.google.dev/gemini-api/docs/rate-limits.

## Precios definitivos en pesos

Diamante $5.900 ARS, Oro $5.300 ARS y Plata $2.300 ARS; los tres $13.500 ARS. Peticiones: $4.000 ARS, solo tras confirmación de administración. Estos precios se mantienen al retirar campañas internacionales temporales.

## Precios internacionales definitivos

Desde el 03/11/2026, Diamante 5,50 USDT, Oro 5,00 USDT y Plata 2,50 USDT; los tres 13,00 USDT. Peticiones: 4 USDT. Estos son los precios normales de Binance Pay. No se cambian importes históricos ni entregas ya preparadas.

## Política de cobro permanente

Desde el 06/11/2026, solo se aceptan transferencias en pesos argentinos a CBU/CVU y Binance Pay en USDT. Las peticiones cuestan 4.000 ARS o 4 USDT y requieren confirmación previa de administración. El titular bancario solo se confirma si el cliente pregunta explícitamente por el titular de una transferencia en pesos.

Los comprobantes nuevos del medio retirado se derivan a revisión; no se registran como nuevos pagos. El historial en USD y su marca de política se conservan para consultas, finanzas y verificación de entregas ya preparadas. Un saldo histórico sin asignación requiere revisión manual y no financia una compra nueva. No se modifican importes, métodos ni filas históricas.

Para refrescar mensajes existentes del bot, ejecutar scripts/refresh_payment_messages.py --apply después del despliegue. El script guarda copia local, comprueba la autoría y desactiva las menciones generales. Los anuncios escritos por el propietario se editan manualmente.
