import unittest
import os
from types import SimpleNamespace as NS

import test_payment_store as payment_fixtures
from ticket_inbox import TicketInbox, SCHEMA


@unittest.skipUnless(os.environ.get('BOT_TEST_DATABASE_URL'), 'PostgreSQL de prueba no configurado')
class InboxDatabaseTests(unittest.IsolatedAsyncioTestCase):
    # Reutiliza el entorno TEMP, no los escenarios de pagos.
    async def asyncSetUp(self):
        await payment_fixtures.PaymentDatabaseTests.asyncSetUp(self)
        await self.conn.execute(SCHEMA.replace('CREATE TABLE IF NOT EXISTS','CREATE TEMP TABLE IF NOT EXISTS'))
        self.inbox=TicketInbox(payment_fixtures.BoundConnection(self.conn))
        await self.inbox.initialize()
        self.message=NS(id=-789,channel=NS(id=-456),guild=NS(id=-567),author=NS(id=-123))

    async def test_claim_completion_and_duplicate_event(self):
        await self.inbox.record(self.message)
        await self.inbox.record(self.message)
        self.assertEqual((await self.inbox.claim(-789))['attempts'],1)
        self.assertIsNone(await self.inbox.claim(-789))
        await self.inbox.done(-789,-999)
        self.assertIsNone(await self.inbox.claim(-789))
        self.assertEqual(await self.inbox.counts(),0)

    async def test_failed_send_survives_restart_and_retries(self):
        await self.inbox.record(self.message)
        await self.inbox.claim(-789)
        await self.inbox.retry(-789,TimeoutError())
        await self.conn.execute("UPDATE ticket_inbox SET retry_at=CURRENT_TIMESTAMP-INTERVAL '1 second'")
        restarted=TicketInbox(payment_fixtures.BoundConnection(self.conn))
        self.assertEqual(len(await restarted.pending()),1)
        self.assertEqual((await restarted.claim(-789))['attempts'],2)

    async def test_crash_lease_expires_without_losing_message(self):
        await self.inbox.record(self.message)
        await self.inbox.claim(-789)
        await self.conn.execute("UPDATE ticket_inbox SET retry_at=CURRENT_TIMESTAMP-INTERVAL '1 second'")
        self.assertEqual(len(await self.inbox.pending()),1)

    async def test_cursor_never_moves_backwards_and_initial_floor_is_stable(self):
        # Los snowflakes reales son positivos.
        await self.inbox.advance(-456,100)
        await self.inbox.advance(-456,90)
        self.assertEqual(await self.inbox.cursor(-456),100)
        first=await self.inbox.initialize()
        self.assertEqual(await self.inbox.initialize(),first)

    async def test_offline_message_is_recovered_and_not_answered_twice(self):
        from test_tickets import fixture
        cog,_,message=fixture()
        cog.bot.pool=payment_fixtures.BoundConnection(self.conn)
        cog.inbox=self.inbox
        cog.payments=self.store
        message.id=1000; message.author.id=-123; message.channel.id=-456; message.guild.id=-567
        message.content='Hola'
        message.channel.hist=[message]
        message.guild.text_channels=[message.channel]
        cog.bot.guilds=[message.guild]
        cog.bot.get_channel=lambda cid: message.channel
        message.channel.fetch_message=lambda mid: self._return_message(message)
        cog._recovery_floor=999
        await self.conn.execute('UPDATE tickets SET bienvenida_id=123 WHERE channel_id=-456')
        await cog._recover_messages()
        self.assertEqual(await self.inbox.counts(),0)
        message.reply.assert_awaited_once()
        await cog._recover_messages()
        message.reply.assert_awaited_once()

    @staticmethod
    async def _return_message(message):
        return message

    async def test_pending_message_prevents_ticket_deletion(self):
        from test_tickets import fixture
        cog,_,message=fixture()
        cog.bot.pool=payment_fixtures.BoundConnection(self.conn)
        message.channel.id=-456
        await self.inbox.record(self.message)
        self.assertFalse(await cog._delete_ticket_if_eligible(message.channel,member_left=True))
        message.channel.delete.assert_not_awaited()

    async def test_manual_resume_preserves_ticket_protection(self):
        from test_tickets import fixture
        from cogs.tickets import Tickets
        from unittest.mock import AsyncMock
        cog,_,message=fixture()
        cog.bot.pool=payment_fixtures.BoundConnection(self.conn)
        message.channel.id=-456
        await self.conn.execute('UPDATE tickets SET conservar=TRUE WHERE channel_id=-456')
        interaction=NS(channel=message.channel,response=NS(defer=AsyncMock()),followup=NS(send=AsyncMock()))
        await Tickets.modo_manual.callback(cog,interaction)
        self.assertEqual(await self.conn.fetchval('SELECT estado FROM tickets WHERE channel_id=-456'),'pausado')
        await Tickets.modo_automatico.callback(cog,interaction)
        row=await self.conn.fetchrow('SELECT estado,conservar FROM tickets WHERE channel_id=-456')
        self.assertEqual(row['estado'],'abierto')
        self.assertTrue(row['conservar'])

    async def test_manual_ticket_does_not_answer_automatically(self):
        from test_tickets import fixture
        cog,_,message=fixture()
        cog.bot.pool=payment_fixtures.BoundConnection(self.conn)
        cog.inbox=self.inbox
        message.author.id=-123;message.channel.id=-456;message.guild.id=-567
        await self.conn.execute("UPDATE tickets SET estado='pausado' WHERE channel_id=-456")
        await cog.on_message(message)
        message.reply.assert_not_awaited()
        self.assertEqual(await self.inbox.counts(),0)
