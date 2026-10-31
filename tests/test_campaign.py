"""Campaña temporal: límites de precio, privacidad y continuidad de entregas."""
import json
import os
import unittest
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

import discord

import payment_policy as policy
from communication import local_answer, message_chunks
from receipts import choose_roles, parse_receipts, split_purchase, vision_prompt, Receipt
from scripts.refresh_payment_messages import replacement, needs_refresh, validate_replacement, owner_replacement
from ticket_messages import ticket_welcome
from test_receipts import reading
from test_tickets import fixture, attachment, ROLES, Tickets
import test_payment_store as store_fixtures
from campaign_clock import freeze_campaign


def setUpModule():
    global _clock
    _clock=freeze_campaign();_clock.start()


def tearDownModule():
    _clock.stop()


def parse(**changes):
    return parse_receipts(json.dumps({'comprobantes':[reading(
        monto='2.00',moneda='USD',metodo_pago='paypal',**changes)]}),1)


class CampaignTests(unittest.IsolatedAsyncioTestCase):
    def test_exact_seven_day_window_and_end_excluded(self):
        self.assertEqual(policy.CAMPAIGN_END-policy.CAMPAIGN_START,timedelta(days=7))
        self.assertFalse(policy.campaign_active(policy.CAMPAIGN_START-timedelta(microseconds=1)))
        self.assertTrue(policy.campaign_active(policy.CAMPAIGN_START))
        self.assertTrue(policy.campaign_active(policy.CAMPAIGN_END-timedelta(microseconds=1)))
        self.assertFalse(policy.campaign_active(policy.CAMPAIGN_END))

    def test_usd_prices_exact_totals_and_no_fx_for_mixed_purchase(self):
        for role,price in [('Diamante','2.00'),('Oro','1.75'),('Plata','1.50')]:
            self.assertEqual(choose_roles(Decimal(price),'USD'),((role,),Decimal(0)))
        self.assertEqual(choose_roles(Decimal('5.25'),'USD')[0],tuple(ROLES))
        self.assertEqual(choose_roles(Decimal('3.75'),'USD',('Diamante','Oro'))[0],('Diamante','Oro'))
        self.assertEqual(split_purchase({'ARS':Decimal(5300),'USD':Decimal(2)},('Diamante','Oro')),
                         {'ARS':('Oro',),'USD':('Diamante',)})
        self.assertFalse(split_purchase({'ARS':Decimal(3000),'USD':Decimal(1)},('Diamante',)))

    def test_public_and_private_paypal_receipt_aliases_are_internal_only(self):
        for alias in (*policy.PAYPAL_RECIPIENT_ALIASES,'PAYPAL *fabrococca','Fabrizio Cocca Ducay'):
            receipts,issues=parse(destinatario=alias)
            self.assertFalse(issues,alias)
            self.assertEqual(receipts[0].currency,'USD')
        self.assertFalse(parse(destinatario='otra.persona@example.org')[0])
        prompt=vision_prompt(2)
        self.assertIn('fabrococca@gmail.com',prompt)
        self.assertIn('equivalencia',prompt)
        self.assertNotIn('Binance',prompt)

    def test_foreign_currency_requires_visible_usd_equivalence(self):
        for currency in ('MXN','EUR','CLP','ARS'):
            item=reading(monto=35.50,moneda=currency,metodo_pago='paypal',
                         destinatario=policy.PAYPAL_EMAIL,monto_usd='2.00',conversion_usd_visible=True)
            receipts,issues=parse_receipts(json.dumps({'comprobantes':[item]}),1)
            self.assertFalse(issues)
            self.assertEqual((receipts[0].currency,receipts[0].amount),('USD',Decimal('2.00')))
            for visible in (False,None,'true'):
                item['conversion_usd_visible']=visible
                self.assertFalse(parse_receipts(json.dumps({'comprobantes':[item]}),1)[0])

    def test_new_binance_payment_is_rejected_even_with_matching_id(self):
        item=reading(monto='4.5',moneda='USDT',metodo_pago='binance',pay_id_destino=policy.BINANCE_PAY_ID)
        receipts,issues=parse_receipts(json.dumps({'comprobantes':[item]}),1)
        self.assertFalse(receipts)
        self.assertIn('PayPal',str(issues))

    def test_ten_unsupported_files_produce_a_bounded_review_message(self):
        items=[reading(index=i,moneda='USDT',metodo_pago='binance') for i in range(10)]
        receipts,issues=parse_receipts(json.dumps({'comprobantes':items}),10)
        self.assertFalse(receipts)
        self.assertEqual(len(issues),10)
        self.assertLess(len('; '.join(issues)),1500)

    def test_refresh_validates_limits_and_preserves_unrelated_owner_content(self):
        for suggestion in (False,True):
            content,embed=ticket_welcome(suggestion)
            validate_replacement({'content':content,'embeds':[embed.to_dict()]})
        with self.assertRaises(ValueError):
            validate_replacement({'content':'a'*2001,'embeds':[]})
        with self.assertRaises(ValueError):
            validate_replacement({'content':'promo @everyone','embeds':[]})
        faq='1. Compra: Mercado Pago, Binance Pay.\n2. Acceso: abrí el ticket.'
        updated=owner_replacement(faq)
        self.assertIn('2. Acceso: abrí el ticket.',updated)
        self.assertIn('PayPal',updated)
        self.assertNotIn('Binance',updated)
        self.assertEqual(owner_replacement('Una lista de enlaces sin datos personales'),
                         'Una lista de enlaces sin datos personales')

    def test_public_copy_never_leaks_private_identity_or_retired_offer(self):
        for query in ('precios','precio en USD','como pago','¿PayPal?','¿Binance?','titular',
                      'Quién recibe PayPal','correo principal de PayPal','quién es el titular de PayPal'):
            for suggestion in (False,True):
                answer=local_answer(query,suggestion=suggestion)
                self.assertFalse(policy.private_identity_reference(answer),query)
                self.assertNotIn('Binance',answer)
                self.assertNotIn('USDT',answer)
                self.assertNotIn(policy.BINANCE_PAY_ID,answer)
        for alias in ('fabrococca@gmail.com','PAYPAL *fabrococca',policy.BANK_HOLDER):
            for query in ('¿Quién recibe PayPal?','Titular del CBU'):
                result=''.join(message_chunks('PayPal a '+alias,query=query))
                self.assertFalse(policy.private_identity_reference(result))
                self.assertIn(policy.PAYPAL_EMAIL,result)
        self.assertFalse(policy.obsolete_payment_reference('PayPal: '+policy.PAYPAL_EMAIL))
        self.assertTrue(policy.obsolete_payment_reference('PayPal: fabrococca@gmail.com'))

    def test_active_welcome_has_promo_exact_prices_deadline_and_no_pings(self):
        for suggestion in (False,True):
            content,embed=ticket_welcome(suggestion)
            body=content+json.dumps(embed.to_dict(),ensure_ascii=False)
            self.assertIn('7 DÍAS',body)
            self.assertIn(policy.PAYPAL_EMAIL,body)
            self.assertNotIn('Binance',body)
            self.assertFalse(policy.private_identity_reference(body))
            self.assertTrue(all(len(field.value)<=1024 for field in embed.fields))
            self.assertLessEqual(len(embed),6000)
        self.assertIn('5,25 USD',local_answer('precio en USD'))
        self.assertIn('$4,00 USD',local_answer('precio',suggestion=True))

    def test_expiry_restores_normal_offer_and_blocks_only_new_paypal(self):
        with patch('payment_policy.utc_now',return_value=policy.CAMPAIGN_END):
            self.assertFalse(policy.current_payment('USD','paypal'))
            self.assertTrue(policy.current_payment('USDT','binance'))
            self.assertIn('Binance',local_answer('datos de pago'))
            self.assertNotIn('PayPal',local_answer('precios'))
            self.assertIn('5.900',local_answer('precios'))
            self.assertIn('4,50 USDT',local_answer('precios'))
            self.assertTrue(policy.recorded_payment({'moneda':'USD','metodo_pago':'paypal','politica_precio':policy.CAMPAIGN_ID}))
            self.assertFalse(parse()[0])

    async def test_expiry_refreshes_owned_welcome_once_without_sending_a_new_message(self):
        cog,_,message=fixture();cog.bot.user.id=42
        content,embed=ticket_welcome()
        welcome=NS(author=NS(id=42),content=content,embeds=[embed],edit=AsyncMock())
        message.channel.fetch_message=AsyncMock(return_value=welcome)
        await cog._refresh_known_welcome(message.channel,777,False)
        welcome.edit.assert_not_awaited()
        with patch('payment_policy.utc_now',return_value=policy.CAMPAIGN_END):
            await cog._refresh_known_welcome(message.channel,777,False)
            self.assertIn('Binance',json.dumps(welcome.edit.await_args.kwargs['embed'].to_dict()))
            self.assertFalse(welcome.edit.await_args.kwargs['allowed_mentions'].everyone)
            await cog._refresh_known_welcome(message.channel,777,False)
        welcome.edit.assert_awaited_once()
        message.channel.send.assert_not_awaited()

    async def test_missing_welcome_does_not_block_channel_recovery(self):
        cog,_,message=fixture()
        message.channel.fetch_message=AsyncMock(side_effect=discord.NotFound(NS(status=404,reason='gone'),'gone'))
        await cog._refresh_known_welcome(message.channel,777,False)
        await cog._refresh_known_welcome(message.channel,777,False)
        message.channel.fetch_message.assert_awaited_once()

    async def test_campaign_never_broadcasts_and_model_pings_are_stripped(self):
        cog,_,message=fixture()
        await Tickets.auto_promo_refresh.coro(cog)
        message.channel.send.assert_not_awaited()
        self.assertNotIn('@everyone',''.join(message_chunks('@everyone promo @here')))
        self.assertNotIn('@here',''.join(message_chunks('@everyone promo @here')))

    async def test_support_prompt_uses_only_current_public_payment_context(self):
        cog,_,message=fixture();message.content='Consulta comercial que no estaba prevista'
        message.channel.hist=[NS(author=NS(id=999,bot=True),attachments=[],
            content='Binance Pay: 552346130; titular '+policy.BANK_HOLDER)]
        cog._answer_with_acknowledgement=AsyncMock(return_value='Te ayudo con esa consulta.')
        await cog.handle_support_query(message)
        prompt=cog._answer_with_acknowledgement.await_args.args[1]
        self.assertIn(policy.PAYPAL_EMAIL,prompt)
        self.assertIn('$5,25 USD',prompt)
        self.assertIn('7 DÍAS',prompt)
        self.assertNotIn('Binance',prompt)
        self.assertNotIn('USDT',prompt)
        self.assertFalse(policy.private_identity_reference(prompt))

    def test_refresh_keeps_legacy_financial_evidence_without_offering_old_method(self):
        result=replacement({'content':'Comprobante registrado: 4.50 USDT por Binance Pay.','embeds':[]})
        self.assertIn('4.50 USDT',result['content'])
        self.assertNotIn('Binance',result['content'])
        self.assertFalse(policy.public_message_needs_refresh(json.dumps(result)))


@unittest.skipUnless(os.environ.get('BOT_TEST_DATABASE_URL'),'PostgreSQL de prueba no configurado')
class CampaignDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await store_fixtures.PaymentDatabaseTests.asyncSetUp(self)

    def bot(self):
        return store_fixtures.PaymentDatabaseTests.purchase_bot(self)

    async def test_two_paypal_files_deliver_both_roles_and_duplicate_restores_only_missing_role(self):
        cog,message=self.bot();message.content='Diamante y Oro'
        message.attachments=[attachment(b'dia','application/pdf'),attachment(b'oro','application/pdf')]
        cog._generate_content_with_rotation=AsyncMock(return_value=json.dumps({'comprobantes':[
            reading(monto='2.00',moneda='USD',metodo_pago='paypal',destinatario='fabrococca@gmail.com',numero_operacion='dia-operation'),
            reading(1,monto='1.75',moneda='USD',metodo_pago='paypal',destinatario='PAYPAL *fabrococca',numero_operacion='oro-operation')]}))
        await cog.on_message(message)
        self.assertEqual({r.id for r in message.author.roles},{ROLES['Diamante']['id'],ROLES['Oro']['id']})
        rows=await self.conn.fetch("SELECT * FROM pagos WHERE comprobante_hash IS NOT NULL")
        self.assertEqual(sum(r['monto'] for r in rows),Decimal('3.75'))
        self.assertTrue(all(r['politica_precio']==policy.CAMPAIGN_ID for r in rows))
        message.author.roles=[NS(id=ROLES['Oro']['id'])];message.id+=1
        await cog.on_message(message)
        self.assertEqual({r.id for r in message.author.roles},{ROLES['Diamante']['id'],ROLES['Oro']['id']})
        self.assertEqual(await self.conn.fetchval("SELECT count(*) FROM pagos WHERE comprobante_hash IS NOT NULL"),2)
        self.assertEqual(cog._generate_content_with_rotation.await_count,1)

    async def test_campaign_payment_recovers_after_expiry_without_accepting_another_new_payment(self):
        await self.store.save(self.message,[Receipt(0,Decimal(2),'USD','paypal','paid-in-promo')],['promo'])
        await self.conn.execute("UPDATE pagos SET entrega_actualizada=CURRENT_TIMESTAMP-INTERVAL '4 minutes'")
        with patch('payment_policy.utc_now',return_value=policy.CAMPAIGN_END):
            self.assertEqual(len(await self.store.pending_purchases()),1)
            delivery=(await self.store.prepare(-456,-123,('Diamante',)))['delivery']
            cog,message=self.bot()
            self.assertTrue(await cog._fulfill_delivery(delivery,message.channel,message.guild))
            self.assertEqual((await self.store.summary(-456,-123))['state'],'delivered')
            with self.assertRaises(ValueError):
                await self.store.save(self.message,[Receipt(0,Decimal(2),'USD','paypal',None)],['late'])
        self.assertEqual(await self.conn.fetchval("SELECT count(*) FROM pagos WHERE comprobante_hash IS NOT NULL"),1)

    async def test_previously_registered_binance_delivery_continues_but_new_binance_is_blocked(self):
        with patch('payment_policy.campaign_active',return_value=False):
            await self.store.save(self.message,[Receipt(0,Decimal('4.5'),'USDT','binance','old-payment')],['old'])
        await self.conn.execute("UPDATE pagos SET entrega_actualizada=CURRENT_TIMESTAMP-INTERVAL '4 minutes'")
        self.assertEqual(len(await self.store.pending_purchases()),1)
        delivery=(await self.store.prepare(-456,-123,('Diamante',)))['delivery']
        await self.conn.execute("UPDATE pagos SET entrega_actualizada=CURRENT_TIMESTAMP-INTERVAL '4 minutes'")
        self.assertEqual(len(await self.store.recoverable()),1)
        cog,message=self.bot()
        self.assertTrue(await cog._fulfill_delivery(delivery,message.channel,message.guild))
        with self.assertRaises(ValueError):
            await self.store.save(self.message,[Receipt(0,Decimal('4.5'),'USDT','binance',None)],['new'])

    async def test_historical_paypal_without_campaign_marker_is_not_repriced(self):
        await self.conn.execute("""INSERT INTO pagos(user_id,ticket_id,monto,moneda,metodo_pago,entrega_estado,aviso_pendiente)
            VALUES(-123,-456,4.50,'USD','paypal','pendiente',TRUE)""")
        self.assertTrue((await self.store.prepare(-456,-123,('Diamante',))).get('blocked'))
        self.assertFalse(await self.store.pending_purchases())
        self.assertEqual(await self.conn.fetchval("SELECT monto FROM pagos WHERE moneda='USD'"),Decimal('4.50'))
