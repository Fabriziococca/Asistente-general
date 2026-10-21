import discord
from discord.ext import commands, tasks
from discord import app_commands
import asyncpg
import asyncio
import os
import google.generativeai as genai
import re
import datetime
import hashlib
import weakref

from payment_store import PaymentStore
from receipts import attachment_mime, parse_receipts, requested_roles, vision_prompt

# --- CONFIGURACIÓN DE NEGOCIO AND ACCESO HIGH-LEVEL ---
ID_CATEGORIA_SUGERENCIAS = 1510095191275737088
ID_CANAL_PROMO_TEST = 1503937966748205056
FABRIZIO_ID = 704501115110162542

# Configuración de Roles y Precios estricta (Sin puntos como separador de miles)
ROLES = {
    "Diamante": {"id": 1423389000122499083, "ars": 4700, "usd": 4.5},
    "Oro": {"id": 1423389001984905248, "ars": 4200, "usd": 4.0},
    "Plata": {"id": 1478203883661820106, "ars": 2200, "usd": 2.0}
}

# Diccionario de respuestas predefinidas para optimizar uso de API
RESPUESTAS_PREDEFINIDAS = {
    "hola bot": "¡Hola! Soy el asistente automatizado. Si tenés un comprobante, subilo. Si tenés dudas, preguntá.",
    "hola": "¡Hola! Soy el asistente automatizado. Si tenés un comprobante, subilo. Si tenés dudas, preguntá.",
    "entrega?": "La entrega es inmediata tras la validación automática de tu comprobante de pago.",
    "gracias bot": "¡De nada! Aquí estaré si necesitas algo más.",
    "gracias": "¡De nada! Aquí estaré si necesitas algo más.",
    "adios bot": "¡Hasta luego! El ticket se cerrará automáticamente en 24hs si no hay más actividad.",
    "adios": "¡Hasta luego! El ticket se cerrará automáticamente en 24hs si no hay más actividad."
}

class Tickets(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.mi_id = FABRIZIO_ID # Tu ID de usuario para detección de menciones
        self.payments = PaymentStore(bot.pool)
        self._ticket_locks = weakref.WeakValueDictionary()
        self._ai_lock = asyncio.Lock()
        
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

    async def cog_load(self):
        # Crear la tabla de tickets en NeonDB asegurando el uso de BIGINT para IDs
        query_tickets = """
            CREATE TABLE IF NOT EXISTS tickets (
                channel_id BIGINT PRIMARY KEY,
                user_id BIGINT,
                estado TEXT DEFAULT 'abierto',
                ultimo_mensaje TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                hablo BOOLEAN DEFAULT FALSE
            )
        """
        # Crear tabla de pagos alineada estrictamente al esquema real de tu NeonDB
        query_pagos = """
            CREATE TABLE IF NOT EXISTS pagos (
                pago_id SERIAL PRIMARY KEY,
                user_id BIGINT,
                ticket_id BIGINT,
                monto NUMERIC(10, 2),
                moneda TEXT,
                concepto TEXT,
                metodo_pago TEXT DEFAULT 'paypal',
                fecha_pago TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """
        try:
            async with self.bot.pool.acquire(timeout=10.0) as conn:
                await conn.execute("CREATE TABLE IF NOT EXISTS usuarios (user_id BIGINT PRIMARY KEY)")
                await conn.execute(query_tickets)
                await conn.execute(query_pagos)
                await conn.execute("ALTER TABLE tickets ADD COLUMN IF NOT EXISTS hablo BOOLEAN DEFAULT FALSE")
                await conn.execute("ALTER TABLE pagos ADD COLUMN IF NOT EXISTS metodo_pago TEXT DEFAULT 'paypal'")
            print("✅ [Tickets & Pagos] Tablas verificadas/creadas exitosamente.")
        except Exception as e:
            print(f"❌ [DB Error] Error al crear las tablas: {e}")
            raise

        await self.payments.initialize()
        # Iniciar la limpieza periódica y el remarketing automático
        self.cleanup_tickets.start()
        self.auto_promo_refresh.start()
        self.recover_deliveries.start()

    async def cog_unload(self):
        self.cleanup_tickets.cancel()
        self.auto_promo_refresh.cancel()
        self.recover_deliveries.cancel()

    # NUNCA MODIFICAR A NO SER QUE EL USUARIO LO PIDA
    # --- OMNI-PROTOCOLO SELECCIÓN Y ROTACIÓN HÍBRIDA DE CAPA GRATUITA ---
    async def _generate_content_with_rotation(self, prompt, image_parts=None):
        async with self._ai_lock:
            return await self._generate_content(prompt, image_parts)

    async def _generate_content(self, prompt, image_parts=None):
        """Ejecuta la conmutación de API Keys y salta secuencialmente por el pool configurado de modelos ante errores."""
        if not self.api_keys:
            raise Exception("No hay claves Gemini configuradas. La consulta requiere revisión manual.")
        
        # El sistema probará un ciclo entero por cada API Key disponible en tu pool
        for intento_key in range(len(self.api_keys)):
            llave_actual = self.api_keys[self.current_key_index]
            genai.configure(api_key=llave_actual)
            
            # Recorremos de forma secuencial el pool configurado de modelos validados para la llave activa
            for model_name in self.model_pool:
                try:
                    model = genai.GenerativeModel(model_name)
                    
                    if image_parts:
                        response = await asyncio.wait_for(
                            asyncio.to_thread(model.generate_content, contents=[prompt, *image_parts],
                                              generation_config={'response_mime_type': 'application/json', 'temperature': 0},
                                              request_options={'timeout': 25, 'retry': None}),
                            timeout=30.0
                        )
                    else:
                        response = await asyncio.wait_for(
                            asyncio.to_thread(model.generate_content, contents=[prompt],
                                              request_options={'timeout': 25, 'retry': None}),
                            timeout=30.0
                        )
                    return response.text.strip()
                    
                except Exception as e:
                    error_type = type(e).__name__
                    print(f"⚠️ [Advertencia IA] ({error_type}) Fallo con modelo '{model_name}' usando API Key índice {self.current_key_index}. Rotando al siguiente modelo disponible...")
                    continue # Salta al siguiente modelo disponible del pool configurado
            
            # Si el bucle de modelos termina sin retornar, la Key actual agotó los modelos
            indice_viejo = self.current_key_index
            self.current_key_index = (self.current_key_index + 1) % len(self.api_keys)
            print(f"🔄 [CONMUTACIÓN CRÍTICA] Pool de modelos agotado para API Key índice {indice_viejo}. Saltando a API Key índice {self.current_key_index} para reiniciar el ciclo completo del pool...")
            
        # Si salimos de ambos bucles, todas las API Keys agotaron de manera consecutiva el pool entero
        raise Exception("🚨 [EXCEPCIÓN CRÍTICA FINAL] Se agotaron todas las API Keys configuradas tras probar el pool configurado. La consulta requiere revisión manual.")

    async def _procesar_bienvenida_y_registro(self, channel: discord.TextChannel):
        """Registra el ticket en la DB de forma garantizada y envía la bienvenida si aún no fue enviada."""
        if not channel:
            return

        es_sugerencia = (getattr(channel, 'category_id', None) == ID_CATEGORIA_SUGERENCIAS or (hasattr(channel, 'name') and channel.name.startswith("sug-")))
        
        # Detectar el usuario en las overwrites
        user_id = None
        if hasattr(channel, 'overwrites'):
            for target in channel.overwrites:
                if isinstance(target, discord.Member) and not target.bot and not target.guild_permissions.administrator:
                    user_id = target.id
                    break

        # Guardado garantizado en DB
        try:
            async with self.bot.pool.acquire(timeout=10.0) as conn:
                if user_id is not None:
                    await conn.execute("INSERT INTO usuarios (user_id) VALUES ($1) ON CONFLICT (user_id) DO NOTHING", user_id)
                await conn.execute("""
                    INSERT INTO tickets (channel_id, user_id, estado, ultimo_mensaje, hablo)
                    VALUES ($1, $2, 'abierto', CURRENT_TIMESTAMP, FALSE)
                    ON CONFLICT (channel_id) DO NOTHING
                """, channel.id, user_id)
        except Exception as e:
            print(f"❌ [DB Error] No se pudo registrar ticket inicial {channel.id}: {e}")

        # Verificar si el bot ya envió el mensaje de bienvenida en este canal
        ya_envio_bienvenida = False
        try:
            async for m in channel.history(limit=10):
                if m.author == self.bot.user and m.embeds and ("Petición Única" in m.embeds[0].title or "Zona de Compras" in m.embeds[0].title):
                    ya_envio_bienvenida = True
                    break
        except Exception as e:
            print(f"⚠️ [Historial Warn] No se pudo leer historial en {channel.id} ({e}). Asumiendo que no envió bienvenida.")
            ya_envio_bienvenida = False
            
        if not ya_envio_bienvenida:
            if es_sugerencia:
                embed = discord.Embed(
                    title="💎 Petición Única de Modelos (Pre-Compra)", 
                    description="Elegí la chica que querés incorporar al catálogo de forma 100% privada o utilizá tus puntos.", 
                    color=0x1DB954 # Color Verde Premium Estilo Spotify
                )
                embed.add_field(
                    name="📋 PROCESO DE PRE-COMPRA", 
                    value=(
                        "1. **Enviá la plataforma de contenido (ej: OnlyFans, Fansly, etc.) Y su red social principal (Instagram, TikTok o Twitter)**.\n"
                        "2. **Aguardá a que confirmemos** la disponibilidad en este chat.\n"
                        "3. **Una vez confirmado**, realizá el pago ($4000 ARS / $4 USD) con los datos abajo y subí el comprobante."
                    ), 
                    inline=False
                )
                embed.add_field(
                    name="💰 DATOS DE PAGO (Pagar solo tras confirmación)", 
                    value=(
                        "🇦🇷 **Pesos (ARS)**:\n"
                        "• **Alias**: LENGUA.LUJOSA.TELAR\n"
                        "• **CBU**: 3840200500000026286680\n\n"
                        "🌍 **PayPal (USD)**:\n"
                        "• **Correo**: sesarjavier28@gmail.com (Monto exacto: $4 USD)"
                    ), 
                    inline=False
                )
                embed.add_field(
                    name="⚠️ COMPROMISO DE COMPRA (100%)",
                    value=(
                        "Iniciar la búsqueda implica un compromiso de pago obligatorio si confirmamos disponibilidad. "
                        "Si te confirmamos que está disponible y decidís no pagar, se te aplicará una advertencia o baneo por abuso de soporte."
                    ), 
                    inline=False
                )
                embed.add_field(
                    name="🎁 CANJEAR POR BUMPS (30 Bumps)",
                    value="Si tenés 30 Bumps acumulados, escribí: **'Quiero canjear mis puntos'** y aguardá la validación.",
                    inline=False
                )
                bienvenida = (
                    "¡Hola! Soy tu asistente automatizado para peticiones exclusivas. 🤖\n"
                    "Estoy acá para darte soporte y procesar tu solicitud bajo estricto anonimato.\n\n"
                )
            else:
                embed = discord.Embed(
                    title="🛒 Zona de Compras", 
                    description="Elegí tu rango y mirá los datos de pago abajo.", 
                    color=0x2B2D31 # Color oscuro de Discord
                )
                embed.add_field(
                    name="📉 LISTA DE PRECIOS", 
                    value="💎 Rango Diamante: 🇦🇷 Argentina: $4700 ARS 🌍 Internacional: $4,5 USD\n🥇 Rango Oro: 🇦🇷 Argentina: $4200 ARS 🌍 Internacional: $4 USD\n🥈 Rango Plata: 🇦🇷 Argentina: $2200 ARS 🌍 Internacional: $2 USD", 
                    inline=False
                )
                embed.add_field(name="Alias:", value="LENGUA.LUJOSA.TELAR", inline=False)
                embed.add_field(name="CBU:", value="3840200500000026286680", inline=False)
                embed.add_field(
                    name="🌍 DOLARES (PayPal) Enviar monto exacto a este correo:", 
                    value="sesarjavier28@gmail.com\n", 
                    inline=False
                )
                embed.add_field(
                    name="✅ ¿Ya pagaste? Seguí estos pasos:", 
                    value=(
                        "1. Aclará qué rango o rangos estás comprando\n"
                        "2. Enviá la FOTO o PDF del comprobante acá al chat *(el banco NO se vincula solo; el bot necesita ver la imagen para darte el rol)*\n"
                        "3. El bot valida la imagen y te entrega el rol al instante\n\n"
                        "❓ Si tenés dudas o problemas, etiquetá a @titocalderon y esperá a recibir ayuda\n\n"
                        "*(Si querés 2 o los 3 rangos juntos, podés transferir el total correspondiente según la combinación elegida.)*"
                    ), 
                    inline=False
                )
                bienvenida = (
                    "¡Hola! Soy tu asistente de ventas automatizado. 🤖\n"
                    "Estoy aquí para ayudarte a obtener tu rango de forma rápida.\n\n"
                )

            try:
                await channel.send(content=bienvenida, embed=embed)
            except discord.HTTPException as err:
                if err.status == 429:
                    print(f"⏳ [429 Rate Limit] Reintentando envío de bienvenida en {channel.id} en 5s...")
                    await asyncio.sleep(5.0)
                    try:
                        await channel.send(content=bienvenida, embed=embed)
                    except Exception as err2:
                        print(f"❌ Falló reintento de bienvenida en {channel.id}: {err2}")
                else:
                    print(f"❌ [Error HTTP] al enviar bienvenida en {channel.id}: {err}")
            except Exception as e:
                print(f"❌ [Error] al procesar bienvenida en {channel.id}: {e}")

    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel):
        """Lanza el mensaje de presentación y el Embed adaptado según la categoría del ticket."""
        if isinstance(channel, discord.TextChannel) and (channel.name.startswith("ticket-") or channel.name.startswith("sug-") or channel.category_id == ID_CATEGORIA_SUGERENCIAS):
            # Delay de seguridad de 10 segundos para dar tiempo a Ticket Tool de ajustar permisos
            await asyncio.sleep(10.0)
            
            refreshed = channel.guild.get_channel(channel.id)
            if refreshed:
                channel = refreshed
                
            await self._procesar_bienvenida_y_registro(channel)

    @commands.Cog.listener()
    async def on_ready(self):
        """Barrido de sincronización automática al arrancar para rescatar tickets creados durante desconexiones."""
        print("🔍 [Sync Ticket Audit] Iniciando escaneo de seguridad en canales de tickets...")
        for guild in self.bot.guilds:
            for channel in guild.text_channels:
                if channel.name.startswith("ticket-") or channel.name.startswith("sug-") or channel.category_id == ID_CATEGORIA_SUGERENCIAS:
                    try:
                        await self._procesar_bienvenida_y_registro(channel)
                    except Exception as e:
                        print(f"❌ Error en sync de ticket {channel.id}: {e}")
        print("✅ [Sync Ticket Audit] Escaneo completado. Todos los tickets están sincronizados en NeonDB.")

    def _ticket_lock(self, channel_id):
        lock = self._ticket_locks.get(channel_id)
        if lock is None:
            lock = asyncio.Lock()
            self._ticket_locks[channel_id] = lock
        return lock

    @commands.Cog.listener()
    async def on_member_remove(self, member):
        async with self.bot.pool.acquire(timeout=10) as conn:
            rows = await conn.fetch("SELECT channel_id FROM tickets WHERE user_id=$1", member.id)
        for row in rows:
            channel = member.guild.get_channel(row['channel_id'])
            if channel:
                await self._delete_ticket_if_eligible(channel, member_left=True)

    @app_commands.command(name="conservar_ticket", description="Impide que este bot borre automáticamente el ticket")
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def conservar_ticket(self, interaction: discord.Interaction):
        channel = interaction.channel
        if not self._is_ticket(channel):
            await interaction.response.send_message("Usá este comando dentro de un ticket.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            async with self._ticket_lock(channel.id):
                async with self.bot.pool.acquire(timeout=10) as conn:
                    await conn.execute("""INSERT INTO tickets (channel_id, conservar)
                        VALUES ($1, TRUE) ON CONFLICT (channel_id) DO UPDATE SET conservar=TRUE""", channel.id)
            await interaction.followup.send(
                "🔒 Ticket conservado. Este bot no lo borrará por inactividad, purga, compra completada "
                "ni salida del cliente. Queda para que lo borres manualmente. "
                "La protección no controla los botones ni automatizaciones de Ticket Tool.", ephemeral=True)
        except Exception as exc:
            print(f"[Conservar ticket] {type(exc).__name__}")
            await interaction.followup.send("No pude guardar la protección. El ticket todavía no quedó conservado.", ephemeral=True)

    @staticmethod
    def _is_ticket(channel):
        return hasattr(channel, 'name') and (channel.name.startswith(('ticket-', 'sug-'))
            or getattr(channel, 'category_id', None) == ID_CATEGORIA_SUGERENCIAS)

    @app_commands.command(name="manual", description="Apaga la IA en este ticket para hablar manualmente con el cliente")
    @app_commands.default_permissions(administrator=True) # Exclusivo para admins
    async def modo_manual(self, interaction: discord.Interaction):
        if not hasattr(interaction.channel, 'name') or (not interaction.channel.name.startswith("ticket-") and not interaction.channel.name.startswith("sug-")):
            await interaction.response.send_message("⚠️ Este comando solo se puede usar dentro de un ticket.", ephemeral=True)
            return

        try:
            async with self.bot.pool.acquire(timeout=5.0) as conn:
                await conn.execute("UPDATE tickets SET estado = 'pausado' WHERE channel_id = $1", interaction.channel.id)
            await interaction.response.send_message("🛑 **Modo Manual Activado.**\nLa IA ha sido desactivada en este ticket.")
        except Exception as e:
            print(f"❌ [DB Error] al pausar ticket {interaction.channel.id}: {e}")
            await interaction.response.send_message("❌ Hubo un error al intentar pausar la IA.", ephemeral=True)

    # --- COMANDO ADMINISTRATIVO EXCLUSIVO: CANJEAR PUNTOS DE BUMPS MANUALMENTE (user_id as TEXT) ---
    @app_commands.command(name="canjear", description="Descuenta 30 puntos a un usuario para realizar un canje manual de modelo")
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(usuario="El miembro al que se le descontarán los 30 puntos de la billetera virtual")
    async def canjear_puntos(self, interaction: discord.Interaction, usuario: discord.User):
        user_str = str(usuario.id) # Forzamos formato string para concordancia estricta con tu NeonDB (image_34f7a9.png)
        try:
            async with self.bot.pool.acquire(timeout=5.0) as conn:
                current_count = await conn.fetchval("SELECT count FROM bumps WHERE user_id = $1", user_str)
                
                if current_count is None or current_count < 30:
                    puntos_actuales = current_count if current_count is not None else 0
                    await interaction.response.send_message(f"⚠️ El usuario {usuario.mention} no tiene suficientes puntos acumulados (Tiene: {puntos_actuales}/30).", ephemeral=True)
                    return
                
                # Descuento atómico de 30 puntos
                await conn.execute("UPDATE bumps SET count = count - 30 WHERE user_id = $1", user_str)
                await interaction.response.send_message(f"✅ **Canje Procesado con Éxito**\nSe han descontado 30 puntos de la billetera virtual de {usuario.mention}.\nSaldo restante: **{current_count - 30} puntos**.")
        except Exception as e:
            print(f"❌ [DB Error] Error al ejecutar el comando de canjear puntos: {e}")
            await interaction.response.send_message("❌ Ocurrió un inconveniente en el motor de la base de datos al intentar procesar el canje.", ephemeral=True)

    @app_commands.command(name="anular", description="Anula y elimina los registros de pago asociados a un ticket específico")
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(
        canal_ticket="El canal del ticket cuyos pagos deseas anular. Por defecto, el canal actual."
    )
    async def anular_pago_ticket(self, interaction: discord.Interaction, canal_ticket: discord.abc.GuildChannel = None):
        target_channel = canal_ticket or interaction.channel
        
        ch_name = getattr(target_channel, 'name', '')
        es_ticket = ch_name.startswith("ticket-") or ch_name.startswith("sug-")
        if not es_ticket:
            await interaction.response.send_message(
                "⚠️ Este comando solo se puede aplicar sobre canales de tickets (`ticket-xxxx` o `sug-xxxx`).",
                ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        try:
            async with self.bot.pool.acquire(timeout=5.0) as conn:
                pagos_eliminados = await conn.fetch(
                    "SELECT monto, moneda, concepto FROM pagos WHERE ticket_id = $1",
                    target_channel.id
                )
                
                if not pagos_eliminados:
                    await interaction.followup.send(
                        f"📭 No se encontraron registros de pago asociados al ticket {target_channel.mention} en la base de datos.",
                        ephemeral=True
                    )
                    return
                
                await conn.execute("DELETE FROM pagos WHERE ticket_id = $1", target_channel.id)
                
            desglose_borrado = []
            for p in pagos_eliminados:
                desglose_borrado.append(f"• **{p['concepto']}**: `${p['monto']:.2f} {p['moneda']}`")
            
            detalles_str = "\n".join(desglose_borrado)
            await interaction.followup.send(
                f"✅ **Pagos Anulados con Éxito**\n\nSe han eliminado de forma permanente los siguientes registros de pago asociados al ticket {target_channel.mention}:\n{detalles_str}\n\n*Las estadísticas financieras de este mes se actualizarán de inmediato.*",
                ephemeral=True
            )
        except Exception as e:
            print(f"❌ [DB Error] al anular pago de ticket {target_channel.id}: {e}")
            await interaction.followup.send("❌ Ocurrió un error al intentar anular los pagos en la base de datos.", ephemeral=True)

    @app_commands.command(name="purgar_tickets", description="Elimina de inmediato todos los tickets viejos y abandonados del servidor")
    @app_commands.default_permissions(administrator=True)
    async def purgar_tickets(self, interaction: discord.Interaction):
        """Comando de administrador para forzar el barrido y borrado masivo de tickets acumulados."""
        await interaction.response.defer(ephemeral=True)
        try:
            await self.cleanup_tickets()
            await interaction.followup.send("✅ **Limpieza completada**: Se ejecutó el barrido de limpieza y se eliminaron los tickets acumulados elegibles.", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Error al ejecutar la purga de tickets: {e}", ephemeral=True)

    @app_commands.command(name="finanzas", description="Muestra el reporte financiero mensual unificado (Pesos ARS + Dólares USD)")
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(
        mes="El mes a consultar. Por defecto el mes actual.",
        ano="El año a consultar. Por defecto el año actual."
    )
    @app_commands.choices(
        mes=[
            app_commands.Choice(name="Enero", value=1),
            app_commands.Choice(name="Febrero", value=2),
            app_commands.Choice(name="Marzo", value=3),
            app_commands.Choice(name="Abril", value=4),
            app_commands.Choice(name="Mayo", value=5),
            app_commands.Choice(name="Junio", value=6),
            app_commands.Choice(name="Julio", value=7),
            app_commands.Choice(name="Agosto", value=8),
            app_commands.Choice(name="Septiembre", value=9),
            app_commands.Choice(name="Octubre", value=10),
            app_commands.Choice(name="Noviembre", value=11),
            app_commands.Choice(name="Diciembre", value=12),
        ],
        ano=[
            app_commands.Choice(name="2025", value=2025),
            app_commands.Choice(name="2026", value=2026),
            app_commands.Choice(name="2027", value=2027),
            app_commands.Choice(name="2028", value=2028),
            app_commands.Choice(name="2029", value=2029),
            app_commands.Choice(name="2030", value=2030),
        ]
    )
    async def ver_finanzas(self, interaction: discord.Interaction, mes: int = None, ano: int = None):
        import datetime
        ahora = datetime.datetime.now()
        
        mes_query = mes or ahora.month
        ano_query = ano or ahora.year
        
        nombres_meses = ["Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio", "Julio", "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre"]
        nombre_mes_str = nombres_meses[mes_query - 1]
        
        await interaction.response.defer(ephemeral=True)
        
        query = """
            SELECT concepto, moneda, monto, COALESCE(metodo_pago, 'paypal') as metodo_pago, COUNT(*) as cantidad
            FROM pagos
            WHERE CAST(EXTRACT(MONTH FROM fecha_pago) AS INTEGER) = $1 
              AND CAST(EXTRACT(YEAR FROM fecha_pago) AS INTEGER) = $2
            GROUP BY concepto, moneda, monto, metodo_pago
            ORDER BY concepto, moneda
        """
        
        try:
            async with self.bot.pool.acquire(timeout=10.0) as conn:
                filas = await conn.fetch(query, int(mes_query), int(ano_query))
                
            if not filas:
                await interaction.followup.send(f"📭 No se encontraron registros de transacciones para **{nombre_mes_str} de {ano_query}**.", ephemeral=True)
                return
                
            # Procesamiento unificado de transacciones (ARS + USD)
            total_monto_ars = 0.0
            total_cant_ars = 0
            desglose_ars = {}

            total_monto_usd = 0.0
            total_cant_usd = 0
            desglose_usd = {}
            
            for fila in filas:
                moneda = (fila["moneda"] or "ARS").upper()
                concepto = fila["concepto"] or "No especificado"
                cantidad = int(fila["cantidad"])
                monto_bruto = float(fila["monto"])
                metodo = (fila["metodo_pago"] or "paypal").lower()
                
                if moneda == "USD":
                    # Calcular comisiones si es PayPal
                    if metodo == "paypal":
                        comision_paypal = round((monto_bruto * 0.054) + 0.30, 2)
                        neto_1 = max(0.0, monto_bruto - comision_paypal)
                        neto_2 = neto_1 * 0.965  # Intermediación 3.5%
                        neto_3 = neto_2 * 0.99   # Lemon 1%
                        monto_neto = round(neto_3, 2)
                    else:
                        monto_neto = monto_bruto
                    
                    total_monto_usd += monto_neto * cantidad
                    total_cant_usd += cantidad
                    if concepto not in desglose_usd:
                        desglose_usd[concepto] = {"monto": 0.0, "cantidad": 0}
                    desglose_usd[concepto]["monto"] += monto_neto * cantidad
                    desglose_usd[concepto]["cantidad"] += cantidad
                else:
                    monto_neto = monto_bruto
                    total_monto_ars += monto_neto * cantidad
                    total_cant_ars += cantidad
                    if concepto not in desglose_ars:
                        desglose_ars[concepto] = {"monto": 0.0, "cantidad": 0}
                    desglose_ars[concepto]["monto"] += monto_neto * cantidad
                    desglose_ars[concepto]["cantidad"] += cantidad
                
            if total_cant_ars == 0 and total_cant_usd == 0:
                await interaction.followup.send(f"📭 No se encontraron transacciones para **{nombre_mes_str} de {ano_query}**.", ephemeral=True)
                return

            # Construir Embed Unificado Estético
            embed = discord.Embed(
                title=f"📊 Reporte Financiero - {nombre_mes_str} {ano_query}",
                color=0x1DB954, # Verde Spotify elegante
                timestamp=interaction.created_at
            )
            
            # Campo resumen general
            resumen_lines = []
            if total_cant_ars > 0:
                resumen_lines.append(f"• 🇦🇷 **Total Pesos (ARS)**: `${total_monto_ars:,.2f} ARS` (`{total_cant_ars}` transacciones)")
            if total_cant_usd > 0:
                resumen_lines.append(f"• 🌍 **Total Dólares (USD Neto)**: `${total_monto_usd:,.2f} USD` (`{total_cant_usd}` transacciones)")
                
            embed.add_field(name="💰 Resumen General de Ingresos", value="\n".join(resumen_lines), inline=False)
            
            # Campo desglose en Pesos
            if total_cant_ars > 0:
                desglose_ars_lines = []
                for conc in sorted(desglose_ars.keys()):
                    d = desglose_ars[conc]
                    desglose_ars_lines.append(f"• **{conc}**: `{d['cantidad']} en ARS` (`${d['monto']:,.2f}`)")
                embed.add_field(name="🇦🇷 Ventas en Pesos (ARS)", value="\n".join(desglose_ars_lines), inline=False)
                
            # Campo desglose en Dólares
            if total_cant_usd > 0:
                desglose_usd_lines = []
                for conc in sorted(desglose_usd.keys()):
                    d = desglose_usd[conc]
                    desglose_usd_lines.append(f"• **{conc}**: `{d['cantidad']} en USD` (`${d['monto']:,.2f}`)")
                embed.add_field(name="🌍 Ventas en Dólares (USD Neto)", value="\n".join(desglose_usd_lines), inline=False)
                
            embed.set_footer(text="Datos extraídos en tiempo real desde NeonDB • Comisiones PayPal deducidas")
            await interaction.followup.send(embed=embed, ephemeral=True)
            
        except asyncio.TimeoutError:
            await interaction.followup.send("⚠️ La base de datos tardó demasiado en responder.", ephemeral=True)
        except Exception as e:
            print(f"❌ [DB Error] en comando finanzas: {e}")
            await interaction.followup.send("❌ Ocurrió un error al intentar consultar el reporte financiero.", ephemeral=True)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.content.startswith('/') or not self._is_ticket(message.channel):
            return
        async with self._ticket_lock(message.channel.id):
            try:
                await self._handle_ticket_message(message)
            except Exception as exc:
                print(f"[Ticket {message.channel.id}] {type(exc).__name__}")
                await self._request_manual_review(message.channel,
                    "No pude completar el procesamiento. Conservamos el caso para revisión; no vuelvas a pagar.")

    async def _handle_ticket_message(self, message):
        await self._update_ticket_activity(message)
        async with self.bot.pool.acquire(timeout=10) as conn:
            ticket = await conn.fetchrow("SELECT * FROM tickets WHERE channel_id=$1", message.channel.id)
        if not ticket:
            raise RuntimeError("Ticket sin registro")
        is_staff = message.author.id == FABRIZIO_ID or message.author.guild_permissions.administrator
        if ticket['estado'] == 'pausado' or is_staff:
            return
        if ticket['user_id'] and ticket['user_id'] != message.author.id:
            await message.reply("Este ticket pertenece a otro usuario. Abrí tu propio ticket para comprar.")
            return

        patron_invitacion = r"(discord\.(gg|io|me|li|com\/invite)|discordapp\.com\/invite)\/([a-zA-Z0-9\-]+)"
        if re.search(patron_invitacion, message.content, re.IGNORECASE):
            await message.delete()
            await message.author.ban(reason="Spam de invitaciones detectado en ticket.")
            return
        if any(attachment_mime(att) for att in message.attachments):
            await self.handle_receipt_image(message)
            return
        if "canjear" in message.content.lower() and "puntos" in message.content.lower():
            await message.reply(f"¡Perfecto! <@{FABRIZIO_ID}> revisará la disponibilidad y el canje de tus 30 puntos.")
            return
        if await self._handle_pending_purchase(message):
            return
        if self.bot.user.mentioned_in(message) or any(u.id == self.mi_id for u in message.mentions):
            await message.reply(f"<@{FABRIZIO_ID}>, el cliente necesita ayuda en este ticket.")
            return
        content = message.content.strip().lower()
        if content in RESPUESTAS_PREDEFINIDAS:
            await message.reply(RESPUESTAS_PREDEFINIDAS[content])
            return
        await self.handle_support_query(message)

    async def _update_ticket_activity(self, message):
        # Una respuesta de administración no cambia el dueño del ticket.
        staff = message.author.id == FABRIZIO_ID or message.author.guild_permissions.administrator
        owner_id = None if staff else message.author.id
        async with self.bot.pool.acquire(timeout=10) as conn:
            async with conn.transaction():
                if owner_id:
                    await conn.execute("INSERT INTO usuarios (user_id) VALUES ($1) ON CONFLICT DO NOTHING", owner_id)
                await conn.execute("""INSERT INTO tickets (channel_id, user_id, estado, ultimo_mensaje, hablo)
                    VALUES ($1,$2,'abierto',CURRENT_TIMESTAMP,TRUE)
                    ON CONFLICT (channel_id) DO UPDATE SET ultimo_mensaje=CURRENT_TIMESTAMP, hablo=TRUE,
                    user_id=CASE WHEN tickets.user_id IS NULL OR tickets.user_id=0
                        THEN EXCLUDED.user_id ELSE tickets.user_id END""", message.channel.id, owner_id)

    async def _requested_purchase(self, message):
        desired = requested_roles(message.content)
        if desired:
            return desired
        # Solo mensajes del comprador y en orden de más reciente a más antiguo.
        try:
            async for previous in message.channel.history(limit=20, before=message):
                if previous.author.id == message.author.id:
                    desired = requested_roles(previous.content)
                    if desired:
                        return desired
        except discord.HTTPException:
            print(f"[Compra {message.channel.id}] No se pudo consultar el historial")
        return ()

    async def _request_manual_review(self, channel, reason):
        try:
            async with self.bot.pool.acquire(timeout=10) as conn:
                await conn.execute("""INSERT INTO tickets (channel_id, conservar) VALUES ($1,TRUE)
                    ON CONFLICT (channel_id) DO UPDATE SET conservar=TRUE""", channel.id)
        except Exception as exc:
            print(f"[Protección revisión {channel.id}] {type(exc).__name__}")
        try:
            await channel.send(f"🔔 <@{FABRIZIO_ID}> {reason}")
        except discord.HTTPException as exc:
            print(f"[Aviso revisión {channel.id}] {type(exc).__name__}")

    async def handle_receipt_image(self, message):
        attachments = [a for a in message.attachments if attachment_mime(a)]
        warning = await message.channel.send("⏳ Estoy leyendo tus comprobantes. No hace falta que vuelvas a pagar.")
        suggestion = message.channel.name.startswith('sug-') or message.channel.category_id == ID_CATEGORIA_SUGERENCIAS
        try:
            parts, digests, known, seen = [], [], [], set()
            total_size = 0
            for attachment in attachments:
                if getattr(attachment, 'size', 0) + total_size > 18 * 1024 * 1024:
                    raise ValueError("Los adjuntos superan 18 MB; requiere revisión manual")
                data = await attachment.read()
                total_size += len(data)
                if total_size > 18 * 1024 * 1024:
                    raise ValueError("Los adjuntos superan 18 MB; requiere revisión manual")
                digest = hashlib.sha256(data).hexdigest()
                if digest in seen:
                    continue
                seen.add(digest)
                existing = await self.payments.known(digest)
                if existing:
                    known.append(existing)
                    continue
                parts.append({'mime_type': attachment_mime(attachment), 'data': data})
                digests.append(digest)
            issues, saved = [], []
            if parts:
                text = await self._generate_content_with_rotation(vision_prompt(len(parts)), parts)
                receipts, issues = parse_receipts(text, len(parts))
                if suggestion:
                    receipts_ok = []
                    for receipt in receipts:
                        if receipt.amount >= (4000 if receipt.currency == 'ARS' else 4):
                            receipts_ok.append(receipt)
                        else:
                            issues.append(f"archivo {receipt.index + 1}: requiere revisar el importe de la petición")
                    receipts = receipts_ok
                saved = await self.payments.save(message, receipts, digests, suggestion)
            related = [r for r in [*known, *saved] if r and r['user_id'] == message.author.id and r['ticket_id'] == message.channel.id]
            if len(related) != len(known) + len(saved):
                issues.append("hay un comprobante que ya se registró en otra compra")
            if suggestion:
                if related:
                    await self._marcar_ticket_completado(message.channel.id)
                    await warning.edit(content="✅ Comprobante de petición registrado. Administración continuará con tu pedido.")
                    await message.channel.send(f"🔔 <@{FABRIZIO_ID}> Hay una petición con comprobante en este ticket.")
                else:
                    await warning.edit(content="No pude completar la lectura. Ya pedí revisión; no vuelvas a pagar.")
            else:
                desired = await self._requested_purchase(message)
                handled = await self._handle_pending_purchase(message, desired)
                if handled:
                    await warning.edit(content="Lectura terminada. El estado de tu compra figura en el mensaje de abajo.")
                elif related:
                    await warning.edit(content="✅ Este comprobante ya estaba registrado. No se volvió a sumar ni a cobrar.")
                else:
                    await warning.edit(content="No pude completar la lectura. Ya pedí revisión; no vuelvas a pagar.")
            if issues or not related:
                await self._request_manual_review(message.channel,
                    "Necesitamos revisar los comprobantes: " + "; ".join(issues or ['lectura incompleta']) + ". No vuelvas a pagar.")
        except Exception as exc:
            print(f"[Comprobantes {message.channel.id}] {type(exc).__name__}")
            await warning.edit(content="No pude terminar el procesamiento. No significa que tu pago sea incorrecto y no tenés que pagar otra vez.")
            await self._request_manual_review(message.channel, "Revisá los comprobantes y la entrega de este ticket, por favor.")

    async def _handle_pending_purchase(self, message, desired=None):
        desired = requested_roles(message.content) if desired is None else desired
        result = await self.payments.prepare(message.channel.id, message.author.id, desired)
        if result.get('empty'):
            return False
        if result.get('delivery'):
            await self._fulfill_delivery(result['delivery'], message.channel, message.guild)
        else:
            totals = ' + '.join(f"{amount} {currency}" for currency, amount in result['totals'].items())
            if result['missing'] is not None:
                await message.channel.send(f"Registré {totals} para {', '.join(result['desired'])}. "
                    f"Faltan {result['missing']} {next(iter(result['totals']))}. Si ya enviaste el resto, adjuntá ese comprobante; no repitas el pago.")
            else:
                await message.channel.send(f"Registré {totals}. Decime qué rangos querés: Diamante, Oro, Plata o los tres. Los comprobantes quedan guardados.")
        return True

    async def _fulfill_delivery(self, delivery, channel, guild):
        if delivery['entrega_estado'] == 'manual':
            await channel.send(f"Tu comprobante está registrado. <@{FABRIZIO_ID}> tiene que revisar la entrega; no vuelvas a pagar.")
            return
        attempts = await self.payments.attempt(delivery['entrega_id'])
        if attempts is None:
            return
        try:
            names = delivery['roles_asignados']
            roles = [guild.get_role(ROLES[name]['id']) for name in names]
            if not all(roles):
                raise RuntimeError("Falta configurar un rol")
            member = guild.get_member(delivery['user_id']) or await guild.fetch_member(delivery['user_id'])
            await member.add_roles(*roles, reason="Compra registrada mediante comprobantes")
            # Se comprueba la entrega real, incluso si Discord asignó solo parte del combo.
            member = await guild.fetch_member(delivery['user_id'])
            if not {r.id for r in roles}.issubset({r.id for r in member.roles}):
                raise RuntimeError("Discord no confirmó todos los roles")
            await self.payments.finish(delivery['entrega_id'], channel.id)
        except Exception as exc:
            print(f"[Entrega {delivery['entrega_id']}] {type(exc).__name__}")
            try:
                await self.payments.failed(delivery['entrega_id'], attempts)
            finally:
                await self._request_manual_review(channel,
                    "El comprobante está registrado, pero falta confirmar todos los rangos. "
                    "La entrega queda pendiente y se reintentará hasta tres veces. No vuelvas a pagar.")
            return
        try:
            await channel.send(f"✅ <@{delivery['user_id']}> Ya tenés los rangos **{', '.join(names)}**. "
                               f"🔔 <@{FABRIZIO_ID}> Entrega completada a partir de los comprobantes.")
        except discord.HTTPException:
            print(f"[Entrega {delivery['entrega_id']}] Rangos confirmados; no se pudo enviar aviso")

    @tasks.loop(minutes=5)
    async def recover_deliveries(self):
        try:
            for delivery in await self.payments.recoverable():
                guild = self.bot.get_guild(delivery['guild_id'])
                channel = self.bot.get_channel(delivery['ticket_id'])
                if guild and channel:
                    async with self._ticket_lock(channel.id):
                        await self._fulfill_delivery(delivery, channel, guild)
        except Exception as exc:
            print(f"[Recuperación de entregas] {type(exc).__name__}")

    @recover_deliveries.before_loop
    async def before_recovery(self):
        await self.bot.wait_until_ready()

    async def _marcar_ticket_completado(self, channel_id: int):
        query = "UPDATE tickets SET estado = 'completado' WHERE channel_id = $1"
        try:
            async with self.bot.pool.acquire(timeout=5.0) as conn:
                await conn.execute(query, channel_id)
        except Exception as e:
            print(f"❌ [DB Error] Fallo al marcar ticket como completado: {e}")

    async def handle_support_query(self, message: discord.Message):
        # Recopilamos historial para dar contexto a la IA y permitir que entienda aclaraciones posteriores
        historial = []
        try:
            async for msg in message.channel.history(limit=10, before=message):
                autor = "Usuario" if not msg.author.bot else "Bot"
                historial.append(f"{autor}: {msg.content}")
        except Exception as e:
            print(f"⚠️ [Historial Warn] Error leyendo historial previo en {message.channel.id}: {e}")
        
        contexto_previo = "\n".join(historial)
        es_sugerencia = (getattr(message.channel, 'category_id', None) == ID_CATEGORIA_SUGERENCIAS) or message.channel.name.startswith("sug-")

        # --- SEPARACIÓN ESTRICTA DE CONTEXTOS PARA EL CHATBOT ---
        if es_sugerencia:
            prompt = f"""
Actúa como un asistente virtual premium de Discord para atender la zona de PETICIONES ÚNICAS DE MODELOS de Tito Calderón.
HISTORIAL DE CONVERSACIÓN RECIENTE:
{contexto_previo}

REGLAS DE NEGOCIO Y RESPUESTA (ESTRICTAS):
1. PROTOCOLO DE PRE-COMPRA: Explica que el usuario debe enviar tanto la plataforma de contenido (ej: OnlyFans, Fansly, Fanvue, etc.) COMO su red social principal (Instagram, TikTok o Twitter) de la modelo para que Tito Calderón pueda buscarla y verificar disponibilidad rápidamente.
2. COMPROMISO DE COMPRA (100%): Si el usuario duda o consulta, recuérdale con educación que sugerir una modelo e iniciar la búsqueda activa un compromiso de pago obligatorio si confirmamos que está disponible. Si decide no pagar tras la confirmación, se le aplicará un aviso (warn) o baneo por abuso de soporte.
3. REGLA DE PAGOS (CRÍTICA): NO incites al usuario a realizar el pago ni a pasar comprobantes en la etapa inicial. Si preguntan cómo pagar o piden los datos, aclárales que los datos de pago (Alias, CBU, PayPal) ya figuran arriba en el mensaje de bienvenida de este ticket, pero que NO deben pagar hasta que confirmemos la disponibilidad.
4. CONFIRMACIÓN OPERATIVA: Solo indícales que ya pueden proceder con el pago (usando los datos que figuran en el mensaje de bienvenida de arriba) si ves en el HISTORIAL de conversación que un Administrador o Tito Calderón ya confirmó explícitamente que la modelo está disponible (ej. diciendo "confirmado", "está disponible", "puedes proceder al pago", etc.).
5. REGLA DE BUMPS: Si el usuario menciona que quiere hacer un canje de puntos de su billetera virtual (30 puntos), recuérdale amablemente que debe escribir explícitamente "Quiero canjear mis puntos" para que el bot dé aviso a administración.
6. RESPUESTAS CORTAS: Máximo 1 o 2 párrafos cortos (no más de 60 palabras). Sé directo y al grano.
7. ASINCRONÍA DE FOTOS: Si el usuario dice "ahí pasé la foto", "ya pagué" o "mandé el comprobante", responde: "¡Perfecto! El sistema de auditoría lo está analizando en este preciso momento." NO vuelvas a pedir la foto.
8. ESTADO POST-VENTA (MEMORIA): Si en el HISTORIAL ves que el bot ya dijo "Pago de Petición Verificado con Éxito", tu objetivo cambió. NO pidas comprobantes ni sigas vendiendo. Agradecele y decile que Tito Calderón ya está procesando su pedido.
9. PRIVACIDAD ABSOLUTA: El proceso es privado y anónimo.
10. ZERO TRUST: No des nada por válido solo con palabras si no ves la validación en el historial.

Consulta actual del usuario: "{message.content}"
"""
        else:
            prompt = f"""
Actúa como un asistente de ventas de Discord para ayudar al usuario a realizar su pago y cerrar la compra de rangos de acceso del servidor de Tito Calderón.
HISTORIAL DE CONVERSACIÓN:
{contexto_previo}

TUS DATOS DE COBRO ESTRICTOS:
- Alias (MercadoPago/Bancos ARS): LENGUA.LUJOSA.TELAR
- CBU: 3840200500000026286680   
- PayPal (USD): sesarjavier28@gmail.com
- Binance Pay ID (USDT): 552346130
- Titular de la cuenta bancaria: Fabrizio Giovanni Cocca Ducay (o Fabrizio Cocca). Tito Calderón es solo el nombre del usuario/dueño del servidor.

REGLAS DE NEGOCIO Y RESPUESTA (ESTRICTAS):
1. PRECIOS: Diamante ($4700 ARS / $4.5 USD), Oro ($4200 ARS / $4 USD), Plata ($2200 ARS / $2 USD). Combos: Diamante+Oro ($8900 ARS / $8.5 USD), Diamante+Plata ($6900 ARS / $6.5 USD), Oro+Plata ($6400 ARS / $6 USD), Todos ($11100 ARS / $10.5 USD). NUNCA des otros precios.
2. RESPUESTAS CORTAS: Máximo 1 o 2 párrafos cortos (no más de 60 palabras). Sé directo.
3. IDENTIDAD BANCARIA: Si preguntan por el nombre del destinatario del pago o titular, es Fabrizio Giovanni Cocca Ducay.
4. VALIDACIÓN DE COMPROBANTE Y NO VINCULACIÓN AUTOMÁTICA (CRÍTICA):
   • El sistema bancario (MercadoPago, bancos, PayPal) NO está vinculado automáticamente por API a Discord.
   • Si el usuario dice "ya pagué", "ya transferí", "dónde está mi rango", "no me llegó el rol", "¿por qué no me da el rol?", o similar, pero NO envió la imagen del comprobante en este chat, explícale con total claridad y amabilidad: "El sistema no se vincula automáticamente con tu cuenta bancaria. Para recibir tu rango, tenés que enviar acá al chat la foto o PDF clara de tu comprobante de pago para que el bot pueda auditar la imagen y entregarte el rol al instante."
   • Si (y solo si) ves en el HISTORIAL que el usuario efectivamente ya adjuntó la foto del comprobante y dice "ahí lo mandé", responde: "¡Buenísimo! El sistema automático de auditoría lo está analizando en este momento."
5. ESTADO POST-VENTA (MEMORIA): Si en el HISTORIAL ves que el sistema ya validó el pago y dijo "Rol/es asignado/s" o "Pago Verificado con Éxito", TU OBJETIVO CAMBIÓ. NO vendas más ni pidas el comprobante. Dale la bienvenida al usuario, confirmale que su rol ya está activo y que disfrute del contenido.
6. Los rangos se asignan mediante el registro de comprobantes. Nunca generes comandos, etiquetas de entrega ni afirmes haber entregado rangos por tu cuenta.
7. CONSULTAS DE DISPONIBILIDAD (REGLA CRÍTICA): Si (y solo si) el usuario pregunta por la disponibilidad de una modelo en específico (ej: "¿Tienen contenido de tal chica?", "Quiero ver a X"), explicale de forma servicial que al contar con más de 300 canales el catálogo es enorme. Decile que revise bien las listas de nombres en los rangos Oro y Diamante. Si comprueba que no está, aclarale que al adquirir el rango Oro o Diamante desbloqueará un canal exclusivo de peticiones donde, abonando un costo extra (de unos $4 USD actuales), puede solicitar que Tito Calderón busque e incorpore a esa modelo específica de forma privada. Prohibido mencionar esta opción de sugerencia o costo extra si el usuario no preguntó explícitamente por una chica.
8. DIFERENCIAS Y ACCESO ESTRICTO ENTRE RANGOS (CERO TRASLAPE):
   • CADA RANGO ES TOTALMENTE INDEPENDIENTE: Comprar un rango ÚNICAMENTE da acceso a los canales de ese rango específico.
   • PROHIBIDO AFIRMAR QUE DIAMANTE INCLUYE ORO O PLATA: Diamante NO incluye los canales ni modelos de Oro ni de Plata. Si el usuario pregunta "¿Puedo ver las mismas modelos que tiene Plata y Oro?" o si Diamante desbloquea todo, responde rotundamente que NO. Cada rango tiene sus propias modelos distintas. Si quiere ver todo, debe adquirir el Combo (ej. Diamante+Oro o Todos).
   • Plata ($2200 ARS / $2 USD): Rango más básico con acceso limitado de canales al fondo del servidor. NO otorga acceso a los canales extra (Novedades, Comunidad, Soporte, Peticiones de modelos, Votaciones, Guías).
   • Oro ($4200 ARS / $4 USD): Catálogo amplio de canales Oro y HABILITA el acceso completo a todos los canales extra (chat de comunidad, peticiones de chicas, votaciones, soporte, guías).
   • Diamante ($4700 ARS / $4.5 USD): El rango VIP supremo con entre 30 y 40 canales MÁS que Oro, con las modelos más exclusivas y premium seleccionadas por Tito Calderón. HABILITA todos los canales extra.
Si el cliente aclara el rango, el sistema de compras comprueba el saldo registrado antes de entregarlo.

Consulta actual del usuario: "{message.content}"
"""
        try:
            async with message.channel.typing():
                # --- INTERCEPCIÓN PROTOCOLO DE CONMUTACIÓN PARA EL CHATBOT ---
                respuesta_texto = await self._generate_content_with_rotation(prompt)
                respuesta_texto = re.sub(r"\[GRANT_ROLE:[^\]]*\]", "", respuesta_texto).strip()
                await message.reply(respuesta_texto or "Decime qué rango necesitás y revisamos tu compra.")

        except asyncio.TimeoutError:
            await message.reply("⚠️ **IA Congestionada**: Los servidores de Google están tardando en responder. Tu consulta es importante; por favor, intentá preguntar de nuevo en un instante.")
        except discord.HTTPException as e:
            if e.status == 429:
                print(f"⏳ [429 Rate Limit] en handle_support_query para {message.channel.id}: {e}")
                await asyncio.sleep(4.0)
                try:
                    await message.reply("¡Hola! Disculpá la demora momentánea. ¿En qué rango estás interesado o cómo te puedo ayudar?")
                except Exception:
                    pass
            else:
                print(f"❌ [Discord HTTP Error]: {e}")
        except Exception as e:
            print(f"[IA Support Error] {type(e).__name__}")
            await self._request_manual_review(message.channel, "No pude responder automáticamente. Administración continuará con tu consulta.")

    async def _delete_ticket_if_eligible(self, channel, member_left=False):
        """Todas las rutas de borrado usan la misma protección persistente y revalidan actividad."""
        async with self._ticket_lock(channel.id):
            async with self.bot.pool.acquire(timeout=10) as conn:
                async with conn.transaction():
                    ticket = await conn.fetchrow("SELECT * FROM tickets WHERE channel_id=$1 FOR UPDATE", channel.id)
                    if not ticket or ticket['conservar']:
                        return False
                    pending = await conn.fetchval("""SELECT EXISTS (SELECT 1 FROM pagos WHERE ticket_id=$1
                        AND entrega_estado IN ('pendiente','preparado','reintentar','manual'))""", channel.id)
                    if pending:
                        return False
                    suggestion = channel.name.startswith('sug-') or channel.category_id == ID_CATEGORIA_SUGERENCIAS
                    if suggestion and (ticket['hablo'] or ticket['estado'] == 'completado'):
                        return False
                    if not member_left:
                        threshold = 3 if not ticket['hablo'] and ticket['estado'] == 'abierto' else 24
                        activity = ticket['ultimo_mensaje']
                        if activity.tzinfo is None:
                            activity = activity.replace(tzinfo=datetime.timezone.utc)
                        # Si no se puede consultar Discord, se conserva el canal.
                        async for recent in channel.history(limit=20):
                            if not recent.author.bot and recent.created_at > activity:
                                activity = recent.created_at
                        if (datetime.datetime.now(datetime.timezone.utc) - activity).total_seconds() < threshold * 3600:
                            return False
                    try:
                        await channel.delete(reason="Cierre automático de ticket sin actividad ni entregas pendientes")
                    except discord.NotFound:
                        pass
                    except discord.HTTPException:
                        return False
                    await conn.execute("DELETE FROM tickets WHERE channel_id=$1 AND conservar=FALSE", channel.id)
                    return True

    @tasks.loop(hours=2)
    async def cleanup_tickets(self):
        try:
            async with self.bot.pool.acquire(timeout=15) as conn:
                rows = await conn.fetch("""SELECT channel_id FROM tickets WHERE conservar=FALSE AND
                    ((estado='abierto' AND hablo=FALSE AND ultimo_mensaje <= CURRENT_TIMESTAMP-INTERVAL '3 hours')
                    OR (estado IN ('abierto','pausado','completado') AND ultimo_mensaje <= CURRENT_TIMESTAMP-INTERVAL '24 hours'))""")
            for row in rows:
                channel = self.bot.get_channel(row['channel_id'])
                if not channel:
                    try:
                        channel = await self.bot.fetch_channel(row['channel_id'])
                    except discord.NotFound:
                        continue
                await self._delete_ticket_if_eligible(channel)
        except Exception as exc:
            print(f"[Limpieza tickets] {type(exc).__name__}")

    @cleanup_tickets.before_loop
    async def before_cleanup(self):
        await self.bot.wait_until_ready()

    # --- 📣 LÓGICA DE REMARKETING ORGÁNICO PROGRAMADO (CANAL DE TESTEO) ---
    @tasks.loop(hours=168) # 168 horas = 7 días de ciclo estricto de recontacto
    async def auto_promo_refresh(self):
        """Tarea de Remarketing: Purgar pings anteriores del bot y disparar el recordatorio corto."""
        canal = self.bot.get_channel(ID_CANAL_PROMO_TEST)
        if canal:
            try:
                # Purga asincrónica de los mensajes viejos enviados por el bot para no acumular basura
                def is_me(m): return m.author == self.bot.user
                await canal.purge(limit=10, check=is_me)
            except Exception as e:
                print(f"⚠️ [Remarketing Error] Fallo al limpiar canal de promo: {e}")

            # Mensaje estratégico corto: Se posiciona abajo de Ticket Tool sin enterrar el botón
            bienvenida = "@everyone 🚀 **¡Actualizamos el contenido recientemente!** Abrí un ticket acá arriba y descubrí todo lo nuevo que subimos. ¡No te lo pierdas! ✨"
            try:
                await canal.send(content=bienvenida)
                print("📣 [Remarketing] Mensaje semanal de recontacto enviado con éxito.")
            except Exception as e:
                print(f"❌ Error enviando recontacto automático: {e}")

    @auto_promo_refresh.before_loop
    async def before_promo(self):
        await self.bot.wait_until_ready()

async def setup(bot: commands.Bot):
    await bot.add_cog(Tickets(bot))
