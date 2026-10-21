import asyncio
import json
from http.server import ThreadingHTTPServer
from threading import Thread
import time
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch
from urllib.error import HTTPError
from urllib.request import urlopen, Request

import discord

from main import Bot, supervise
from runtime_health import HealthState, health_handler
from test_tickets import Connection, pool_for


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
