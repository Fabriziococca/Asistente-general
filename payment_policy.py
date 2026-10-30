"""Medios de cobro vigentes y contexto interno del destinatario."""

from decimal import Decimal
import re
import unicodedata


# Contexto interno: solo confirmar ante una consulta explícita sobre el titular.
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

BANK_DETAILS = f'Alias: `{BANK_ALIAS}`\nCBU/CVU: `{BANK_ACCOUNT}`'
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


def identity_text(text):
    text = ''.join(c for c in unicodedata.normalize('NFKD', str(text)) if not unicodedata.combining(c))
    return re.sub(r'[\W_]+', ' ', text.casefold()).strip()


def private_identity_reference(text):
    words = set(identity_text(text).split())
    return bool(words.intersection(identity_text(BANK_HOLDER).split()) or
                words.intersection(BINANCE_RECIPIENT_ALIASES))


def bank_holder_query(text):
    """Solo preguntas explícitas; pedir datos de pago no autoriza publicar el nombre."""
    raw = str(text)
    text = identity_text(raw)
    if re.search(r'\bno (digas|publiques|muestres|reveles|quiero saber)\b', text):
        return False
    holder = re.search(r'\btitular\b', text) and (
        '?' in raw or re.search(r'\b(quien|cual|como|confirma\w*|deci\w*|indic\w*|pasa\w*)\b', text)
        or re.fullmatch(r'(el )?(nombre del )?titular( de la cuenta)?', text))
    return bool(holder or re.search(r'\ba nombre de quien\b|\bquien (recibe|cobra)\b|'
                          r'\bnombre (completo )?(del (destinatario|receptor)|de la cuenta)\b|'
                          r'\b(destinatario|receptor) (es|se llama)\b', text))


def public_message_needs_refresh(text):
    return obsolete_payment_reference(text) or private_identity_reference(text)


def payment_context(text):
    if obsolete_payment_reference(text):
        return '[Instrucciones de cobro anteriores omitidas: usar únicamente los medios vigentes.]'
    if private_identity_reference(text):
        return '[Datos personales omitidos del historial público.]'
    return str(text)


def safe_payment_response(text, *, allow_bank_holder=False):
    if obsolete_payment_reference(text) or (private_identity_reference(text) and not allow_bank_holder):
        return PAYMENT_METHODS + '\n\n' + PAYMENT_DETAILS
    # El antiguo apodo tampoco se ofrece como dato de cobro al confirmar el titular bancario.
    if set(identity_text(text).split()).intersection(BINANCE_RECIPIENT_ALIASES):
        return PAYMENT_METHODS + '\n\n' + PAYMENT_DETAILS
    return str(text)
