"""Medios de cobro vigentes y datos públicos de destino, compartidos por todo el bot."""

from decimal import Decimal
import re


BANK_HOLDER = 'Fabrizio Giovanni Cocca Ducay'
BANK_ACCOUNT = '3840200500000026286680'
BANK_ALIAS = 'LENGUA.LUJOSA.TELAR'
BINANCE_PAY_ID = '552346130'
# Solo ayuda a leer comprobantes que no muestran el ID. No se publica como dato de cobro.
BINANCE_RECIPIENT_ALIASES = ('fabrococca',)

PRICES = {
    'Diamante': {'ARS': Decimal('4700'), 'USDT': Decimal('4.5')},
    'Oro': {'ARS': Decimal('4200'), 'USDT': Decimal('4')},
    'Plata': {'ARS': Decimal('2200'), 'USDT': Decimal('2')},
}
SUGGESTION_PRICES = {'ARS': Decimal('4000'), 'USDT': Decimal('4')}
CURRENT_METHODS = {'ARS': 'transferencia', 'USDT': 'binance'}
LEGACY_BANK_METHODS = {'transferencia', 'mercadopago', 'uala', 'otro', None}

BANK_DETAILS = (f'Titular: **{BANK_HOLDER}**.\n'
                f'Alias: `{BANK_ALIAS}`\nCBU/CVU: `{BANK_ACCOUNT}`')
BINANCE_DETAILS = f'**Binance Pay · USDT**\nPay ID: `{BINANCE_PAY_ID}`\nEnviá USDT por Binance Pay al ID indicado.'
PAYMENT_DETAILS = ('**Pesos argentinos · transferencia a CBU/CVU**\n' + BANK_DETAILS +
                   '\n\n' + BINANCE_DETAILS)
PAYMENT_METHODS = 'Solo aceptamos **transferencia en pesos argentinos a CBU/CVU** o **USDT por Binance Pay**.'
PRICE_LIST = ('💎 Diamante: **$4.700 ARS / 4,50 USDT**\n'
              '🥇 Oro: **$4.200 ARS / 4 USDT**\n🥈 Plata: **$2.200 ARS / 2 USDT**\n'
              'Los tres: **$11.100 ARS / 10,50 USDT**.\n'
              'Son permanentes y cada rango tiene su propio contenido; Diamante no incluye Oro ni Plata.')


def current_payment(currency, method, *, legacy_bank=False):
    currency = str(currency or '').upper()
    method = str(method).lower() if method is not None else None
    return (method == CURRENT_METHODS.get(currency) and currency in CURRENT_METHODS) or (
        legacy_bank and currency == 'ARS' and method in LEGACY_BANK_METHODS)


def obsolete_payment_reference(text):
    # Guardia de salida/contexto: las instrucciones anteriores no vuelven a publicarse.
    return bool(re.search(r'\bpay[\W_]*pal\b|[\w.+-]+@[\w.-]+\.[a-z]{2,}', str(text), re.I))


def payment_context(text):
    return '[Instrucciones de cobro anteriores omitidas: usar únicamente los medios vigentes.]' \
        if obsolete_payment_reference(text) else str(text)


def safe_payment_response(text):
    return PAYMENT_METHODS + '\n\n' + PAYMENT_DETAILS if obsolete_payment_reference(text) else str(text)
