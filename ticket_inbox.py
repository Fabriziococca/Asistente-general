"""Bandeja de mensajes por ID: recuperable sin copiar conversaciones a Neon."""

SCHEMA = """
ALTER TABLE tickets ADD COLUMN IF NOT EXISTS bienvenida_id BIGINT;
ALTER TABLE tickets ADD COLUMN IF NOT EXISTS mensaje_cursor BIGINT;
CREATE TABLE IF NOT EXISTS ticket_inbox (
    message_id BIGINT PRIMARY KEY, channel_id BIGINT NOT NULL, guild_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL, state TEXT NOT NULL DEFAULT 'pending', attempts INT NOT NULL DEFAULT 0,
    retry_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    reply_id BIGINT, error_type TEXT, updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ticket_inbox_pending ON ticket_inbox(retry_at)
    WHERE state IN ('pending','processing');
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
        async with self.pool.acquire(timeout=10) as conn:
            await conn.execute("""INSERT INTO ticket_inbox(message_id,channel_id,guild_id,user_id)
                VALUES ($1,$2,$3,$4) ON CONFLICT DO NOTHING""",
                message.id, message.channel.id, message.guild.id, message.author.id)

    async def claim(self, message_id):
        async with self.pool.acquire(timeout=10) as conn:
            return await conn.fetchrow("""UPDATE ticket_inbox SET state='processing', attempts=attempts+1,
                retry_at=CURRENT_TIMESTAMP+INTERVAL '3 minutes',updated_at=CURRENT_TIMESTAMP
                WHERE message_id=$1 AND state IN ('pending','processing')
                AND retry_at <= CURRENT_TIMESTAMP RETURNING *""", message_id)

    async def done(self, message_id, reply_id=None):
        async with self.pool.acquire(timeout=10) as conn:
            await conn.execute("""UPDATE ticket_inbox SET state='done',reply_id=$2,
                updated_at=CURRENT_TIMESTAMP,error_type=NULL WHERE message_id=$1""", message_id, reply_id)

    async def retry(self, message_id, error):
        async with self.pool.acquire(timeout=10) as conn:
            await conn.execute("""UPDATE ticket_inbox SET state='pending',error_type=$2,
                retry_at=CURRENT_TIMESTAMP+make_interval(secs => LEAST(3600,30*power(2,LEAST(attempts,7)))),updated_at=CURRENT_TIMESTAMP
                WHERE message_id=$1""", message_id, type(error).__name__)

    async def pending(self):
        async with self.pool.acquire(timeout=10) as conn:
            return await conn.fetch("""SELECT * FROM ticket_inbox WHERE state IN ('pending','processing')
                AND retry_at <= CURRENT_TIMESTAMP ORDER BY message_id LIMIT 30""")

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
