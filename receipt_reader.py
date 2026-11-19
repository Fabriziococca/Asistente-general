"""Motor de lectura compartido por el bot principal y la atención local."""
import asyncio
import os
import time

import google.generativeai as genai

from receipt_ocr import read_receipts as local_receipt_reading, ReceiptProcessingUnavailable
from receipts import parse_receipts, vision_prompt


class ReceiptReader:
    def __init__(self):
        self._ai_lock = asyncio.Lock()
        self._ocr_lock = asyncio.Lock()
        self._cooldowns = {}
        # Pool de 3 modelos funcionales solicitados para rotación
        self.model_pool = [
            "gemini-3.8-flash",
            "gemini-3.7-flash",
            "gemini-3.6-flash"
        ]

        # Setup Multi-API Key para evitar saturación
        self.api_keys = [os.environ.get(k) for k in os.environ.keys() if k.startswith("GEMINI_API_KEY") and os.environ.get(k)]
        self.current_key_index = 0 # Índice fijo para controlar la conmutación secuencial de cuotas

        if not self.api_keys:
            print("⚠️ [ADVERTENCIA] No se encontraron variables GEMINI_API_KEY en .env o Render.")
        else:
            print(f"✅ BÚNKER DE IA: {len(self.api_keys)} API Keys y pool de {len(self.model_pool)} modelos listos para conmutación.")

    # NUNCA MODIFICAR A NO SER QUE EL USUARIO LO PIDA
    # --- OMNI-PROTOCOLO SELECCIÓN Y ROTACIÓN HÍBRIDA DE CAPA GRATUITA ---
    async def _generate_content_with_rotation(self, prompt, image_parts=None):
        # El presupuesto incluye la espera por el SDK global, no solo la llamada HTTP.
        budget = 45 if image_parts else 35
        deadline = time.monotonic() + budget
        async with asyncio.timeout(budget):
            async with self._ai_lock:
                return await self._generate_content(prompt, image_parts, deadline)

    async def _generate_content(self, prompt, image_parts=None, deadline=None):
        if not self.api_keys:
            raise RuntimeError("No hay claves Gemini disponibles")
        deadline = deadline or time.monotonic() + (45 if image_parts else 35)
        for _ in range(len(self.api_keys)):
            key_index = self.current_key_index
            genai.configure(api_key=self.api_keys[key_index])
            # El chat prioriza el modelo comprobado más rápido; visión mantiene
            # el orden 3.8/3.7/3.6 para conservar la prioridad de lectura.
            models = self.model_pool if image_parts else sorted(
                self.model_pool,key=lambda name:name != 'gemini-3.6-flash')
            for model_name in models:
                if max(self._cooldowns.get((key_index, model_name), 0),
                       self._cooldowns.get(('service', model_name), 0)) > time.monotonic():
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Presupuesto de respuesta agotado")
                timeout = min(15 if image_parts else 12, remaining)
                started = time.monotonic()
                try:
                    model = genai.GenerativeModel(model_name)
                    options = {'request_options': {'timeout': timeout, 'retry': None}}
                    if image_parts:
                        options['generation_config'] = {'response_mime_type':'application/json','temperature':0}
                    response = await asyncio.wait_for(asyncio.to_thread(
                        model.generate_content, contents=[prompt, *(image_parts or [])], **options), timeout=timeout+0.5)
                    result = response.text.strip()
                    if not result:
                        raise ValueError("Respuesta vacía")
                    print(f"[IA] {model_name} key={key_index} ok segundos={time.monotonic()-started:.1f}", flush=True)
                    return result
                except Exception as exc:
                    kind = type(exc).__name__
                    cooldown = 60 if kind in ('ResourceExhausted','TooManyRequests') else 30
                    self._cooldowns[(key_index, model_name)] = time.monotonic() + cooldown
                    if kind == 'ServiceUnavailable':
                        # Un 503 es indisponibilidad del modelo, no una clave inválida.
                        # Evitar repetirlo con cada clave y cada mensaje del ticket.
                        self._cooldowns[('service', model_name)] = time.monotonic() + 120
                    print(f"[IA] {model_name} key={key_index} error={kind} segundos={time.monotonic()-started:.1f}; respaldo activado", flush=True)
            self.current_key_index = (key_index + 1) % len(self.api_keys)
        raise RuntimeError("La IA no está disponible temporalmente")

    async def _read_local_receipts(self, parts):
        async with asyncio.timeout(45):
            async with self._ocr_lock:
                return await local_receipt_reading(parts)

    async def _read_receipts(self, parts):
        provider_error = None
        parse_error = None
        try:
            text = await self._generate_content_with_rotation(vision_prompt(len(parts)), parts)
        except Exception as exc:
            provider_error = exc
            accepted, issues = [], []
        else:
            try:
                accepted, issues = parse_receipts(text, len(parts))
            except ValueError as exc:
                parse_error = exc
                accepted, issues = [], []
        if len(accepted) == len(parts):
            return accepted, issues, False
        try:
            text = await self._read_local_receipts(parts)
            local, local_issues = parse_receipts(text, len(parts))
            # Un estado explícitamente pendiente o un medio retirado no se rescata con OCR.
            blocked = {i for i in range(len(parts)) if any(
                str(issue).startswith(f'archivo {i + 1}:') and
                ('pendiente o sin ejecutar' in str(issue) or 'medio o moneda no admitidos' in str(issue))
                for issue in issues)}
            combined = {r.index: r for r in local if r.index not in blocked}
            recovered = set(combined) - {r.index for r in accepted}
            combined.update((r.index, r) for r in accepted)
            accepted = list(combined.values())
            issues = [issue for issue in [*issues, *local_issues] if not any(
                str(issue).startswith(f'archivo {i + 1}:') for i in combined)]
            if recovered:
                print(f'[OCR] Archivos recuperados={len(recovered)}', flush=True)
        except Exception as exc:
            print(f'[OCR] Lectura local incompleta error={type(exc).__name__}', flush=True)
        if not accepted:
            if provider_error:
                raise ReceiptProcessingUnavailable('Lectores temporalmente indisponibles') from provider_error
            if parse_error:
                raise parse_error
        return accepted, issues, provider_error is not None
