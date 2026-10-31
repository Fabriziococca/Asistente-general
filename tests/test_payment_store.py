"""Integración PostgreSQL: exclusivamente tablas TEMP de una conexión dedicada.

BOT_TEST_DATABASE_URL es optativa. Nunca se modifica ni se lee una fila de negocio.
"""
import os
import unittest
import json
from decimal import Decimal
from types import SimpleNamespace as NS

import asyncpg

from payment_store import PaymentStore
from receipts import Receipt


class BoundConnection:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self, **kw):
        return self

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *args):
        return False


@unittest.skipUnless(os.environ.get('BOT_TEST_DATABASE_URL'), 'PostgreSQL de prueba no configurado')
class PaymentDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Las conexiones de pruebas usan el endpoint directo: los TEMP pertenecen
        # a una sesión PostgreSQL, no a un backend prestado por PgBouncer.
        database_url = os.environ['BOT_TEST_DATABASE_URL'].replace('-pooler.', '.')
        self.conn = await asyncpg.connect(database_url, timeout=20)
        self.transaction = self.conn.transaction()
        await self.transaction.start()
        async def cleanup():
            try:
                await self.transaction.rollback()
            finally:
                await self.conn.close()
        self.addAsyncCleanup(cleanup)
        # El esquema público se excluye para que una tabla faltante falle sin tocar producción.
        await self.conn.execute("SET search_path = pg_temp")
        await self.conn.execute("""
            CREATE TEMP TABLE usuarios (user_id BIGINT PRIMARY KEY);
            CREATE TEMP TABLE tickets (channel_id BIGINT PRIMARY KEY, user_id BIGINT,
                estado TEXT DEFAULT 'abierto', ultimo_mensaje TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                hablo BOOLEAN DEFAULT FALSE);
            CREATE TEMP TABLE pagos (pago_id SERIAL PRIMARY KEY, user_id BIGINT REFERENCES usuarios(user_id),
                ticket_id BIGINT, monto NUMERIC, moneda TEXT, concepto TEXT, metodo_pago TEXT,
                fecha_pago TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
            INSERT INTO tickets (channel_id,user_id) VALUES (-456,-123);
            INSERT INTO usuarios VALUES (-123);
            INSERT INTO pagos (user_id,ticket_id,monto,moneda,concepto)
                VALUES (-123,-456,100,'ARS','Histórico');
        """)
        self.store = PaymentStore(BoundConnection(self.conn))
        await self.store.initialize()
        self.message = NS(author=NS(id=-123), channel=NS(id=-456), guild=NS(id=-567))

    async def save_two(self):
        return await self.store.save(self.message,
            [Receipt(0, Decimal(5900), 'ARS', 'transferencia', 'first-operation'),
             Receipt(1, Decimal(7600), 'ARS', 'transferencia', 'second-operation')], ['hash-first','hash-second'])

    async def test_two_receipts_one_complete_delivery_and_exact_finances(self):
        await self.save_two()
        result = await self.store.prepare(-456,-123,('Diamante','Oro','Plata'))
        delivery = result['delivery']
        self.assertEqual(delivery['roles_asignados'], ['Diamante','Oro','Plata'])
        await self.store.attempt(delivery['entrega_id'])
        await self.store.finish(delivery['entrega_id'],-456)
        rows = await self.conn.fetch("SELECT * FROM pagos WHERE comprobante_hash IS NOT NULL")
        self.assertEqual(sum(row['monto'] for row in rows), 13500)
        self.assertTrue(all(row['entrega_estado']=='entregado' for row in rows))
        self.assertTrue((await self.store.prepare(-456,-123)).get('empty'))

    async def test_message_to_roles_to_finances_and_preservation_end_to_end(self):
        from unittest.mock import AsyncMock
        from test_tickets import fixture, attachment
        from test_receipts import reading
        from cogs.tickets import Tickets
        cog, _, message = fixture()
        cog.bot.pool = BoundConnection(self.conn)
        cog.payments = self.store
        message.author.id = -123
        message.channel.id = -456
        message.guild.id = -567
        message.attachments = [attachment(b'first image'), attachment(b'second image')]
        cog._generate_content_with_rotation = AsyncMock(return_value=json.dumps({'comprobantes': [
            reading(), reading(1,7600,numero_operacion='E1792432948955Q')]}))
        async def assign(*roles, **kwargs):
            message.author.roles = list(roles)
        message.author.add_roles.side_effect = assign
        await cog.on_message(message)
        self.assertEqual(len(message.author.roles),3)
        self.assertEqual(await self.conn.fetchval("SELECT count(*) FROM pagos WHERE entrega_estado='entregado'"),2)
        self.assertEqual(await self.conn.fetchval("SELECT sum(monto) FROM pagos WHERE entrega_estado='entregado'"),13500)
        await cog.on_message(message)
        self.assertEqual(await self.conn.fetchval("SELECT count(*) FROM pagos WHERE comprobante_hash IS NOT NULL"),2)
        message.author.add_roles.assert_awaited_once()
        interaction=NS(channel=message.channel,response=NS(defer=AsyncMock()),followup=NS(send=AsyncMock()))
        await Tickets.conservar_ticket.callback(cog,interaction)
        restarted=Tickets(cog.bot)
        self.assertFalse(await restarted._delete_ticket_if_eligible(message.channel))
        message.channel.delete.assert_not_awaited()

    async def test_duplicate_bytes_and_operation_do_not_duplicate_finances(self):
        await self.save_two()
        await self.save_two()
        await self.store.save(self.message,
            [Receipt(0,Decimal(5900),'ARS','transferencia','first-operation')], ['different-image'])
        self.assertEqual(await self.conn.fetchval("SELECT count(*) FROM pagos WHERE comprobante_hash IS NOT NULL"),2)

    async def test_retired_method_is_blocked_before_any_insert(self):
        with self.assertRaises(ValueError):
            await self.store.save(self.message,[Receipt(0,Decimal('2.00'),'USDT','binance',None)],['retired'])
        self.assertEqual(await self.conn.fetchval('SELECT count(*) FROM pagos'),1)

    async def test_retired_pending_balance_does_not_fund_new_purchase_or_recover(self):
        await self.conn.execute("""INSERT INTO pagos (user_id,ticket_id,monto,moneda,metodo_pago,
            entrega_estado,entrega_id,entrega_actualizada)
            VALUES (-123,-456,4.50,'USD','paypal','pendiente',NULL,CURRENT_TIMESTAMP-INTERVAL '3 minutes')""")
        self.assertTrue((await self.store.prepare(-456,-123,('Diamante',)))['blocked'])
        await self.conn.execute("UPDATE pagos SET entrega_estado='reintentar',entrega_id='legacy' WHERE moneda='USD'")
        self.assertFalse(await self.store.recoverable())
        self.assertEqual(await self.conn.fetchval("SELECT monto FROM pagos WHERE moneda='USD'"),Decimal('4.50'))

    async def test_partial_then_second_message_and_restart(self):
        await self.store.save(self.message,[Receipt(0,Decimal(5900),'ARS','transferencia',None)],['first'])
        partial = await self.store.prepare(-456,-123,('Diamante','Oro','Plata'))
        self.assertEqual(partial['missing'],7600)
        restarted = PaymentStore(BoundConnection(self.conn))
        await restarted.save(self.message,[Receipt(0,Decimal(7600),'ARS','transferencia',None)],['second'])
        self.assertEqual((await restarted.prepare(-456,-123))['delivery']['roles_asignados'],['Diamante','Oro','Plata'])

    async def test_paypal_followup_oro_keeps_original_diamante_and_oro(self):
        await self.store.save(self.message,
            [Receipt(0,Decimal('2.00'),'USD','paypal','diamond-payment')], ['diamond-pdf'])
        first = await self.store.prepare(-456,-123,('Diamante','Oro'))
        self.assertEqual(first['missing'],Decimal('1.75'))
        restarted = PaymentStore(BoundConnection(self.conn))
        await restarted.save(self.message,
            [Receipt(0,Decimal('1.75'),'USD','paypal','gold-payment')], ['gold-pdf'])
        delivery = (await restarted.prepare(-456,-123,('Oro',)))['delivery']
        self.assertEqual(delivery['roles_asignados'],['Diamante','Oro'])
        await restarted.finish(delivery['entrega_id'],-456)
        self.assertTrue(all(row['roles_asignados']==['Diamante','Oro'] for row in
            await self.conn.fetch("SELECT roles_asignados FROM pagos WHERE moneda='USD'")))

    async def test_finishing_one_delivery_keeps_other_requested_roles_and_payments(self):
        await self.store.save(self.message,
            [Receipt(0,Decimal('2.00'),'USD','paypal',None)], ['diamond-pdf'])
        first = (await self.store.prepare(-456,-123,('Diamante',)))['delivery']
        await self.store.save(self.message,
            [Receipt(0,Decimal('1.75'),'USD','paypal',None)], ['gold-pdf'])
        await self.store.prepare(-456,-123,('Oro',))
        await self.store.finish(first['entrega_id'],-456)
        ticket = await self.conn.fetchrow("SELECT * FROM tickets WHERE channel_id=-456")
        self.assertEqual(ticket['rangos_solicitados'],['Oro'])
        self.assertEqual(ticket['estado'],'abierto')
        second = (await self.store.prepare(-456,-123))['delivery']
        self.assertEqual(second['roles_asignados'],['Oro'])

    def purchase_bot(self):
        from unittest.mock import AsyncMock
        from test_tickets import fixture
        cog, _, message = fixture()
        cog.bot.pool = BoundConnection(self.conn)
        cog.payments = self.store
        message.author.id = -123
        message.channel.id = -456
        message.guild.id = -567
        async def assign(*roles, **kwargs):
            existing = {r.id:r for r in message.author.roles}
            existing.update({r.id:r for r in roles})
            message.author.roles = list(existing.values())
        message.author.add_roles = AsyncMock(side_effect=assign)
        return cog, message

    async def test_paypal_two_distinct_pdfs_one_message_delivers_both_roles(self):
        from unittest.mock import AsyncMock
        from test_tickets import attachment
        from test_receipts import reading
        cog, message = self.purchase_bot()
        message.content = 'Rango diamante y Rango oro'
        message.attachments = [attachment(b'diamond','application/pdf','pago.pdf'),
                               attachment(b'gold','application/pdf','pago.pdf')]
        cog._generate_content_with_rotation = AsyncMock(return_value=json.dumps({'comprobantes':[
            reading(monto='2.00',moneda='USD',metodo_pago='paypal',numero_operacion='diamond-paypal'),
            reading(1,monto='1.75',moneda='USD',metodo_pago='paypal',numero_operacion='gold-paypal')]}))
        await cog.on_message(message)
        self.assertEqual(len(cog._generate_content_with_rotation.await_args.args[1]),2)
        self.assertEqual(len(message.author.add_roles.await_args.args),2)
        rows = await self.conn.fetch("SELECT * FROM pagos WHERE moneda='USD'")
        self.assertEqual(len(rows),2)
        self.assertEqual(sum(r['monto'] for r in rows),Decimal('3.75'))
        self.assertTrue(all(r['roles_asignados']==['Diamante','Oro'] and r['entrega_estado']=='entregado' for r in rows))

    async def test_incident_sequence_and_duplicate_restore_without_another_payment(self):
        from unittest.mock import AsyncMock
        from test_tickets import attachment
        from test_receipts import reading
        from cogs.tickets import ROLES
        cog, message = self.purchase_bot()
        message.content = 'Rango diamante y Rango oro'
        # Incluso si el primer mensaje repite el mismo PDF, el pedido no se pierde.
        message.attachments = [attachment(b'diamond','application/pdf'),attachment(b'diamond','application/pdf')]
        cog._generate_content_with_rotation = AsyncMock(return_value=json.dumps({'comprobantes':[
            reading(monto='2.00',moneda='USD',metodo_pago='paypal',numero_operacion='diamond-paypal')]}))
        await cog.on_message(message)
        message.author.add_roles.assert_not_awaited()
        self.assertIn('completar **Oro**',message.channel.sent[-1])
        message.id += 1
        message.content = 'Oro'
        message.attachments = [attachment(b'gold','application/pdf')]
        cog._generate_content_with_rotation.return_value = json.dumps({'comprobantes':[
            reading(monto='1.75',moneda='USD',metodo_pago='paypal',numero_operacion='gold-paypal')]})
        await cog.on_message(message)
        self.assertEqual({r.id for r in message.author.roles},{ROLES['Diamante']['id'],ROLES['Oro']['id']})
        message.id += 1
        message.content = 'Rango diamante'
        message.attachments = [attachment(b'diamond','application/pdf')]
        await cog.on_message(message)
        self.assertEqual(message.author.add_roles.await_count,1)
        message.author.roles = [NS(id=ROLES['Oro']['id'])]
        message.id += 1
        await cog.on_message(message)
        self.assertEqual({r.id for r in message.author.roles},{ROLES['Diamante']['id'],ROLES['Oro']['id']})
        self.assertEqual(message.author.add_roles.await_count,2)
        self.assertEqual(cog._generate_content_with_rotation.await_count,2)
        rows = await self.conn.fetch("SELECT * FROM pagos WHERE moneda='USD'")
        self.assertEqual(len(rows),2)
        self.assertEqual(sum(r['monto'] for r in rows),Decimal('3.75'))
        self.assertTrue(all(r['roles_asignados']==['Diamante','Oro'] and r['entrega_estado']=='entregado' for r in rows))

    async def test_legacy_incomplete_delivery_requests_review_instead_of_duplicate_success(self):
        from test_tickets import attachment
        await self.store.save(self.message,[Receipt(0,Decimal('2.00'),'USD','paypal',None),
            Receipt(1,Decimal('1.75'),'USD','paypal',None)],['diamond-hash','gold-hash'])
        delivery = (await self.store.prepare(-456,-123,('Oro',)))['delivery']
        await self.store.finish(delivery['entrega_id'],-456)
        cog,message = self.purchase_bot()
        message.content='Rango diamante'
        from hashlib import sha256
        await self.conn.execute("UPDATE pagos SET comprobante_hash=$1 WHERE comprobante_hash='diamond-hash'",
            sha256(b'diamond').hexdigest())
        message.attachments=[attachment(b'diamond','application/pdf')]
        await cog.on_message(message)
        self.assertTrue(any('conciliar **Diamante**' in (s or '') for s in message.channel.sent))
        self.assertTrue(await self.conn.fetchval("SELECT conservar FROM tickets WHERE channel_id=-456"))
        self.assertEqual((await self.store.summary(-456,-123))['state'],'receipt_pending')
        self.assertEqual(await self.conn.fetchval("SELECT count(*) FROM pagos WHERE moneda='USD'"),2)
        message.author.add_roles.assert_not_awaited()

    async def test_failed_first_read_keeps_combo_before_second_payment(self):
        from unittest.mock import AsyncMock
        from test_tickets import attachment
        from test_receipts import reading
        cog,message = self.purchase_bot()
        message.content='Diamante y Oro'
        message.attachments=[attachment(b'diamond','application/pdf')]
        cog._generate_content_with_rotation=AsyncMock(return_value='incomplete JSON')
        await cog.on_message(message)
        self.assertEqual(await self.conn.fetchval("SELECT rangos_solicitados FROM tickets WHERE channel_id=-456"),['Diamante','Oro'])
        message.id += 1
        message.content='Oro'
        message.attachments=[attachment(b'gold','application/pdf')]
        cog._generate_content_with_rotation.return_value=json.dumps({'comprobantes':[
            reading(monto='1.75',moneda='USD',metodo_pago='paypal',numero_operacion='gold-payment')]})
        await cog.on_message(message)
        message.author.add_roles.assert_not_awaited()
        self.assertEqual(await self.conn.fetchval("SELECT rangos_solicitados FROM tickets WHERE channel_id=-456"),['Diamante','Oro'])
        self.assertIn('2.00 USD para completar **Diamante**',message.channel.sent[-1])

    async def test_recovery_is_persistent_and_bounded(self):
        await self.save_two()
        delivery = (await self.store.prepare(-456,-123))['delivery']
        for expected_attempts in range(1,4):
            self.assertEqual(await self.store.attempt(delivery['entrega_id']),expected_attempts)
            await self.store.failed(delivery['entrega_id'],expected_attempts)
        self.assertEqual((await self.store.prepare(-456,-123))['delivery']['entrega_estado'],'manual')
        self.assertIsNone(await self.store.attempt(delivery['entrega_id']))

    async def test_recovery_finds_old_delivery_and_respects_manual_mode(self):
        await self.save_two()
        await self.store.prepare(-456,-123)
        await self.conn.execute("UPDATE pagos SET entrega_actualizada=CURRENT_TIMESTAMP-INTERVAL '3 minutes'")
        self.assertEqual(len(await self.store.recoverable()),1)
        await self.conn.execute("UPDATE tickets SET estado='pausado' WHERE channel_id=-456")
        self.assertFalse(await self.store.recoverable())

    async def test_schema_is_repeatable_and_does_not_rewrite_history(self):
        await self.store.initialize()
        old = await self.conn.fetchrow("SELECT * FROM pagos WHERE concepto='Histórico'")
        self.assertEqual(old['monto'],100)
        self.assertIsNone(old['entrega_estado'])

    async def test_currencies_are_not_added_together(self):
        await self.store.save(self.message,
            [Receipt(0,Decimal(2300),'ARS','transferencia',None), Receipt(1,Decimal(1),'USD','paypal',None)], ['ars','usd'])
        result=await self.store.prepare(-456,-123,('Diamante',))
        self.assertNotIn('delivery',result)

    async def test_preservation_survives_delivery_and_schema_reinitialization(self):
        await self.conn.execute("UPDATE tickets SET conservar=TRUE WHERE channel_id=-456")
        await self.save_two()
        delivery = (await self.store.prepare(-456,-123))['delivery']
        await self.store.finish(delivery['entrega_id'],-456)
        await self.store.initialize()
        self.assertTrue(await self.conn.fetchval("SELECT conservar FROM tickets WHERE channel_id=-456"))


from campaign_clock import freeze_campaign

def setUpModule():
    global _campaign_clock
    _campaign_clock = freeze_campaign()
    _campaign_clock.start()

def tearDownModule():
    _campaign_clock.stop()
