"""Ventas de contingencia en SQLite. El libro conserva importes en centavos."""

from contextlib import contextmanager
from decimal import Decimal
import hashlib
import re
from uuid import uuid4

from payment_policy import current_payment
from receipts import PRICES, choose_roles, split_purchase


SCHEMA = """
CREATE TABLE IF NOT EXISTS sale_orders(
    channel INTEGER PRIMARY KEY, buyer INTEGER NOT NULL, wanted TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS sale_deliveries(
    id TEXT PRIMARY KEY, channel INTEGER NOT NULL, buyer INTEGER NOT NULL, roles TEXT NOT NULL,
    done INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS sale_payments(
    digest TEXT PRIMARY KEY, operation_key TEXT NOT NULL UNIQUE, operation_id TEXT NOT NULL UNIQUE,
    message INTEGER NOT NULL, channel INTEGER NOT NULL, buyer INTEGER NOT NULL, guild INTEGER NOT NULL,
    cents INTEGER NOT NULL CHECK(cents>0), currency TEXT NOT NULL, method TEXT NOT NULL,
    paid_on TEXT NOT NULL, booked_at TEXT NOT NULL, delivery TEXT REFERENCES sale_deliveries(id),
    imported INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS sale_reviews(
    message INTEGER PRIMARY KEY, channel INTEGER NOT NULL, reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sale_retries(
    message INTEGER PRIMARY KEY, attempts INTEGER NOT NULL, retry_after REAL NOT NULL);
"""


class SaleReview(ValueError):
    """La evidencia o el estado necesitan una decisión humana."""


@contextmanager
def transaction(db):
    db.execute('BEGIN IMMEDIATE')
    try:
        yield
        db.commit()
    except BaseException:
        db.rollback()
        raise


def operation_id(receipt):
    value = re.sub(r'[^A-Z0-9]', '', (receipt.operation or '').upper())
    if len(value) < 6 or not any(c.isdigit() for c in value):
        raise SaleReview('No se distingue el número de operación.')
    return hashlib.sha256(value.encode()).hexdigest()


class LocalSales:
    def __init__(self, db):
        self.db = db
        db.executescript(SCHEMA)

    def review(self, message, reason):
        self.db.execute('INSERT INTO sale_reviews VALUES (?,?,?) ON CONFLICT(message) DO UPDATE SET reason=excluded.reason',
                        (message.id,message.channel.id,reason))

    def record(self, message, files, wanted, first_day, last_day):
        """Guarda el lote completo y reserva sus entregas antes de tocar Discord."""
        channel, buyer = message.channel.id, message.author.id
        with transaction(self.db):
            self.db.execute('INSERT OR IGNORE INTO sale_orders(channel,buyer) VALUES (?,?)',(channel,buyer))
            owner, previous = self.db.execute('SELECT buyer,wanted FROM sale_orders WHERE channel=?',(channel,)).fetchone()
            if owner != buyer:
                raise SaleReview('Este ticket ya tiene otra persona asociada a la compra.')
            for digest, receipt in files:
                if not current_payment(receipt.currency,receipt.method) or not 0 < receipt.amount <= Decimal('99999999.99'):
                    raise SaleReview('El importe o el medio de pago requiere revisión.')
                if receipt.paid_on is None or not first_day <= receipt.paid_on <= last_day:
                    raise SaleReview('No pude confirmar que la fecha del pago corresponda a esta compra nueva.')
                op = operation_id(receipt)
                if self.db.execute('SELECT 1 FROM receipt_files WHERE digest=?',(digest,)).fetchone():
                    raise SaleReview('Ese archivo ya tiene una entrega de administración.')
                known = self.db.execute("""SELECT channel,buyer,cents,currency,operation_id FROM sale_payments
                    WHERE digest=? OR operation_id=? OR operation_key=?""",(digest,op,receipt.operation_key)).fetchall()
                cents = int(receipt.amount * 100)
                if known:
                    if known != [(channel,buyer,cents,receipt.currency,op)]:
                        raise SaleReview('La operación ya figura en otra compra o tiene datos distintos.')
                    continue
                self.db.execute("""INSERT INTO sale_payments(digest,operation_key,operation_id,message,channel,buyer,
                    guild,cents,currency,method,paid_on,booked_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (digest,receipt.operation_key,op,message.id,channel,buyer,message.guild.id,cents,receipt.currency,
                     receipt.method,receipt.paid_on.isoformat(),message.created_at.isoformat()))
            assigned = {r for (roles,) in self.db.execute('SELECT roles FROM sale_deliveries WHERE channel=?',(channel,))
                        for r in roles.split(',')}
            desired = tuple(r for r in PRICES if r in (set(previous.split(',')) | set(wanted)) and r not in assigned)
            self.db.execute('UPDATE sale_orders SET wanted=? WHERE channel=?',(','.join(desired),channel))
            totals = {currency:Decimal(cents)/100 for currency,cents in self.db.execute(
                'SELECT currency,SUM(cents) FROM sale_payments WHERE channel=? AND delivery IS NULL GROUP BY currency',
                (channel,))}
            plan = split_purchase(totals,desired)
            for currency,total in totals.items():
                roles = plan.get(currency) if plan else choose_roles(total,currency,desired)[0]
                if not roles or assigned.intersection(roles):
                    continue
                delivery = 'local-'+str(uuid4())
                self.db.execute('INSERT INTO sale_deliveries(id,channel,buyer,roles) VALUES (?,?,?,?)',
                                (delivery,channel,buyer,','.join(roles)))
                self.db.execute('UPDATE sale_payments SET delivery=? WHERE channel=? AND currency=? AND delivery IS NULL',
                                (delivery,channel,currency))
                assigned.update(roles)

    def pending(self, channel):
        return self.db.execute('SELECT id,buyer,roles FROM sale_deliveries WHERE channel=? AND done=0',(channel,)).fetchall()

    def summarize(self, channel):
        delivered = {r for (names,) in self.db.execute(
            'SELECT roles FROM sale_deliveries WHERE channel=? AND done=1',(channel,)) for r in names.split(',')}
        balance = [(cur, Decimal(cents)/100) for cur,cents in self.db.execute(
            'SELECT currency,SUM(cents) FROM sale_payments WHERE channel=? AND delivery IS NULL GROUP BY currency',(channel,))]
        parts = []
        if delivered:
            parts.append('Rangos confirmados en Discord: **'+', '.join(r for r in PRICES if r in delivered)+'**.')
        if balance:
            parts.append('Saldo registrado: '+ ' + '.join(f'{v:.2f} {c}' for c,v in balance)+
                         '. Si ya enviaste todos los pagos, indicá qué rangos querés; no vuelvas a pagar.')
        return ' '.join(parts)
