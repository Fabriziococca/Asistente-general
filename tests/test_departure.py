"""Salida del creador: cierre silencioso, carreras y preservación de pagos."""
import asyncio
import json
import os
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

import discord

from payment_store import PaymentStore
from ticket_inbox import TicketInbox, SCHEMA
from cogs.tickets import Tickets
from test_tickets import fixture, delivery, attachment
from test_receipts import reading
import test_payment_store as payment_fixtures
import test_ticket_inbox as inbox_fixtures
from campaign_clock import freeze_campaign


def setUpModule():
    global _clock
    _clock=freeze_campaign();_clock.start()


def tearDownModule():
    _clock.stop()


def absent_member():
    return discord.NotFound(NS(status=404,reason='Unknown Member'),{'code':10007,'message':'Unknown Member'})


class DepartureTests(unittest.IsolatedAsyncioTestCase):
    async def test_departure_overrides_preservation_without_touching_payments(self):
        cog,conn,message=fixture()
        conn.row={'conservar':True};conn.pending=True
        self.assertTrue(await cog._delete_ticket_if_eligible(message.channel,member_left=True))
        cog.inbox.close_channel.assert_awaited_once_with(message.channel.id,departed=True)
        message.channel.delete.assert_awaited_once()
        cog.payments.failed.assert_not_awaited()
        cog.payments.notified.assert_not_awaited()
        message.channel.send.assert_not_awaited()

    async def test_member_remove_does_not_wait_for_a_running_receipt_lock(self):
        cog,conn,message=fixture()
        conn.fetch=AsyncMock(return_value=[{'channel_id':message.channel.id}])
        message.guild.get_channel=lambda cid:message.channel
        async with cog._ticket_lock(message.channel.id):
            await asyncio.wait_for(cog.on_member_remove(NS(id=message.author.id,guild=message.guild)),timeout=1)
        message.channel.delete.assert_awaited_once()

    async def test_member_remove_fetches_uncached_channel_and_leaves_other_guild_alone(self):
        cog,conn,message=fixture();message.guild.get_channel=lambda cid:None
        other=NS(id=888,guild=NS(id=999),delete=AsyncMock())
        conn.fetch=AsyncMock(return_value=[{'channel_id':message.channel.id},{'channel_id':other.id}])
        cog.bot.fetch_channel=AsyncMock(side_effect=[message.channel,other])
        await cog.on_member_remove(NS(id=message.author.id,guild=message.guild))
        message.channel.delete.assert_awaited_once()
        other.delete.assert_not_awaited()

    async def test_offline_departure_is_confirmed_by_rest_before_deleting(self):
        cog,_,message=fixture();cog.inbox.owner.return_value=message.author.id
        message.guild.fetch_member.side_effect=absent_member()
        self.assertFalse(await cog._ensure_creator_present(message.channel))
        message.guild.fetch_member.assert_awaited_once_with(message.author.id)
        message.channel.delete.assert_awaited_once()
        cog.inbox.close_channel.assert_awaited_once_with(message.channel.id,departed=True)

    async def test_failed_member_lookup_does_not_delete_a_present_clients_ticket(self):
        for error in (discord.Forbidden(NS(status=403,reason='Forbidden'),'no permission'),
                      discord.HTTPException(NS(status=503,reason='Unavailable'),'temporary'),
                      discord.NotFound(NS(status=404,reason='Unknown Guild'),{'code':10004,'message':'Unknown Guild'})):
            cog,_,message=fixture();cog.inbox.owner.return_value=message.author.id
            message.guild.fetch_member.side_effect=error
            with self.assertRaises(type(error)):
                await cog._ensure_creator_present(message.channel)
            message.channel.delete.assert_not_awaited()
            cog.inbox.close_channel.assert_not_awaited()

    async def test_alerts_and_delivery_notices_are_silent_after_departure(self):
        cog,_,message=fixture();cog.inbox.closed.return_value=True
        await cog._request_manual_review(message.channel,'revisar')
        await cog._notify_delivery(delivery(),message.channel)
        self.assertFalse(await cog._fulfill_delivery(delivery(),message.channel,message.guild))
        message.channel.send.assert_not_awaited()
        cog.payments.attempt.assert_not_awaited()

    async def test_inflight_failure_cannot_send_alerts_or_requeue_after_departure(self):
        cog,_,message=fixture()
        async def fail_after_departure(*args,**kwargs):
            await cog._close_ticket(message.channel,departed=True)
            raise RuntimeError('provider completed late')
        cog._handle_ticket_message=AsyncMock(side_effect=fail_after_departure)
        await cog._process_inbox_message(message)
        cog.inbox.retry.assert_not_awaited()
        cog.inbox.manual.assert_not_awaited()
        message.channel.send.assert_not_awaited()

    async def test_deleted_channel_cancels_receipts_without_alerting_or_marking_success(self):
        cog,_,message=fixture();cog.bot.guilds=[];cog.bot.get_channel=lambda cid:None
        cog.bot.fetch_channel=AsyncMock(side_effect=discord.NotFound(NS(status=404,reason='gone'),{'code':10003,'message':'Unknown Channel'}))
        cog.inbox.pending=AsyncMock(return_value=[{'channel_id':456,'message_id':789,'is_receipt':True}])
        await cog._recover_messages()
        cog.inbox.close_channel.assert_awaited_once_with(456)
        cog.inbox.done.assert_not_awaited()
        cog.inbox.manual.assert_not_awaited()
        message.channel.send.assert_not_awaited()

    async def test_deleted_message_with_present_creator_still_requests_manual_review(self):
        cog,_,message=fixture();cog.bot.guilds=[];cog.bot.get_channel=lambda cid:message.channel
        cog.inbox.owner.return_value=message.author.id
        message.channel.fetch_message=AsyncMock(side_effect=discord.NotFound(NS(status=404,reason='gone'),{'code':10008,'message':'Unknown Message'}))
        cog.inbox.pending=AsyncMock(return_value=[{'channel_id':456,'message_id':789,'is_receipt':True}])
        await cog._recover_messages()
        cog.inbox.manual.assert_awaited_once()
        message.channel.delete.assert_not_awaited()
        self.assertTrue(message.channel.sent)

    async def test_delete_permission_error_stops_work_and_leaves_retryable_close_state(self):
        cog,_,message=fixture()
        message.channel.delete.side_effect=discord.Forbidden(NS(status=403,reason='Forbidden'),'cannot delete')
        with self.assertRaises(discord.Forbidden):
            await cog._close_ticket(message.channel,departed=True)
        self.assertIn(message.channel.id,cog._closed_tickets)
        cog.inbox.close_channel.assert_awaited_once()
        await cog._request_manual_review(message.channel,'revisar')
        message.channel.send.assert_not_awaited()

    async def test_departure_during_delivery_is_not_reported_as_another_technical_retry(self):
        cog,_,message=fixture();message.guild.fetch_member.side_effect=absent_member()
        self.assertFalse(await cog._fulfill_delivery(delivery(),message.channel,message.guild))
        message.channel.delete.assert_awaited_once()
        cog.payments.failed.assert_not_awaited()
        message.channel.send.assert_not_awaited()

    async def test_acknowledgement_database_failure_does_not_break_the_main_answer(self):
        cog,_,message=fixture();cog.inbox.closed.side_effect=TimeoutError()
        with patch('cogs.tickets.asyncio.sleep',new=AsyncMock()):
            await cog._acknowledge_slow_message(message)
        message.channel.send.assert_not_awaited()

    async def test_late_success_saves_receipt_but_does_not_announce_after_departure(self):
        for channel_name in ('ticket-test','sug-test'):
            cog,_,message=fixture();message.channel.name=channel_name
            message.attachments=[attachment()]
            warning=NS(id=10001,edit=AsyncMock())
            message.channel.send=AsyncMock(return_value=warning)
            async def late_reading(*args,**kwargs):
                await cog._close_ticket(message.channel,departed=True)
                return json.dumps({'comprobantes':[reading()]})
            cog._generate_content_with_rotation=AsyncMock(side_effect=late_reading)
            await cog.handle_receipt_image(message)
            cog.payments.save.assert_awaited_once()
            cog.payments.prepare.assert_not_awaited()
            warning.edit.assert_not_awaited()
            message.channel.send.assert_awaited_once()


@unittest.skipUnless(os.environ.get('BOT_TEST_DATABASE_URL'),'PostgreSQL de prueba no configurado')
class DepartureDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await payment_fixtures.PaymentDatabaseTests.asyncSetUp(self)
        await self.conn.execute(SCHEMA.replace('CREATE TABLE IF NOT EXISTS','CREATE TEMP TABLE IF NOT EXISTS'))
        self.inbox=TicketInbox(payment_fixtures.BoundConnection(self.conn))
        await self.inbox.initialize()

    def bot(self):
        return inbox_fixtures.InboxDatabaseTests.receipt_bot(self)

    async def test_close_cancels_pending_processing_manual_and_survives_late_results_and_restart(self):
        cog,message,_=self.bot()
        await self.inbox.record(message)
        await self.inbox.claim(message.id)
        for mid,state in [(1001,'pending'),(1002,'manual'),(1003,'done')]:
            await self.conn.execute("""INSERT INTO ticket_inbox(message_id,channel_id,guild_id,user_id,state,is_receipt)
                VALUES($1,-456,-567,-123,$2,TRUE)""",mid,state)
        await self.conn.execute("UPDATE tickets SET conservar=TRUE WHERE channel_id=-456")
        await self.inbox.close_channel(-456,departed=True)
        await self.inbox.retry(message.id,TimeoutError())
        await self.inbox.manual(message.id,RuntimeError())
        await self.inbox.done(message.id)
        await self.inbox.record(message)
        await self.inbox.initialize()
        self.assertEqual(await self.conn.fetchval('SELECT state FROM ticket_inbox WHERE message_id=1000'),'cancelled')
        self.assertEqual(await self.conn.fetchval('SELECT state FROM ticket_inbox WHERE message_id=1002'),'cancelled')
        self.assertEqual(await self.conn.fetchval('SELECT state FROM ticket_inbox WHERE message_id=1003'),'done')
        self.assertFalse(await self.inbox.pending())
        self.assertIsNone(await self.inbox.claim(message.id))
        message.id=1004;await self.inbox.record(message)
        self.assertFalse(await self.conn.fetchval('SELECT EXISTS(SELECT 1 FROM ticket_inbox WHERE message_id=1004)'))
        self.assertEqual(await self.conn.fetchval('SELECT estado FROM tickets WHERE channel_id=-456'),'salio')

    async def test_pending_payments_and_financial_fields_remain_exactly_unchanged(self):
        await payment_fixtures.PaymentDatabaseTests.save_two(self)
        await self.store.prepare(-456,-123,('Diamante','Oro','Plata'))
        before=[dict(row) for row in await self.conn.fetch('SELECT * FROM pagos ORDER BY pago_id')]
        await self.inbox.close_channel(-456,departed=True)
        self.assertEqual([dict(row) for row in await self.conn.fetch('SELECT * FROM pagos ORDER BY pago_id')],before)
        self.assertFalse(await self.store.pending_purchases())
        self.assertFalse(await self.store.recoverable())
        self.assertTrue((await self.store.prepare(-456,-123,('Diamante',)))['closed'])
        self.assertIsNone(await self.store.attempt(before[-1]['entrega_id']))
        await self.store.remember(-456,-123,('Oro',))
        self.assertEqual(await self.conn.fetchval('SELECT estado FROM tickets WHERE channel_id=-456'),'salio')

    async def test_departure_during_receipt_failure_cancels_job_without_another_notice(self):
        cog,message,_=self.bot()
        async def failed_late(*args,**kwargs):
            await cog._close_ticket(message.channel,departed=True)
            raise RuntimeError('late result')
        cog._handle_ticket_message=AsyncMock(side_effect=failed_late)
        await cog.on_message(message)
        self.assertEqual(await self.conn.fetchval('SELECT state FROM ticket_inbox WHERE message_id=1000'),'cancelled')
        self.assertFalse(await self.inbox.pending())
        message.channel.send.assert_not_awaited()
        message.channel.delete.assert_awaited_once()

    async def test_offline_departure_closes_preserved_ticket_with_paid_delivery_without_losing_money(self):
        cog,message,_=self.bot()
        await payment_fixtures.PaymentDatabaseTests.save_two(self)
        await self.store.prepare(-456,-123)
        await self.conn.execute("UPDATE tickets SET conservar=TRUE WHERE channel_id=-456")
        cog.bot.guilds=[NS(text_channels=[message.channel])]
        message.guild.fetch_member.side_effect=absent_member()
        await cog._recover_messages()
        self.assertEqual(await self.conn.fetchval('SELECT estado FROM tickets WHERE channel_id=-456'),'salio')
        self.assertEqual(await self.conn.fetchval("SELECT sum(monto) FROM pagos WHERE comprobante_hash IS NOT NULL"),13500)
        self.assertFalse(await self.store.recoverable())
        message.channel.delete.assert_awaited_once()
        message.channel.send.assert_not_awaited()

    async def test_paid_delivery_notice_does_not_reappear_after_channel_is_closed(self):
        await payment_fixtures.PaymentDatabaseTests.save_two(self)
        result=await self.store.prepare(-456,-123)
        delivery=result['delivery']
        await self.store.finish(delivery['entrega_id'],-456)
        await self.inbox.close_channel(-456,departed=True)
        self.assertFalse(await self.store.recoverable())
        self.assertIsNone(await self.store.reopen(delivery['entrega_id'],-456,-123))
        self.assertEqual(await self.conn.fetchval('SELECT estado FROM tickets WHERE channel_id=-456'),'salio')

    async def test_channel_delete_event_does_not_downgrade_departure_or_clear_preservation(self):
        await self.conn.execute("UPDATE tickets SET conservar=TRUE WHERE channel_id=-456")
        await self.inbox.close_channel(-456,departed=True)
        await self.inbox.close_channel(-456)
        row=await self.conn.fetchrow('SELECT estado,conservar FROM tickets WHERE channel_id=-456')
        self.assertEqual(row['estado'],'salio')
        self.assertTrue(row['conservar'])

    async def test_late_admin_commands_do_not_reopen_a_closed_ticket(self):
        cog,message,_=self.bot()
        await self.inbox.close_channel(-456,departed=True)
        interaction=NS(channel=message.channel,response=NS(defer=AsyncMock()),
                       followup=NS(send=AsyncMock()))
        for command in (Tickets.modo_manual,Tickets.modo_automatico,Tickets.conservar_ticket):
            await command.callback(cog,interaction)
            self.assertEqual(await self.conn.fetchval('SELECT estado FROM tickets WHERE channel_id=-456'),'salio')
        self.assertEqual(interaction.followup.send.await_count,3)
