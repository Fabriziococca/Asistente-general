"""Respuestas locales, contexto verificable y envío acotado a Discord."""

import asyncio
import re

import discord

from receipts import normalize
from payment_policy import (PAYMENT_DETAILS, PAYMENT_METHODS, PRICE_LIST,
                            BANK_HOLDER, bank_holder_query, payment_context,
                            safe_payment_response, obsolete_payment_reference)


PAYMENT_QUERY = r'\b(alias|cbu|cvu|titular|datos de pago|como (pago|pagar)|binance|usdt|usd|dolares|metodos? de pago|tarjeta|cripto|\w*pay\w*)\b'


def local_answer(content, *, suggestion=False, purchase=None):
    text = re.sub(r'<@!?\d+>', '', normalize(content)).strip(' ¡!¿?.\n')
    if bank_holder_query(content):
        return f'El titular de la cuenta para transferencias en pesos es **{BANK_HOLDER}**.'
    if suggestion:
        if obsolete_payment_reference(content) or re.search(PAYMENT_QUERY, text) or re.search(r'\b(pagar|pago|precio|cuanto)\b', text):
            return ("La petición cuesta **$4.000 ARS / 4 USDT**. Primero mandá la plataforma de contenido "
                    "y su red social; esperá la confirmación de disponibilidad de administración antes de pagar. " + PAYMENT_METHODS)
        if re.fullmatch(r'(hola|buenas|buen dia|buenas tardes|buenas noches)( bot)?', text):
            return "¡Hola! Mandame la plataforma de contenido y la red social de la modelo que querés pedir. Administración revisará si está disponible."
        return None
    if re.search(r'\b(ya pague|ya transferi|ya mande|ya envie|mande la foto|pase la foto|comprobante|mi rango|mi rol|mis rangos|no me (llego|llegaron|dieron|da|dan)|donde (esta|estan))\b', text):
        state = (purchase or {}).get('state')
        if state == 'delivered':
            return "Los rangos registrados para esta compra ya fueron entregados. Si no podés ver algún canal, decime cuál para revisar el acceso."
        if state == 'delivery_pending':
            return "Tu comprobante está registrado y la entrega de rangos sigue pendiente. Administración ya tiene el caso para revisar; no vuelvas a pagar."
        if state == 'receipt_pending':
            if (purchase or {}).get('pending_roles'):
                return (f"El pedido sigue abierto para **{', '.join(purchase['pending_roles'])}**. "
                        "Los comprobantes que recibí quedan registrados. Si ya mandaste todo, administración tiene que revisar el pedido completo; no vuelvas a pagar.")
            return "Tu comprobante quedó registrado. Decime qué rangos querés para comprobar el saldo y completar la entrega; no vuelvas a pagar."
        if (purchase or {}).get('has_attachment'):
            return "Veo que ya mandaste un archivo, pero todavía no tengo una validación confirmada. Voy a pedir que revisen el caso; no vuelvas a pagar."
        return "Para revisar tu compra necesito la foto o PDF del comprobante acá y qué rangos querés. El bot no está conectado al banco; no hace falta pagar otra vez."
    if obsolete_payment_reference(content) or re.search(PAYMENT_QUERY, text):
        return PAYMENT_METHODS + '\n\n' + PAYMENT_DETAILS + "\nDespués mandá el comprobante y los rangos que elegiste."
    if re.search(r'\b(precio|precios|cuanto|cuestan|cuesta|valor|valen)\b', text):
        return PRICE_LIST
    if re.search(r'\b(incluye|incluyen|diferencia|diferencias|desbloquea|desbloquean)\b', text):
        return ("Cada rango da acceso a su propio catálogo. **Diamante no incluye Oro ni Plata**; para ver todo necesitás los tres. "
                "Oro y Diamante habilitan los canales extra; Plata no.\n" + PRICE_LIST)
    if re.search(r'\b(dura|duran|duracion|vence|vencen|renovar|mensual|permanente|permanentes)\b', text):
        return "Los rangos son **permanentes**, sin renovación mensual. Cada uno habilita únicamente su propio contenido."
    if re.search(r'\b(no (encuentro|veo)|no puedo ver|no aparece|no me aparece)\b', text):
        if (purchase or {}).get('state') == 'delivered':
            return ("La entrega de tu compra figura completada. Oro y Diamante habilitan los canales extra, incluida Comunidad. "
                    "Si un canal no aparece, administración tiene que revisar sus permisos; no compres el rango otra vez.")
        return "Decime qué rango tenés y qué canal no aparece, así podemos revisar el acceso. Cada rango habilita su propio contenido."
    if re.fullmatch(r'(hola|buenas|buen dia|buenas tardes|buenas noches)( bot)?', text):
        return "¡Hola! ¿Querés conocer los rangos o necesitás ayuda con una compra? Si ya pagaste, mandá el comprobante y los rangos que elegiste."
    if re.fullmatch(r'(muchas )?gracias( bot)?|dale gracias|perfecto gracias', text):
        return "¡De nada! Si necesitás algo más con tu acceso, decime."
    if re.search(r'\b(entrega|demora|tarda)\b', text):
        return "La entrega es automática cuando el comprobante y los rangos quedan confirmados. Si hay una demora técnica, se conserva el ticket y administración puede ayudarte."
    if re.fullmatch(r'adios( bot)?|chau|hasta luego', text):
        return "¡Hasta luego! Si necesitás algo más, escribí por acá."
    return None


def message_chunks(text, limit=1900, *, query=''):
    text = safe_payment_response(re.sub(r'\[GRANT_ROLE:[^\]]*\]', '', str(text)).strip(),
                                 allow_bank_holder=bank_holder_query(query))
    if not text:
        text = 'Decime qué necesitás revisar y te ayudo.'
    chunks = []
    while text:
        end = min(len(text), limit)
        if end < len(text):
            split = max(text.rfind('\n', 0, end), text.rfind(' ', 0, end))
            if split > limit // 2:
                end = split
        chunks.append(text[:end])
        text = text[end:].lstrip()
    return chunks


async def send_response(message, text):
    """Si falla el envío, propaga el error para que la bandeja lo reintente."""
    result = None
    for chunk in message_chunks(text, query=getattr(message, 'content', '')):
        for attempt in range(3):
            try:
                result = await message.reply(chunk, allowed_mentions=discord.AllowedMentions.none(),
                                             mention_author=False)
                break
            except discord.HTTPException as exc:
                if exc.status not in (429, 500, 502, 503, 504) or attempt == 2:
                    raise
                await asyncio.sleep(min(float(getattr(exc, 'retry_after', 1) or 1), 5))
    return result


def author_label(message, owner_id):
    if message.author.bot:
        return 'BOT'
    permissions = getattr(message.author, 'guild_permissions', None)
    if message.author.id == owner_id or getattr(permissions, 'administrator', False):
        return 'ADMINISTRACION'
    return 'CLIENTE'


def history_line(message, owner_id):
    attachment_note = ' [adjuntó archivo]' if message.attachments else ''
    return f"{author_label(message, owner_id)}: {payment_context(message.content[:1600])}{attachment_note}"
