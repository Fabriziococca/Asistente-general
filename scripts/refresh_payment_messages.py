"""Retira instrucciones de cobro antiguas y datos personales de mensajes propios.

Por defecto solo inspecciona. --apply guarda copia previa y edita mensajes del bot.
Los mensajes de otras personas se informan para edición con la cuenta propietaria.
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))

import requests
from dotenv import dotenv_values

from payment_policy import (payment_copy, announcement_text, BANK_HOLDER, PAYPAL_EMAIL, campaign_active,
                            BINANCE_RECIPIENT_ALIASES, obsolete_payment_reference,
                            private_identity_reference, public_message_needs_refresh)
from ticket_messages import ticket_welcome, welcome_needs_refresh


def redacted(text):
    retired = r'\bbinance(?:\s+pay)?\b|552346130' if campaign_active() else r'\bpay[\W_]*pal\b'
    text=re.sub(retired,'medio anterior (retirado)',str(text),flags=re.I)
    text=re.sub(r'[\w.+-]+@[\w.-]+\.[a-z]{2,}',
        lambda match: match[0] if campaign_active() and match[0].casefold()==PAYPAL_EMAIL
        else '[dato de cobro retirado]',text,flags=re.I)
    text=re.sub(r'@(everyone|here)\b','',text,flags=re.I)
    if private_identity_reference(text):
        names=[BANK_HOLDER, BANK_HOLDER.split()[0]+' '+BANK_HOLDER.split()[-2],
               *BANK_HOLDER.split(), *BINANCE_RECIPIENT_ALIASES]
        for name in names:
            text=re.sub(r'\b'+r'[\W_]+'.join(map(re.escape,name.split()))+r'\b',
                        '[dato personal omitido]',text,flags=re.I)
    return text


def replacement(message,suggestion=False):
    methods, details, prices, _ = payment_copy()
    embeds=message.get('embeds',[])
    if any('Zona de Compras' in (e.get('title') or '') or 'Petición Única' in (e.get('title') or '') for e in embeds):
        content,embed=ticket_welcome(suggestion)
        return {'content':content,'embeds':[embed.to_dict()]}
    content=message.get('content','')
    if obsolete_payment_reference(content):
        if re.search(r'\b(registr\w*|entreg\w*|comprobante)\b',content,re.I):
            content=redacted(content)+'\n\n'+methods
        else:
            content=methods+'\n\n'+details
    elif private_identity_reference(content):
        content=redacted(content)
    updated=[]
    for original in embeds:
        embed={k:v for k,v in original.items() if k not in {'provider','video'}}
        for key in ('title','description'):
            if public_message_needs_refresh(embed.get(key,'')):
                embed[key]=redacted(embed[key])
        fields=[]
        for field in embed.get('fields',[]):
            if public_message_needs_refresh(field.get('name','')+' '+field.get('value','')):
                fields.append({'name':'Medios de cobro vigentes','value':details,'inline':False})
            else:
                fields.append(field)
        if fields:
            embed['fields']=fields
        if public_message_needs_refresh(embed.get('footer',{}).get('text','')):
            embed['footer']={'text':'Información de cobro actualizada. Consultá los medios y precios vigentes en el ticket.'}
        for key in ('url','image','thumbnail','author'):
            if public_message_needs_refresh(json.dumps(embed.get(key,''))):
                embed.pop(key,None)
        updated.append(embed)
    return {'content':content,'embeds':updated}


def needs_refresh(message, suggestion=False):
    """Detecta también bienvenidas con precios anteriores, sin reescribir ventas."""
    body = message.get('content','') + json.dumps(message.get('embeds',[]),ensure_ascii=False)
    if public_message_needs_refresh(body):
        return True
    if any('Zona de Compras' in (e.get('title') or '') or 'Petición Única' in (e.get('title') or '')
           for e in message.get('embeds',[])):
        return welcome_needs_refresh(message.get('content',''), message.get('embeds',[]), suggestion)
    return False


def owner_replacement(content):
    # Solo propuesta para copiar manualmente; el bot no edita mensajes ajenos.
    if 'ADQUIRIR RANGOS' in content or re.search(r'Rango\s+<@&\d+>',content,re.I):
        tail = re.search(r'[^\n]*(?:DEMOSTRACIÓN|DEMOSTRACION|GUÍAS|GUIAS)[\s\S]*',content)
        return announcement_text() + ('\n\n' + redacted(tail[0]) if tail else '')
    retired = r'\bbinance(?:\s+pay)?(?:\s*\(?USDT\)?)?\b' if campaign_active() else r'\bpay[\W_]*pal\b'
    current = 'PayPal en USD (promo de rangos por 7 días; consultá el ticket)' if campaign_active() else 'Binance Pay en USDT'
    return redacted(re.sub(retired,current,content,flags=re.I))


def validate_replacement(payload):
    if public_message_needs_refresh(json.dumps(payload,ensure_ascii=False)):
        raise ValueError('La sustitución contiene un medio retirado o datos privados')
    if len(payload.get('content','')) > 2000:
        raise ValueError('La sustitución supera el límite de contenido de Discord')
    total=0
    for embed in payload.get('embeds',[]):
        for key,limit in (('title',256),('description',4096)):
            value=embed.get(key,'')
            if len(value)>limit:
                raise ValueError('La sustitución supera un límite de embed')
            total+=len(value)
        for field in embed.get('fields',[]):
            if len(field.get('name',''))>256 or len(field.get('value',''))>1024:
                raise ValueError('La sustitución supera un límite de campo')
            total+=len(field.get('name',''))+len(field.get('value',''))
        total+=len(embed.get('footer',{}).get('text',''))+len(embed.get('author',{}).get('name',''))
    if total>6000 or re.search(r'@(everyone|here)\b',json.dumps(payload),re.I):
        raise ValueError('La sustitución supera límites o contiene menciones generales')


class DiscordAPI:
    def __init__(self,token):
        self.session=requests.Session()
        self.session.headers['Authorization']='Bot '+token

    def request(self,method,path,**kwargs):
        for attempt in range(6):
            response=self.session.request(method,'https://discord.com/api/v10'+path,timeout=25,**kwargs)
            if response.status_code==429:
                time.sleep(min(float(response.json().get('retry_after',2)),10))
                continue
            if response.status_code>=500 and attempt<5:
                time.sleep(2)
                continue
            if not response.ok:
                raise RuntimeError(f'Discord {response.status_code} en {path}')
            return response.json()
        raise RuntimeError('Discord no permitió completar la solicitud tras varios intentos')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply',action='store_true')
    parser.add_argument('--all-channels',action='store_true',help='Inspeccionar todos los canales de texto y anuncios accesibles')
    parser.add_argument('--guild',default='1423388693082935420')
    args=parser.parse_args()
    api=DiscordAPI(dotenv_values(ROOT/'.env')['DISCORD_TOKEN'])
    identity=api.request('GET','/users/@me')
    if identity['id']!='1444824969480769718':
        raise RuntimeError('La credencial no corresponde al asistente esperado')
    channels=api.request('GET',f'/guilds/{args.guild}/channels')
    selected=[c for c in channels if c['type'] in (0,5) and (
        args.all_channels or re.search(r'^(ticket-|sug-)|compr|rang|informaci[oó]n|sugeren|promo|venta',c['name'],re.I)
        or c['id']=='1503937966748205056')]
    directory=ROOT.parent/'.local'/'operations'/('payment-policy-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
    directory.mkdir(parents=True,exist_ok=False)
    own,manual,errors,truncated=[],[],[],[]
    scanned=0
    for channel in selected:
        before=None
        for page in range(10):
            params={'limit':100}
            if before:
                params['before']=before
            try:
                messages=api.request('GET',f"/channels/{channel['id']}/messages",params=params)
            except RuntimeError as exc:
                errors.append({'channel_id':channel['id'],'error':str(exc)})
                break
            scanned+=len(messages)
            for message in messages:
                body=message.get('content','')+json.dumps(message.get('embeds',[]),ensure_ascii=False)
                suggestion=channel['name'].startswith('sug-') or channel.get('parent_id')=='1510095191275737088'
                if not needs_refresh(message,suggestion):
                    continue
                record={'channel_id':channel['id'],'message':message,
                        'suggestion':suggestion,
                        'url':f"https://discord.com/channels/{args.guild}/{channel['id']}/{message['id']}"}
                if message['author']['id']==identity['id']:
                    record['replacement']=replacement(message,channel['name'].startswith('sug-') or channel.get('parent_id')=='1510095191275737088')
                    own.append(record)
                elif message['author']['id']=='704501115110162542':
                    record['replacement_content']=owner_replacement(message.get('content',''))
                    manual.append(record)
            if len(messages)<100:
                break
            before=messages[-1]['id']
            if page==9:
                truncated.append(channel['id'])
    # La copia de recuperación nunca se guarda en Git ni se envía a otra plataforma.
    (directory/'before.json').write_text(json.dumps({'bot':own,'owner':manual},ensure_ascii=False,indent=2),encoding='utf-8')
    for record in own:
        validate_replacement(record['replacement'])
    results=[]
    if args.apply:
        for record in own:
            path=f"/channels/{record['channel_id']}/messages/{record['message']['id']}"
            current=api.request('GET',path)
            if current.get('content')!=record['message'].get('content') or current.get('embeds')!=record['message'].get('embeds'):
                if not needs_refresh(current,record.get('suggestion',False)):
                    continue
                raise RuntimeError('El mensaje cambió desde la inspección; no se sobrescribe')
            payload=record['replacement']
            validate_replacement(payload)
            api.request('PATCH',path,json={**payload,'allowed_mentions':{'parse':[]}})
            after=api.request('GET',path)
            if public_message_needs_refresh(after.get('content','')+json.dumps(after.get('embeds',[]))):
                raise RuntimeError('No se pudo verificar la actualización del mensaje')
            results.append(record['url'])
    report={'apply':args.apply,'channels':len(selected),'messages_scanned':scanned,'bot_candidates':len(own),
            'edited':results,'owner_messages':[{'url':r['url'],'replacement_content':r['replacement_content']} for r in manual],
            'errors':errors,'truncated_channels':truncated,'backup':str(directory/'before.json')}
    (directory/'result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=True),flush=True)
    return 1 if errors or truncated else 0


if __name__=='__main__':
    raise SystemExit(main())
