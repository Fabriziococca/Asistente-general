"""Importación aditiva del libro local, sin acciones ni mensajes en Discord."""

from datetime import datetime
from decimal import Decimal
import json
import sqlite3


class ImportConflict(ValueError):
    pass


async def import_sales(db, conn, expected_guild):
    guild, since = db.execute('SELECT guild,since FROM config WHERE id=1').fetchone()
    if guild != expected_guild:
        raise ValueError('El servidor no coincide con el registro local')
    db.execute("""CREATE TABLE IF NOT EXISTS sale_import_conflicts(
        channel INTEGER PRIMARY KEY, reason TEXT NOT NULL)""")
    channels = [r[0] for r in db.execute('SELECT DISTINCT channel FROM sale_payments WHERE imported=0 ORDER BY channel')]
    count = 0
    for channel in channels:
        cursor = db.execute("""SELECT p.*,d.roles,d.done FROM sale_payments p
            LEFT JOIN sale_deliveries d ON d.id=p.delivery WHERE p.channel=? AND p.imported=0""",(channel,))
        cursor.row_factory = sqlite3.Row
        rows = cursor.fetchall()
        try:
            async with conn.transaction():
                await conn.execute("SET LOCAL TIME ZONE 'UTC'")
                await conn.execute('SELECT pg_advisory_xact_lock($1)',channel)
                for row in rows:
                    if channel < since or row['guild'] != guild:
                        raise ImportConflict('El pago no corresponde a la contingencia del servidor')
                    await import_payment(conn,row)
        except ImportConflict as exc:
            db.execute('INSERT OR REPLACE INTO sale_import_conflicts VALUES (?,?)',(channel,str(exc)))
            continue
        # Si Windows se reinicia entre ambos commits, se comparan las filas ya importadas.
        db.executemany('UPDATE sale_payments SET imported=1 WHERE digest=?',[(r['digest'],) for r in rows])
        db.execute('DELETE FROM sale_import_conflicts WHERE channel=?',(channel,))
        count += len(rows)
    await import_review_records(db,conn,guild,since)
    return count


async def import_review_records(db, conn, guild, since):
    """Conserva las verificaciones humanas sin inventar importes de ingresos."""
    notes = [(mid,ch,'revision',{'motivo':reason}) for mid,ch,reason in db.execute(
        'SELECT message,channel,reason FROM sale_reviews')]
    for mid,ch,buyer,staff,roles,done in db.execute('SELECT receipt,channel,buyer,staff,roles,done FROM grants'):
        notes.append((mid,ch,'entrega_manual',{'comprador':buyer,'administrador':staff,
                     'rangos':roles.split(','),'confirmada':bool(done),
                     'archivos':[r[0] for r in db.execute('SELECT digest FROM receipt_files WHERE receipt=? ORDER BY digest',(mid,))]}))
    if not notes:
        return
    async with conn.transaction():
        await conn.execute("""CREATE TABLE IF NOT EXISTS contingencia_revisiones(
            guild_id BIGINT NOT NULL, mensaje_id BIGINT NOT NULL, tipo TEXT NOT NULL, ticket_id BIGINT NOT NULL,
            detalle JSONB NOT NULL, PRIMARY KEY(guild_id,mensaje_id,tipo))""")
        for mid,ch,kind,detail in notes:
            if ch < since:
                raise ImportConflict('Revisión fuera del período local')
            data = json.dumps(detail)
            await conn.execute("""INSERT INTO contingencia_revisiones VALUES ($1,$2,$3,$4,$5::jsonb)
                ON CONFLICT DO NOTHING""",guild,mid,kind,ch,data)
            same = await conn.fetchval("""SELECT ticket_id=$4 AND detalle=$5::jsonb FROM contingencia_revisiones
                WHERE guild_id=$1 AND mensaje_id=$2 AND tipo=$3""",guild,mid,kind,ch,data)
            if not same:
                raise ImportConflict('Revisión local y remota diferentes')


async def import_payment(conn, row):
    roles = row['roles'].split(',') if row['roles'] else None
    state = 'entregado' if row['done'] else 'manual' if roles else 'pendiente'
    amount = Decimal(row['cents']) / 100
    await conn.execute('INSERT INTO usuarios(user_id) VALUES ($1) ON CONFLICT DO NOTHING',row['buyer'])
    await conn.execute("""INSERT INTO tickets(channel_id,user_id,estado,hablo,conservar)
        VALUES ($1,$2,'pausado',TRUE,TRUE) ON CONFLICT DO NOTHING""",row['channel'],row['buyer'])
    owner = await conn.fetchval('SELECT user_id FROM tickets WHERE channel_id=$1',row['channel'])
    if owner is not None and owner != row['buyer']:
        raise ImportConflict('El titular del ticket en Neon es distinto')
    booked = datetime.fromisoformat(row['booked_at'])
    await conn.execute("""INSERT INTO pagos(user_id,ticket_id,monto,moneda,concepto,metodo_pago,
        comprobante_hash,operacion_key,guild_id,entrega_id,roles_asignados,entrega_estado,fecha_pago,
        entrega_actualizada,aviso_pendiente) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,
        $13::timestamptz AT TIME ZONE 'UTC',$13,FALSE) ON CONFLICT DO NOTHING""",
        row['buyer'],row['channel'],amount,row['currency'],', '.join(roles) if roles else 'Pendiente de rango',
        row['method'],row['digest'],row['operation_key'],row['guild'],row['delivery'],roles,state,booked)
    matches = await conn.fetch("""SELECT user_id,ticket_id,monto,moneda,metodo_pago,comprobante_hash,
        operacion_key,guild_id,entrega_id,roles_asignados,entrega_estado FROM pagos
        WHERE comprobante_hash=$1 OR operacion_key=$2""",row['digest'],row['operation_key'])
    expected = (row['buyer'],row['channel'],amount,row['currency'],row['method'],row['digest'],
                row['operation_key'],row['guild'],row['delivery'],roles,state)
    if len(matches)!=1 or tuple(matches[0]) != expected:
        raise ImportConflict('Ya existe en Neon un comprobante u operación con otros datos')
