import asyncio
import json
from http.server import ThreadingHTTPServer
from threading import Thread
import time
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.error import HTTPError
from urllib.request import urlopen, Request

import discord

from main import Bot, supervise
from runtime_health import HealthState, health_handler
from test_tickets import Connection, pool_for
from test_tickets import fixture


class HealthTests(unittest.TestCase):
    def test_readiness_reflects_components_and_stale_loop(self):
        state=HealthState()
        self.assertFalse(state.snapshot()['ready'])
        state.update(discord=True,database=True,tickets=True,commands=True)
        self.assertTrue(state.snapshot()['ready'])
        with patch('runtime_health.time.monotonic',return_value=time.monotonic()+61):
            self.assertFalse(state.snapshot()['ready'])
            self.assertFalse(state.snapshot()['event_loop'])

    def test_real_http_status_and_head_without_external_services(self):
        state=HealthState()
        server=ThreadingHTTPServer(('127.0.0.1',0),health_handler(state))
        thread=Thread(target=server.serve_forever,daemon=True);thread.start()
        url=f'http://127.0.0.1:{server.server_port}'
        try:
            with self.assertRaises(HTTPError) as error:
                urlopen(url+'/readyz',timeout=2)
            self.assertEqual(error.exception.code,503)
            error.exception.close()
            with self.assertRaises(HTTPError) as root_error:
                urlopen(url+'/',timeout=2)
            self.assertEqual(root_error.exception.code,503)
            root_error.exception.close()
            state.update(discord=True,database=True,tickets=True,commands=True)
            with urlopen(url+'/readyz',timeout=2) as response:
                self.assertTrue(json.load(response)['ready'])
            with urlopen(Request(url+'/healthz',method='HEAD'),timeout=2) as response:
                self.assertEqual(response.read(),b'')
        finally:
            server.shutdown();server.server_close();thread.join()


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_idle_ticket_recovery_only_wakes_database_once_per_hour(self):
        cog, _, _ = fixture()
        cog.bot.guilds = []
        cog._recover_messages = AsyncMock(return_value=False)
        cog._recover_deliveries = AsyncMock(return_value=False)
        cog.inbox.next_retry_time = AsyncMock(return_value=None)
        for minute in range(0, 61, 5):
            with patch('cogs.tickets.time.time', return_value=10000 + minute*60):
                await cog._run_recovery()
        self.assertEqual(cog.inbox.next_retry_time.await_count, 2)
        self.assertEqual(cog._recover_deliveries.await_count, 2)

    async def test_pending_work_is_retried_before_idle_sweep(self):
        cog, _, _ = fixture()
        cog.bot.guilds = []
        cog._recover_messages = AsyncMock(return_value=False)
        cog._recover_deliveries = AsyncMock(return_value=False)
        cog.inbox.next_retry_time = AsyncMock(side_effect=[10700, None])
        for stamp in (10000,10300,10600,10900):
            with patch('cogs.tickets.time.time', return_value=stamp):
                await cog._run_recovery()
        self.assertEqual(cog._recover_messages.await_count, 2)

    async def test_unrelated_member_departure_skips_sql(self):
        cog, conn, message = fixture()
        conn.fetch = AsyncMock(return_value=[])
        cog._ticket_owners = {message.author.id + 1}
        await cog.on_member_remove(message.author)
        conn.fetch.assert_not_awaited()

    async def test_database_health_recovers_ten_seconds_after_transient_timeout(self):
        conn = Connection()
        conn.fetchval = AsyncMock(side_effect=[TimeoutError(), 1])
        health = HealthState()
        client = NS(pool=pool_for(conn), health=health, is_ready=lambda: True,
                    is_closed=MagicMock(side_effect=[False, False, True]))
        now = [1000]
        async def advance(seconds):
            now[0] += seconds
        with patch('main.time', NS(monotonic=lambda: now[0])), patch('main.asyncio.sleep', new=advance):
            await Bot._monitor_health(client)
        self.assertEqual(conn.fetchval.await_count, 2)
        self.assertTrue(health.snapshot()['database'])

    async def test_transient_failure_creates_fresh_client_and_closes_previous(self):
        clients=[]
        def factory(**kwargs):
            client=NS(start=AsyncMock(side_effect=RuntimeError('offline') if not clients else asyncio.CancelledError()),close=AsyncMock())
            clients.append(client);return client
        with self.assertRaises(asyncio.CancelledError):
            await supervise('fake','fake',HealthState(),bot_factory=factory,sleep=AsyncMock())
        self.assertEqual(len(clients),2)
        for client in clients: client.close.assert_awaited_once()

    async def test_invalid_credentials_are_not_retried_forever(self):
        client=NS(start=AsyncMock(side_effect=discord.LoginFailure('invalid')),close=AsyncMock())
        sleep=AsyncMock()
        with self.assertRaises(discord.LoginFailure):
            await supervise('fake','fake',HealthState(),bot_factory=lambda **kw:client,sleep=sleep)
        sleep.assert_not_awaited()

    async def test_failed_ticket_cog_aborts_setup_before_command_sync(self):
        client=Bot('fake')
        conn=Connection();pool=pool_for(conn)
        pool.close=AsyncMock();pool.terminate=lambda:None
        client.load_extension=AsyncMock(side_effect=[None,RuntimeError('cog failed')])
        client.tree.sync=AsyncMock()
        with patch('main.asyncpg.create_pool',new=AsyncMock(return_value=pool)):
            try:
                with self.assertRaises(RuntimeError): await client.setup_hook()
                self.assertFalse(client.health.snapshot()['tickets'])
                client.tree.sync.assert_not_awaited()
            finally:
                await client.close()
