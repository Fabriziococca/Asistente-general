"""Totales de importes registrados por moneda, sin conversiones ni comisiones supuestas."""

from decimal import Decimal


CURRENCY_LABELS={'ARS':'Pesos (ARS)','USDT':'USDT · Binance Pay','USD':'USD históricos'}


def financial_totals(rows):
    totals={}
    for row in rows:
        currency=str(row['moneda'] or 'ARS').upper()
        group=totals.setdefault(currency,{'amount':Decimal(0),'count':0,'concepts':{}})
        quantity=int(row['cantidad'])
        amount=Decimal(str(row['monto']))*quantity
        group['amount']+=amount
        group['count']+=quantity
        concept=row['concepto'] or 'No especificado'
        detail=group['concepts'].setdefault(concept,{'amount':Decimal(0),'count':0})
        detail['amount']+=amount
        detail['count']+=quantity
    return totals
