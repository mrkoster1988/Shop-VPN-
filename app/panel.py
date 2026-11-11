"""Explicit VLESS API adapters. Never guess or fall back after a remote write."""
import json
from datetime import datetime, timezone
from urllib.parse import quote, urlsplit


class PanelError(RuntimeError):
    """A fixed local error; never include remote bodies or credentials."""


def result(response):
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict) or data.get('success') is not True:
        raise PanelError('PanelOperationFailed')
    return data.get('obj')


class Legacy:
    def __init__(self, profile):
        self.profile = profile
        self.base = profile['url']
        self.inbound_id = profile['inbound_id']

    async def inbound(self, client):
        response = await client.get(self.base + f'/panel/api/inbounds/get/{self.inbound_id}')
        obj = result(response)
        # Новые панели выдают CSRF-токен при чтении; передаём его при записи.
        # Старые версии без заголовка продолжают работать по прежнему контракту.
        csrf = response.headers.get('X-CSRF-Token')
        if csrf and isinstance(self, Legacy) and not isinstance(self, Clients):
            client.headers['X-CSRF-Token'] = csrf
        if (not isinstance(obj, dict) or obj.get('protocol') != 'vless'
                or ('id' in obj and (type(obj['id']) is not int or obj['id'] != self.inbound_id))):
            raise PanelError('VLESSInboundUnavailable')
        return obj

    async def check(self, client):
        result(await client.post(self.base + '/login', data={key:self.profile[key] for key in ('username','password')}))
        inbound = await self.inbound(client)
        self.records(inbound)
        return inbound

    def records(self, inbound):
        settings = inbound.get('settings')
        if isinstance(settings, str):
            try:
                settings = json.loads(settings)
            except (ValueError, TypeError):
                raise PanelError('UnsupportedLegacySchema') from None
        if (not isinstance(settings, dict) or not isinstance(settings.get('clients'), list)
                or any(not isinstance(row, dict) or not isinstance(row.get('id'), str)
                       or not row['id'] for row in settings['clients'])):
            raise PanelError('UnsupportedLegacySchema')
        return settings['clients']

    def find(self, records, identity):
        matches = [row for row in records if row['id'] == identity]
        if len(matches) > 1:
            raise PanelError('AmbiguousLegacyClient')
        return matches[0] if matches else None

    def identity_available(self, records, payload):
        for row in records:
            if row['id'] != payload['id'] and any(
                    payload.get(key) and row.get(key) == payload[key] for key in ('email', 'subId')):
                raise PanelError('LegacyClientIdentityCollision')

    async def apply(self, client, sub, enabled):
        inbound = await self.check(client)
        records = self.records(inbound)
        existing = self.find(records, sub['id'])
        if not enabled and not existing:
            return ''
        payload = dict(existing) if existing else {
            'id': sub['id'], 'email': 'pushok_' + sub['id'], 'flow': self.profile['flow'],
            'limitIp': 0, 'totalGB': 0, 'subId': sub['id'].replace('-', '')}
        self.identity_available(records, payload)
        payload.update(enable=enabled, expiryTime=sub['expires'] * 1000)
        endpoint = f"updateClient/{sub['id']}" if existing else 'addClient'
        result(await client.post(self.base + '/panel/api/inbounds/' + endpoint,
            data={'id': self.inbound_id, 'settings': json.dumps({'clients': [payload]})}))
        confirmed_records = self.records(await self.inbound(client))
        confirmed = self.find(confirmed_records, sub['id'])
        # Some panels remove disabled clients. Absence confirms revocation, never issuance.
        if confirmed is None and not enabled:
            return ''
        if (confirmed is None or confirmed.get('enable') is not enabled
                or type(confirmed.get('expiryTime')) is not int
                or confirmed['expiryTime'] != sub['expires'] * 1000
                or any(confirmed.get(key) != payload.get(key) for key in ('email', 'subId'))):
            raise PanelError('LegacyClientWriteNotConfirmed')
        self.identity_available(confirmed_records, payload)
        return self.profile['link_template'].replace('{uuid}', sub['id'])


class Clients(Legacy):
    # JSON clients API; contract snapshot 83ea59fb693fee1815d867dff4d33028ddaf12a9.
    async def check(self, client):
        client.headers['Authorization'] = 'Bearer ' + self.profile['token']
        inbound = await self.inbound(client)
        await self.search(client, '')
        return inbound

    async def search(self, client, email):
        obj = result(await client.get(self.base + '/panel/api/clients/list/paged',
            params={'page':1, 'pageSize':200 if email else 1, 'search':email}))
        if (not isinstance(obj,dict) or not isinstance(obj.get('items'),list)
                or type(obj.get('filtered')) is not int or obj['filtered'] < 0):
            raise PanelError('UnsupportedClientsSchema')
        if email and obj['filtered'] > len(obj['items']):
            raise PanelError('AmbiguousClientLookup')
        if any(not isinstance(row,dict) or not isinstance(row.get('email'),str) for row in obj['items']):
            raise PanelError('UnsupportedClientsSchema')
        return any(row['email']==email for row in obj['items'])

    async def read(self, client, email, sub):
        obj = result(await client.get(self.base + '/panel/api/clients/get/' + quote(email,safe='')))
        if not isinstance(obj,dict) or not isinstance(obj.get('client'),dict):
            raise PanelError('UnsupportedClientsSchema')
        record = obj['client']
        if (record.get('email') != email or record.get('uuid') != sub['id']
                or obj.get('inboundIds') != [self.inbound_id]
                or record.get('subId') != sub['id'].replace('-','')):
            raise PanelError('ClientIdentityMismatch')
        return record

    async def apply(self, client, sub, enabled):
        client.headers['Authorization'] = 'Bearer ' + self.profile['token']
        await self.inbound(client)
        email = 'pushok_' + sub['id']
        exists = await self.search(client,email)
        if not exists and not enabled:
            return ''
        if exists:
            record = await self.read(client,email,sub)
            # Preserve VLESS settings. The API replaces the client, rather than patching it.
            payload = {key:value for key,value in record.items() if key not in
                ('id','uuid','createdAt','updatedAt','allowedIPs','reverse')}
            payload['id'] = record['uuid']
            if record.get('reverse'):
                payload['reverse'] = json.loads(record['reverse']) if isinstance(record['reverse'],str) else record['reverse']
            if record.get('allowedIPs'):
                raise PanelError('UnexpectedTunnelClient')
        else:
            payload = {'id':sub['id'], 'email':email, 'subId':sub['id'].replace('-',''),
                       'flow':self.profile['flow'], 'limitIp':0, 'limitHwid':0, 'totalGB':0}
        payload.update(enable=enabled, expiryTime=sub['expires']*1000)
        path = '/panel/api/clients/update/'+quote(email,safe='') if exists else '/panel/api/clients/add'
        body = payload if exists else {'client':payload, 'inboundIds':[self.inbound_id]}
        result(await client.post(self.base+path,json=body))
        confirmed = await self.read(client,email,sub)
        if confirmed.get('enable') is not enabled or confirmed.get('expiryTime') != sub['expires']*1000:
            raise PanelError('ClientWriteNotConfirmed')
        return self.profile['link_template'].replace('{uuid}',sub['id'])


class Remnawave:
    """Создаёт, продлевает и отключает пользователя панели для VLESS-профиля магазина."""
    def __init__(self, profile):
        self.profile = profile
        self.base = profile['url'].rstrip('/') + '/api'

    def headers(self):
        return {'Authorization': 'Bearer ' + self.profile['token']}

    def response(self, response, *, allow_missing=False):
        if allow_missing and response.status_code == 404:
            return None
        response.raise_for_status()
        try:
            body = response.json()
            data = body.get('response') if isinstance(body, dict) else None
        except (ValueError, TypeError):
            raise PanelError('UnsupportedPanelSchema') from None
        if not isinstance(data, dict):
            raise PanelError('UnsupportedPanelSchema')
        return data

    async def check(self, client):
        response = await client.get(self.base + '/users', params={'start': 0, 'size': 1},
                                    headers=self.headers())
        data = self.response(response)
        if not isinstance(data.get('users'), list) or type(data.get('total')) is not int:
            raise PanelError('UnsupportedPanelSchema')
        return data

    def username(self, identity):
        import uuid
        try:
            if str(uuid.UUID(identity)) != identity:
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            raise PanelError('InvalidSubscriptionIdentity') from None
        compact = identity.replace('-', '')
        if len(compact) != 32 or any(ch not in '0123456789abcdefABCDEF' for ch in compact):
            raise PanelError('InvalidSubscriptionIdentity')
        return 'p' + compact.lower()

    async def find(self, client, username):
        response = await client.get(self.base + '/users/by-username/' + quote(username, safe=''),
                                    headers=self.headers())
        return self.response(response, allow_missing=True)

    def expiry(self, timestamp):
        return datetime.fromtimestamp(int(timestamp), timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')

    def subscription_url(self, record):
        value = record.get('subscriptionUrl')
        try:
            parsed = urlsplit(value)
            parsed.port
        except (TypeError, ValueError):
            raise PanelError('SubscriptionUrlUnavailable') from None
        if (not isinstance(value, str) or len(value) > 4096 or parsed.scheme != 'https'
                or not parsed.hostname or parsed.username or parsed.password
                or any(ord(ch) < 33 for ch in value)):
            raise PanelError('SubscriptionUrlUnavailable')
        return value

    def verify_identity(self, record, username, sub):
        if (record.get('username') != username or record.get('vlessUuid') != sub['id']
                or type(record.get('id')) is not int or record['id'] < 1):
            raise PanelError('PanelUserIdentityMismatch')
        try:
            actual_expiry = datetime.fromisoformat(record['expireAt'].replace('Z', '+00:00'))
            valid_expiry = actual_expiry.tzinfo is not None and actual_expiry.timestamp() == sub['expires']
        except (ValueError, TypeError, KeyError, AttributeError):
            valid_expiry = False
        if record.get('status') != 'ACTIVE' or not valid_expiry:
            raise PanelError('PanelUserWriteNotConfirmed')
        expected_squads = set(self.profile['squad_uuids'])
        if expected_squads:
            squads = record.get('activeInternalSquads')
            if (not isinstance(squads, list) or any(not isinstance(row, dict) for row in squads)
                    or any(not isinstance(row.get('uuid'), str) for row in squads)
                    or {row.get('uuid') for row in squads} != expected_squads):
                raise PanelError('PanelUserSquadsNotConfirmed')
        return self.subscription_url(record)

    async def apply(self, client, sub, enabled=True):
        await self.check(client)
        username = self.username(sub['id'])
        record = await self.find(client, username)
        headers = self.headers()
        expiry = self.expiry(sub['expires'])
        if record is None:
            if not enabled:
                return ''
            body = {'username': username, 'vlessUuid': sub['id'], 'status': 'ACTIVE', 'expireAt': expiry}
            if self.profile['squad_uuids']:
                body['activeInternalSquads'] = self.profile['squad_uuids']
            response = await client.post(self.base + '/users', json=body, headers=headers)
            # Потерянный ответ не создаёт дубликат: повтор ищет того же пользователя.
            self.response(response)
        else:
            if record.get('username') != username or record.get('vlessUuid') != sub['id']:
                raise PanelError('PanelUserIdentityMismatch')
            if not enabled:
                await self.disable_record(client, record)
                return ''
            response = await client.patch(self.base + '/users',
                json={'username': username, 'status': 'ACTIVE', 'expireAt': expiry}, headers=headers)
            self.response(response)
        confirmed = await self.find(client, username)
        if confirmed is None:
            raise PanelError('PanelUserWriteNotConfirmed')
        return self.verify_identity(confirmed, username, sub)

    async def disable_record(self, client, record):
        user_id = record.get('id')
        if type(user_id) is not int or user_id < 1:
            raise PanelError('UnsupportedPanelSchema')
        if record.get('status') != 'DISABLED':
            response = await client.post(self.base + f'/users/{user_id}/actions/disable',
                                         headers=self.headers())
            self.response(response)
        confirmed = await client.get(self.base + f'/users/{user_id}', headers=self.headers())
        result = self.response(confirmed)
        if (result.get('id') != user_id or result.get('status') != 'DISABLED'
                or result.get('username') != record.get('username')
                or result.get('vlessUuid') != record.get('vlessUuid')):
            raise PanelError('PanelUserDisableNotConfirmed')

    async def disable(self, client, sub):
        await self.check(client)
        username = self.username(sub['id'])
        record = await self.find(client, username)
        if record is None:
            return
        if record.get('username') != username or record.get('vlessUuid') != sub['id']:
            raise PanelError('PanelUserIdentityMismatch')
        await self.disable_record(client, record)


def adapter(profile):
    mode = profile.get('api','legacy')
    if mode == 'legacy':
        return Legacy(profile)
    if mode == 'clients':
        return Clients(profile)
    if mode == 'remnawave':
        return Remnawave(profile)
    raise PanelError('UnsupportedPanelAPI')
