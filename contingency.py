"""Atención local de tickets nuevos durante una interrupción de la base principal."""

import argparse
import asyncio
import datetime as dt
import hashlib
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import socket
import sqlite3
import time
from urllib.parse import urlsplit

import asyncpg
import discord
from discord.ext import tasks
from dotenv import load_dotenv

from communication import local_answer
from discord_config import ADMIN_CONTACT_ID, LOCAL_TICKET_MARK, is_local_ticket
from local_sales import LocalSales, SaleReview
from payment_policy import payment_copy
from receipt_reader import ReceiptReader
from receipt_ocr import ReceiptProcessingUnavailable
from receipts import attachment_mime, normalize, requested_roles, parse_receipts
from sale_import import import_sales
from cogs.tickets import ID_CATEGORIA_SUGERENCIAS, ROLES


log = logging.getLogger('contingencia')
WELCOME = 'Atención temporal de Chaquetas'
RANK_HELP = 'Después de verificar la acreditación, administración puede usar `!entregar ID_DEL_MENSAJE Oro` en este ticket. Para varios rangos, separalos por comas.'
REVIEW = 'Administración revisa el comprobante y entrega los rangos desde este ticket. Si ya pagaste, no vuelvas a pagar.'
AUTO = 'Indicá los rangos que querés y adjuntá la foto o PDF del pago. Voy a leer el comprobante y entregar los rangos. Si hace falta una revisión, te aviso acá.'
ARGENTINA = dt.timezone(dt.timedelta(hours=-3))


def answer(message, suggestion):
    text = normalize(message.content)
    if message.attachments or 'canjear' in text:
        return REVIEW
    if any(word in text for word in ('comprobante','transferi','pague','entrega','demora','tarda','mi rango','mis rango','mi rol','mis rol')):
        return REVIEW if suggestion else 'Si ya pagaste, adjuntá el comprobante y decime qué rangos elegiste. No vuelvas a pagar.'
    result = local_answer(message.content, suggestion=suggestion)
    if result:
        return result
    roles = requested_roles(message.content)
    if roles and not suggestion:
        return payment_copy()[2] + '\n' + REVIEW
    return 'Dejá tu consulta acá para que administración pueda ayudarte.'


class QuotaGuard:
    """Solo atiende cuando Neon confirma el bloqueo de este proyecto por cuota."""

    def __init__(self, dsn):
        self.dsn = dsn
        self.valid_until = 0

    async def blocked(self, *, refresh=False):
        if not refresh and time.monotonic() < self.valid_until:
            return True
        self.valid_until = 0
        try:
            connection = await asyncpg.connect(self.dsn,timeout=10)
        except asyncpg.InsufficientResourcesError as exc:
            if 'quota' in str(exc).lower():
                self.valid_until = time.monotonic() + 120
                return True
            raise
        else:
            await connection.close()
            return False


class LocalTickets(discord.Client):
    def __init__(self, state, since, guild_id, bot_id, guard=None):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        super().__init__(intents=intents, allowed_mentions=discord.AllowedMentions.none())
        self.guild_id, self.bot_id = guild_id, bot_id
        self.guard = guard
        self.state = sqlite3.connect(state, isolation_level=None)
        self.state.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS config (id INTEGER PRIMARY KEY CHECK(id=1), guild INTEGER, bot INTEGER, since INTEGER);
            CREATE TABLE IF NOT EXISTS channels (id INTEGER PRIMARY KEY, welcome INTEGER, cursor INTEGER DEFAULT 0,
                manual INTEGER DEFAULT 0, keep INTEGER DEFAULT 0, activity INTEGER DEFAULT 0, closed INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS grants (receipt INTEGER PRIMARY KEY, channel INTEGER, buyer INTEGER, staff INTEGER, roles TEXT, done INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS receipt_files (digest TEXT PRIMARY KEY, receipt INTEGER NOT NULL);
        """)
        self.state.execute('INSERT OR IGNORE INTO config VALUES (1,?,?,?)',(guild_id,bot_id,since))
        if self.state.execute('SELECT guild,bot,since FROM config').fetchone() != (guild_id,bot_id,since):
            raise ValueError('El alcance no coincide con el de este registro local')
        self.since = since
        self.lock = asyncio.Lock()
        self.stopping = False
        self.next_quota_check = time.monotonic() + 3600
        self.sales = LocalSales(self.state)
        self.reader = ReceiptReader()

    def eligible(self, channel):
        return (isinstance(channel, discord.TextChannel) and channel.guild.id == self.guild_id
                and channel.id >= self.since and (channel.name.startswith(('ticket-', 'sug-'))
                or channel.category_id == ID_CATEGORIA_SUGERENCIAS)
                and not channel.permissions_for(channel.guild.default_role).view_channel)

    async def setup_hook(self):
        if self.user.id != self.bot_id:
            raise ValueError('La credencial no corresponde al bot esperado')
        self.reconcile.start()

    async def on_ready(self):
        guild = self.get_guild(self.guild_id)
        if guild is None:
            await self.close()
            return
        log.info('Discord listo bot=%s guild=%s modo=ventas-locales', self.user.id, guild.id)

    @tasks.loop(minutes=2)
    async def reconcile(self):
        if self.guard and time.monotonic() >= self.next_quota_check:
            try:
                if not await self.guard.blocked(refresh=True):
                    await self.close()
                    return
                self.next_quota_check = time.monotonic() + 3600
            except (OSError,asyncpg.PostgresError):
                return
        guild = self.get_guild(self.guild_id)
        if guild:
            for channel in guild.text_channels:
                if self.eligible(channel):
                    try:
                        await self.sync(channel)
                    except Exception as exc:
                        log.warning('Ticket %s pendiente: %s',channel.id,type(exc).__name__)

    @reconcile.before_loop
    async def wait_for_gateway(self):
        await self.wait_until_ready()

    async def on_guild_channel_create(self, channel):
        if self.eligible(channel):
            await asyncio.sleep(8)
            await self.sync(channel)

    async def on_message(self, message):
        if not message.author.bot and self.eligible(message.channel):
            await self.sync(message.channel)

    async def on_interaction(self, interaction):
        if self.stopping or interaction.guild_id != self.guild_id or interaction.type != discord.InteractionType.application_command:
            return
        if self.guard and not await self.guard.blocked():
            await self.close()
            return
        await interaction.response.defer(ephemeral=True)
        cmd = interaction.data.get('name')
        if self.eligible(interaction.channel) and interaction.permissions.administrator and cmd in ('manual','automatico','conservar_ticket'):
            async with self.lock:
                self.state.execute('INSERT OR IGNORE INTO channels(id) VALUES (?)',(interaction.channel_id,))
                if cmd != 'conservar_ticket':
                    self.state.execute('UPDATE channels SET manual=? WHERE id=?',(int(cmd=='manual'),interaction.channel_id))
                else:
                    self.state.execute('UPDATE channels SET keep=1 WHERE id=?',(interaction.channel_id,))
            text = {'manual':'La atención automática de este ticket quedó pausada.',
                    'automatico':'Volví a habilitar las respuestas de atención temporal.',
                    'conservar_ticket':'Guardé este ticket para la conciliación.'}[cmd]
        else:
            text = 'El historial de Neon está temporalmente fuera de línea. ' + REVIEW
        await interaction.followup.send(text,ephemeral=True)

    async def sync(self, channel):
        async with self.lock:
            if self.stopping:
                return
            self.state.execute('INSERT OR IGNORE INTO channels(id) VALUES (?)',(channel.id,))
            welcome, cursor, manual = self.state.execute('SELECT welcome,cursor,manual FROM channels WHERE id=?',(channel.id,)).fetchone()
            if welcome and not is_local_ticket(channel):
                return
            if self.guard and not await self.guard.blocked(refresh=not welcome):
                log.info('Neon disponible. Finaliza la atención local antes de escribir en Discord.')
                await self.close()
                return
            # Un envío puede llegar a Discord justo antes de que se apague la PC.
            # Las referencias de respuesta permiten reanudar sin responder dos veces.
            history = [m async for m in channel.history(limit=None,after=discord.Object(id=cursor or channel.id),oldest_first=True)]
            own = [m for m in history if m.author.id == self.bot_id]
            if not welcome:
                sent = next((m for m in own if any(e.title == WELCOME for e in m.embeds)),None)
                if not sent and any(own):
                    # Otra instancia ya atendió este canal con la lógica habitual.
                    return
                if not is_local_ticket(channel):
                    topic = '\n'.join(filter(None,(channel.topic,LOCAL_TICKET_MARK)))
                    if len(topic) > 1024:
                        log.warning('Sin espacio para identificar el ticket local %s',channel.id)
                        return
                    await channel.edit(topic=topic,reason='Conservar la revisión manual al volver a Neon')
                if not sent and not manual:
                    suggestion = channel.name.startswith('sug-') or channel.category_id == ID_CATEGORIA_SUGERENCIAS
                    _, details, prices, suggestion_price = payment_copy()
                    body = (f'La petición cuesta {suggestion_price}. Esperá la confirmación de disponibilidad antes de pagar.' if suggestion else prices)
                    embed = discord.Embed(title=WELCOME,description=body+'\n\n'+details+'\n\n'+(REVIEW if suggestion else AUTO))
                    sent = await channel.send(embed=embed)
                    log.info('Bienvenida canal=%s mensaje=%s',channel.id,sent.id)
                if sent:
                    self.state.execute('UPDATE channels SET welcome=? WHERE id=?',(sent.id,channel.id))
            answered = {m.reference.message_id for m in own if m.reference}
            for message in history:
                if not message.author.bot or message.attachments:
                    self.state.execute('UPDATE channels SET activity=1 WHERE id=?',(channel.id,))
                if not message.author.bot and message.id not in answered:
                    if getattr(getattr(message.author,'guild_permissions',None),'administrator',False):
                        if message.content.split()[:1] == ['!entregar']:
                            await self.grant(message)
                    elif not manual and not message.content.startswith(('/', '!')):
                        suggestion = channel.name.startswith('sug-') or channel.category_id == ID_CATEGORIA_SUGERENCIAS
                        reply = None
                        mentions = discord.AllowedMentions.none()
                        sale = not suggestion and (message.attachments or requested_roles(message.content))
                        if sale:
                            retry = self.state.execute('SELECT attempts,retry_after FROM sale_retries WHERE message=?',(message.id,)).fetchone()
                            if retry and retry[1] > time.time():
                                return
                            try:
                                reply = await self.process_sale(message)
                            except (SaleReview,discord.Forbidden,discord.NotFound) as exc:
                                reason = str(exc) if isinstance(exc,SaleReview) else 'No pude verificar los permisos o la entrega en Discord.'
                                self.sales.review(message,reason)
                                reply = reason + ' ' + REVIEW
                            except (ReceiptProcessingUnavailable,ValueError,RuntimeError,OSError,discord.HTTPException) as exc:
                                attempts = retry[0]+1 if retry else 1
                                log.warning('Reintento de venta canal=%s mensaje=%s intento=%s error=%s',channel.id,message.id,attempts,type(exc).__name__)
                                if attempts < 3:
                                    self.state.execute('INSERT OR REPLACE INTO sale_retries VALUES (?,?,?)',
                                                       (message.id,attempts,time.time()+60*attempts))
                                    if attempts == 1:
                                        await channel.send('La lectura o la entrega está demorada. Voy a reintentar con el mismo comprobante; no vuelvas a pagar.')
                                    return
                                self.sales.review(message,'Verificación técnica pendiente: '+type(exc).__name__)
                                reply = 'Guardé el pedido para revisión. '+REVIEW
                            self.state.execute('DELETE FROM sale_retries WHERE message=?',(message.id,))
                        if reply is None and not suggestion and any(w in normalize(message.content) for w in ('pague','transferi','mi rango','mis rango','mi rol','comprobante','entrega')):
                            owns = self.state.execute('SELECT 1 FROM sale_orders WHERE channel=? AND buyer=?',(channel.id,message.author.id)).fetchone()
                            if owns:
                                reply = self.sales.summarize(channel.id) or None
                        if reply is None:
                            reply = answer(message,suggestion)
                        if REVIEW in reply and (message.attachments or sale):
                            reply += f' <@{ADMIN_CONTACT_ID}>'
                            mentions = discord.AllowedMentions(users=[discord.Object(id=ADMIN_CONTACT_ID)])
                        await message.reply(reply,mention_author=False,allowed_mentions=mentions)
                        log.info('Respuesta canal=%s mensaje=%s adjunto=%s',channel.id,message.id,bool(message.attachments))
                self.state.execute('UPDATE channels SET cursor=? WHERE id=?',(message.id,channel.id))
            row = self.state.execute('SELECT welcome FROM channels WHERE id=? AND manual=0 AND keep=0 AND activity=0 AND closed=0',(channel.id,)).fetchone()
            if row and row[0] and discord.utils.utcnow()-discord.utils.snowflake_time(row[0]) >= dt.timedelta(hours=3):
                permissions = channel.permissions_for(channel.guild.me)
                if permissions.read_message_history and permissions.manage_channels:
                    async for entry in channel.history(limit=None):
                        if not entry.author.bot or entry.attachments:
                            self.state.execute('UPDATE channels SET activity=1 WHERE id=?',(channel.id,))
                            return
                    await channel.delete(reason='Ticket temporal vacío tres horas después de la bienvenida')
                    self.state.execute('UPDATE channels SET closed=1 WHERE id=?',(channel.id,))
                    log.info('Cierre de ticket vacío canal=%s',channel.id)

    async def process_sale(self, message):
        channel = message.channel
        member = await message.guild.fetch_member(message.author.id)
        if member.bot or member.guild_permissions.administrator or channel.overwrites_for(member).view_channel is not True:
            raise SaleReview('No pude confirmar a la persona compradora en los permisos privados del ticket.')
        wanted = requested_roles(message.content)
        attachments = message.attachments
        if len(attachments) > 10 or sum(a.size for a in attachments) > 18 * 1024 * 1024:
            raise SaleReview('Los adjuntos superan el límite de lectura.')
        files, parts, digests = [], [], []
        for a in attachments:
            mime = attachment_mime(a)
            if not mime:
                raise SaleReview('Necesito una foto o PDF legible del comprobante.')
            data = await a.read()
            digest = hashlib.sha256(data).hexdigest()
            if digest in digests:
                continue
            recorded = self.state.execute('SELECT channel,buyer FROM sale_payments WHERE digest=?',(digest,)).fetchone()
            if recorded == (channel.id,member.id):
                continue
            if recorded or self.state.execute('SELECT 1 FROM receipt_files WHERE digest=?',(digest,)).fetchone():
                raise SaleReview('El archivo ya figura en otra operación.')
            digests.append(digest)
            parts.append({'mime_type':mime,'data':data})
        if parts:
            readings, issues, _ = await self.reader._read_receipts(parts)
            if len(readings) != len(parts) or issues:
                raise SaleReview('La lectura no confirmó todos los importes, destinos y estados del pago.')
            if any(r.paid_on is None or not r.operation_key for r in readings):
                local, _ = parse_receipts(await self.reader._read_local_receipts(parts),len(parts))
                local_by_index = {r.index:r for r in local}
                # Reemplazar la lectura completa solo si los dos lectores coinciden en el pago.
                for i, r in enumerate(readings):
                    alt = local_by_index.get(r.index)
                    if (alt and alt.paid_on and alt.operation_key and (not r.paid_on or not r.operation_key)
                            and (alt.amount,alt.currency,alt.method)==(r.amount,r.currency,r.method)
                            and (not r.operation_key or r.operation_key==alt.operation_key)
                            and (not r.paid_on or r.paid_on==alt.paid_on)):
                        readings[i] = alt
            files = [(digests[r.index],r) for r in readings]
        first = discord.utils.snowflake_time(max(channel.id,self.since)).astimezone(ARGENTINA).date()
        last = message.created_at.astimezone(ARGENTINA).date()
        self.sales.record(message,files,wanted,first,last)
        for delivery,buyer,names in self.sales.pending(channel.id):
            if buyer != member.id:
                raise SaleReview('El comprador no coincide con la entrega guardada.')
            roles = [message.guild.get_role(ROLES[name]['id']) for name in names.split(',')]
            if not all(roles) or any(r.managed or r.permissions.administrator or r >= message.guild.me.top_role for r in roles):
                raise SaleReview('La jerarquía de rangos requiere revisión.')
            member = await message.guild.fetch_member(buyer)
            missing = [r for r in roles if r not in member.roles]
            if missing:
                await member.add_roles(*missing,reason=f'Compra de contingencia {delivery}')
            member = await message.guild.fetch_member(buyer)
            if not all(r in member.roles for r in roles):
                raise RuntimeError('Discord todavía no confirmó la entrega')
            self.state.execute('UPDATE sale_deliveries SET done=1 WHERE id=?',(delivery,))
        return self.sales.summarize(channel.id) or ('Anoté **'+', '.join(wanted)+'**. '+AUTO if wanted else AUTO)

    async def grant(self, message):
        """Confirmación humana contra un comprobante del comprador en el mismo ticket."""
        parts = message.content.split(maxsplit=2)
        staff = message.author
        if staff.id not in (ADMIN_CONTACT_ID,message.guild.owner_id) or not staff.guild_permissions.administrator:
            return
        tokens = [x.strip().capitalize() for x in parts[2].split(',')] if len(parts)==3 else []
        if (len(parts) != 3 or not parts[1].isascii() or not parts[1].isdecimal()
                or len(parts[1]) > 19 or not tokens or any(x not in ROLES for x in tokens)):
            await message.reply(RANK_HELP,mention_author=False)
            return
        try:
            receipt = await message.channel.fetch_message(int(parts[1]))
        except discord.NotFound:
            await message.reply('No encontré ese mensaje en este ticket.',mention_author=False)
            return
        if receipt.author.bot or receipt.created_at > message.created_at or not any(attachment_mime(a) for a in receipt.attachments):
            await message.reply('Necesito el mensaje del comprador con la foto o PDF del comprobante.',mention_author=False)
            return
        try:
            member = await message.guild.fetch_member(receipt.author.id)
        except discord.NotFound:
            await message.reply('El comprador ya no está en el servidor.',mention_author=False)
            return
        if member.guild_permissions.administrator or message.channel.overwrites_for(member).view_channel is not True:
            await message.reply('No pude confirmar al comprador con los permisos privados del ticket.',mention_author=False)
            return
        names = ','.join(sorted(set(tokens)))
        roles = [message.guild.get_role(ROLES[name]['id']) for name in names.split(',')]
        if not all(roles) or any(role >= message.guild.me.top_role or role.managed or role.permissions.administrator for role in roles):
            await message.reply('Revisá la jerarquía y los permisos de los rangos.',mention_author=False)
            return
        attachments = [a for a in receipt.attachments if attachment_mime(a)]
        if sum(a.size for a in attachments) > 18 * 1024 * 1024:
            await message.reply('Los adjuntos superan 18 MB. Revisá el caso manualmente.',mention_author=False)
            return
        digests = {hashlib.sha256(await a.read()).hexdigest() for a in attachments}
        for digest in digests:
            if self.state.execute('SELECT 1 FROM sale_payments WHERE digest=?',(digest,)).fetchone():
                await message.reply('Ese pago ya está en el registro automático. Revisá el estado de esa venta.',mention_author=False)
                return
            row = self.state.execute('SELECT receipt FROM receipt_files WHERE digest=?',(digest,)).fetchone()
            if row and row[0] != receipt.id:
                await message.reply('Ese archivo ya se usó en otra entrega local.',mention_author=False)
                return
        self.state.execute('BEGIN IMMEDIATE')
        try:
            self.state.execute('INSERT OR IGNORE INTO grants(receipt,channel,buyer,staff,roles) VALUES (?,?,?,?,?)',
                               (receipt.id,message.channel.id,member.id,staff.id,names))
            self.state.executemany('INSERT OR IGNORE INTO receipt_files VALUES (?,?)',[(d,receipt.id) for d in digests])
            self.state.commit()
        except Exception:
            self.state.rollback()
            raise
        saved = self.state.execute('SELECT channel,buyer,roles,done FROM grants WHERE receipt=?',(receipt.id,)).fetchone()
        if saved[:3] != (message.channel.id,member.id,names) or saved[3]:
            await message.reply('Ese comprobante ya tiene una entrega registrada. Revisá el registro antes de hacer otra.',mention_author=False)
            return
        missing = [role for role in roles if role not in member.roles]
        if missing:
            try:
                await member.add_roles(*missing,reason=f'Pago verificado manualmente por {staff.id}, comprobante {receipt.id}')
            except discord.Forbidden:
                await message.reply('Discord rechazó la asignación. La entrega quedó pendiente. Revisá los permisos del bot antes de repetir el comando.',mention_author=False)
                return
        member = await message.guild.fetch_member(member.id)
        if not all(role in member.roles for role in roles):
            raise RuntimeError('La entrega sigue pendiente de confirmación de Discord')
        self.state.execute('UPDATE grants SET done=1 WHERE receipt=?',(receipt.id,))
        await message.reply(f'Entrega verificada para <@{member.id}>: {", ".join(names.split(","))}.',mention_author=False)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--state',type=Path,required=True)
    parser.add_argument('--since',type=dt.datetime.fromisoformat,required=True)
    parser.add_argument('--guild',type=int,required=True)
    parser.add_argument('--bot',type=int,required=True)
    parser.add_argument('--until',type=dt.datetime.fromisoformat,help='Fin anticipado opcional de la atención local')
    parser.add_argument('--log',type=Path)
    args = parser.parse_args()
    if args.since.tzinfo is None or (args.until and (args.until.tzinfo is None or args.until <= args.since)):
        parser.error('El intervalo requiere fechas ordenadas con zona horaria')
    handlers = [RotatingFileHandler(args.log,maxBytes=2_000_000,backupCount=2,encoding='utf-8')] if args.log else None
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(name)s %(message)s',handlers=handlers)
    load_dotenv(Path(__file__).with_name('.env'))
    async def serve():
        dsn = os.environ['DATABASE_URL']
        if args.state.is_file():
            db = sqlite3.connect(args.state)
            try:
                cutover = db.execute("SELECT 1 FROM sqlite_master WHERE name='sale_cutover'").fetchone()
                cutover = cutover and db.execute('SELECT 1 FROM sale_cutover').fetchone()
            finally:
                db.close()
            if cutover:
                await hand_back(args.state,args.guild,dsn)
                return
        guard = QuotaGuard(dsn)
        quota_blocked = False
        while args.until is None or discord.utils.utcnow() < args.until:
            try:
                quota_blocked = await guard.blocked()
                break
            except (OSError,asyncpg.PostgresConnectionError):
                log.info('Esperando conexión luego del inicio de Windows')
                await asyncio.sleep(60)
        if quota_blocked:
            async with LocalTickets(args.state,discord.utils.time_snowflake(args.since),args.guild,args.bot,guard) as client:
                bind_database(client.state,dsn,initialize=True)
                try:
                    remaining = (args.until-discord.utils.utcnow()).total_seconds() if args.until else None
                    async with asyncio.timeout(remaining):
                        await client.start(os.environ['DISCORD_TOKEN'])
                except TimeoutError:
                    log.info('Fin de la ventana temporal')
                finally:
                    client.stopping = True
                    client.reconcile.stop()
                    await client.close()
                    async with client.lock:
                        client.reconcile.cancel()
                        client.state.close()
        await hand_back(args.state,args.guild,dsn)

    with socket.socket() as single_instance:
        single_instance.bind(('127.0.0.1',49837))
        try:
            asyncio.run(serve())
        except Exception as exc:
            log.error('No se pudo mantener la atención local: %s',type(exc).__name__)
            raise


def bind_database(db, dsn, *, initialize=False):
    target = urlsplit(dsn)
    identity = (target.hostname or '').replace('-pooler.', '.') + ':' + str(target.port or 5432) + target.path
    fingerprint = hashlib.sha256(identity.encode()).hexdigest()
    db.execute('CREATE TABLE IF NOT EXISTS sale_target(id INTEGER PRIMARY KEY CHECK(id=1), fingerprint TEXT NOT NULL)')
    if initialize:
        db.execute('INSERT OR IGNORE INTO sale_target VALUES (1,?)',(fingerprint,))
    if db.execute('SELECT fingerprint FROM sale_target').fetchone() != (fingerprint,):
        raise RuntimeError('El destino de la importación no coincide con la base original')


async def hand_back(path, guild, dsn):
    if not path.is_file():
        raise FileNotFoundError('No se encontró el registro local de la contingencia')
    db = sqlite3.connect(path,isolation_level=None)
    try:
        bind_database(db,dsn)
        LocalSales(db)
        db.execute('CREATE TABLE IF NOT EXISTS sale_cutover (id INTEGER PRIMARY KEY CHECK(id=1))')
        db.execute('INSERT OR IGNORE INTO sale_cutover VALUES (1)')
        db.execute('CREATE TABLE IF NOT EXISTS sale_handoff (id INTEGER PRIMARY KEY CHECK(id=1), completed TEXT NOT NULL)')
        if db.execute('SELECT 1 FROM sale_handoff').fetchone():
            return
        while True:
            try:
                conn = await asyncpg.connect(dsn,timeout=15,command_timeout=45)
            except (OSError,asyncpg.PostgresConnectionError,asyncpg.InsufficientResourcesError) as exc:
                log.info('Importación pendiente de conexión a Neon: %s',type(exc).__name__)
                await asyncio.sleep(600)
                continue
            try:
                count = await import_sales(db,conn,guild)
            except (OSError,asyncpg.PostgresConnectionError,asyncpg.InsufficientResourcesError) as exc:
                log.warning('Importación interrumpida, se retomará: %s',type(exc).__name__)
                count = None
            finally:
                await conn.close()
            if count is None:
                await asyncio.sleep(120)
                continue
            conflicts = db.execute('SELECT COUNT(*) FROM sale_import_conflicts').fetchone()[0]
            if conflicts:
                log.error('Importación parcial. Tickets con datos distintos en Neon: %s. Se conserva el libro local.',conflicts)
            else:
                db.execute('INSERT INTO sale_handoff VALUES (1,?)',(discord.utils.utcnow().isoformat(),))
                log.info('Importación a Neon verificada. Ventas copiadas en esta ejecución: %s',count)
            return
    finally:
        db.close()


if __name__ == '__main__':
    main()
