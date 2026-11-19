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

import asyncpg
import discord
from discord.ext import tasks
from dotenv import load_dotenv

from communication import local_answer
from discord_config import ADMIN_CONTACT_ID, LOCAL_TICKET_MARK, is_local_ticket
from payment_policy import payment_copy
from receipts import attachment_mime, normalize, requested_roles
from cogs.tickets import ID_CATEGORIA_SUGERENCIAS, ROLES


log = logging.getLogger('contingencia')
WELCOME = 'Atención temporal de Chaquetas'
RANK_HELP = 'Después de verificar la acreditación, administración puede usar `!entregar ID_DEL_MENSAJE Oro` en este ticket. Para varios rangos, separalos por comas.'
REVIEW = 'Administración revisa el comprobante y entrega los rangos desde este ticket. Si ya pagaste, no vuelvas a pagar.'


def answer(message, suggestion):
    text = normalize(message.content)
    if message.attachments or any(word in text for word in ('comprobante', 'transferi', 'pague', 'entrega', 'demora', 'tarda', 'mi rango', 'mis rango', 'mi rol', 'mis rol', 'canjear')):
        return REVIEW
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
        log.info('Discord listo bot=%s guild=%s modo=temporal', self.user.id, guild.id)

    @tasks.loop(minutes=2)
    async def reconcile(self):
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
        if interaction.guild_id != self.guild_id or interaction.type != discord.InteractionType.application_command:
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
                    embed = discord.Embed(title=WELCOME,description=body+'\n\n'+details+'\n\n'+REVIEW)
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
                        reply = answer(message,suggestion)
                        mentions = discord.AllowedMentions.none()
                        if message.attachments:
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
    parser.add_argument('--until',type=dt.datetime.fromisoformat,required=True)
    parser.add_argument('--log',type=Path)
    args = parser.parse_args()
    if args.since.tzinfo is None or args.until.tzinfo is None or args.until <= args.since:
        parser.error('El intervalo requiere fechas ordenadas con zona horaria')
    handlers = [RotatingFileHandler(args.log,maxBytes=2_000_000,backupCount=2,encoding='utf-8')] if args.log else None
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(name)s %(message)s',handlers=handlers)
    load_dotenv(Path(__file__).with_name('.env'))
    async def serve():
        guard = QuotaGuard(os.environ['DATABASE_URL'])
        if not await guard.blocked():
            log.info('Neon disponible. No se inicia atención temporal.')
            return
        async with LocalTickets(args.state,discord.utils.time_snowflake(args.since),args.guild,args.bot,guard) as client:
            try:
                async with asyncio.timeout((args.until-discord.utils.utcnow()).total_seconds()):
                    await client.start(os.environ['DISCORD_TOKEN'])
            except TimeoutError:
                log.info('Fin de la ventana temporal')
            finally:
                client.reconcile.cancel()
                client.state.close()

    if discord.utils.utcnow() < args.until:
        with socket.socket() as single_instance:
            single_instance.bind(('127.0.0.1',49837))
            try:
                asyncio.run(serve())
            except Exception as exc:
                log.error('No se pudo mantener la atención local: %s',type(exc).__name__)
                raise


if __name__ == '__main__':
    main()
