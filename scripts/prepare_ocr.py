"""Instala y comprueba el modelo OCR público durante el build de Render."""

import hashlib
import io
from pathlib import Path
import sys
from urllib.request import urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from receipt_ocr import MODEL_DIR, MODEL_SHA256, MODEL_URL, verify_model, read_file
from payment_policy import BANK_ACCOUNT


def main():
    MODEL_DIR.mkdir(exist_ok=True)
    model = MODEL_DIR / 'eng.traineddata'
    if not model.exists() or hashlib.sha256(model.read_bytes()).hexdigest() != MODEL_SHA256:
        with urlopen(MODEL_URL, timeout=60) as response:
            data = response.read()
        if hashlib.sha256(data).hexdigest() != MODEL_SHA256:
            raise RuntimeError('La descarga del modelo OCR no coincide con la versión fijada')
        temporary = model.with_suffix('.tmp')
        temporary.write_bytes(data)
        temporary.replace(model)
    verify_model()
    from tesserocr import PyTessBaseAPI
    with PyTessBaseAPI(path=str(MODEL_DIR), lang='eng'):
        pass
    from PIL import Image, ImageDraw, ImageFont
    with Image.new('RGB', (1400, 600), 'white') as image:
        ImageDraw.Draw(image).multiline_text(
            (40, 40), 'Comprobante de Transferencia\n\nImporte debitado $ 1.234,00\n\n'
            f'Cuenta destino {BANK_ACCOUNT}\n\nNumero de comprobante 11001122',
            fill='black', font=ImageFont.load_default(size=30), spacing=10)
        for mime, format in [('image/png', 'PNG'), ('application/pdf', 'PDF')]:
            data = io.BytesIO()
            image.save(data, format=format)
            receipt = read_file(data.getvalue(), mime)
            if not receipt['es_comprobante'] or receipt['monto'] != '1234.00':
                raise RuntimeError('Falló la autoprueba del lector OCR')
    print('[OCR] Modelo verificado. Autoprueba de imagen y PDF correcta')


if __name__ == '__main__':
    main()
