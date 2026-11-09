"""Respaldo OCR local. Cada archivo se procesa en un proceso con tiempo limitado."""

import asyncio
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sys
import time

from payment_policy import BANK_ACCOUNT, BANK_HOLDER, BINANCE_PAY_ID, RETIRED_PAYMENT_PATTERN
from receipts import money, normalize


MODEL_DIR = Path(__file__).resolve().parent / '.ocr'
MODEL_SHA256 = '7d4322bd2a7749724879683fc3912cb542f19906c83bcc1a52132556427170b2'
MODEL_URL = ('https://raw.githubusercontent.com/tesseract-ocr/tessdata_fast/'
             '923915d4ced2a7235221788285785a29c4a42d4a/eng.traineddata')


class ReceiptProcessingUnavailable(RuntimeError):
    """Los lectores fallaron técnicamente. El comprobante sigue siendo recuperable."""


def verify_model():
    if hashlib.sha256((MODEL_DIR / 'eng.traineddata').read_bytes()).hexdigest() != MODEL_SHA256:
        raise RuntimeError('El modelo OCR local no coincide con la versión verificada')


def receipt_from_text(text, index=0):
    """Extrae evidencia visible y deja las decisiones de pago al validador existente."""
    result = {'archivo_indice': index, 'es_comprobante': False}
    text = normalize(text)
    if re.search(RETIRED_PAYMENT_PATTERN, text, re.I):
        return result
    if not re.search(r'\b(comprobante|transferencia|enviaste|enviado|pagaste|binance|sent|payment)\b', text):
        return result

    destination = ''
    account = ''
    pay_id = ''
    # Limitarse a secciones del destinatario evita aceptar la cuenta del emisor.
    marker = r'\b(cuenta destino|titular cuenta destino|destinatario|beneficiario|receptor|para|recipient|sent to|to)\b'
    for match in re.finditer(marker, text):
        section = text[match.end():match.end() + 260]
        section = re.split(r'\b(cuenta debito|cuenta origen|emisor|desde|sender|from)\b', section)[0]
        digits = re.sub(r'\D', '', section)
        if BANK_ACCOUNT in digits:
            account = BANK_ACCOUNT
        if 'fabrizio' in section and 'cocca' in section:
            destination = BANK_HOLDER
        if re.search(r'\bbinance\b', text) and re.search(rf'(?<!\d){BINANCE_PAY_ID}(?!\d)', section):
            pay_id = BINANCE_PAY_ID
    # Mercado Pago presenta emisor y receptor consecutivos bajo un único título.
    if 'origen y destino' in text:
        accounts = re.findall(r'(?<!\d)\d{22}(?!\d)', text)
        if len(accounts) == 2 and accounts[-1] == BANK_ACCOUNT:
            account = BANK_ACCOUNT

    is_binance = bool(re.search(r'\bbinance\b', text))
    if is_binance:
        if not pay_id or not re.search(r'\busdt\b', text):
            return result
        currency, method = 'USDT', 'binance'
    else:
        if not (account or destination) or re.search(r'\b(usd|usdt|eur|mxn|clp|brl)\b', text):
            return result
        currency, method = 'ARS', 'transferencia'

    amounts = set()
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    amount_label = (r'\b(importe(?: debitado| transferido| enviado)?|monto(?: transferido| enviado| total)?|'
                    r'total (?:transferido|enviado|pagado)|cantidad enviada|amount|enviaste|pagaste|'
                    r'has enviado|sent amount)\b')
    for position, line in enumerate(lines):
        label = re.search(amount_label, line)
        if label:
            value = line[label.end():].strip(' :')
            if not re.search(r'\d', value) and position + 1 < len(lines):
                value = lines[position + 1]
        elif re.fullmatch(r'(?:\$|ars|usdt)\s*[\d.,]+(?:\s*(?:ars|usdt))?|[\d.,]+\s*(?:ars|usdt)', line):
            value = line
        else:
            continue
        match = re.fullmatch(r'(?:\$|ars|usdt|s)?\s*(\d[\d.,]*)(?:\s*(?:ars|usdt))?', value)
        if match:
            try:
                amounts.add(money(match[1], currency))
            except ValueError:
                pass
    if len(amounts) != 1:
        return result
    operation = re.search(
        r'\b(?:(?:numero|nro|n[.°º]+)\s*(?:de\s*)?)?(?:operacion|comprobante|transaccion|transaction id|order id)'
        r'(?: de mercado pago)?\s*[:#]?\s*([a-z0-9-]{6,})\b', text)
    pending = bool(re.search(r'\b(pendiente|fallida|fallido|rechazada|cancelada|sin ejecutar|en proceso|'
                             r'no completad[oa]|pending|failed|declined|cancelled|in progress|not completed)\b', text))
    result.update(es_comprobante=True, monto=str(next(iter(amounts))), moneda=currency,
                  metodo_pago=method, destinatario=destination, cuenta_destino=account,
                  pay_id_destino=pay_id, pago_completado=False if pending else None,
                  numero_operacion=operation[1] if operation else None)
    return result


def _recognize(image):
    from PIL import ImageOps
    from tesserocr import PyTessBaseAPI, OEM, PSM

    image.thumbnail((2000, 3000))
    with ImageOps.grayscale(image) as gray:
        with PyTessBaseAPI(path=str(MODEL_DIR), lang='eng', oem=OEM.LSTM_ONLY, psm=PSM.AUTO) as api:
            api.SetImage(gray)
            return api.GetUTF8Text()


def read_file(data, mime):
    """Los originales y el texto OCR solo permanecen en memoria del trabajador."""
    from PIL import Image, ImageOps

    verify_model()
    if mime != 'application/pdf':
        Image.MAX_IMAGE_PIXELS = 12_000_000
        with Image.open(io.BytesIO(data)) as original:
            if original.width * original.height > 12_000_000:
                raise ValueError('Imagen demasiado grande para OCR local')
            with ImageOps.exif_transpose(original) as image:
                return receipt_from_text(_recognize(image))

    import pypdfium2 as pdfium
    with pdfium.PdfDocument(data) as document:
        if len(document) > 5:
            raise ValueError('PDF demasiado extenso para OCR local')
        text = []
        for page in document:
            try:
                textpage = page.get_textpage()
                try:
                    text.append(textpage.get_text_range())
                finally:
                    textpage.close()
            finally:
                page.close()
        extracted = receipt_from_text('\n'.join(text))
        if extracted['es_comprobante']:
            return extracted
        text = []
        for page in document:
            try:
                width, height = page.get_size()
                scale = min(2, (5_000_000 / max(1, width * height)) ** 0.5)
                bitmap = page.render(scale=scale)
                try:
                    with bitmap.to_pil() as image:
                        text.append(_recognize(image))
                finally:
                    bitmap.close()
            finally:
                page.close()
        return receipt_from_text('\n'.join(text))


async def read_receipts(parts):
    results = []
    deadline = time.monotonic() + 40
    for index, part in enumerate(parts):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            results.append({'archivo_indice': index, 'es_comprobante': False})
            continue
        process = await asyncio.create_subprocess_exec(
            sys.executable, str(Path(__file__).resolve()), part['mime_type'],
            env=dict(os.environ, OMP_THREAD_LIMIT='1'),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        try:
            async with asyncio.timeout(min(20, remaining)):
                output, _ = await process.communicate(part['data'])
            if process.returncode != 0:
                raise RuntimeError('El trabajador OCR no pudo completar la lectura')
            item = json.loads(output)
            item['archivo_indice'] = index
            results.append(item)
        except (TimeoutError, ValueError, RuntimeError):
            results.append({'archivo_indice': index, 'es_comprobante': False})
        finally:
            if process.returncode is None:
                process.kill()
            await process.wait()
    return json.dumps({'comprobantes': results})


if __name__ == '__main__':
    data = sys.stdin.buffer.read(18 * 1024 * 1024 + 1)
    if len(data) > 18 * 1024 * 1024:
        raise ValueError('Archivo demasiado grande')
    print(json.dumps(read_file(data, sys.argv[1])))
