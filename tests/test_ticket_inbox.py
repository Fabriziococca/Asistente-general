import unittest
import os
import json
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import discord

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

    def receipt_bot(self):
        from test_tickets import fixture, attachment
        cog, _, message = fixture()
        cog.bot.pool = payment_fixtures.BoundConnection(self.conn)
        cog.inbox = self.inbox
        cog.payments = self.store
        message.id=1000; message.author.id=-123; message.channel.id=-456; message.guild.id=-567
        message.content=''
        message.attachments=[attachment()]
        progress = {}
        async def send(content=None, **kwargs):
            message.channel.sent.append(content)
            reply=NS(id=10000+len(message.channel.sent),edit=AsyncMock())
            progress[reply.id]=reply
            return reply
        message.channel.send=AsyncMock(side_effect=send)
        async def fetch(mid):
            return message if mid==message.id else progress[mid]
        message.channel.fetch_message=AsyncMock(side_effect=fetch)
        async def assign(*roles, **kwargs):
            current={r.id:r for r in message.author.roles}
            current.update({r.id:r for r in roles})
            message.author.roles=list(current.values())
        message.author.add_roles=AsyncMock(side_effect=assign)
        return cog,message,progress

    @staticmethod
    def receipt_reading():
        from test_receipts import reading
        return json.dumps({'comprobantes':[reading(monto=4700)]})

    async def make_retry_due(self, message):
        await self.conn.execute("UPDATE ticket_inbox SET retry_at=CURRENT_TIMESTAMP-INTERVAL '1 second' WHERE message_id=$1",message.id)

    async def test_receipt_outage_then_rank_then_restart_delivers_once(self):
        cog,message,progress=self.receipt_bot()
        cog._generate_content_with_rotation=AsyncMock(side_effect=TimeoutError())
        await cog.on_message(message)
        job=await self.conn.fetchrow('SELECT * FROM ticket_inbox WHERE message_id=1000')
        self.assertEqual((job['state'],job['attempts'],job['error_type']),('pending',1,'TimeoutError'))
        self.assertTrue(job['is_receipt'])
        self.assertIsNotNone(job['reply_id'])
        warning_id=job['reply_id']
        self.assertEqual(await self.conn.fetchval('SELECT count(*) FROM pagos WHERE comprobante_hash IS NOT NULL'),0)
        self.assertTrue(await self.conn.fetchval('SELECT conservar FROM tickets WHERE channel_id=-456'))

        followup=NS(**vars(message));followup.id=1001;followup.content='diamante';followup.attachments=[]
        await cog.on_message(followup)
        self.assertEqual(cog._generate_content_with_rotation.await_count,1)
        self.assertIn('reintentará',followup.reply.await_args.args[0])
        self.assertIn('**Diamante**',followup.reply.await_args.args[0])
        self.assertEqual(await self.conn.fetchval('SELECT rangos_solicitados FROM tickets WHERE channel_id=-456'),['Diamante'])

        # Nueva instancia de la bandeja: el estado sale de PostgreSQL, no de memoria.
        cog.inbox=TicketInbox(payment_fixtures.BoundConnection(self.conn))
        await self.make_retry_due(message)
        cog._generate_content_with_rotation=AsyncMock(return_value=self.receipt_reading())
        message.guild.text_channels=[];cog.bot.guilds=[]
        cog.bot.get_channel=lambda cid:message.channel
        await cog._recover_messages()
        job=await self.conn.fetchrow('SELECT * FROM ticket_inbox WHERE message_id=1000')
        self.assertEqual((job['state'],job['attempts'],job['error_type']),('done',2,None))
        self.assertEqual(await self.conn.fetchval('SELECT count(*) FROM pagos WHERE comprobante_hash IS NOT NULL'),1)
        self.assertEqual(await self.conn.fetchval('SELECT sum(monto) FROM pagos WHERE comprobante_hash IS NOT NULL'),4700)
        message.author.add_roles.assert_awaited_once()
        self.assertEqual((await self.store.summary(-456,-123))['state'],'delivered')
        self.assertIsNone(await cog.inbox.receipt_status(-456,-123))
        self.assertIn('Lectura terminada',progress[warning_id].edit.await_args.kwargs['content'])
        # Evento duplicado y barrido repetido no vuelven a registrar ni entregar.
        await cog.on_message(message)
        await cog._recover_messages()
        message.author.add_roles.assert_awaited_once()
        self.assertEqual(cog._generate_content_with_rotation.await_count,1)

    async def test_receipt_retries_pause_without_consuming_attempts_and_resume(self):
        cog,message,_=self.receipt_bot()
        cog._generate_content_with_rotation=AsyncMock(side_effect=TimeoutError())
        await cog.on_message(message)
        await self.make_retry_due(message)
        await self.conn.execute("UPDATE tickets SET estado='pausado' WHERE channel_id=-456")
        self.assertEqual(await self.inbox.pending(),[])
        await cog.on_message(message)  # Incluso un evento repetido respeta la pausa.
        self.assertEqual((await self.conn.fetchrow('SELECT state,attempts FROM ticket_inbox WHERE message_id=1000'))['attempts'],1)
        cog._generate_content_with_rotation.assert_awaited_once()
        from cogs.tickets import Tickets
        interaction=NS(channel=message.channel,response=NS(defer=AsyncMock()),followup=NS(send=AsyncMock()))
        await Tickets.modo_automatico.callback(cog,interaction)
        cog._generate_content_with_rotation=AsyncMock(return_value=self.receipt_reading())
        await cog._process_inbox_message(message)
        self.assertEqual(await self.conn.fetchval('SELECT state FROM ticket_inbox WHERE message_id=1000'),'done')
        message.author.add_roles.assert_awaited_once()

    async def test_three_failed_receipt_attempts_end_in_visible_manual_state(self):
        cog,message,progress=self.receipt_bot()
        cog._generate_content_with_rotation=AsyncMock(side_effect=TimeoutError())
        for attempt in range(3):
            await cog.on_message(message)
            await self.make_retry_due(message)
        job=await self.conn.fetchrow('SELECT * FROM ticket_inbox WHERE message_id=1000')
        self.assertEqual((job['state'],job['attempts'],job['error_type']),('manual',3,'TimeoutError'))
        self.assertEqual(await self.inbox.receipt_status(-456,-123),'manual')
        self.assertEqual(await self.inbox.pending(),[])
        await cog.on_message(message)
        self.assertEqual(cog._generate_content_with_rotation.await_count,3)
        message.author.add_roles.assert_not_awaited()
        self.assertIn('varios intentos',progress[job['reply_id']].edit.await_args.kwargs['content'])
        # Aun sin la marca de conservar, el trabajo manual impide borrar el ticket.
        await self.conn.execute('UPDATE tickets SET conservar=FALSE WHERE channel_id=-456')
        self.assertFalse(await cog._delete_ticket_if_eligible(message.channel,member_left=True))
        followup=NS(**vars(message));followup.id=1001;followup.content='rango diamante';followup.attachments=[]
        await cog.on_message(followup)
        self.assertIn('revisión de administración',followup.reply.await_args.args[0])
        self.assertEqual(cog._generate_content_with_rotation.await_count,3)

    async def test_oversized_receipt_goes_to_manual_without_retrying_ai(self):
        cog,message,_=self.receipt_bot()
        message.attachments[0].size=19*1024*1024
        cog._generate_content_with_rotation=AsyncMock()
        await cog.on_message(message)
        job=await self.conn.fetchrow('SELECT state,attempts,error_type FROM ticket_inbox WHERE message_id=1000')
        self.assertEqual((job['state'],job['attempts'],job['error_type']),('manual',1,'ReceiptReviewRequired'))
        cog._generate_content_with_rotation.assert_not_awaited()
        message.attachments[0].read.assert_not_awaited()

    async def test_failed_confirmation_after_delivery_retries_without_duplicate_money_or_roles(self):
        cog,message,progress=self.receipt_bot()
        message.content='Diamante'
        original_send=message.channel.send.side_effect
        async def send(content=None,**kwargs):
            reply=await original_send(content,**kwargs)
            if 'leyendo tus comprobantes' in (content or ''):
                reply.edit.side_effect=[discord.HTTPException(NS(status=503,reason='Unavailable'),'temporary'),None]
            return reply
        message.channel.send.side_effect=send
        cog._generate_content_with_rotation=AsyncMock(return_value=self.receipt_reading())
        await cog.on_message(message)
        self.assertEqual(await self.conn.fetchval('SELECT state FROM ticket_inbox WHERE message_id=1000'),'pending')
        self.assertEqual((await self.store.summary(-456,-123))['state'],'delivered')
        for reply in progress.values():
            reply.edit.side_effect=None
        await self.make_retry_due(message)
        await cog._process_inbox_message(message)
        self.assertEqual(await self.conn.fetchval('SELECT state FROM ticket_inbox WHERE message_id=1000'),'done')
        self.assertEqual(await self.conn.fetchval('SELECT count(*) FROM pagos WHERE comprobante_hash IS NOT NULL'),1)
        self.assertEqual(cog._generate_content_with_rotation.await_count,1)
        message.author.add_roles.assert_awaited_once()

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
