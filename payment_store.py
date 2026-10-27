"""Registro atómico de comprobantes y entregas recuperables sobre el esquema existente."""

from decimal import Decimal
from uuid import uuid4

from receipts import PRICES, choose_roles


SCHEMA = """
ALTER TABLE tickets ADD COLUMN IF NOT EXISTS conservar BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE tickets ADD COLUMN IF NOT EXISTS rangos_solicitados TEXT[];
ALTER TABLE pagos ADD COLUMN IF NOT EXISTS comprobante_hash TEXT;
ALTER TABLE pagos ADD COLUMN IF NOT EXISTS operacion_key TEXT;
ALTER TABLE pagos ADD COLUMN IF NOT EXISTS guild_id BIGINT;
ALTER TABLE pagos ADD COLUMN IF NOT EXISTS entrega_id TEXT;
ALTER TABLE pagos ADD COLUMN IF NOT EXISTS roles_asignados TEXT[];
ALTER TABLE pagos ADD COLUMN IF NOT EXISTS entrega_estado TEXT;
ALTER TABLE pagos ADD COLUMN IF NOT EXISTS entrega_intentos INTEGER NOT NULL DEFAULT 0;
ALTER TABLE pagos ADD COLUMN IF NOT EXISTS entrega_actualizada TIMESTAMPTZ;
CREATE UNIQUE INDEX IF NOT EXISTS pagos_comprobante_hash_unique
    ON pagos (comprobante_hash) WHERE comprobante_hash IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS pagos_operacion_key_unique
    ON pagos (operacion_key) WHERE operacion_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS pagos_entrega_pendiente
    ON pagos (entrega_estado) WHERE entrega_estado IN ('preparado', 'reintentar');
"""


class PaymentStore:
    def __init__(self, pool):
        self.pool = pool

    async def initialize(self):
        async with self.pool.acquire(timeout=15) as conn:
            async with conn.transaction():
                await conn.execute(SCHEMA)

    async def known(self, digest):
        async with self.pool.acquire(timeout=10) as conn:
            return await conn.fetchrow("SELECT * FROM pagos WHERE comprobante_hash = $1", digest)

    async def summary(self, ticket_id, user_id):
        async with self.pool.acquire(timeout=10) as conn:
            rows = await conn.fetch("""SELECT entrega_estado,roles_asignados FROM pagos
                WHERE ticket_id=$1 AND user_id=$2 ORDER BY pago_id DESC""", ticket_id, user_id)
            wanted = await conn.fetchval("SELECT rangos_solicitados FROM tickets WHERE channel_id=$1", ticket_id)
        states = {row['entrega_estado'] for row in rows}
        if states & {'preparado','reintentar','manual'}:
            state = 'delivery_pending'
        elif 'pendiente' in states or (rows and wanted):
            state = 'receipt_pending'
        elif 'entregado' in states:
            state = 'delivered'
        else:
            state = 'unknown'
        return {'state': state, 'roles': list(dict.fromkeys(
            name for row in rows for name in (row['roles_asignados'] or []))),
            'pending_roles': list(wanted or [])}

    async def save(self, message, receipts, digests, suggestion=False):
        """No se asigna acceso antes de guardar. Los reenvíos no agregan ingresos."""
        rows = []
        async with self.pool.acquire(timeout=10) as conn:
            async with conn.transaction():
                await conn.execute("SELECT pg_advisory_xact_lock($1)", message.channel.id)
                await conn.execute("INSERT INTO usuarios (user_id) VALUES ($1) ON CONFLICT DO NOTHING", message.author.id)
                for receipt in receipts:
                    row = await conn.fetchrow("""
                        INSERT INTO pagos (user_id, ticket_id, monto, moneda, concepto, metodo_pago,
                            comprobante_hash, operacion_key, guild_id, entrega_estado)
                        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
                        ON CONFLICT DO NOTHING RETURNING *
                    """, message.author.id, message.channel.id, receipt.amount, receipt.currency,
                        "Sugerencia" if suggestion else "Pendiente de rango", receipt.method,
                        digests[receipt.index], receipt.operation_key, message.guild.id,
                        "entregado" if suggestion else "pendiente")
                    if not row:
                        row = await conn.fetchrow("""SELECT * FROM pagos
                            WHERE comprobante_hash = $1 OR (operacion_key IS NOT NULL AND operacion_key = $2)
                            LIMIT 1""", digests[receipt.index], receipt.operation_key)
                    rows.append(row)
        return rows

    async def _remember(self, conn, ticket_id, user_id, desired):
        ticket = await conn.fetchrow("SELECT rangos_solicitados FROM tickets WHERE channel_id=$1 FOR UPDATE", ticket_id)
        delivered = await conn.fetch("""SELECT roles_asignados FROM pagos
            WHERE ticket_id=$1 AND user_id=$2 AND entrega_estado='entregado'""", ticket_id, user_id)
        delivered_roles = {name for row in delivered for name in (row['roles_asignados'] or [])}
        # "Oro" junto al segundo comprobante NO cancela el Diamante pendiente.
        # La intención guardada sobrevive a mensajes, reintentos y reinicios.
        wanted = set((ticket['rangos_solicitados'] or ()) if ticket else ()) | set(desired)
        remaining = tuple(name for name in PRICES if name in wanted - delivered_roles)
        await conn.execute("""UPDATE tickets SET rangos_solicitados=$2,
            estado=CASE WHEN cardinality($2::text[]) > 0 AND estado IS DISTINCT FROM 'pausado'
                THEN 'abierto' ELSE estado END WHERE channel_id=$1""",
            ticket_id, list(remaining) or None)
        return remaining

    async def remember(self, ticket_id, user_id, desired):
        """Persiste el pedido antes de leer archivos o llamar a la IA."""
        async with self.pool.acquire(timeout=10) as conn:
            async with conn.transaction():
                await conn.execute("SELECT pg_advisory_xact_lock($1)", ticket_id)
                return await self._remember(conn, ticket_id, user_id, desired)

    async def prepare(self, ticket_id, user_id, desired=()):
        async with self.pool.acquire(timeout=10) as conn:
            async with conn.transaction():
                await conn.execute("SELECT pg_advisory_xact_lock($1)", ticket_id)
                desired = await self._remember(conn, ticket_id, user_id, desired)
                pending = await conn.fetchrow("""SELECT * FROM pagos WHERE ticket_id=$1 AND user_id=$2
                    AND entrega_estado IN ('preparado','reintentar','manual') ORDER BY pago_id LIMIT 1""", ticket_id, user_id)
                if pending:
                    return {"delivery": dict(pending)}
                rows = await conn.fetch("""SELECT * FROM pagos WHERE ticket_id=$1 AND user_id=$2
                    AND entrega_estado='pendiente' ORDER BY pago_id FOR UPDATE""", ticket_id, user_id)
                if not rows:
                    return {"empty": True}
                totals = {}
                for row in rows:
                    totals[row['moneda']] = totals.get(row['moneda'], Decimal(0)) + row['monto']
                # Nunca se suman ARS y USD entre sí.
                for currency, amount in totals.items():
                    roles, missing = choose_roles(amount, currency, desired)
                    if not roles:
                        continue
                    delivery_id = str(uuid4())
                    ids = [row['pago_id'] for row in rows if row['moneda'] == currency]
                    await conn.execute("""UPDATE pagos SET entrega_id=$2, roles_asignados=$3,
                        concepto=$4, entrega_estado='preparado', entrega_actualizada=CURRENT_TIMESTAMP
                        WHERE pago_id=ANY($1::int[])""", ids, delivery_id, list(roles), ", ".join(roles))
                    row = await conn.fetchrow("SELECT * FROM pagos WHERE entrega_id=$1 ORDER BY pago_id LIMIT 1", delivery_id)
                    return {"delivery": dict(row)}
                currency = next(iter(totals))
                _, missing = choose_roles(totals[currency], currency, desired)
                return {"totals": totals, "missing": missing, "desired": tuple(desired)}

    async def attempt(self, delivery_id):
        async with self.pool.acquire(timeout=10) as conn:
            return await conn.fetchval("""UPDATE pagos SET entrega_intentos=entrega_intentos+1,
                entrega_actualizada=CURRENT_TIMESTAMP WHERE entrega_id=$1
                AND entrega_estado IN ('preparado','reintentar') RETURNING entrega_intentos""", delivery_id)

    async def finish(self, delivery_id, ticket_id):
        async with self.pool.acquire(timeout=10) as conn:
            async with conn.transaction():
                await conn.execute("SELECT pg_advisory_xact_lock($1)", ticket_id)
                delivery = await conn.fetchrow("""SELECT roles_asignados FROM pagos
                    WHERE entrega_id=$1 AND ticket_id=$2 LIMIT 1""", delivery_id, ticket_id)
                if not delivery:
                    raise ValueError("Entrega sin comprobantes en este ticket")
                ticket = await conn.fetchrow("SELECT rangos_solicitados FROM tickets WHERE channel_id=$1 FOR UPDATE", ticket_id)
                assigned = set(delivery['roles_asignados'] or ())
                remaining = tuple(name for name in (ticket['rangos_solicitados'] or ()) if name not in assigned) if ticket else ()
                await conn.execute("UPDATE pagos SET entrega_estado='entregado', entrega_actualizada=CURRENT_TIMESTAMP WHERE entrega_id=$1", delivery_id)
                pending = await conn.fetchval("""SELECT EXISTS(SELECT 1 FROM pagos WHERE ticket_id=$1
                    AND entrega_estado IN ('pendiente','preparado','reintentar','manual'))""", ticket_id)
                await conn.execute("""UPDATE tickets SET estado=CASE WHEN estado='pausado' THEN estado
                    WHEN $3 THEN 'abierto' ELSE 'completado' END, rangos_solicitados=$2,
                    ultimo_mensaje=CURRENT_TIMESTAMP WHERE channel_id=$1""",
                    ticket_id, list(remaining) or None, bool(remaining) or pending)
                return {'remaining': remaining, 'pending': pending}

    async def completed(self, ticket_id, user_id):
        async with self.pool.acquire(timeout=10) as conn:
            return await conn.fetch("""SELECT DISTINCT ON (entrega_id) * FROM pagos
                WHERE ticket_id=$1 AND user_id=$2 AND entrega_estado='entregado'
                AND entrega_id IS NOT NULL ORDER BY entrega_id,pago_id""", ticket_id, user_id)

    async def reopen(self, delivery_id, ticket_id, user_id):
        """Revisa acceso de una entrega registrada sin sumar otro pago ni inventar rangos."""
        async with self.pool.acquire(timeout=10) as conn:
            async with conn.transaction():
                await conn.execute("SELECT pg_advisory_xact_lock($1)", ticket_id)
                rows = await conn.fetch("""UPDATE pagos SET entrega_estado='preparado',
                    entrega_intentos=0, entrega_actualizada=CURRENT_TIMESTAMP
                    WHERE entrega_id=$1 AND ticket_id=$2 AND user_id=$3 AND entrega_estado='entregado'
                    RETURNING *""", delivery_id, ticket_id, user_id)
                return dict(rows[0]) if rows else None

    async def failed(self, delivery_id, attempts):
        async with self.pool.acquire(timeout=10) as conn:
            await conn.execute("UPDATE pagos SET entrega_estado=$2, entrega_actualizada=CURRENT_TIMESTAMP WHERE entrega_id=$1",
                               delivery_id, 'manual' if attempts >= 3 else 'reintentar')

    async def recoverable(self):
        async with self.pool.acquire(timeout=10) as conn:
            return await conn.fetch("""SELECT DISTINCT ON (p.entrega_id) p.* FROM pagos p
                JOIN tickets t ON t.channel_id=p.ticket_id
                WHERE p.entrega_estado IN ('preparado','reintentar') AND p.entrega_intentos < 3
                AND p.entrega_actualizada < CURRENT_TIMESTAMP - INTERVAL '2 minutes'
                AND t.estado IS DISTINCT FROM 'pausado'
                ORDER BY p.entrega_id, p.pago_id LIMIT 25""")
