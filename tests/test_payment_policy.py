import json
import unittest
from decimal import Decimal
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

from communication import local_answer, message_chunks, history_line
from finance import financial_totals
from payment_policy import BINANCE_PAY_ID, PAYMENT_DETAILS, current_payment
from receipts import parse_receipts, choose_roles, vision_prompt
from test_receipts import reading
from test_tickets import fixture, attachment, delivery
from ticket_messages import ticket_welcome


class PaymentPolicyTests(unittest.IsolatedAsyncioTestCase):
    def test_only_supported_method_currency_pairs(self):
        for currency,method in [('USD','paypal'),('ARS','paypal'),('USD','binance'),
                                ('BTC','binance'),('USDT','otro'),('ARS','otro')]:
            with self.subTest(currency=currency,method=method):
                self.assertFalse(current_payment(currency,method))
        self.assertTrue(current_payment('ARS','transferencia'))
        self.assertTrue(current_payment('USDT','binance'))
        self.assertTrue(current_payment('ARS','mercadopago',legacy_bank=True))

    def test_retired_receipt_cannot_pass_even_with_matching_bank_holder(self):
        for currency in ('USD','ARS','USDT'):
            receipts,issues=parse_receipts(json.dumps({'comprobantes':[
                reading(metodo_pago='paypal',moneda=currency)]}),1)
            self.assertFalse(receipts)
            self.assertTrue(issues)

    def test_binance_id_allows_changed_nickname(self):
        receipts,issues=parse_receipts(json.dumps({'comprobantes':[
            reading(monto='4.50',moneda='USDT',metodo_pago='binance',
                    destinatario='UnApodoNuevo',pay_id_destino=BINANCE_PAY_ID)]}),1)
        self.assertFalse(issues)
        self.assertEqual(choose_roles(receipts[0].amount,'USDT')[0],('Diamante',))

    def test_other_binance_recipient_is_not_accepted_by_nickname(self):
        receipts,issues=parse_receipts(json.dumps({'comprobantes':[
            reading(moneda='USDT',metodo_pago='binance',destinatario='fabrococca',pay_id_destino='999999999')]}),1)
        self.assertFalse(receipts)
        self.assertTrue(issues)

    def test_usd_quote_is_not_a_usdt_payment(self):
        receipts,issues=parse_receipts(json.dumps({'comprobantes':[
            reading(moneda='USD',metodo_pago='binance',pay_id_destino=BINANCE_PAY_ID)]}),1)
        self.assertFalse(receipts)
        self.assertEqual(choose_roles(Decimal('4.50'),'USD'),((),None))

    def test_retired_method_questions_only_return_current_details(self):
        for question in ('¿Aceptan PayPal?','¿Se puede pagar con PAY PAL?', '¿Pay-pal todavía?'):
            for suggestion in (True,False):
                answer=local_answer(question,suggestion=suggestion)
                self.assertIsNotNone(answer)
                self.assertNotIn('paypal',answer.lower())
                self.assertIn('Binance Pay',answer)
                self.assertIn('CBU/CVU',answer)

    def test_welcome_and_prompt_use_only_confirmed_current_details(self):
        for suggestion in (True,False):
            content,embed=ticket_welcome(suggestion)
            body=content+json.dumps(embed.to_dict(),ensure_ascii=False)
            self.assertNotIn('paypal',body.lower())
            self.assertNotIn('@gmail',body.lower())
            self.assertIn(BINANCE_PAY_ID,body)
            self.assertIn('USDT',body)
            self.assertTrue(all(len(field.value)<=1024 for field in embed.fields))
        prompt=vision_prompt(2)
        self.assertIn(BINANCE_PAY_ID,prompt)
        self.assertNotIn('paypal',prompt.lower())

    def test_old_instructions_do_not_reenter_model_context(self):
        old=NS(author=NS(bot=True,id=1),content='Mandá PayPal al correo viejo@example.org',attachments=[])
        line=history_line(old,99)
        self.assertNotIn('PayPal',line)
        self.assertNotIn('viejo@example.org',line)

    def test_model_cannot_recommend_retired_method_or_email(self):
        for text in ('Podés pagar por PayPal','Mandá el pago a viejo@example.org'):
            answer=''.join(message_chunks(text))
            self.assertNotIn('paypal',answer.lower())
            self.assertNotIn('viejo@example.org',answer)
            self.assertIn(BINANCE_PAY_ID,answer)

    def test_finances_keep_historical_amounts_and_usdt_separate(self):
        totals=financial_totals([
            {'moneda':'USD','monto':Decimal('4.50'),'cantidad':2,'concepto':'Anterior'},
            {'moneda':'USDT','monto':Decimal('4.50'),'cantidad':1,'concepto':'Diamante'},
            {'moneda':'ARS','monto':4700,'cantidad':1,'concepto':'Diamante'}])
        self.assertEqual(totals['USD']['amount'],Decimal('9.00'))
        self.assertEqual(totals['USDT']['amount'],Decimal('4.50'))
        self.assertEqual(totals['ARS']['amount'],4700)

    async def test_finance_command_renders_usdt_and_historical_usd_separately(self):
        from datetime import datetime,timezone
        from cogs.tickets import Tickets
        cog,conn,_=fixture()
        conn.fetch=AsyncMock(return_value=[
            {'moneda':'USD','monto':Decimal('4.50'),'cantidad':2,'concepto':'Anterior'},
            {'moneda':'USDT','monto':Decimal('4.50'),'cantidad':1,'concepto':'Diamante'},
            {'moneda':'ARS','monto':4700,'cantidad':1,'concepto':'Diamante'}])
        interaction=NS(response=NS(defer=AsyncMock()),followup=NS(send=AsyncMock()),created_at=datetime.now(timezone.utc))
        await Tickets.ver_finanzas.callback(cog,interaction,mes=10,ano=2026)
        embed=interaction.followup.send.await_args.kwargs['embed']
        body=json.dumps(embed.to_dict(),ensure_ascii=False)
        self.assertIn('USD históricos',body)
        self.assertIn('USDT · Binance Pay',body)
        self.assertIn('9.00 USD',body)
        self.assertNotIn('paypal',body.lower())

    async def test_retired_known_receipt_does_not_reuse_ai_or_register_another_payment(self):
        cog,_,message=fixture()
        message.attachments=[attachment()]
        cog.payments.known.return_value={'user_id':123,'ticket_id':456,'moneda':'USD','metodo_pago':'paypal'}
        cog._generate_content_with_rotation=AsyncMock()
        await cog.handle_receipt_image(message)
        cog._generate_content_with_rotation.assert_not_awaited()
        cog.payments.save.assert_not_awaited()
        message.author.add_roles.assert_not_awaited()
        self.assertTrue(any('medio de cobro anterior' in (text or '') for text in message.channel.sent))

    async def test_retired_delivery_cannot_bypass_current_policy(self):
        cog,_,message=fixture()
        await cog._fulfill_delivery(dict(delivery(),moneda='USD',metodo_pago='paypal'),message.channel,message.guild)
        cog.payments.attempt.assert_not_awaited()
        message.author.add_roles.assert_not_awaited()

    def test_existing_ticket_message_is_rewritten_with_current_welcome(self):
        from scripts.refresh_payment_messages import replacement
        result=replacement({'content':'Bienvenida','embeds':[{
            'title':'🛒 Zona de Compras','fields':[{'name':'PayPal','value':'viejo@example.org'}]}]})
        body=json.dumps(result,ensure_ascii=False)
        self.assertNotIn('PayPal',body)
        self.assertNotIn('viejo@example.org',body)
        self.assertIn(BINANCE_PAY_ID,body)

    def test_existing_payment_record_message_keeps_amount_and_status(self):
        from scripts.refresh_payment_messages import replacement
        result=replacement({'content':'Comprobante registrado: 4.50 USD por PayPal.','embeds':[]})
        self.assertIn('Comprobante registrado: 4.50 USD',result['content'])
        self.assertNotIn('PayPal',result['content'])

    def test_owner_announcement_keeps_roles_and_navigation(self):
        from scripts.refresh_payment_messages import owner_replacement
        result=owner_replacement('Rango <@&123>: 4.5 USD\nInternacionales: PayPal y Binance Pay.\nTocá el botón de abajo.')
        self.assertIn('<@&123>: 4.5 USDT',result)
        self.assertIn(BINANCE_PAY_ID,result)
        self.assertIn('Tocá el botón de abajo.',result)
        self.assertNotIn('PayPal',result)
