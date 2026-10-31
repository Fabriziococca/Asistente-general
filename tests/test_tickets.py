import asyncio
import datetime
import json
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock, patch
import warnings

warnings.filterwarnings('ignore', category=FutureWarning)
from cogs.tickets import Tickets, ROLES, FABRIZIO_ID
from ticket_inbox import ReceiptReviewRequired
from receipts import PRICES
from test_receipts import reading


class Connection:
    def __init__(self, row=None, pending=False):
        self.row = row
        self.pending = pending
        self.execute = AsyncMock()

    def transaction(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def fetchrow(self, *args):
        return self.row

    async def fetchval(self, *args):
        return self.pending


def pool_for(conn):
    pool = NS(acquire=lambda **kw: conn)
    return pool


def fixture():
    conn = Connection()
    bot = NS(pool=pool_for(conn), user=NS(mentioned_in=lambda m: False))
    cog = Tickets(bot)
    cog.payments = NS(known=AsyncMock(return_value=None), save=AsyncMock(return_value=[]),
                      remember=AsyncMock(),
                      prepare=AsyncMock(return_value={'empty': True}),
                      attempt=AsyncMock(return_value=1), finish=AsyncMock(return_value={'remaining':(), 'pending':False}), failed=AsyncMock(),
                      completed=AsyncMock(return_value=[]), reopen=AsyncMock(return_value=None),
                      summary=AsyncMock(return_value={'state':'unknown','roles':[]}),
                      paused=AsyncMock(return_value=False),notice=AsyncMock(return_value=dict(delivery(),entrega_estado='entregado')),
                      notified=AsyncMock(),pending_notified=AsyncMock(),pending_purchases=AsyncMock(return_value=[]),
                      recoverable=AsyncMock(return_value=[]))
    cog.inbox = NS(record=AsyncMock(),claim=AsyncMock(return_value={'attempts':1}),
                   done=AsyncMock(),retry=AsyncMock(),manual=AsyncMock(),progress=AsyncMock(),
                   receipt_status=AsyncMock(return_value=None))
    author = NS(id=123, bot=False, guild_permissions=NS(administrator=False), roles=[])
    author.add_roles = AsyncMock()
    roles = {r['id']: NS(id=r['id']) for r in ROLES.values()}
    guild = NS(id=567, get_role=roles.get, get_member=lambda uid: author,
               fetch_member=AsyncMock(return_value=author))
    channel = NS(id=456, name='ticket-test', category_id=0, sent=[], delete=AsyncMock())
    async def send(content=None, **kw):
        channel.sent.append(content)
        return NS(id=10000+len(channel.sent),edit=AsyncMock())
    channel.send = AsyncMock(side_effect=send)
    def history(**kw):
        async def iterate():
            for message in getattr(channel, 'hist', []):
                yield message
        return iterate()
    channel.history = history
    message = NS(id=789, channel=channel, guild=guild, author=author,
                 content='rango diamante, rango oro, rango plata', attachments=[], mentions=[], reply=AsyncMock())
    return cog, conn, message


def attachment(data=b'photo', mime='image/png', filename='pago.png'):
    return NS(content_type=mime, filename=filename, size=len(data), read=AsyncMock(return_value=data))


def delivery():
    return dict(entrega_estado='preparado', entrega_id='test', roles_asignados=['Diamante', 'Oro', 'Plata'], user_id=123,moneda='ARS',metodo_pago='transferencia')


class TicketTests(unittest.IsolatedAsyncioTestCase):
    async def test_preservation_command_saves_flag_before_confirming(self):
        cog, conn, msg = fixture()
        interaction = NS(channel=msg.channel, response=NS(defer=AsyncMock()),
                         followup=NS(send=AsyncMock()))
        await Tickets.conservar_ticket.callback(cog, interaction)
        self.assertIn('conservar=TRUE', conn.execute.await_args.args[0])
        self.assertTrue(Tickets.conservar_ticket.default_permissions.administrator)
        self.assertTrue(Tickets.conservar_ticket.checks)
        interaction.followup.send.assert_awaited_once()

    async def test_passes_all_photos_and_current_roles_to_purchase(self):
        cog, _, msg = fixture()
        msg.attachments = [attachment(b'photo1'), attachment(b'photo2', None)]
        cog._generate_content_with_rotation = AsyncMock(return_value=json.dumps({'comprobantes': [reading(), reading(1, 7600)]}))
        cog.payments.save.return_value = [dict(pago_id=1,user_id=123,ticket_id=456), dict(pago_id=2,user_id=123,ticket_id=456)]
        cog._handle_pending_purchase = AsyncMock(return_value=True)
        await cog.handle_receipt_image(msg)
        self.assertEqual(len(cog._generate_content_with_rotation.await_args.args[1]), 2)
        self.assertEqual(len(cog.payments.save.await_args.args[1]), 2)
        cog._handle_pending_purchase.assert_awaited_once_with(msg, ('Diamante', 'Oro', 'Plata'))

    async def test_identical_photos_are_read_only_once_by_ai(self):
        cog, _, msg = fixture()
        msg.attachments = [attachment(), attachment()]
        cog._generate_content_with_rotation = AsyncMock(return_value=json.dumps({'comprobantes': [reading()]}))
        cog.payments.save.return_value = [dict(pago_id=1,user_id=123,ticket_id=456)]
        await cog.handle_receipt_image(msg)
        self.assertEqual(len(cog._generate_content_with_rotation.await_args.args[1]), 1)

    async def test_partial_reading_of_two_files_is_queued_without_losing_the_readable_file(self):
        cog,conn,msg=fixture()
        conn.row={'estado':'abierto','user_id':msg.author.id}
        msg.attachments=[attachment(b'first'),attachment(b'second')]
        cog._generate_content_with_rotation=AsyncMock(return_value=json.dumps({'comprobantes':[reading()]}))
        cog.payments.save.return_value=[dict(pago_id=1,user_id=msg.author.id,ticket_id=msg.channel.id)]
        cog._handle_pending_purchase=AsyncMock(return_value=True)
        await cog._process_inbox_message(msg)
        cog.inbox.done.assert_not_awaited()
        cog.inbox.manual.assert_not_awaited()
        cog.inbox.retry.assert_awaited_once()
        cog.payments.save.assert_awaited_once()
        self.assertEqual(len(cog.payments.save.await_args.args[1]),1)

    async def test_known_receipt_uses_no_ai_and_no_new_payment(self):
        cog, _, msg = fixture()
        msg.attachments = [attachment()]
        cog.payments.known.return_value = dict(user_id=123,ticket_id=456,moneda='ARS',metodo_pago='transferencia')
        cog._generate_content_with_rotation = AsyncMock()
        await cog.handle_receipt_image(msg)
        cog._generate_content_with_rotation.assert_not_awaited()
        cog.payments.save.assert_not_awaited()

    async def test_duplicate_verifies_all_registered_roles_before_confirming(self):
        cog, _, msg = fixture()
        msg.content = 'Diamante'
        msg.attachments = [attachment()]
        cog.payments.known.return_value = dict(user_id=123,ticket_id=456,moneda='ARS',metodo_pago='transferencia')
        cog.payments.completed.return_value = [delivery()]
        msg.author.roles = [NS(id=r['id']) for r in ROLES.values()]
        await cog.handle_receipt_image(msg)
        msg.guild.fetch_member.assert_awaited()
        msg.author.add_roles.assert_not_awaited()
        self.assertFalse(any('conciliar' in (s or '') for s in msg.channel.sent))
        cog.payments.save.assert_not_awaited()

    async def test_duplicate_cannot_confirm_requested_role_missing_from_records(self):
        cog, _, msg = fixture()
        msg.content = 'Rango diamante'
        msg.attachments = [attachment()]
        cog.payments.known.return_value = dict(user_id=123,ticket_id=456,moneda='ARS',metodo_pago='transferencia')
        cog.payments.completed.return_value = [dict(delivery(),roles_asignados=['Oro'])]
        await cog.handle_receipt_image(msg)
        msg.author.add_roles.assert_not_awaited()
        self.assertTrue(any('conciliar **Diamante**' in (s or '') for s in msg.channel.sent))
        cog.payments.reopen.assert_not_awaited()

    async def test_duplicate_without_text_also_checks_persisted_pending_roles(self):
        cog, _, msg = fixture()
        msg.content = ''
        msg.attachments = [attachment()]
        cog.payments.known.return_value = dict(user_id=123,ticket_id=456,moneda='ARS',metodo_pago='transferencia')
        cog.payments.completed.return_value = [dict(delivery(),roles_asignados=['Oro'])]
        cog.payments.summary.return_value = {'state':'receipt_pending','roles':['Oro'],'pending_roles':['Diamante']}
        await cog.handle_receipt_image(msg)
        self.assertTrue(any('conciliar **Diamante**' in (s or '') for s in msg.channel.sent))
        msg.author.add_roles.assert_not_awaited()

    async def test_distinct_files_with_same_operation_notify_for_review(self):
        cog, _, msg = fixture()
        msg.attachments = [attachment(b'first'),attachment(b'second')]
        cog._generate_content_with_rotation = AsyncMock(return_value=json.dumps({'comprobantes':[reading(),reading(1)]}))
        cog.payments.save.return_value = [dict(pago_id=1,user_id=123,ticket_id=456)]*2
        with self.assertRaises(ReceiptReviewRequired):
            await cog.handle_receipt_image(msg)
        self.assertTrue(any('misma operación' in (s or '') for s in msg.channel.sent))

    async def test_partial_status_names_the_order_and_uncovered_role(self):
        from decimal import Decimal
        cog, _, msg = fixture()
        cog.payments.prepare.return_value = {
            'totals':{'USDT':Decimal('4.50')}, 'missing':Decimal('4.00'), 'desired':('Diamante','Oro')}
        await cog._handle_pending_purchase(msg)
        self.assertIn('**Diamante, Oro**',msg.channel.sent[-1])
        self.assertIn('4.00 USDT para completar **Oro**',msg.channel.sent[-1])

    async def test_invalid_json_notifies_owner_and_preserves_ticket(self):
        cog, conn, msg = fixture()
        msg.attachments = [attachment()]
        cog._generate_content_with_rotation = AsyncMock(return_value='not JSON')
        with self.assertRaises(json.JSONDecodeError):
            await cog.handle_receipt_image(msg)
        cog.payments.save.assert_not_awaited()
        self.assertTrue(any('conservar=TRUE' in call.args[0] for call in conn.execute.await_args_list))
        self.assertTrue(any(str(FABRIZIO_ID) in (text or '') for text in msg.channel.sent))

    async def test_no_grant_before_payment_persistence(self):
        cog, _, msg = fixture()
        msg.attachments = [attachment()]
        cog._generate_content_with_rotation = AsyncMock(return_value=json.dumps({'comprobantes': [reading()]}))
        cog.payments.save.side_effect = RuntimeError('DB down')
        with self.assertRaises(RuntimeError):
            await cog.handle_receipt_image(msg)
        msg.author.add_roles.assert_not_awaited()

    async def test_delivery_checks_all_roles_and_persists_success(self):
        cog, _, msg = fixture()
        msg.author.roles = [NS(id=r['id']) for r in ROLES.values()]
        await cog._fulfill_delivery(delivery(), msg.channel, msg.guild)
        msg.author.add_roles.assert_not_awaited()
        msg.guild.fetch_member.assert_awaited_once_with(123)
        cog.payments.finish.assert_awaited_once_with('test', 456)

    async def test_partial_discord_assignment_remains_recoverable(self):
        cog, _, msg = fixture()
        msg.author.roles = [NS(id=ROLES['Diamante']['id'])]
        await cog._fulfill_delivery(delivery(), msg.channel, msg.guild)
        cog.payments.finish.assert_not_awaited()
        cog.payments.failed.assert_awaited_once_with('test', 1)

    async def test_missing_role_never_announces_combo_success(self):
        cog, _, msg = fixture()
        msg.guild.get_role = lambda rid: None
        await cog._fulfill_delivery(delivery(), msg.channel, msg.guild)
        msg.author.add_roles.assert_not_awaited()
        cog.payments.finish.assert_not_awaited()

    async def test_already_finished_delivery_does_not_grant_again(self):
        cog, _, msg = fixture()
        cog.payments.attempt.return_value = None
        await cog._fulfill_delivery(delivery(), msg.channel, msg.guild)
        msg.author.add_roles.assert_not_awaited()

    async def test_preserved_ticket_survives_all_bot_deletion_paths(self):
        cog, conn, msg = fixture()
        conn.row = {'conservar': True}
        for member_left in (True, False):
            self.assertFalse(await cog._delete_ticket_if_eligible(msg.channel, member_left))
        msg.channel.delete.assert_not_awaited()

    async def test_recent_discord_activity_prevents_stale_db_cleanup(self):
        cog, conn, msg = fixture()
        conn.row = dict(conservar=False, hablo=True, estado='abierto',
                        ultimo_mensaje=datetime.datetime.now()-datetime.timedelta(days=3))
        msg.created_at = datetime.datetime.now(datetime.timezone.utc)
        msg.channel.hist = [msg]
        self.assertFalse(await cog._delete_ticket_if_eligible(msg.channel))
        msg.channel.delete.assert_not_awaited()

    async def test_unfinished_delivery_prevents_cleanup_even_if_user_left(self):
        cog, conn, msg = fixture()
        conn.row = {'conservar': False}
        conn.pending = True
        self.assertFalse(await cog._delete_ticket_if_eligible(msg.channel, True))
        msg.channel.delete.assert_not_awaited()

    async def test_staff_message_does_not_become_ticket_owner(self):
        cog, conn, msg = fixture()
        msg.author.guild_permissions.administrator = True
        await cog._update_ticket_activity(msg)
        call = conn.execute.await_args_list[-1]
        self.assertIsNone(call.args[2])
        self.assertIn('ELSE tickets.user_id END', call.args[0])

    async def test_no_model_generated_grant_is_executable(self):
        cog, _, msg = fixture()
        channel = msg.channel
        channel.typing = lambda: Connection()
        cog._generate_content_with_rotation = AsyncMock(return_value='[GRANT_ROLE: Diamante]')
        await cog.handle_support_query(msg)
        msg.author.add_roles.assert_not_awaited()
        self.assertNotIn('GRANT_ROLE', msg.reply.await_args.args[0])

    async def test_all_image_parts_reach_provider(self):
        cog, _, _ = fixture()
        cog.api_keys = ['fake-local-test']
        cog.model_pool = ['gemini-3.8-flash']
        model = NS(generate_content=MagicMock(return_value=NS(text='OK')))
        with patch('cogs.tickets.genai.configure'), patch('cogs.tickets.genai.GenerativeModel', return_value=model):
            parts = [{'mime_type': 'image/png', 'data': b'first'}, {'mime_type': 'image/png', 'data': b'second'}]
            self.assertEqual(await cog._generate_content_with_rotation('prompt', parts), 'OK')
        self.assertEqual(model.generate_content.call_args.kwargs['contents'], ['prompt', *parts])

    def test_prices_remain_consistent(self):
        for name, prices in PRICES.items():
            self.assertEqual(float(prices['ARS']), ROLES[name]['ars'])
            self.assertEqual(float(prices['USDT']), ROLES[name]['usdt'])
