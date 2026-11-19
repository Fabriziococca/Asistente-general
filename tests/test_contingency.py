from types import SimpleNamespace as NS
from datetime import timedelta
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

import asyncpg
import discord

from contingency import ADMIN_CONTACT_ID, LocalTickets, QuotaGuard, REVIEW, ROLES, answer
from discord_config import LOCAL_TICKET_MARK
from test_tickets import fixture


class LocalTicketTests(IsolatedAsyncioTestCase):
    def setUp(self):
        self.bot = LocalTickets(':memory:', 100, 500, 900)
        self.addCleanup(self.bot.state.close)
        guild = discord.Object(id=500)
        role = discord.Role(guild=guild,state=None,data={'id':str(ROLES['Oro']['id']),'name':'Oro','position':2,'permissions':'0'})
        higher = discord.Role(guild=guild,state=None,data={'id':'400','name':'bot','position':5,'permissions':'0'})
        self.buyer = NS(id=55,roles=[],guild_permissions=NS(administrator=False))
        async def assign(*roles,**kwargs):
            self.buyer.roles.extend(roles)
        self.buyer.add_roles = AsyncMock(side_effect=assign)
        buyer = self.buyer
        receipt = NS(id=600,created_at=1,author=NS(id=55,bot=False),
                     attachments=[NS(content_type='image/png',filename='pago.png',size=4,read=AsyncMock(return_value=b'foto'))])
        self.receipt = receipt
        self.msg = NS(content='!entregar 600 Oro',created_at=2,
            author=NS(id=ADMIN_CONTACT_ID,guild_permissions=NS(administrator=True)),
            guild=NS(owner_id=100,me=NS(top_role=higher),get_role=lambda _:role,fetch_member=AsyncMock(return_value=buyer)),
            channel=NS(id=400,fetch_message=AsyncMock(return_value=receipt),overwrites_for=lambda _:NS(view_channel=True)),
            reply=AsyncMock())

    async def test_human_confirmation_is_idempotent_and_duplicate_attachment_blocked(self):
        await self.bot.grant(self.msg)
        await self.bot.grant(self.msg)
        self.receipt.id = 601
        self.msg.content = '!entregar 601 Oro'
        await self.bot.grant(self.msg)
        self.buyer.add_roles.assert_awaited_once()
        self.assertEqual(self.bot.state.execute('SELECT receipt,done FROM grants').fetchall(),[(600,1)])
        self.assertIn('ya se usó',self.msg.reply.await_args.args[0])

    async def test_ordinary_buyer_or_other_admin_cannot_approve(self):
        for admin in (False, True):
            self.msg.author = NS(id=777,guild_permissions=NS(administrator=admin))
            await self.bot.grant(self.msg)
        self.msg.channel.fetch_message.assert_not_awaited()
        self.buyer.add_roles.assert_not_awaited()

    async def test_attachment_alone_never_approves_payment(self):
        self.assertEqual(answer(NS(content='Ya pagué',attachments=[1]),False),REVIEW)
        self.buyer.add_roles.assert_not_awaited()

    async def test_normal_bot_ignores_locally_marked_tickets_including_cleanup(self):
        cog, _, msg = fixture()
        msg.channel.topic = 'Ticket Tool\n' + LOCAL_TICKET_MARK
        self.assertFalse(cog._is_ticket(msg.channel))
        await cog._procesar_bienvenida_y_registro(msg.channel)
        self.assertFalse(await cog._delete_ticket_if_eligible(msg.channel))
        msg.channel.send.assert_not_awaited()
        msg.channel.delete.assert_not_awaited()

    async def test_only_confirmed_quota_failure_allows_local_service(self):
        guard = QuotaGuard('test')
        db = NS(close=AsyncMock())
        with patch('contingency.asyncpg.connect',new=AsyncMock(side_effect=[
                asyncpg.InsufficientResourcesError('quota exceeded'), db, OSError('offline')])) as connect:
            self.assertTrue(await guard.blocked())
            self.assertTrue(await guard.blocked())
            self.assertFalse(await guard.blocked(refresh=True))
            db.close.assert_awaited_once()
            with self.assertRaises(OSError):
                await guard.blocked()
            self.assertEqual(connect.await_count,3)

    async def test_reconnect_reply_once_and_keep_tickets_with_conversation(self):
        channel = NS(id=101, name='ticket-9999',topic=LOCAL_TICKET_MARK,category_id=None)
        customer = NS(id=55,bot=False,guild_permissions=NS(administrator=False))
        bot = NS(id=900,bot=True)
        created = discord.utils.utcnow()-timedelta(hours=4)
        first = discord.utils.time_snowflake(created)
        message = NS(id=first,author=customer,content='Hola',attachments=[])
        history = [message]
        async def entries(limit,after=None,oldest_first=False):
            for item in list(history):
                if after is None or item.id > after.id:
                    yield item
        async def welcome(embed):
            msg = NS(id=first+1,author=bot,embeds=[embed],reference=None,attachments=[])
            history.append(msg)
            return msg
        async def reply(*args,**kw):
            history.append(NS(id=first+2,author=bot,embeds=[],reference=NS(message_id=first),attachments=[]))
        message.reply=AsyncMock(side_effect=reply)
        channel.history=entries
        channel.send=AsyncMock(side_effect=welcome)
        channel.delete=AsyncMock()
        await self.bot.sync(channel)
        self.bot.state.execute('UPDATE channels SET cursor=0,welcome=NULL')
        await self.bot.sync(channel)
        channel.send.assert_awaited_once()
        message.reply.assert_awaited_once()
        channel.delete.assert_not_awaited()

    async def test_only_empty_unprotected_channel_closes_after_three_hours(self):
        old = discord.utils.time_snowflake(discord.utils.utcnow()-timedelta(hours=4))
        async def empty(**kwargs):
            for item in ():
                yield item
        channel = NS(id=101,name='ticket-9999',topic=LOCAL_TICKET_MARK,category_id=None,
                     guild=NS(me=object()),permissions_for=lambda _:NS(read_message_history=True,manage_channels=True),
                     history=empty,delete=AsyncMock())
        self.bot.state.execute('INSERT INTO channels(id,welcome,keep) VALUES (101,?,1)',(old,))
        await self.bot.sync(channel)
        channel.delete.assert_not_awaited()
        self.bot.state.execute('UPDATE channels SET keep=0')
        await self.bot.sync(channel)
        channel.delete.assert_awaited_once()
