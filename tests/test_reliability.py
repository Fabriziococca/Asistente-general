"""Interrupciones entre registro, entrega y aviso: PostgreSQL TEMP, proveedores simulados."""
import json
import os
import unittest
from decimal import Decimal
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import discord

from payment_store import PaymentStore
from receipts import Receipt
from ticket_inbox import TicketInbox, SCHEMA
import test_payment_store as payment_fixtures
from test_payment_store import BoundConnection
import test_ticket_inbox as inbox_fixtures
from test_receipts import reading
from test_tickets import ROLES, attachment


@unittest.skipUnless(os.environ.get('BOT_TEST_DATABASE_URL'), 'PostgreSQL de prueba no configurado')
class ReliabilityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await payment_fixtures.PaymentDatabaseTests.asyncSetUp(self)
        await self.conn.execute(SCHEMA.replace('CREATE TABLE IF NOT EXISTS','CREATE TEMP TABLE IF NOT EXISTS'))
        self.inbox=TicketInbox(BoundConnection(self.conn))
        await self.inbox.initialize()

    def bot(self):
        cog,message,progress=inbox_fixtures.InboxDatabaseTests.receipt_bot(self)
        cog.bot.get_guild=lambda gid:message.guild
        cog.bot.get_channel=lambda cid:message.channel if cid==message.channel.id else None
        cog.bot.fetch_channel=AsyncMock(side_effect=discord.NotFound(NS(status=404,reason='Not found'),'gone'))
        return cog,message,progress

    async def save(self, message, amount=4700, currency='ARS', digest='one'):
        return await self.store.save(message,[Receipt(0,Decimal(amount),currency,
            'transferencia' if currency=='ARS' else 'binance',digest+'-operation')],[digest])

    async def stale(self):
        await self.conn.execute("UPDATE pagos SET entrega_actualizada=CURRENT_TIMESTAMP-INTERVAL '4 minutes'")

    async def prepared(self, message):
        await self.save(message)
        return (await self.store.prepare(-456,-123,('Diamante',)))['delivery']

    async def test_crash_after_payment_save_recovers_before_delivery_was_prepared(self):
        cog,message,_=self.bot()
        await self.save(message)
        await self.store.remember(-456,-123,('Diamante',))
        await self.stale()
        self.assertEqual(await self.conn.fetchval("SELECT entrega_estado FROM pagos WHERE comprobante_hash='one'"),'pendiente')
        await cog._recover_deliveries()
        self.assertEqual((await self.store.summary(-456,-123))['state'],'delivered')
        message.author.add_roles.assert_awaited_once()
        count=message.channel.send.await_count
        await cog._recover_deliveries()
        self.assertEqual(message.channel.send.await_count,count)
        self.assertEqual(await self.conn.fetchval("SELECT count(*) FROM pagos WHERE comprobante_hash IS NOT NULL"),1)

    async def test_saved_ambiguous_amount_asks_for_rank_only_once_after_crash(self):
        cog,message,_=self.bot()
        await self.save(message,5000)
        await self.stale()
        await cog._recover_deliveries()
        self.assertIn('Decime qué rangos',message.channel.sent[-1])
        count=message.channel.send.await_count
        await cog._recover_deliveries()
        self.assertEqual(message.channel.send.await_count,count)
        message.author.add_roles.assert_not_awaited()

    async def test_acknowledging_old_balance_does_not_hide_a_concurrent_new_payment(self):
        cog,message,_=self.bot()
        old=await self.save(message,5000)
        await self.save(message,4700,digest='new')
        await self.store.pending_notified(-456,-123,[old[0]['pago_id']])
        self.assertFalse(await self.conn.fetchval("SELECT aviso_pendiente FROM pagos WHERE comprobante_hash='one'"))
        self.assertTrue(await self.conn.fetchval("SELECT aviso_pendiente FROM pagos WHERE comprobante_hash='new'"))

    async def test_mixed_currency_purchase_delivers_both_ranks_without_conversion(self):
        cog,message,_=self.bot()
        message.content='Diamante y Oro'
        message.attachments=[attachment(b'ars'),attachment(b'usdt')]
        cog._generate_content_with_rotation=AsyncMock(return_value=json.dumps({'comprobantes':[
            reading(monto='4.700',metodo_pago='Mercado Pago'),
            reading(1,monto='4',moneda='USDT',metodo_pago='binance',pay_id_destino='552346130')]}))
        await cog.on_message(message)
        expected={ROLES[name]['id'] for name in ('Diamante','Oro')}
        self.assertTrue(expected.issubset({role.id for role in message.author.roles}))
        self.assertEqual((await self.store.summary(-456,-123))['state'],'delivered')
        rows=await self.conn.fetch("SELECT moneda,monto,roles_asignados FROM pagos WHERE comprobante_hash IS NOT NULL ORDER BY pago_id")
        self.assertEqual([(r['moneda'],r['monto'],r['roles_asignados']) for r in rows],
                         [('ARS',Decimal(4700),['Diamante']),('USDT',Decimal(4),['Oro'])])
        await cog.on_message(message)
        self.assertEqual(message.author.add_roles.await_count,2)

    async def test_crash_in_third_attempt_finalizes_existing_roles_without_fourth_assignment(self):
        cog,message,_=self.bot()
        delivery=await self.prepared(message)
        for _ in range(3):
            await self.store.attempt(delivery['entrega_id'])
        self.assertIsNone(await self.store.attempt(delivery['entrega_id']))
        message.author.roles=[NS(id=ROLES['Diamante']['id'])]
        await self.stale()
        await cog._recover_deliveries()
        self.assertEqual((await self.store.summary(-456,-123))['state'],'delivered')
        message.author.add_roles.assert_not_awaited()
        self.assertEqual(await self.conn.fetchval("SELECT entrega_intentos FROM pagos WHERE comprobante_hash='one'"),3)

    async def test_missing_roles_after_third_attempt_require_manual_and_retry_lost_alert(self):
        cog,message,_=self.bot()
        delivery=await self.prepared(message)
        for _ in range(3):
            await self.store.attempt(delivery['entrega_id'])
        await self.stale()
        normal_send=message.channel.send.side_effect
        message.channel.send.side_effect=discord.HTTPException(NS(status=503,reason='Unavailable'),'temporary')
        await cog._recover_deliveries()
        row=await self.conn.fetchrow("SELECT entrega_estado,aviso_pendiente FROM pagos WHERE comprobante_hash='one'")
        self.assertEqual(row['entrega_estado'],'manual')
        self.assertTrue(row['aviso_pendiente'])
        message.channel.send.side_effect=normal_send
        await cog._recover_deliveries()
        self.assertFalse(await self.conn.fetchval("SELECT aviso_pendiente FROM pagos WHERE comprobante_hash='one'"))
        self.assertIn('atención de administración',message.channel.sent[-1])
        message.author.add_roles.assert_not_awaited()

    async def test_lost_success_notification_recovers_without_assigning_or_charging_again(self):
        cog,message,_=self.bot()
        delivery=await self.prepared(message)
        normal_send=message.channel.send.side_effect
        message.channel.send.side_effect=discord.HTTPException(NS(status=503,reason='Unavailable'),'temporary')
        with self.assertRaises(discord.HTTPException):
            await cog._fulfill_delivery(delivery,message.channel,message.guild)
        self.assertEqual((await self.store.summary(-456,-123))['state'],'delivered')
        self.assertTrue(await self.conn.fetchval("SELECT aviso_pendiente FROM pagos WHERE comprobante_hash='one'"))
        self.assertFalse(await cog._delete_ticket_if_eligible(message.channel,member_left=True))
        message.channel.send.side_effect=normal_send
        await cog._recover_deliveries()
        self.assertIn('Ya tenés los rangos **Diamante**',message.channel.sent[-1])
        self.assertFalse(await self.conn.fetchval("SELECT aviso_pendiente FROM pagos WHERE comprobante_hash='one'"))
        message.author.add_roles.assert_awaited_once()
        self.assertEqual(await self.conn.fetchval("SELECT count(*) FROM pagos WHERE comprobante_hash IS NOT NULL"),1)
        count=message.channel.send.await_count
        await cog._recover_deliveries()
        self.assertEqual(message.channel.send.await_count,count)

    async def test_crash_after_roles_before_db_confirmation_does_not_repeat_role_assignment(self):
        cog,message,_=self.bot()
        delivery=await self.prepared(message)
        finish=self.store.finish
        self.store.finish=AsyncMock(side_effect=TimeoutError())
        self.assertFalse(await cog._fulfill_delivery(delivery,message.channel,message.guild))
        message.author.add_roles.assert_awaited_once()
        self.store.finish=finish
        await self.stale()
        await cog._recover_deliveries()
        message.author.add_roles.assert_awaited_once()
        self.assertEqual((await self.store.summary(-456,-123))['state'],'delivered')

    async def test_late_failure_cannot_overwrite_a_completed_delivery(self):
        cog,message,_=self.bot()
        delivery=await self.prepared(message)
        await cog._fulfill_delivery(delivery,message.channel,message.guild)
        await self.store.failed(delivery['entrega_id'],3)
        self.assertEqual((await self.store.summary(-456,-123))['state'],'delivered')

    async def test_owner_completed_manual_rank_is_reconciled_and_does_not_block_other_paid_rank(self):
        cog,message,_=self.bot()
        delivery=await self.prepared(message)
        for _ in range(3):
            await self.store.attempt(delivery['entrega_id'])
        await self.store.failed(delivery['entrega_id'],3)
        await self.save(message,4200,digest='gold')
        await self.store.remember(-456,-123,('Oro',))
        message.author.roles=[NS(id=ROLES['Diamante']['id'])]  # Entrega hecha por administración.
        await self.stale()
        await cog._recover_deliveries()
        self.assertEqual({r.id for r in message.author.roles},{ROLES['Diamante']['id'],ROLES['Oro']['id']})
        self.assertEqual((await self.store.summary(-456,-123))['state'],'delivered')
        message.author.add_roles.assert_awaited_once()
        self.assertEqual(await self.conn.fetchval("SELECT sum(monto) FROM pagos WHERE comprobante_hash IS NOT NULL"),8900)

    async def test_one_broken_ticket_does_not_block_other_pending_purchases(self):
        cog,message,_=self.bot()
        await self.save(message)
        await self.stale()
        purchases=[dict(row) for row in await self.store.pending_purchases()]
        self.store.pending_purchases=AsyncMock(return_value=[dict(purchases[0],ticket_id=-999),*purchases])
        await cog._recover_deliveries()
        self.assertEqual((await self.store.summary(-456,-123))['state'],'delivered')
        message.author.add_roles.assert_awaited_once()

    async def test_cached_delivery_rechecks_manual_mode_before_any_assignment(self):
        cog,message,_=self.bot()
        delivery=await self.prepared(message)
        await self.conn.execute("UPDATE tickets SET estado='pausado' WHERE channel_id=-456")
        self.assertFalse(await cog._fulfill_delivery(delivery,message.channel,message.guild))
        message.author.add_roles.assert_not_awaited()
        self.assertEqual(await self.conn.fetchval("SELECT entrega_intentos FROM pagos WHERE comprobante_hash='one'"),0)

    async def test_paid_member_can_report_missing_role_without_reuploading_receipt(self):
        cog,message,_=self.bot()
        message.content='Diamante'
        cog._generate_content_with_rotation=AsyncMock(return_value=inbox_fixtures.InboxDatabaseTests.receipt_reading())
        await cog.on_message(message)
        self.assertEqual((await self.store.summary(-456,-123))['state'],'delivered')
        message.author.roles=[]
        followup=NS(**vars(message));followup.id=1001;followup.content='no tengo mi rango';followup.attachments=[]
        await cog.on_message(followup)
        self.assertIn(ROLES['Diamante']['id'],{r.id for r in message.author.roles})
        self.assertIn('Verifiqué en Discord',followup.reply.await_args.args[0])
        self.assertEqual(await self.conn.fetchval("SELECT count(*) FROM pagos WHERE comprobante_hash IS NOT NULL"),1)
        self.assertEqual(cog._generate_content_with_rotation.await_count,1)

    async def test_omitted_second_image_is_retried_without_losing_or_recharging_first(self):
        cog,message,_=self.bot()
        message.content='Diamante y Oro'
        message.attachments=[attachment(b'first'),attachment(b'second')]
        cog._generate_content_with_rotation=AsyncMock(return_value=json.dumps({'comprobantes':[reading()]}))
        await cog.on_message(message)
        self.assertEqual(await self.conn.fetchval('SELECT state FROM ticket_inbox WHERE message_id=1000'),'pending')
        self.assertEqual(await self.conn.fetchval("SELECT count(*) FROM pagos WHERE comprobante_hash IS NOT NULL"),1)
        message.author.add_roles.assert_not_awaited()
        await self.conn.execute("UPDATE ticket_inbox SET retry_at=CURRENT_TIMESTAMP-INTERVAL '1 second'")
        cog._generate_content_with_rotation=AsyncMock(return_value=json.dumps({'comprobantes':[
            reading(monto=4200,numero_operacion='second-operation')]}))
        await cog._process_inbox_message(message)
        self.assertEqual(len(cog._generate_content_with_rotation.await_args.args[1]),1)
        self.assertEqual((await self.store.summary(-456,-123))['state'],'delivered')
        self.assertEqual(await self.conn.fetchval("SELECT sum(monto) FROM pagos WHERE comprobante_hash IS NOT NULL"),8900)
        self.assertEqual(await self.conn.fetchval("SELECT count(*) FROM pagos WHERE comprobante_hash IS NOT NULL"),2)
        message.author.add_roles.assert_awaited_once()
