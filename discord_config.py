"""Identificadores de Discord compartidos por el asistente de Chaquetas."""

ADMIN_CONTACT_ID = 1221274084718018562
LOCAL_TICKET_MARK = '[Chaquetas: revisión local]'


def is_local_ticket(channel):
    return LOCAL_TICKET_MARK in (getattr(channel, 'topic', None) or '').splitlines()
