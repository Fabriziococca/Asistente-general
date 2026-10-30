"""Bandeja de mensajes por ID: recuperable sin copiar conversaciones a Neon."""

from receipts import attachment_mime

MAX_RECEIPT_ATTEMPTS = 3


class ReceiptReviewRequired(ValueError):
    """El archivo requiere decisión humana, no otra llamada automática a la IA."""


SCHEMA = """
ALTER TABLE tickets ADD COLUMN IF NOT EXISTS bienvenida_id BIGINT;
ALTER TABLE tickets ADD COLUMN IF NOT EXISTS mensaje_cursor BIGINT;
CREATE TABLE IF NOT EXISTS ticket_inbox (
    message_id BIGINT PRIMARY KEY, channel_id BIGINT NOT NULL, guild_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL, state TEXT NOT NULL DEFAULT 'pending', attempts INT NOT NULL DEFAULT 0,
    retry_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    reply_id BIGINT, error_type TEXT, updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
ALTER TABLE ticket_inbox ADD COLUMN IF NOT EXISTS is_receipt BOOLEAN NOT NULL DEFAULT FALSE;
CREATE INDEX IF NOT EXISTS ticket_inbox_pending ON ticket_inbox(retry_at)
    WHERE state IN ('pending','processing');
CREATE INDEX IF NOT EXISTS ticket_inbox_receipt_status ON ticket_inbox(channel_id,user_id,message_id)
    WHERE is_receipt AND state IN ('pending','processing','manual');
CREATE TABLE IF NOT EXISTS bot_task_state (name TEXT PRIMARY KEY, last_run TIMESTAMPTZ NOT NULL);
"""


class TicketInbox:
    def __init__(self, pool):
        self.pool = pool

    async def initialize(self):
        async with self.pool.acquire(timeout=15) as conn:
            async with conn.transaction():
                await conn.execute(SCHEMA)
                return await conn.fetchval("""INSERT INTO bot_task_state(name,last_run)
                    VALUES ('inbox_start',CURRENT_TIMESTAMP) ON CONFLICT(name) DO UPDATE
                    SET name=EXCLUDED.name RETURNING last_run""")

    async def record(self, message):
        is_receipt = any(attachment_mime(a) for a in getattr(message, 'attachments', []))
        async with self.pool.acquire(timeout=10) as conn:
            await conn.execute("""INSERT INTO ticket_inbox(message_id,channel_id,guild_id,user_id,is_receipt)
                VALUES ($1,$2,$3,$4,$5) ON CONFLICT(message_id) DO UPDATE
                SET is_receipt=TRUE WHERE NOT ticket_inbox.is_receipt AND EXCLUDED.is_receipt""",
                message.id, message.channel.id, message.guild.id, message.author.id, is_receipt)

    async def claim(self, message_id):
        async with self.pool.acquire(timeout=10) as conn:
            return await conn.fetchrow("""UPDATE ticket_inbox SET state='processing', attempts=attempts+1,
                retry_at=CURRENT_TIMESTAMP+INTERVAL '3 minutes',updated_at=CURRENT_TIMESTAMP
                WHERE message_id=$1 AND state IN ('pending','processing')
                AND retry_at <= CURRENT_TIMESTAMP
                AND (attempts=0 OR NOT EXISTS(SELECT 1 FROM tickets t
                    WHERE t.channel_id=ticket_inbox.channel_id AND t.estado='pausado'))
                RETURNING *""", message_id)

    async def progress(self, message_id, reply_id):
        async with self.pool.acquire(timeout=10) as conn:
            await conn.execute('UPDATE ticket_inbox SET reply_id=$2 WHERE message_id=$1', message_id, reply_id)

    async def manual(self, message_id, error):
        async with self.pool.acquire(timeout=10) as conn:
            await conn.execute("""UPDATE ticket_inbox SET state='manual',error_type=$2,
                updated_at=CURRENT_TIMESTAMP WHERE message_id=$1""", message_id, type(error).__name__)

    async def receipt_status(self, channel_id, user_id):
        async with self.pool.acquire(timeout=10) as conn:
            row = await conn.fetchrow("""SELECT state FROM ticket_inbox
                WHERE channel_id=$1 AND user_id=$2 AND is_receipt
                AND state IN ('pending','processing','manual')
                ORDER BY CASE WHEN state='manual' THEN 1 ELSE 0 END, message_id LIMIT 1""", channel_id, user_id)
        return ('manual' if row['state']=='manual' else 'pending') if row else None

    async def done(self, message_id, reply_id=None):
        async with self.pool.acquire(timeout=10) as conn:
            await conn.execute("""UPDATE ticket_inbox SET state='done',reply_id=COALESCE($2,reply_id),
                updated_at=CURRENT_TIMESTAMP,error_type=NULL WHERE message_id=$1""", message_id, reply_id)

    async def retry(self, message_id, error):
        async with self.pool.acquire(timeout=10) as conn:
            await conn.execute("""UPDATE ticket_inbox SET state='pending',error_type=$2,
                retry_at=CURRENT_TIMESTAMP+make_interval(secs => LEAST(3600,30*power(2,LEAST(attempts,7)))),updated_at=CURRENT_TIMESTAMP
                WHERE message_id=$1""", message_id, type(error).__name__)

    async def pending(self):
        async with self.pool.acquire(timeout=10) as conn:
            return await conn.fetch("""SELECT i.* FROM ticket_inbox i LEFT JOIN tickets t ON t.channel_id=i.channel_id
                WHERE i.state IN ('pending','processing') AND i.retry_at <= CURRENT_TIMESTAMP
                AND (i.attempts=0 OR t.estado IS DISTINCT FROM 'pausado') ORDER BY i.message_id LIMIT 30""")

    async def cursor(self, channel_id):
        async with self.pool.acquire(timeout=10) as conn:
            return await conn.fetchval('SELECT mensaje_cursor FROM tickets WHERE channel_id=$1', channel_id)

    async def advance(self, channel_id, cursor):
        async with self.pool.acquire(timeout=10) as conn:
            await conn.execute("""UPDATE tickets SET mensaje_cursor=GREATEST(COALESCE(mensaje_cursor,0),$2)
                WHERE channel_id=$1""", channel_id, cursor)

    async def counts(self):
        async with self.pool.acquire(timeout=10) as conn:
            return await conn.fetchval("SELECT count(*) FROM ticket_inbox WHERE state IN ('pending','processing')")
