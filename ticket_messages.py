"""Bienvenidas con los medios de cobro vigentes, reutilizables en tickets existentes."""

import discord

from payment_policy import payment_copy


def welcome_needs_refresh(content, embeds, suggestion=False):
    expected_content, expected_embed = ticket_welcome(suggestion)
    keys = ('title','description','color','fields','footer')
    actual = [{k:e[k] for k in keys if k in e} for e in embeds]
    expected = expected_embed.to_dict()
    return content != expected_content or actual != [{k:expected[k] for k in keys if k in expected}]


def ticket_welcome(suggestion=False):
    methods, details, prices, suggestion_prices = payment_copy()
    if suggestion:
        embed = discord.Embed(title='💎 Petición Única de Modelos (Pre-Compra)',
            description='Enviá la modelo y la plataforma que querés consultar.', color=0x1DB954)
        embed.add_field(name='📋 PROCESO DE PRE-COMPRA', value=(
            '1. **Enviá la plataforma de contenido y su red social principal**.\n'
            '2. **Aguardá a que administración confirme** la disponibilidad en este chat.\n'
            f'3. **Una vez confirmado**, realizá el pago ({suggestion_prices}) y subí el comprobante. '
            'El megadescuento de rangos no aplica a peticiones.'), inline=False)
        embed.add_field(name='💰 DATOS DE PAGO (Pagar solo tras confirmación)',
            value=methods + '\n\n' + details, inline=False)
        embed.add_field(name='⚠️ COMPROMISO DE COMPRA (100%)', value=(
            'Iniciar la búsqueda implica un compromiso de pago obligatorio si confirmamos disponibilidad. '
            'Si te confirmamos que está disponible y decidís no pagar, se te aplicará una advertencia o baneo por abuso de soporte.'), inline=False)
        embed.add_field(name='🎁 CANJEAR POR BUMPS (30 Bumps)',
            value="Si tenés 30 Bumps acumulados, escribí: **'Quiero canjear mis puntos'** y aguardá la validación.", inline=False)
        content='¡Hola! Soy tu asistente automatizado para peticiones exclusivas. 🤖\nContinuamos tu solicitud por este ticket privado.'
    else:
        embed=discord.Embed(title='🛒 Zona de Compras',
            description='Elegí tu rango y mirá los datos de pago abajo.',color=0x2B2D31)
        embed.add_field(name='📉 LISTA DE PRECIOS',value=prices,inline=False)
        embed.add_field(name='💰 MEDIOS Y DATOS DE PAGO',value=methods+'\n\n'+details,inline=False)
        embed.add_field(name='✅ ¿Ya pagaste? Seguí estos pasos:',value=(
            '1. Aclará qué rango o rangos estás comprando.\n'
            '2. Enviá la FOTO o PDF del comprobante acá al chat. Podés adjuntar varios pagos en un mensaje.\n'
            '3. El bot revisa los comprobantes y entrega los rangos. Si hay un problema técnico, avisamos a administración.\n\n'
            '❓ Si tenés dudas o problemas, etiquetá a @titocalderon para recibir ayuda.\n\n'
            'Podés enviar el total de varios rangos juntos. No hay conexión bancaria: el bot necesita el comprobante.'),inline=False)
        content='¡Hola! Soy tu asistente de ventas automatizado. 🤖\nEstoy acá para ayudarte a obtener tus rangos.'
    return content,embed
