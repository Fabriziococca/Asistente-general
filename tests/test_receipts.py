import json
import unittest
from decimal import Decimal
from types import SimpleNamespace

from receipts import attachment_mime, choose_roles, money, parse_receipts, requested_roles


def reading(index=0, amount=4700, **changes):
    result = dict(archivo_indice=index, es_comprobante=True, monto=amount, moneda='ARS',
                  destinatario='Fabrizio Giovanni Cocca Ducay', cuenta_destino=None,
                  pago_completado=None, numero_operacion='E1792432861708X', metodo_pago='transferencia')
    result.update(changes)
    return result


def parse(*items, count=None):
    return parse_receipts(json.dumps({'comprobantes': items}), count or len(items))


class ReceiptTests(unittest.TestCase):
    def test_wilobank_receipt_without_completion_phrase_or_account(self):
        receipts, issues = parse(reading(banco_destino='WILOBANK S.A.'))
        self.assertFalse(issues)
        self.assertEqual(receipts[0].amount, Decimal('4700'))

    def test_account_is_enough_when_name_is_abbreviated(self):
        receipts, _ = parse(reading(destinatario='F. C.', cuenta_destino='3840200500000026286680'))
        self.assertEqual(len(receipts), 1)

    def test_other_recipient_requires_review(self):
        receipts, issues = parse(reading(destinatario='Otra persona'))
        self.assertFalse(receipts)
        self.assertTrue(issues)

    def test_pending_receipt_is_not_approved(self):
        receipts, issues = parse(reading(pago_completado=False))
        self.assertFalse(receipts)
        self.assertTrue(issues)

    def test_string_false_is_not_true(self):
        for field in ('es_comprobante', 'pago_completado'):
            with self.subTest(field=field):
                receipts, issues = parse(reading(**{field: 'false'}))
                self.assertFalse(receipts)
                self.assertTrue(issues)

    def test_two_images_cover_all_three_roles(self):
        receipts, issues = parse(reading(), reading(1, 6400, numero_operacion='E1792432948955Q'))
        self.assertFalse(issues)
        roles, missing = choose_roles(sum(r.amount for r in receipts), 'ARS', requested_roles('rango diamante, rango oro, rango plata'))
        self.assertEqual(roles, ('Diamante', 'Oro', 'Plata'))
        self.assertEqual(missing, 0)

    def test_partial_payment_is_not_an_ai_decision(self):
        roles, missing = choose_roles(Decimal(4700), 'ARS', ('Diamante', 'Oro', 'Plata'))
        self.assertFalse(roles)
        self.assertEqual(missing, 6400)

    def test_combo_and_overpayment(self):
        self.assertEqual(choose_roles(Decimal(6400), 'ARS')[0], ('Oro', 'Plata'))
        self.assertEqual(choose_roles(Decimal(5000), 'ARS', ('Diamante',))[0], ('Diamante',))
        self.assertFalse(choose_roles(Decimal(5000), 'ARS')[0])

    def test_binance_usd(self):
        receipts, issues = parse(reading(monto='4.50', moneda='USDT', destinatario='fabrococca',pay_id_destino='552346130', metodo_pago='binance'))
        self.assertFalse(issues)
        self.assertEqual(choose_roles(receipts[0].amount, 'USDT')[0], ('Diamante',))

    def test_missing_reading_does_not_discard_readable_receipt(self):
        receipts, issues = parse(reading(), count=2)
        self.assertEqual(len(receipts), 1)
        self.assertEqual(len(issues), 1)

    def test_literal_newline_in_ocr_field_is_tolerated(self):
        raw = json.dumps({'comprobantes': [reading(destinatario='Fabrizio\nCocca')]})
        receipts, issues = parse_receipts(raw.replace('\\n', '\n'), 1)
        self.assertEqual(len(receipts), 1)
        self.assertFalse(issues)

    def test_repeated_or_invalid_image_index_rejected(self):
        for items in ((reading(), reading()), (reading(2),), (reading(True),)):
            with self.subTest(items=items), self.assertRaises(ValueError):
                parse(*items)

    def test_invalid_amounts(self):
        for value in (0, -1, True, 'NaN', 'Infinity', None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                money(value)
        self.assertEqual(money('6.400,00'), 6400)
        self.assertEqual(money('6,400.00'), 6400)

    def test_role_questions_are_not_purchase_orders(self):
        self.assertFalse(requested_roles('¿Diamante incluye Oro y Plata?'))
        self.assertEqual(requested_roles('Quiero los tres'), ('Diamante', 'Oro', 'Plata'))

    def test_optional_operation_id_and_stable_key(self):
        first, _ = parse(reading(numero_operacion='E179-2432'))
        second, _ = parse(reading(numero_operacion='e1792432'))
        self.assertEqual(first[0].operation_key, second[0].operation_key)
        no_id, _ = parse(reading(numero_operacion=None))
        self.assertIsNone(no_id[0].operation_key)

    def test_mime_fallback_handles_missing_type(self):
        self.assertEqual(attachment_mime(SimpleNamespace(content_type=None, filename='pago.PNG')), 'image/png')
        self.assertIsNone(attachment_mime(SimpleNamespace(content_type=None, filename='archivo.txt')))
