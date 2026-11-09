import json
import unittest
from decimal import Decimal
from unittest.mock import AsyncMock

from payment_policy import BANK_ACCOUNT, BANK_HOLDER, BINANCE_PAY_ID
from receipt_ocr import receipt_from_text, ReceiptProcessingUnavailable
from receipts import parse_receipts
from test_tickets import fixture, attachment
from test_receipts import reading


class OCRParsingTests(unittest.TestCase):
    def bank_text(self):
        return (f'Comprobante de Transferencia\nImporte debitado S 13.500,00\n'
                f'Cuenta destino {BANK_ACCOUNT}\nTitular cuenta destino {BANK_HOLDER}\n'
                'Cuenta debito CTA $ 999-000000/0\nNumero de comprobante 11001122')

    def test_santander_amount_account_and_operation_reach_existing_validator(self):
        result = receipt_from_text(self.bank_text())
        receipts, issues = parse_receipts(json.dumps({'comprobantes': [result]}), 1)
        self.assertEqual(issues, [])
        self.assertEqual(receipts[0].amount, Decimal('13500.00'))
        self.assertEqual(receipts[0].operation, '11001122')

    def test_sender_identity_does_not_validate_someone_elses_destination(self):
        text = (f'Comprobante de Transferencia\nImporte debitado $13.500,00\n'
                f'De {BANK_HOLDER}\nCuenta origen {BANK_ACCOUNT}\n'
                'Para otra persona\nCuenta destino 1111111111111111111111')
        self.assertFalse(receipt_from_text(text)['es_comprobante'])

    def test_pending_payment_remains_pending(self):
        result = receipt_from_text(self.bank_text() + '\nTransferencia pendiente')
        receipts, issues = parse_receipts(json.dumps({'comprobantes': [result]}), 1)
        self.assertEqual(receipts, [])
        self.assertIn('pendiente', str(issues))

    def test_conflicting_amounts_are_left_for_another_reader(self):
        self.assertFalse(receipt_from_text(self.bank_text() + '\nMonto enviado $14.000')['es_comprobante'])

    def test_binance_uses_explicit_recipient_and_actual_usdt(self):
        text = f'Binance Pay\nPayment completed\nAmount 13.00 USDT\nRecipient Pay ID {BINANCE_PAY_ID}'
        receipts, _ = parse_receipts(json.dumps({'comprobantes': [receipt_from_text(text)]}), 1)
        self.assertEqual((receipts[0].currency, receipts[0].amount), ('USDT', Decimal('13.00')))
        self.assertFalse(receipt_from_text(text.replace('USDT', 'USD'))['es_comprobante'])

    def test_retired_provider_cannot_be_reclassified_as_a_bank_transfer(self):
        self.assertFalse(receipt_from_text('PayPal\n' + self.bank_text())['es_comprobante'])


class OCRRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_gemini_outage_reads_all_files_and_uses_normal_payment_delivery(self):
        cog, conn, message = fixture()
        conn.row = {'estado': 'abierto', 'user_id': message.author.id}
        message.attachments = [attachment(b'one'), attachment(b'two')]
        cog._generate_content_with_rotation = AsyncMock(side_effect=RuntimeError('provider unavailable'))
        cog._read_local_receipts.return_value = json.dumps({'comprobantes': [reading(), reading(1, 7600)]})
        cog.payments.save.return_value = [dict(pago_id=1, user_id=123, ticket_id=456),
                                        dict(pago_id=2, user_id=123, ticket_id=456)]
        cog._handle_pending_purchase = AsyncMock(return_value=True)
        await cog._process_inbox_message(message)
        self.assertEqual(len(cog.payments.save.await_args.args[1]), 2)
        cog.inbox.done.assert_awaited_once()
        cog.inbox.manual.assert_not_awaited()

    async def test_partial_gemini_result_keeps_first_receipt_and_recovers_second(self):
        cog, _, _ = fixture()
        cog._generate_content_with_rotation = AsyncMock(return_value=json.dumps({'comprobantes': [reading()]}))
        cog._read_local_receipts.return_value = json.dumps({'comprobantes': [reading(1, 7600)]})
        accepted, issues, outage = await cog._read_receipts([{}, {}])
        self.assertEqual({r.index for r in accepted}, {0, 1})
        self.assertEqual(sum(r.amount for r in accepted), Decimal(13500))
        self.assertEqual(issues, [])
        self.assertFalse(outage)

    async def test_explicitly_pending_gemini_result_cannot_be_approved_by_local_reader(self):
        cog, _, _ = fixture()
        cog._generate_content_with_rotation = AsyncMock(return_value=json.dumps({
            'comprobantes': [reading(pago_completado=False)]}))
        cog._read_local_receipts.return_value = json.dumps({'comprobantes': [reading()]})
        accepted, issues, _ = await cog._read_receipts([{}])
        self.assertEqual(accepted, [])
        self.assertIn('pendiente', str(issues))

    async def test_provider_outage_survives_more_than_three_attempts_without_repeated_alert(self):
        cog, conn, message = fixture()
        conn.row = {'estado': 'abierto', 'user_id': message.author.id}
        message.attachments = [attachment()]
        cog.inbox.claim.return_value = {'attempts': 4, 'error_type': 'ReceiptProcessingUnavailable'}
        cog._generate_content_with_rotation = AsyncMock(side_effect=RuntimeError('provider unavailable'))
        cog._request_manual_review = AsyncMock()
        await cog._process_inbox_message(message)
        cog._generate_content_with_rotation.assert_awaited_once()
        cog.inbox.manual.assert_not_awaited()
        cog.inbox.retry.assert_awaited_once()
        self.assertIsInstance(cog.inbox.retry.await_args.args[1], ReceiptProcessingUnavailable)
        cog._request_manual_review.assert_not_awaited()

    async def test_exhausted_ocr_outage_can_recover_when_provider_returns(self):
        cog, conn, message = fixture()
        conn.row = {'estado': 'abierto', 'user_id': message.author.id}
        message.attachments = [attachment()]
        cog.inbox.claim.return_value = {'attempts': 8, 'error_type': 'ReceiptProcessingUnavailable'}
        cog._generate_content_with_rotation = AsyncMock(return_value=json.dumps({'comprobantes': [reading()]}))
        cog.payments.save.return_value = [dict(pago_id=1, user_id=123, ticket_id=456)]
        cog._handle_pending_purchase = AsyncMock(return_value=True)
        await cog._process_inbox_message(message)
        cog.inbox.done.assert_awaited_once()
        cog.inbox.retry.assert_not_awaited()

    async def test_clear_purchase_intent_gets_correct_total_when_gemini_is_unavailable(self):
        cog, _, message = fixture()
        message.content = 'Los tres rangos'
        cog._answer_with_acknowledgement = AsyncMock(side_effect=RuntimeError('provider unavailable'))
        cog._request_manual_review = AsyncMock()
        await cog.handle_support_query(message)
        response = message.reply.await_args.args[0]
        self.assertIn('13.500 ARS', response)
        self.assertIn('13,00 USDT', response)
        cog._request_manual_review.assert_not_awaited()
