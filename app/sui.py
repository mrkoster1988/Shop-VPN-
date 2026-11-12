"""Hysteria2 и TUIC через S-UI apiv2; контракт d1c2ce92a77b32388845bc863a7f0a70871f74ca."""
import hmac
import json
import re
import secrets
import uuid
from urllib.parse import quote, urlencode

from . import config as c
from .db import db
from .panel import PanelError
from .servers import Hysteria2Profile, TUICProfile


def client_name(identity):
    try:
        if str(uuid.UUID(identity)) != identity:
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise PanelError('InvalidSubscriptionIdentity') from None
    return 'pushok_' + identity


def stored_password(server_id, identity, *, create=True, protocol='hysteria2'):
    """Случайный пароль фиксируется до сети; UUID подписки не является паролем."""
    client_name(identity)
    variants = {'hysteria2':('hysteria2_credentials','Hysteria2'), 'tuic':('tuic_credentials','TUIC')}
    if protocol not in variants:
        raise PanelError('UnsupportedSUIProtocol')
    table, label = variants[protocol]
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        server = conn.execute('SELECT protocol FROM servers WHERE id=?', (server_id,)).fetchone()
        if server is None or server['protocol'] != protocol:
            raise PanelError(label + 'ServerRequired')
        row = conn.execute('SELECT password FROM ' + table + ' WHERE server_id=? AND credential_id=?',
                           (server_id, identity)).fetchone()
        if row:
            password = c.decrypt(row['password'])
            if not re.fullmatch(r'[A-Za-z0-9_-]{43}', password):
                raise PanelError('Invalid' + label + 'StoredPassword')
            return password
        if not create:
            return None
        password = secrets.token_urlsafe(32)
        conn.execute('INSERT INTO ' + table + '(server_id,credential_id,password) VALUES (?,?,?)',
                     (server_id, identity, c.encrypt(password)))
        return password


class SUIClient:
    protocol = None
    profile_model = None
    error_prefix = 'SUI'

    def credentials(self, name, password):
        return {'name':name, 'password':password}

    def validate_transport(self, inbound):
        raise NotImplementedError

    def valid_alpn(self, value):
        raise NotImplementedError

    def __init__(self, profile):
        self.profile = self.profile_model.model_validate(profile).model_dump()

    async def request(self, client, method, endpoint, **kwargs):
        response = await client.request(method, self.profile['url'] + '/apiv2/' + endpoint,
                                       headers={'Token':self.profile['token']}, **kwargs)
        response.raise_for_status()
        try:
            body = response.json()
        except ValueError:
            raise PanelError('UnsupportedSUISchema') from None
        if not isinstance(body, dict) or body.get('success') is not True:
            raise PanelError('SUIOperationFailed')
        return body.get('obj')

    @staticmethod
    def rows(data, key):
        rows = data.get(key) if isinstance(data, dict) else None
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise PanelError('UnsupportedSUISchema')
        return rows

    async def running(self, client):
        data = await self.request(client, 'GET', 'status', params={'r':'sbd'})
        state = data.get('sbd') if isinstance(data, dict) else None
        if (not isinstance(state, dict) or state.get('running') is not True
                or state.get('maintenance') is not False):
            raise PanelError('SUICoreUnavailable')

    async def inbound(self, client):
        data = await self.request(client, 'GET', 'inbounds', params={'id':self.profile['inbound_id']})
        rows = self.rows(data, 'inbounds')
        if (len(rows) != 1 or type(rows[0].get('id')) is not int
                or rows[0]['id'] != self.profile['inbound_id'] or rows[0].get('type') != self.protocol
                or rows[0].get('enable') is not True):
            raise PanelError(self.error_prefix + 'InboundUnavailable')
        inbound = rows[0]
        tls_id = inbound.get('tls_id')
        if type(tls_id) is not int or tls_id < 1:
            raise PanelError(self.error_prefix + 'TLSRequired')
        tls_rows = self.rows(await self.request(client, 'GET', 'tls'), 'tls')
        matches = [row for row in tls_rows if row.get('id') == tls_id]
        if len(matches) != 1 or type(matches[0].get('id')) is not int:
            raise PanelError(self.error_prefix + 'TLSRequired')
        for side in ('server', 'client'):
            tls = matches[0].get(side)
            if (not isinstance(tls, dict) or tls.get('enabled') is not True
                    or tls.get('insecure', False) is not False
                    or tls.get('server_name', self.profile['sni']) != self.profile['sni']
                    or not self.valid_alpn(tls.get('alpn'))
                    or any(tls.get(key) for key in ('reality', 'ech', 'client_authentication', 'client_certificate'))):
                raise PanelError(self.error_prefix + 'TLSMismatch')
        self.validate_transport(inbound)
        return inbound

    async def check(self, client):
        await self.inbound(client)
        await self.running(client)
        self.rows(await self.request(client, 'GET', 'clients'), 'clients')

    async def find(self, client, name):
        rows = self.rows(await self.request(client, 'GET', 'clients'), 'clients')
        if any(not isinstance(row.get('name'), str) for row in rows):
            raise PanelError('UnsupportedSUISchema')
        matches = [row for row in rows if row['name'] == name]
        if not matches:
            return None
        if len(matches) != 1 or type(matches[0].get('id')) is not int or matches[0]['id'] < 1:
            raise PanelError('SUIClientIdentityMismatch')
        identity = matches[0]['id']
        rows = self.rows(await self.request(client, 'GET', 'clients', params={'id':identity}), 'clients')
        if (len(rows) != 1 or type(rows[0].get('id')) is not int or rows[0]['id'] != identity
                or rows[0].get('name') != name):
            raise PanelError('SUIClientIdentityMismatch')
        return rows[0]

    def verify_identity(self, row, name, password):
        if password is None:
            raise PanelError(self.error_prefix + 'StoredPasswordMissing')
        config = row.get('config')
        auth = config.get(self.protocol) if isinstance(config, dict) else None
        if (not isinstance(config, dict) or set(config) != {self.protocol}
                or not isinstance(auth, dict)
                or any(auth.get(key) != value for key,value in self.credentials(name,password).items() if key != 'password')
                or not isinstance(auth.get('password'), str)
                or not hmac.compare_digest(auth['password'].encode(), password.encode())
                or row.get('inbounds') != [self.profile['inbound_id']]
                or type(row['inbounds'][0]) is not int):
            raise PanelError('SUIClientIdentityMismatch')

    @staticmethod
    def verify_policy(row):
        if row.get('delayStart') is not False or row.get('autoReset') is not False:
            raise PanelError('UnsupportedSUIClientPolicy')
        if any(type(row.get(k)) is not int or row[k] < 0 for k in ('volume', 'up', 'down')):
            raise PanelError('UnsupportedSUISchema')
        if row['volume'] and row['up'] + row['down'] >= row['volume']:
            raise PanelError('SUIClientQuotaExceeded')

    async def close_sessions(self, client, name):
        await self.running(client)
        await self.request(client, 'POST', 'closeSessions', data={'u':name})
        rows = await self.request(client, 'GET', 'sessions', params={'resource':'user', 'tag':name})
        if not isinstance(rows, list) or rows:
            raise PanelError('SUISessionCloseNotConfirmed')

    async def apply(self, client, sub, password, enabled=True):
        name = client_name(sub['id'])
        if enabled:
            await self.inbound(client)
            await self.running(client)
        row = await self.find(client, name)
        if row is None and not enabled:
            # Даже после удаления записи в панели могла остаться открытая QUIC-сессия.
            await self.close_sessions(client, name)
            return ''
        if row is None:
            if not isinstance(password, str) or not re.fullmatch(r'[A-Za-z0-9_-]{43}', password):
                raise PanelError('Invalid' + self.error_prefix + 'StoredPassword')
            payload = {'id':0, 'name':name, 'remark':'puShok shop VPN', 'desc':'', 'group':'pushok',
                       'config':{self.protocol:self.credentials(name,password)},
                       'inbounds':[self.profile['inbound_id']], 'links':[], 'volume':0,
                       'up':0, 'down':0, 'delayStart':False, 'autoReset':False,
                       'resetDays':0, 'nextReset':0}
        else:
            self.verify_identity(row, name, password)
            if enabled:
                self.verify_policy(row)
            payload = dict(row)
            # В этом контракте up=down=0 означает сброс трафика. Ненулевой маркер
            # заставляет панель сохранить актуальные счётчики в своей транзакции.
            if type(row.get('up')) is not int or row['up'] < 0:
                raise PanelError('UnsupportedSUISchema')
            payload['up'] = max(row['up'], 1)
        payload.update(enable=enabled, expiry=sub['expires'])
        await self.request(client, 'POST', 'save', data={
            'object':'clients', 'action':'new' if row is None else 'edit',
            'data':json.dumps(payload, separators=(',', ':'))})
        confirmed = await self.find(client, name)
        if confirmed is None:
            raise PanelError('SUIClientWriteNotConfirmed')
        self.verify_identity(confirmed, name, password)
        if (row is not None and confirmed['id'] != row['id']
                or confirmed.get('enable') is not enabled
                or type(confirmed.get('expiry')) is not int or confirmed['expiry'] != sub['expires']):
            raise PanelError('SUIClientWriteNotConfirmed')
        if enabled:
            self.verify_policy(confirmed)
            await self.inbound(client)
            await self.running(client)
            return self.link(password, sub['id'])
        await self.close_sessions(client, name)
        return ''


class SUIHysteria2(SUIClient):
    protocol = 'hysteria2'
    profile_model = Hysteria2Profile
    error_prefix = 'Hysteria2'

    def valid_alpn(self, value):
        return value in (None, [], ['h3'])

    def validate_transport(self, inbound):
        expected = ({'type':'salamander', 'password':self.profile['obfs_password']}
                    if self.profile['obfs_password'] else None)
        if (inbound.get('obfs') or None) != expected:
            raise PanelError('Hysteria2ObfsMismatch')

    def link(self, password, identity):
        host = self.profile['address']
        if ':' in host:
            host = '[' + host + ']'
        query = {'sni':self.profile['sni'], 'insecure':'0'}
        if self.profile['obfs_password']:
            query.update(obfs='salamander', **{'obfs-password':self.profile['obfs_password']})
        return (f"hysteria2://{quote(password, safe='')}@{host}:{self.profile['port']}/?"
                + urlencode(query, quote_via=quote) + '#puShok%20shop%20VPN')


class SUITUIC(SUIClient):
    protocol = 'tuic'
    profile_model = TUICProfile
    error_prefix = 'TUIC'

    def credentials(self, name, password):
        return {'name':name, 'uuid':name.removeprefix('pushok_'), 'password':password}

    def valid_alpn(self, value):
        return value == ['h3']

    def validate_transport(self, inbound):
        if ((inbound.get('congestion_control') or 'cubic') != self.profile['congestion_control']
                or inbound.get('zero_rtt_handshake', False) is not False
                or inbound.get('obfs') or inbound.get('udp_over_stream')):
            raise PanelError('TUICTransportMismatch')

    def link(self, password, identity):
        client_name(identity)
        host = self.profile['address']
        if ':' in host:
            host = '[' + host + ']'
        query = {'security':'tls', 'sni':self.profile['sni'], 'alpn':'h3', 'insecure':'0',
                 'congestion_control':self.profile['congestion_control'],
                 'udp_relay_mode':self.profile['udp_relay_mode']}
        return (f"tuic://{identity}:{quote(password, safe='')}@{host}:{self.profile['port']}/?"
                + urlencode(query, quote_via=quote) + '#puShok%20shop%20VPN')
