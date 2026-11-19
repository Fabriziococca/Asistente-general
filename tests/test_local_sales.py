import datetime as dt
from decimal import Decimal
import io
import os
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock

import asyncpg
import discord
from PIL import Image, ImageDraw, ImageFont

from contingency import LocalTickets, ROLES
from local_sales import LocalSales, SaleReview
from payment_policy import BANK_ACCOUNT
from payment_store import PaymentStore
from receipt_ocr import operation_date, MODEL_DIR
from receipts import Receipt
from sale_import import import_sales
from test_payment_store import BoundConnection


DAY = dt.date(2026,11,19)
WHEN = dt.datetime(2026,11,19,19,tzinfo=dt.timezone.utc)


def msg(channel=501,buyer=55,mid=700):
    return NS(id=mid,channel=NS(id=channel),author=NS(id=buyer),guild=NS(id=500),created_at=WHEN)


def paid(op='12345678', amount='5300', currency='ARS', day=DAY):
    return Receipt(0,Decimal(amount),currency,'transferencia' if currency=='ARS' else 'binance',op,day)


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.client = LocalTickets(':memory:',100,500,900)
        self.addCleanup(self.client.state.close)
        self.book = self.client.sales

    def test_multi_file_installments_and_multi_currency(self):
        self.book.record(msg(),[('a',paid('1234567','2000'))],('Oro','Plata'),DAY,DAY)
        self.assertFalse(self.book.pending(501))
        self.book.record(msg(mid=701),[('b',paid('2234567','3300')),('c',paid('3234567','2.50','USDT'))],(),DAY,DAY)
        pending = self.book.pending(501)
        self.assertEqual({r[2] for r in pending},{'Oro','Plata'})
        # El mismo archivo reenviado y la misma operación fotografiada de nuevo se cuentan una sola vez.
        self.book.record(msg(mid=702),[('a',paid('1234567','2000')),('alt-b',paid('2234567','3300'))],(),DAY,DAY)
        self.assertEqual(self.book.pending(501),pending)
        self.assertEqual(self.client.state.execute('SELECT count(*) FROM sale_payments').fetchone(),(3,))

    def test_bad_identity_rolls_back_whole_batch(self):
        for bad in [paid(day=None), paid(day=DAY-dt.timedelta(days=1)),paid(day=DAY+dt.timedelta(days=1)),paid(op=None)]:
            with self.assertRaises(SaleReview):
                self.book.record(msg(),[('valid',paid('789123456')),('bad',bad)],('Oro',),DAY,DAY)
        self.assertEqual(self.client.state.execute('SELECT count(*) FROM sale_payments').fetchone(),(0,))
        self.book.record(msg(),[('valid',paid())],('Oro',),DAY,DAY)
        with self.assertRaises(SaleReview):
            self.book.record(msg(502,56),[('new copy',paid())],('Oro',),DAY,DAY)
        with self.assertRaises(SaleReview):
            self.book.record(msg(),[('new copy',paid(amount='2300'))],(),DAY,DAY)

    def test_dates(self):
        self.assertEqual(operation_date('19 de noviembre de 2026'),str(DAY))
        self.assertEqual(operation_date('Fecha: 19/11/2026\nDescarga: 20/11/2026'),str(DAY))
        self.assertIsNone(operation_date('17/11/2026\n19/11/2026'))


class CheckoutTests(unittest.IsolatedAsyncioTestCase):
    @unittest.skipUnless((MODEL_DIR/'eng.traineddata').is_file(),'Requiere modelo OCR local')
    async def test_real_image_and_pdf_restart_after_discord_accepted_grant(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'sales.db'
            since = discord.utils.time_snowflake(WHEN - dt.timedelta(hours=1))
            cid = since+10
            customer = NS(id=55,bot=False,guild_permissions=NS(administrator=False),roles=[])
            guild_obj = discord.Object(id=500)
            def role(name,rid,pos):
                return discord.Role(guild=guild_obj,state=None,data=dict(name=name,id=str(rid),position=pos,permissions='0'))
            gold = role('Oro',ROLES['Oro']['id'],2)
            guild = NS(id=500,me=NS(top_role=role('Bot',400,5)),get_role=lambda _:gold,
                       fetch_member=AsyncMock(return_value=customer))
            async def interrupted(*roles,**kwargs):
                customer.roles.extend(roles)
                raise OSError('Se perdió el acuse de Discord')
            customer.add_roles = AsyncMock(side_effect=interrupted)
            blobs = []
            for fmt,mime,amount,operation in [('PNG','image/png','2.000,00','11001122'),('PDF','application/pdf','3.300,00','22002233')]:
                with Image.new('RGB',(1550,680),'white') as image:
                    ImageDraw.Draw(image).multiline_text((40,40),
                        f'Comprobante de Transferencia\nFecha 19/11/2026\nImporte debitado $ {amount}\n'
                        f'Cuenta destino {BANK_ACCOUNT}\nNumero de comprobante {operation}',
                        fill='black',font=ImageFont.load_default(size=34),spacing=22)
                    out=io.BytesIO()
                    image.save(out,format=fmt)
                data=out.getvalue()
                blobs.append(NS(content_type=mime,size=len(data),read=AsyncMock(return_value=data)))
            message=msg(cid)
            message.guild=guild
            message.channel.overwrites_for=lambda _:NS(view_channel=True)
            message.content='Quiero Oro'
            message.attachments=blobs
            one=LocalTickets(path,since,500,900)
            one.reader._generate_content_with_rotation=AsyncMock(side_effect=TimeoutError())
            try:
                with self.assertRaises(OSError):
                    await one.process_sale(message)
                self.assertEqual(len(one.sales.pending(cid)),1)
            finally:
                one.state.close()
            two=LocalTickets(path,since,500,900)
            try:
                two.reader._read_receipts=AsyncMock(side_effect=AssertionError('No releer un pago guardado'))
                self.assertIn('Oro',await two.process_sale(message))
                self.assertFalse(two.sales.pending(cid))
                customer.add_roles.assert_awaited_once()
                self.assertEqual(two.state.execute('SELECT SUM(cents) FROM sale_payments').fetchone(),(530000,))
            finally:
                two.state.close()


@unittest.skipUnless(os.environ.get('BOT_TEST_DATABASE_URL'),'PostgreSQL de prueba no configurado')
class ImportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = LocalTickets(':memory:',100,500,900)
        self.db=self.client.state
        self.addCleanup(self.db.close)
        self.pg=await asyncpg.connect(os.environ['BOT_TEST_DATABASE_URL'])
        self.addAsyncCleanup(self.pg.close)
        await self.pg.execute("""SET search_path=pg_temp;
            CREATE TEMP TABLE usuarios(user_id BIGINT PRIMARY KEY);
            CREATE TEMP TABLE tickets(channel_id BIGINT PRIMARY KEY,user_id BIGINT,estado TEXT,hablo BOOLEAN);
            CREATE TEMP TABLE pagos(pago_id SERIAL PRIMARY KEY,user_id BIGINT REFERENCES usuarios,ticket_id BIGINT,
                monto NUMERIC,moneda TEXT,metodo_pago TEXT,concepto TEXT,fecha_pago TIMESTAMP);
            INSERT INTO usuarios VALUES (222);
            INSERT INTO pagos(user_id,monto,concepto) VALUES (222,17.25,'Histórico');""")
        await PaymentStore(BoundConnection(self.pg)).initialize()

    async def test_copy_replay_and_conflict_do_not_alter_history(self):
        book = self.client.sales
        book.record(msg(),[('one',paid())],('Oro',),DAY,DAY)
        self.db.execute('UPDATE sale_deliveries SET done=1')
        self.assertEqual(await import_sales(self.db,self.pg,500),1)
        self.db.execute('UPDATE sale_payments SET imported=0')
        self.assertEqual(await import_sales(self.db,self.pg,500),1)
        row=await self.pg.fetchrow("SELECT monto,roles_asignados,entrega_estado,fecha_pago FROM pagos WHERE comprobante_hash='one'")
        self.assertEqual(tuple(row),(Decimal(5300),['Oro'],'entregado',WHEN.replace(tzinfo=None)))
        book.record(msg(502,56,701),[('two',paid('87654321','2300'))],('Plata',),DAY,DAY)
        await self.pg.execute("INSERT INTO pagos(comprobante_hash,concepto,monto) VALUES ('two','Otro registro',10)")
        self.assertEqual(await import_sales(self.db,self.pg,500),0)
        self.assertEqual(self.db.execute('SELECT channel FROM sale_import_conflicts').fetchall(),[(502,)])
        self.assertEqual(await self.pg.fetchval('SELECT count(*) FROM pagos'),3)
        self.assertEqual(await self.pg.fetchval("SELECT monto FROM pagos WHERE concepto='Histórico'"),Decimal('17.25'))
        self.assertEqual(await self.pg.fetchval("SELECT monto FROM pagos WHERE comprobante_hash='two'"),10)

    async def test_manual_review_is_preserved_once_and_not_counted_as_revenue(self):
        self.client.sales.review(msg(),'Falta la fecha de operación')
        self.db.execute("INSERT INTO grants VALUES (700,501,55,77,'Oro',1)")
        self.db.execute("INSERT INTO receipt_files VALUES ('archivo',700)")
        for _ in range(2):
            self.assertEqual(await import_sales(self.db,self.pg,500),0)
        notes=await self.pg.fetch("SELECT tipo, detalle->>'motivo' AS motivo, detalle->>'confirmada' AS confirmada FROM contingencia_revisiones ORDER BY tipo")
        self.assertEqual([tuple(row) for row in notes],[
            ('entrega_manual',None,'true'),('revision','Falta la fecha de operación',None)])
        self.assertEqual(await self.pg.fetchval('SELECT COUNT(*) FROM pagos'),1)
