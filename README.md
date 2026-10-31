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

Los errores técnicos de lectura (IA, descarga, base o respuesta incompleta) dejan el comprobante pendiente, con el tipo de error, hasta tres intentos totales. El barrido cada cinco minutos retoma el archivo original de Discord y actualiza el mismo aviso; no requiere que el cliente lo reenvíe. Los archivos que requieren decisión humana y los intentos agotados quedan en estado `manual`, nunca como lectura completada, y mantienen el ticket protegido. `/manual` pausa los reintentos sin consumirlos; `/automatico` permite retomarlos. Un rango escrito después del archivo se guarda y responde localmente con el estado real de lectura, aunque la foto haya quedado fuera del historial reciente. El éxito, los pagos y las asignaciones conservan sus controles de idempotencia.

La recuperación comienza con el despliegue de esta versión; no reproduce conversaciones ni comprobantes históricos anteriores. Un archivo eliminado de Discord o un canal sin permisos requiere intervención humana. La recuperación depende de que Discord mantenga el mensaje y el servicio vuelva a ejecutarse.

El modo manual se respeta durante la recuperación. Las consultas demoradas tienen un aviso de recepción y, si falla Google, una respuesta útil y derivación a administración. Los modelos con errores quedan temporalmente en espera para no agotar cuota repitiendo llamadas. El presupuesto incluye la espera por la IA: 35 segundos para texto y 90 para comprobantes.

Durante la campaña temporal se suspenden los avisos semanales. No se emiten anuncios masivos ni menciones generales; las respuestas del modelo también eliminan esas menciones.

## Recuperación entre pago, acceso y confirmación

La recuperación comprueba también pagos guardados sin entrega preparada y entregas interrumpidas durante el tercer intento. En ese último caso verifica los roles reales: confirma los que ya existen o deriva a administración, sin hacer una cuarta asignación. Si administración entrega el rango manualmente y el ticket sigue en automático, verifica ese acceso y concilia la entrega sin otro cobro ni reasignación; así otros rangos pagados pueden continuar. Los fallos tardíos no sobrescriben una entrega completada. Los permisos y el modo manual se respetan al retomar cada caso; un ticket con error no frena los demás.

`pagos.aviso_pendiente` conserva las confirmaciones y derivaciones a administración que todavía no pudieron enviarse. El aviso se recupera sin volver a cobrar ni asignar roles. Los pagos pendientes sin rango definido reciben una sola consulta; no se repite cada cinco minutos. Esta marca se activa en operaciones nuevas y no reanuncia el historial previo al despliegue. Un aviso sin confirmar protege el ticket frente al borrado automático.

Una respuesta de IA que omite un archivo o no logra leer sus datos se reintenta conservando los comprobantes ya registrados. Los archivos que ya constan en Neon no vuelven a enviarse a la IA. Si desaparece el original de Discord, queda para revisión humana, nunca como lectura completada. Los importes ARS escritos con miles (`4.700`) se interpretan como 4700; los importes USD y USDT conservan su interpretación decimal. Mercado Pago y Ualá se reconocen como transferencias ARS.

Un pedido de varios rangos puede pagarse con distintas monedas: cada rango debe quedar cubierto íntegramente por su precio en la moneda registrada. No se convierten monedas ni se suman para cubrir un mismo rango. Si la combinación no alcanza o es ambigua, se conservan los importes y se pide revisión sin indicar un faltante inventado.

Ante un reclamo de acceso de una compra con entrega registrada, el bot consulta Discord y recupera únicamente los roles documentados si faltan. Las consultas breves de un cliente que espera reciben el estado real sin reiniciar la venta ni depender de Gemini. La reparación no reconstruye compras históricas sin una entrega identificada ni modifica permisos generales del servidor.

## Operación gratuita y salud

El supervisor recrea el cliente después de fallos transitorios, cierra el pool anterior y conserva los trabajos en Neon. Un módulo esencial que no carga aborta ese cliente; no se sincroniza un árbol incompleto de comandos. Las dependencias directas se fijan a versiones verificadas.

`/` y `/readyz` devuelven 200 solo con conexión Discord, base comprobada, tickets cargados y comandos sincronizados; en caso contrario devuelven 503. `/healthz` comprueba la actividad del proceso/event loop. La base se comprueba cada cinco minutos, sin consultas por cada health check HTTP. Los endpoints exponen únicamente estados, sin credenciales ni conversaciones.

Render Free puede suspender servicios por inactividad o al consumir sus horas incluidas; Gemini gratuito también tiene cuotas y disponibilidad variable. Estas mejoras recuperan trabajo al volver a estar disponible el servicio y reducen silencios; no ofrecen disponibilidad continua garantizada. No se agregan servicios pagos ni mecanismos artificiales para impedir la suspensión. Referencias: https://render.com/docs/free y https://ai.google.dev/gemini-api/docs/rate-limits.

## Precios definitivos en pesos

Diamante $5.900 ARS, Oro $5.300 ARS y Plata $2.300 ARS; los tres $13.500 ARS. Peticiones: $4.000 ARS, solo tras confirmación de administración. Estos precios se mantienen al retirar campañas internacionales temporales.

## Campaña temporal internacional: PayPal en USD

Durante siete días, desde `CAMPAIGN_START` hasta `CAMPAIGN_END` (límite excluido, UTC), solo se ofrecen transferencias ARS y PayPal en USD. El cierre figura con fecha y hora argentina en precios, respuestas y bienvenidas; no se reinicia el plazo al reconectar. Los precios promocionales de rangos son Diamante 2,00 USD, Oro 1,75 USD, Plata 1,50 USD; los tres 5,25 USD. El descuento no aplica a ARS ni a peticiones: estas cuestan 4.000 ARS o 4 USD y requieren confirmación previa de administración.

El único dato público de cobro PayPal es `sesarjavier28@gmail.com`. Los alias principales y el titular se usan internamente para leer comprobantes. La identidad bancaria solo se confirma ante una consulta explícita sobre transferencias ARS; no se revela al preguntar por PayPal. Si un comprobante muestra moneda local y una equivalencia USD explícita, se usa esa equivalencia; no se calcula una conversión ni se descuentan comisiones.

Los pagos nuevos de la campaña se marcan con `pagos.politica_precio`. La columna es aditiva y no reescribe el historial. USD de PayPal anterior a la campaña sin esta marca no financia nuevas compras. Las entregas ya registradas en USDT mantienen compatibilidad defensiva, pero durante la campaña no se aceptan comprobantes nuevos de ese medio. La deduplicación, acumulación de pagos y verificación de todos los rangos permanecen activas.

Al vencer, las respuestas y los pagos nuevos vuelven automáticamente al modo normal: precios ARS definitivos y Binance Pay (Diamante 4,50 USDT, Oro 4 USDT, Plata 2 USDT; petición 4 USDT). Las bienvenidas propias identificadas en tickets abiertos se actualizan en el barrido periódico, sin enviar otro mensaje. Los pagos PayPal registrados con la marca de campaña todavía pueden terminar su entrega o recuperar un aviso después del cierre. Un comprobante nuevo de un medio ya cerrado requiere revisión humana, sin pedir otro pago.

El aumento definitivo de ARS está en `c42d17a` y la campaña temporal en `50e3f2a`; los ajustes posteriores de su texto se retiran junto con la campaña. Para retirar también su código cuando el propietario pida modo normal: comprobar primero que no quedan pagos/entregas/avisos USD ni lecturas de comprobantes sin resolver; completar o derivar esos casos, revertir primero los ajustes posteriores de textos de campaña y luego `50e3f2a`, probar, publicar y ejecutar `scripts/refresh_payment_messages.py --apply --all-channels`. No borrar columnas ni pagos. La reversión restaura los precios normales USDT y conserva 5.900/5.300/2.300 ARS. El anuncio escrito por el propietario se entrega como propuesta para copiar manualmente; el script no edita mensajes ajenos.

Los pagos históricos conservan su moneda, importe y método originales. `/finanzas` separa cada moneda y presenta importes brutos, sin conversiones, comisiones estimadas ni afirmaciones sobre acreditación bancaria. Los servicios gratuitos y el pool de modelos no cambian.
