"""Cierre permanente: historial intacto y únicamente medios vigentes para cobrar."""
import json
import os
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

import payment_policy as policy
from receipts import Receipt, parse_receipts
from scripts.refresh_payment_messages import needs_refresh, replacement, validate_replacement
from test_receipts import reading
from test_tickets import fixture
import test_payment_store as store_fixtures


class RetiredPaymentTests(unittest.IsolatedAsyncioTestCase):
    def test_campaign_cannot_reactivate_and_historical_record_is_recognized(self):
        self.assertFalse(policy.campaign_active())
        self.assertFalse(policy.campaign_active(datetime(2026, 11, 2, tzinfo=timezone.utc)))
        row={'moneda':'USD','metodo_pago':'paypal','politica_precio':policy.CAMPAIGN_ID}
        self.assertTrue(policy.recorded_payment(row))
        self.assertFalse(policy.current_payment(row['moneda'],row['metodo_pago']))
        self.assertFalse(policy.recorded_payment(dict(row,politica_precio=None)))

    def test_prices_are_permanent_and_only_in_active_currencies(self):
        self.assertEqual([p['USDT'] for p in policy.PRICES.values()],
                         [Decimal('5.50'),Decimal('5.00'),Decimal('2.50')])
        self.assertEqual(sum(p['USDT'] for p in policy.PRICES.values()),13)
        self.assertEqual(policy.SUGGESTION_PRICES,{'ARS':4000,'USDT':4})
        self.assertTrue(all(set(p)=={'ARS','USDT'} for p in policy.PRICES.values()))

    def test_retired_receipts_are_rejected_even_with_visible_conversion(self):
        items=[reading(index=i,moneda='MXN',metodo_pago='paypal',monto_usd='2.00',
                       conversion_usd_visible=True) for i in range(10)]
        receipts,issues=parse_receipts(json.dumps({'comprobantes':items}),10)
        self.assertFalse(receipts)
        self.assertEqual(len(issues),10)
        self.assertTrue(all('medio o moneda no admitidos' in issue for issue in issues))
        self.assertLess(len('; '.join(issues)),1500)

    async def test_support_prompt_cannot_reuse_campaign_or_personal_identity(self):
        cog,_,message=fixture();message.content='Consulta comercial que no estaba prevista'
        message.channel.hist=[NS(author=NS(id=999,bot=True),attachments=[],
            content='PayPal en USD: anterior@example.org; titular '+policy.BANK_HOLDER)]
        cog._answer_with_acknowledgement=AsyncMock(return_value='Te ayudo con esa consulta.')
        await cog.handle_support_query(message)
        prompt=cog._answer_with_acknowledgement.await_args.args[1]
        for retired in ('PayPal','anterior@example.org','megadescuento','65% OFF',policy.BANK_HOLDER):
            self.assertNotIn(retired,prompt)
        self.assertIn('Binance Pay',prompt)

    def test_refresh_replaces_old_offer_even_when_it_asks_for_a_receipt(self):
        old={'content':'⚡ **PROMO PAYPAL · HASTA 65% OFF**\n'
                       'Mandá 2 USD a anterior@example.org y adjuntá el comprobante.','embeds':[]}
        self.assertTrue(needs_refresh(old))
        updated=replacement(old)
        self.assertIn('Binance Pay',updated['content'])
        for retired in ('PayPal','anterior@example.org','65% OFF','2 USD'):
            self.assertNotIn(retired,updated['content'])
        self.assertFalse(needs_refresh(updated))
        validate_replacement(updated)


@unittest.skipUnless(os.environ.get('BOT_TEST_DATABASE_URL'),'PostgreSQL de prueba no configurado')
class RetiredPaymentDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await store_fixtures.PaymentDatabaseTests.asyncSetUp(self)

    def bot(self):
        return store_fixtures.PaymentDatabaseTests.purchase_bot(self)

    async def test_prepared_historical_delivery_still_recovers_without_accepting_new_paypal(self):
        await self.conn.execute("""INSERT INTO pagos(user_id,ticket_id,monto,moneda,metodo_pago,politica_precio,
            entrega_estado,entrega_id,roles_asignados,entrega_actualizada,comprobante_hash,guild_id)
            VALUES(-123,-456,2,'USD','paypal',$1,'preparado','old',ARRAY['Diamante'],
            CURRENT_TIMESTAMP-INTERVAL '4 minutes','old-hash',-567)""",policy.CAMPAIGN_ID)
        self.assertEqual(len(await self.store.recoverable()),1)
        prepared=(await self.store.prepare(-456,-123,('Diamante',)))['delivery']
        cog,message=self.bot()
        self.assertTrue(await cog._fulfill_delivery(prepared,message.channel,message.guild))
        with self.assertRaises(ValueError):
            await self.store.save(self.message,[Receipt(0,Decimal(2),'USD','paypal',None)],['late'])
        row=await self.conn.fetchrow("SELECT monto,politica_precio,entrega_estado FROM pagos WHERE comprobante_hash='old-hash'")
        self.assertEqual(tuple(row),(Decimal(2),policy.CAMPAIGN_ID,'entregado'))
        self.assertEqual(await self.conn.fetchval("SELECT count(*) FROM pagos WHERE moneda='USD'"),1)

    async def test_historical_unallocated_balance_is_not_a_new_purchase(self):
        await self.conn.execute("""INSERT INTO pagos(user_id,ticket_id,monto,moneda,metodo_pago,politica_precio,
            entrega_estado,aviso_pendiente,entrega_actualizada)
            VALUES(-123,-456,2,'USD','paypal',$1,'pendiente',TRUE,CURRENT_TIMESTAMP-INTERVAL '4 minutes')""",policy.CAMPAIGN_ID)
        self.assertTrue((await self.store.prepare(-456,-123,('Diamante',)))['blocked'])
        self.assertFalse(await self.store.pending_purchases())
        self.assertEqual(await self.conn.fetchval("SELECT monto FROM pagos WHERE moneda='USD'"),Decimal(2))

    async def test_prepared_delivery_at_previous_price_keeps_amount_and_roles_after_increase(self):
        old_price=dict(policy.PRICES['Diamante'],USDT=Decimal('4.50'))
        with patch.dict(policy.PRICES,Diamante=old_price):
            await self.store.save(self.message,[Receipt(0,Decimal('4.50'),'USDT','binance','before-increase')],['old-price'])
            prepared=(await self.store.prepare(-456,-123,('Diamante',)))['delivery']
        cog,message=self.bot()
        self.assertTrue(await cog._fulfill_delivery(prepared,message.channel,message.guild))
        row=await self.conn.fetchrow("SELECT monto,roles_asignados,entrega_estado FROM pagos WHERE comprobante_hash='old-price'")
        self.assertEqual(row['monto'],Decimal('4.50'))
        self.assertEqual(row['roles_asignados'],['Diamante'])
        self.assertEqual(row['entrega_estado'],'entregado')
