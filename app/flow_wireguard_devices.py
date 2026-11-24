"""Device-scoped WireGuard feeds. Never expose the parent or another device key."""
import hashlib
import json
import re
import secrets
import time

from fastapi import HTTPException

from . import config as c
from .db import db
from .security import digest


def _owned_locked(conn, user_id, device_id):
    row = conn.execute(
        """SELECT d.*,s.protocol,s.state AS parent_state,s.expires AS parent_expires,
                  s.access_source
           FROM devices d JOIN subscriptions s ON s.id=d.subscription_id
           WHERE d.id=? AND s.user_id=?""", (device_id, user_id),
    ).fetchone()
    if not row or row['protocol']!='wg' or row['access_source']=='trial':
        raise HTTPException(404, 'WireGuard-устройство не найдено')
    return row


def _state(row, now):
    if row['state']=='revoked' or row['parent_state']=='revoked':
        return 'revoked'
    if row['state'] in ('pending','rotating','revoking') or row['parent_state']=='pending':
        # A queued remote operation is not confirmation of revocation.
        raise HTTPException(409, 'Устройство ещё не готово')
    if (row['state']!='active' or row['parent_state']!='active'
            or min(row['expires'],row['parent_expires'])<=now):
        return 'expired'
    return 'active'


def _config(row):
    if not row['secret']:
        raise HTTPException(409, 'Устройство ещё не готово')
    try:
        value = c.decrypt(row['secret'])
    except Exception:
        raise HTTPException(503, 'Настройки временно недоступны') from None
    if (not isinstance(value,str) or len(value)>65536 or not value.startswith('[Interface]')
            or '[Peer]' not in value):
        raise HTTPException(503, 'Настройки временно недоступны')
    return value


def _feed_locked(conn, user_id, device_id):
    if not conn.in_transaction:
        raise RuntimeError('Device feed requires a transaction')
    row = _owned_locked(conn,user_id,device_id)
    state = _state(row,int(time.time()))
    expiry = max(1,min(int(row['expires']),int(row['parent_expires'])))
    profiles = []
    if state=='active':
        profiles = [{'id':row['id'],'name':'Pushok WireGuard',
                     'protocol':'wireguard','config':_config(row)}]
    fingerprint = hashlib.sha256(json.dumps([state,expiry,profiles],ensure_ascii=False,
                                    separators=(',',':')).encode('utf-8')).hexdigest()
    ledger = conn.execute(
        'SELECT fingerprint,revision FROM flow_wireguard_device_versions WHERE device_id=?',
        (device_id,)).fetchone()
    revision = ledger['revision'] if ledger else 0
    if not ledger or ledger['fingerprint']!=fingerprint:
        revision += 1
        if revision>9007199254740991:
            raise HTTPException(503,'Версия подписки недоступна')
        conn.execute("""INSERT INTO flow_wireguard_device_versions(device_id,fingerprint,revision)
            VALUES (?,?,?) ON CONFLICT(device_id) DO UPDATE SET
            fingerprint=excluded.fingerprint,revision=excluded.revision""",
            (device_id,fingerprint,revision))
    return {'schema':'pushok-flow-subscription','version':1,'revision':revision,
            'state':state,'expires_at':expiry,'profiles':profiles}


def feed_for_user(user_id, device_id):
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        return _feed_locked(conn,user_id,device_id)


def _metadata(row):
    if not row:
        return {'enabled':False,'version':0,'url':None}
    if not row['enabled']:
        return {'enabled':False,'version':row['version'],'url':None}
    try:
        raw=c.decrypt(row['token_secret'])
        if not re.fullmatch(r'[A-Za-z0-9_-]{43}',raw) or digest(raw)!=row['token_hash']:
            raise ValueError('Invalid link')
    except Exception:
        raise HTTPException(503,'Ссылка временно недоступна') from None
    return {'enabled':True,'version':row['version'],
            'url':c.PUBLIC_URL.rstrip('/')+'/flow/wireguard/device/'+raw}


def _link_row(conn, device_id):
    return conn.execute('SELECT * FROM flow_wireguard_device_links WHERE device_id=?',
                        (device_id,)).fetchone()


def link_for_user(user_id,device_id):
    with db() as conn:
        conn.execute('BEGIN')
        _owned_locked(conn,user_id,device_id)
        return _metadata(_link_row(conn,device_id))


def invalidate_locked(conn,device_id):
    """Disable old URL atomically before starting key rotation."""
    conn.execute("""UPDATE flow_wireguard_device_links
        SET enabled=0,token_hash=NULL,token_secret=NULL,version=version+1
        WHERE device_id=? AND enabled=1""",(device_id,))


def change_link(user_id,device_id,action,expected_version):
    if action not in ('enable','rotate','disable'):
        raise HTTPException(422,'Неизвестное действие')
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        device=_owned_locked(conn,user_id,device_id)
        row=_link_row(conn,device_id)
        version=row['version'] if row else 0
        if action!='disable':
            if _state(device,int(time.time()))!='active':
                raise HTTPException(409,'Нужен действующий профиль устройства')
            _config(device)
        if action=='enable' and row and row['enabled']:
            # A lost successful response can be retrieved without creating another token.
            if expected_version not in (version,version-1):
                raise HTTPException(409,'Ссылка изменилась. Обновите кабинет.')
            return _metadata(row)
        if expected_version!=version:
            raise HTTPException(409,'Ссылка изменилась. Обновите кабинет.')
        if action=='disable':
            invalidate_locked(conn,device_id)
        else:
            if action=='rotate' and (not row or not row['enabled']):
                raise HTTPException(409,'Сначала создайте ссылку')
            raw=secrets.token_urlsafe(32)
            conn.execute("""INSERT INTO flow_wireguard_device_links
                (device_id,token_hash,token_secret,enabled,version) VALUES (?,?,?,1,?)
                ON CONFLICT(device_id) DO UPDATE SET token_hash=excluded.token_hash,
                token_secret=excluded.token_secret,enabled=1,version=excluded.version""",
                (device_id,digest(raw),c.encrypt(raw),version+1))
        return _metadata(_link_row(conn,device_id))


def feed_for_link(token):
    if not isinstance(token,str) or not re.fullmatch(r'[A-Za-z0-9_-]{43}',token):
        raise HTTPException(404,'Ссылка не найдена')
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        row=conn.execute("""SELECT l.device_id,s.user_id FROM flow_wireguard_device_links l
            JOIN devices d ON d.id=l.device_id JOIN subscriptions s ON s.id=d.subscription_id
            WHERE l.token_hash=? AND l.enabled=1""",(digest(token),)).fetchone()
        if not row:
            raise HTTPException(404,'Ссылка не найдена')
        return _feed_locked(conn,row['user_id'],row['device_id'])
