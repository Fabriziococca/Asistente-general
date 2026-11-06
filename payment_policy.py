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
CAMPAIGN_ID = 'paypal_flash_20261031'  # Marca histórica, nunca se aplica a pagos nuevos.


def campaign_active(now=None):
    return False


PRICES = {
    'Diamante': {'ARS': Decimal('5900'), 'USDT': Decimal('5.50')},
    'Oro': {'ARS': Decimal('5300'), 'USDT': Decimal('5.00')},
    'Plata': {'ARS': Decimal('2300'), 'USDT': Decimal('2.50')},
}
SUGGESTION_PRICES = {'ARS': Decimal('4000'), 'USDT': Decimal('4')}
NORMAL_METHODS = {'ARS': 'transferencia', 'USDT': 'binance'}
LEGACY_BANK_METHODS = {'transferencia', 'mercadopago', 'uala', 'otro', None}

BANK_DETAILS = f'Alias: `{BANK_ALIAS}`\nCBU/CVU: `{BANK_ACCOUNT}`'
BINANCE_DETAILS = f'**Binance Pay · USDT**\nPay ID: `{BINANCE_PAY_ID}`\nEnviá USDT por Binance Pay al ID indicado.'
PAYMENT_DETAILS = ('**Pesos argentinos · transferencia a CBU/CVU**\n' + BANK_DETAILS +
                   '\n\n' + BINANCE_DETAILS)
PAYMENT_METHODS = 'Solo aceptamos **transferencia en pesos argentinos a CBU/CVU** o **USDT por Binance Pay**.'
PRICE_LIST = ('💎 Diamante: **$5.900 ARS / 5,50 USDT**\n'
              '🥇 Oro: **$5.300 ARS / 5,00 USDT**\n🥈 Plata: **$2.300 ARS / 2,50 USDT**\n'
              'Los tres: **$13.500 ARS / 13,00 USDT**.\n'
              'Son permanentes y cada rango tiene su propio contenido; Diamante no incluye Oro ni Plata.')
def payment_copy():
    return PAYMENT_METHODS, PAYMENT_DETAILS, PRICE_LIST, '$4.000 ARS / 4 USDT'


def announcement_text():
    methods, details, prices, _ = payment_copy()
    return (prices + '\n\n' + details +
            '\n\nPodés elegir uno, dos o los tres rangos. Para comprar, abrí un ticket, '
            'indicá los rangos y adjuntá la foto o PDF del comprobante. Si hiciste varios pagos, '
            'podés enviarlos juntos. Si hay una demora técnica, conservamos el ticket y administración te ayuda; no vuelvas a pagar.')


def current_payment(currency, method, *, legacy_bank=False):
    currency = str(currency or '').upper()
    method = str(method).lower() if method is not None else None
    methods = NORMAL_METHODS
    return (method == methods.get(currency) and currency in methods) or (
        legacy_bank and currency == 'ARS' and method in LEGACY_BANK_METHODS)


def recorded_payment(row):
    """Compatibilidad defensiva de registros; jamás habilita un pago nuevo."""
    currency, method = row['moneda'], row['metodo_pago']
    return ((currency == 'ARS' and method in LEGACY_BANK_METHODS) or
            (currency == 'USDT' and method == 'binance') or
            (currency == 'USD' and method == 'paypal' and row.get('politica_precio') == CAMPAIGN_ID))


RETIRED_PAYMENT_PATTERN = r'\bpay[\W_]*pal\b'
EMAIL_PATTERN = r'[\w.+-]+@[\w.-]+\.[a-z]{2,}'


def obsolete_payment_reference(text):
    """Oculta ofertas anteriores y correos de la memoria y las respuestas."""
    text = str(text)
    return bool(re.search(RETIRED_PAYMENT_PATTERN, text, re.I) or re.search(EMAIL_PATTERN, text, re.I))


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
    bank_words = r'\b(banco|bancari\w*|pesos|ars|cbu|cvu|transf(?:er\w*|ier\w*))\b'
    if re.search(r'\bno (digas|publiques|muestres|reveles|quiero saber)\b', text):
        return False
    if re.search(r'\b(binance|usdt|pay\s*pal|usd|dolares|correo)\b', text) and not re.search(bank_words, text):
        return False
    if obsolete_payment_reference(raw) or not re.search(bank_words, text):
        return False
    holder = re.search(r'\btitular\b', text) and (
        '?' in raw or re.search(r'\b(quien|cual|como|confirma\w*|deci\w*|indic\w*|pasa\w*)\b', text)
        or re.fullmatch(r'(el )?(nombre del )?titular( de la (cuenta( bancaria)?|transferencia))?', text))
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
    methods, details, _, _ = payment_copy()
    if obsolete_payment_reference(text) or (private_identity_reference(text) and not allow_bank_holder):
        return methods + '\n\n' + details
    # El antiguo apodo tampoco se ofrece como dato de cobro al confirmar el titular bancario.
    if set(identity_text(text).split()).intersection(BINANCE_RECIPIENT_ALIASES):
        return methods + '\n\n' + details
    return str(text)
