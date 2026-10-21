import asyncio
import contextlib
import logging
import os
import time

import asyncpg
import discord
from discord.ext import commands
from dotenv import load_dotenv

from runtime_health import HealthState, start_health_server

log = logging.getLogger('asistente')


class Bot(commands.Bot):
    def __init__(self, database_url=None, health=None):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        super().__init__(command_prefix='!', intents=intents, allowed_mentions=discord.AllowedMentions.none())
        self.database_url = database_url
        self.health = health or HealthState()
        self.pool = None
        self._health_task = None
        self._sync_task = None
        self.tree.on_error = self._command_error

    async def _command_error(self, interaction, error):
        if isinstance(error, discord.app_commands.CheckFailure):
            text = 'No tenés permisos para usar este comando.'
        else:
            text = 'No pude completar el comando por una demora técnica. Intentá nuevamente en un momento.'
            log.warning('Comando incompleto: %s', type(error).__name__)
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)

    async def setup_hook(self):
        self._health_task = asyncio.create_task(self._monitor_health())
        # Un error esencial aborta este cliente; el supervisor crea uno nuevo.
        self.pool = await asyncpg.create_pool(dsn=self.database_url, min_size=0, max_size=5,
            command_timeout=20, timeout=20, max_inactive_connection_lifetime=10)
        async with self.pool.acquire(timeout=15) as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS bumps (
                    user_id TEXT, guild_id TEXT, count INTEGER DEFAULT 0, PRIMARY KEY(user_id,guild_id));
                CREATE TABLE IF NOT EXISTS tickets (
                    channel_id BIGINT PRIMARY KEY,user_id BIGINT,estado TEXT DEFAULT 'abierto',
                    ultimo_mensaje TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP);
            """)
        self.health.update(database=True)
        for extension in ('cogs.bumps', 'cogs.tickets'):
            await self.load_extension(extension)
            log.info('Módulo cargado: %s', extension)
        self.health.update(tickets=True)
        self._sync_task = asyncio.create_task(self._sync_commands())

    async def _sync_commands(self):
        while not self.is_closed():
            try:
                synced = await self.tree.sync()
                self.health.update(commands=True)
                log.info('Comandos sincronizados: %s', len(synced))
                return
            except Exception as exc:
                log.warning('Sincronización pendiente: %s', type(exc).__name__)
                await asyncio.sleep(30)

    async def _monitor_health(self):
        checked = 0
        while not self.is_closed():
            self.health.update(discord=self.is_ready())
            if self.pool is not None and time.monotonic()-checked >= 300:
                try:
                    async with self.pool.acquire(timeout=10) as conn:
                        await conn.fetchval('SELECT 1', timeout=5)
                    self.health.update(database=True)
                except Exception as exc:
                    self.health.update(database=False)
                    log.warning('Base temporalmente no disponible: %s', type(exc).__name__)
                checked = time.monotonic()
            await asyncio.sleep(10)

    async def on_ready(self):
        self.health.update(discord=True)
        log.info('Discord conectado como %s', self.user.name)

    async def on_disconnect(self):
        self.health.update(discord=False)

    async def on_resumed(self):
        self.health.update(discord=True)
        log.info('Sesión Discord recuperada')

    async def close(self):
        self.health.update(discord=False, tickets=False, commands=False)
        for task in (self._sync_task, self._health_task):
            if task:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        await super().close()
        if self.pool is not None:
            try:
                await asyncio.wait_for(self.pool.close(), timeout=10)
            except TimeoutError:
                self.pool.terminate()
            self.pool = None
        self.health.update(database=False)


async def supervise(token, database_url, health, *, bot_factory=Bot, sleep=asyncio.sleep):
    delay = 5
    while True:
        client = bot_factory(database_url=database_url, health=health)
        try:
            await client.start(token, reconnect=True)
            log.warning('Discord terminó inesperadamente; reiniciando cliente')
        except discord.LoginFailure:
            log.error('La credencial de Discord fue rechazada; requiere intervención')
            raise
        except Exception as exc:
            log.warning('Reinicio recuperable: %s', type(exc).__name__)
        finally:
            await client.close()
        remaining = delay
        while remaining > 0:
            health.update(discord=False)
            await sleep(min(10, remaining))
            remaining -= 10
        delay = min(delay * 2, 120)


def main():
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
    token = os.environ.get('DISCORD_TOKEN')
    database_url = os.environ.get('DATABASE_URL')
    if not token or not database_url:
        raise SystemExit('Falta DISCORD_TOKEN o DATABASE_URL; no se inició el servicio')
    health = HealthState()
    server = start_health_server(health, int(os.environ.get('PORT', '8080')))
    try:
        asyncio.run(supervise(token, database_url, health))
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()


if __name__ == '__main__':
    main()
