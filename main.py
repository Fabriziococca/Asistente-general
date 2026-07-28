import discord
from discord.ext import commands
import os
from dotenv import load_dotenv
import asyncpg
import asyncio
import time
from keep_alive import keep_alive 

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

# Intents necesarios para leer el contenido de los mensajes
intents = discord.Intents.default()
intents.message_content = True 
intents.members = True

class Bot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)
        self.pool = None

    async def setup_hook(self):
        """Inicializa la DB, carga los Cogs y sincroniza comandos al arrancar."""
        print("Iniciando setup_hook...")
        
        # 1. Configuración y conexión robusta a la base de datos NeonDB (asyncpg)
        try:
            print("Conectando al pool de base de datos...")
            self.pool = await asyncpg.create_pool(
                dsn=DATABASE_URL,
                min_size=0, # Permite reducir a 0 conexiones en inactividad para que NeonDB auto-suspenda (Ahorro de CU-hrs)
                max_size=10,
                command_timeout=60.0,
                timeout=20.0, # Margen amplio para auto-despertar de NeonDB (500ms-1s)
                max_inactive_connection_lifetime=60.0 # Cierra conexiones inactivas tras 60 seg para permitir el sleep de Neon
            )
            
            # Schema: Clave compuesta (user_id, guild_id) para conteo por servidor
            async with self.pool.acquire(timeout=10.0) as conn:
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS bumps (
                        user_id TEXT,
                        guild_id TEXT,
                        count INTEGER DEFAULT 0,
                        PRIMARY KEY (user_id, guild_id)
                    )
                """)
                # También verificamos la tabla de tickets por las dudas
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS tickets (
                        channel_id BIGINT PRIMARY KEY,
                        user_id BIGINT,
                        estado TEXT DEFAULT 'abierto',
                        ultimo_mensaje TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                    )
                """)
            print("✅ Base de datos y pool inicializados correctamente.")
            
        except asyncio.TimeoutError:
            print("❌ [CRITICAL] Timeout: No se pudo conectar a la base de datos a tiempo.")
            await self.close()
            return
        except asyncpg.PostgresError as e:
            print(f"❌ [CRITICAL] Error de PostgreSQL al iniciar el pool: {e}")
            await self.close()
            return
        except Exception as e:
            print(f"❌ [CRITICAL] Error inesperado en la base de datos: {e}")
            await self.close()
            return

        # 2. Cargar todos los módulos/Cogs
        cogs_dir = './cogs'
        if not os.path.exists(cogs_dir):
            os.makedirs(cogs_dir)
            print(f"📁 Directorio {cogs_dir} creado.")

        for filename in os.listdir(cogs_dir):
            if filename.endswith('.py'):
                try:
                    await self.load_extension(f'cogs.{filename[:-3]}')
                    print(f"📦 Cog cargado correctamente: {filename}")
                except Exception as e:
                    print(f"❌ [ERROR] Falló la carga del cog {filename}: {e}")

        # 3. Sincronizar el árbol de comandos para los slash commands
        try:
            print("🔄 Sincronizando comandos Slash...")
            synced = await self.tree.sync()
            print(f"✅ Sincronizados {len(synced)} comandos Slash a nivel global.")
        except discord.HTTPException as e:
            print(f"❌ [ERROR] Falló la sincronización con la API de Discord: {e}")
        except Exception as e:
            print(f"❌ [ERROR] Error inesperado al sincronizar comandos: {e}")

bot = Bot()

@bot.event
async def on_ready():
    print(f"🤖 Bot conectado como {bot.user} (ID: {bot.user.id})")
    print("=======================================")

if __name__ == '__main__':
    # Mantenemos vivo el servidor HTTP para Render
    keep_alive()
    
    if not TOKEN:
        print("❌ [CRITICAL] DISCORD_TOKEN no encontrado en el archivo .env.")
    elif not DATABASE_URL:
        print("❌ [CRITICAL] DATABASE_URL no encontrado en el archivo .env.")
    else:
        # LÓGICA DE REINTENTOS ANTI-CLOUDFLARE (Error 429/1015)
        max_reintentos = 5
        reintento_actual = 0
        
        while reintento_actual < max_reintentos:
            try:
                print(f"🚀 Iniciando el bot (Intento {reintento_actual + 1})...")
                bot.run(TOKEN)
                # Si bot.run termina normalmente (cosa que no debería), salimos del bucle
                break
            except discord.errors.HTTPException as e:
                # El error 429 es Too Many Requests, típico de Cloudflare 1015
                if e.status == 429:
                    espera = (2 ** reintento_actual) * 30 # Espera progresiva (30s, 60s, 120s...)
                    print(f"⚠️ [BLOQUEO] Cloudflare detectado (429). Reintentando en {espera} segundos...")
                    time.sleep(espera)
                    reintento_actual += 1
                else:
                    print(f"❌ [CRITICAL] Error de HTTP no manejado: {e}")
                    break
            except Exception as e:
                print(f"❌ [CRITICAL] El bot falló por un error inesperado: {e}")
                # Esperamos un poco antes de un reintento genérico
                time.sleep(10)
                reintento_actual += 1

        if reintento_actual == max_reintentos:
            print("💀 [FATAL] Se alcanzaron los reintentos máximos. La IP de Render está baneada por Cloudflare.")