import json
import unittest
from decimal import Decimal
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

from communication import local_answer, message_chunks, history_line
from finance import financial_totals
from payment_policy import (BINANCE_PAY_ID, BANK_HOLDER, BINANCE_RECIPIENT_ALIASES,
                            PAYMENT_DETAILS, current_payment, private_identity_reference,
                            bank_holder_query, public_message_needs_refresh)
from receipts import parse_receipts, choose_roles, vision_prompt
from test_receipts import reading
from test_tickets import fixture, attachment, delivery
from ticket_messages import ticket_welcome
from ticket_inbox import ReceiptReviewRequired


class PaymentPolicyTests(unittest.IsolatedAsyncioTestCase):
    def test_identity_is_not_published_in_generic_payment_answers_or_welcomes(self):
        for question in ('hola', 'datos de pago', 'cómo pago', 'pasame el alias', 'binance'):
            for suggestion in (False, True):
                self.assertFalse(private_identity_reference(local_answer(question, suggestion=suggestion)))
        self.assertFalse(private_identity_reference(PAYMENT_DETAILS))
        for suggestion in (False, True):
            content, embed = ticket_welcome(suggestion)
            self.assertFalse(private_identity_reference(content + json.dumps(embed.to_dict())))

    def test_holder_is_confirmed_only_after_explicit_request_without_google(self):
        for question in ('¿Quién es el titular?', 'titular', 'A nombre de quién transfiero',
                         f'¿El titular es {BANK_HOLDER}?', 'Decime el titular de la cuenta'):
            for suggestion in (False, True):
                self.assertTrue(bank_holder_query(question))
                answer = local_answer(question, suggestion=suggestion)
                self.assertIn(BANK_HOLDER, answer)
                self.assertIn(BANK_HOLDER, ''.join(message_chunks(answer, query=question)))

    def test_receipt_text_is_not_an_identity_request(self):
        for question in ('Ya pagué, el titular estaba bien', 'Datos de pago', 'No publiques el titular',
                         'Quién es el dueño del servidor', 'hola'):
            self.assertFalse(bank_holder_query(question))

    def test_binance_questions_do_not_disclose_banking_identity(self):
        for question in ('¿Quién es el titular de Binance?', 'Quién recibe USDT',
                         'Nombre del destinatario de Binance', '¿El receptor de Binance es el mismo?'):
            self.assertFalse(bank_holder_query(question))
            self.assertFalse(private_identity_reference(local_answer(question)))
        self.assertTrue(bank_holder_query('¿Quién es el titular del CBU? Pago en pesos, no Binance'))

    def test_unsolicited_identity_from_model_is_blocked(self):
        for name in (BANK_HOLDER, BANK_HOLDER.upper(), BANK_HOLDER.split()[0],
                     '**' + BANK_HOLDER.replace(' ', '** **') + '**', BINANCE_RECIPIENT_ALIASES[0]):
            answer = ''.join(message_chunks('Podés pagar a ' + name, query='Pasame los datos de pago'))
            self.assertFalse(private_identity_reference(answer))
            self.assertIn(BINANCE_PAY_ID, answer)

    def test_identity_from_old_history_is_omitted(self):
        for name in (BANK_HOLDER, BINANCE_RECIPIENT_ALIASES[0]):
            old = NS(author=NS(bot=True,id=1),content='Titular: ' + name,attachments=[])
            self.assertFalse(private_identity_reference(history_line(old,99)))

    async def test_holder_confirmation_passes_through_discord_response(self):
        cog, _, message = fixture()
        message.content = '¿Quién es el titular de la cuenta?'
        cog._generate_content_with_rotation = AsyncMock()
        await cog.handle_support_query(message)
        cog._generate_content_with_rotation.assert_not_awaited()
        self.assertIn(BANK_HOLDER, message.reply.await_args.args[0])

    async def test_model_response_cannot_publish_holder_on_unrelated_query(self):
        cog, _, message = fixture()
        message.content = 'Necesito ayuda con una opción poco común'
        cog._answer_with_acknowledgement = AsyncMock(return_value='El titular es ' + BANK_HOLDER)
        await cog.handle_support_query(message)
        self.assertFalse(private_identity_reference(message.reply.await_args.args[0]))
        prompt = cog._answer_with_acknowledgement.await_args.args[1]
        self.assertNotIn(BANK_HOLDER, prompt)

    def test_existing_personalized_welcomes_are_detected_and_rebuilt(self):
        from scripts.refresh_payment_messages import replacement
        old = {'content':'Bienvenida','embeds':[{'title':'🛒 Zona de Compras',
                'fields':[{'name':'Datos de pago','value':'Titular: ' + BANK_HOLDER}]}]}
        self.assertTrue(public_message_needs_refresh(json.dumps(old)))
        result = replacement(old)
        self.assertFalse(public_message_needs_refresh(json.dumps(result)))
        self.assertIn(BINANCE_PAY_ID, json.dumps(result))

    def test_existing_personalized_status_keeps_payment_evidence(self):
        from scripts.refresh_payment_messages import replacement
        old = {'content':f'Comprobante registrado: 4700 ARS. Titular: {BANK_HOLDER}.','embeds':[]}
        result = replacement(old)
        self.assertIn('Comprobante registrado: 4700 ARS', result['content'])
        self.assertFalse(private_identity_reference(result['content']))

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

    def test_changed_nickname_without_visible_id_is_accepted_with_audit_notice(self):
        receipts,issues=parse_receipts(json.dumps({'comprobantes':[
            reading(monto='4.50',moneda='USDT',metodo_pago='binance',destinatario='UnApodoNuevo')]}),1)
        self.assertEqual(len(receipts),1)
        self.assertTrue(any('revisar destinatario' in issue for issue in issues))

    def test_missing_binance_recipient_does_not_look_like_an_anonymous_nickname(self):
        for recipient in (None,'','null','no visible','desconocido'):
            receipts,issues=parse_receipts(json.dumps({'comprobantes':[
                reading(monto='4.50',moneda='USDT',metodo_pago='binance',destinatario=recipient)]}),1)
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
        with self.assertRaises(ReceiptReviewRequired):
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

    async def test_permissive_binance_audit_notice_is_not_a_failed_reading(self):
        cog,conn,message=fixture()
        conn.row={'estado':'abierto','user_id':message.author.id}
        message.attachments=[attachment()]
        cog._generate_content_with_rotation=AsyncMock(return_value=json.dumps({'comprobantes':[
            reading(monto='4.50',moneda='USDT',metodo_pago='binance',destinatario='UnApodoNuevo')]}))
        cog.payments.save.return_value=[dict(pago_id=1,user_id=message.author.id,ticket_id=message.channel.id)]
        cog._handle_pending_purchase=AsyncMock(return_value=True)
        await cog._process_inbox_message(message)
        cog.inbox.done.assert_awaited_once()
        cog.inbox.manual.assert_not_awaited()
        cog.inbox.retry.assert_not_awaited()
        self.assertTrue(any('revisar destinatario' in (text or '') for text in message.channel.sent))

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
