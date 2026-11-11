"""Адаптер WGDashboard; методы записи пока не подключены к заказам магазина."""
from .servers import WGDashboardProfile
from .panel import PanelError


class CheckProfile(WGDashboardProfile):
    pass


class WGDashboard:
    def __init__(self, profile):
        self.profile = CheckProfile.model_validate(profile).model_dump()

    async def check(self, client):
        response = await client.get(self.profile['url'] + '/api/getWireguardConfigurations',
            headers={'wg-dashboard-apikey': self.profile['token']})
        response.raise_for_status()
        try:
            body = response.json()
        except ValueError:
            raise PanelError('UnsupportedWGDashboardSchema') from None
        if (not isinstance(body, dict) or body.get('status') is not True
                or not isinstance(body.get('data'), list)
                or any(not isinstance(row, dict) or not isinstance(row.get('Name'), str)
                       for row in body['data'])):
            raise PanelError('UnsupportedWGDashboardSchema')
        matches = [row for row in body['data'] if row['Name'] == self.profile['configuration']]
        if (len(matches) != 1 or matches[0].get('Protocol') != 'wg'
                or matches[0].get('Status') is not True):
            raise PanelError('WGDashboardInterfaceUnavailable')
        # Ответ панели содержит приватные ключи. Наружу возвращаются только флаги.
        return {'wireguard_interface': True, 'supported_schema': True}

    async def request(self, client, method, endpoint, **kwargs):
        response = await client.request(method, self.profile['url'] + '/api/' + endpoint,
            headers={'wg-dashboard-apikey': self.profile['token']}, **kwargs)
        response.raise_for_status()
        try:
            body = response.json()
        except ValueError:
            raise PanelError('UnsupportedWGDashboardSchema') from None
        if not isinstance(body, dict) or body.get('status') is not True:
            raise PanelError('WGDashboardOperationFailed')
        return body.get('data')

    async def peer(self, client, public_key):
        data = await self.request(client, 'GET', 'getWireguardConfigurationInfo',
            params={'configurationName': self.profile['configuration']})
        if not isinstance(data, dict):
            raise PanelError('UnsupportedWGDashboardSchema')
        info = data.get('configurationInfo')
        if (not isinstance(info, dict) or info.get('Name') != self.profile['configuration']
                or info.get('Protocol') != 'wg'):
            raise PanelError('WGDashboardInterfaceMismatch')
        matches = []
        for field in ('configurationPeers', 'configurationRestrictedPeers'):
            rows = data.get(field)
            if not isinstance(rows, list) or any(not isinstance(row, dict) or not isinstance(row.get('id'), str) for row in rows):
                raise PanelError('UnsupportedWGDashboardSchema')
            matches.extend((field, row) for row in rows if row['id'] == public_key)
        if len(matches) > 1:
            raise PanelError('AmbiguousWGDashboardPeer')
        return matches[0] if matches else None

    @staticmethod
    def identity(private_key):
        """Ключ создаёт и сохраняет вызывающий код ДО первой внешней записи."""
        import base64
        from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
        from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
        try:
            raw = base64.b64decode(private_key, validate=True)
            key = X25519PrivateKey.from_private_bytes(raw)
            if base64.b64encode(raw).decode() != private_key:
                raise ValueError()
        except (TypeError, ValueError):
            raise PanelError('InvalidWireGuardPrivateKey') from None
        return base64.b64encode(key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()

    async def ensure_peer(self, client, private_key, name):
        """Создание по постоянному ключу; не управляет сроком подписки."""
        from urllib.parse import quote
        public_key = self.identity(private_key)
        if not isinstance(name, str) or not name or len(name) > 80 or any(ord(ch) < 32 for ch in name):
            raise PanelError('InvalidWGDashboardPeerName')
        await self.check(client)
        existing = await self.peer(client, public_key)
        if existing and existing[0] == 'configurationRestrictedPeers':
            raise PanelError('WGDashboardPeerRestricted')
        if existing is None:
            await self.request(client, 'POST', 'addPeers/' + quote(self.profile['configuration'], safe=''),
                json={'public_key':public_key, 'private_key':private_key, 'name':name,
                      'allowed_ips':[], 'bulkAdd':False})
        confirmed = await self.peer(client, public_key)
        if confirmed is None or confirmed[0] != 'configurationPeers':
            raise PanelError('WGDashboardPeerWriteNotConfirmed')
        return public_key

    async def revoke_peer(self, client, public_key):
        """Удаляет только выбранный peer и проверяет отсутствие после записи."""
        import base64
        from urllib.parse import quote
        try:
            if len(base64.b64decode(public_key, validate=True)) != 32:
                raise ValueError()
        except (TypeError, ValueError):
            raise PanelError('InvalidWireGuardPublicKey') from None
        await self.check(client)
        if await self.peer(client, public_key) is None:
            return
        await self.request(client, 'POST', 'deletePeers/' + quote(self.profile['configuration'], safe=''),
            json={'peers':[public_key]})
        if await self.peer(client, public_key) is not None:
            raise PanelError('WGDashboardPeerDeleteNotConfirmed')

    async def ensure_expiration(self, client, private_key, expires_at, server_id, credential_id):
        """Create/update the panel's delete job and confirm it before returning access."""
        import time
        import uuid
        from datetime import datetime
        from zoneinfo import ZoneInfo
        from .db import db

        if not self.profile.get('schedule_expiry') or not self.profile.get('panel_timezone'):
            raise PanelError('WGDashboardExpiryNotConfigured')
        if not isinstance(expires_at, int) or expires_at <= int(time.time()):
            raise PanelError('WGDashboardExpiryInvalid')
        public_key = self.identity(private_key)
        current = await self.peer(client, public_key)
        if current is None or current[0] != 'configurationPeers':
            raise PanelError('WGDashboardPeerUnavailable')
        peer = current[1]
        jobs = peer.get('jobs')
        if not isinstance(jobs, list) or any(not isinstance(job, dict) for job in jobs):
            raise PanelError('UnsupportedWGDashboardJobsSchema')
        zone = ZoneInfo(self.profile['panel_timezone'])
        local_expiry = datetime.fromtimestamp(expires_at, zone)
        local_value = local_expiry.strftime('%Y-%m-%d %H:%M:%S')
        # WGDashboard stores a naive wall-clock value. On the second occurrence
        # of a DST fall-back hour that representation would expire an hour early.
        parsed_local = datetime.strptime(local_value, '%Y-%m-%d %H:%M:%S')
        if int(parsed_local.replace(tzinfo=zone, fold=0).timestamp()) != expires_at:
            raise PanelError('WGDashboardExpiryAmbiguousLocalTime')
        now = int(time.time())
        with db() as conn:
            conn.execute('BEGIN IMMEDIATE')
            record = conn.execute(
                'SELECT job_id,target_expires FROM wgdashboard_expirations WHERE server_id=? AND credential_id=?',
                (server_id, credential_id)).fetchone()
            remote_ids = {job.get('JobID') for job in jobs if isinstance(job.get('JobID'), str)}
            if record is None:
                job_id = str(uuid.uuid4())
                conn.execute('INSERT INTO wgdashboard_expirations(server_id,credential_id,job_id,target_expires,updated) VALUES (?,?,?,?,?)',
                             (server_id, credential_id, job_id, expires_at, now))
            else:
                job_id = record['job_id']
                if job_id not in remote_ids and record['target_expires'] <= now:
                    job_id = str(uuid.uuid4())
                conn.execute('UPDATE wgdashboard_expirations SET job_id=?,target_expires=?,updated=? WHERE server_id=? AND credential_id=?',
                             (job_id, expires_at, now, server_id, credential_id))

        payload = {'JobID':job_id, 'Configuration':self.profile['configuration'], 'Peer':public_key,
                   'Field':'date', 'Operator':'lgt', 'Value':local_value, 'Action':'delete'}
        method = 'PUT' if job_id in remote_ids else 'POST'
        try:
            await self.request(client, method, 'PeerScheduleJob', json=payload)
        except Exception as exc:
            # The panel can commit a job and lose the HTTP response. Re-read before retrying.
            refreshed = await self.peer(client, public_key)
            refreshed_jobs = refreshed[1].get('jobs', []) if refreshed else []
            if not isinstance(refreshed_jobs, list):
                raise PanelError('UnsupportedWGDashboardJobsSchema')
            confirmed = next((job for job in refreshed_jobs
                              if isinstance(job, dict) and job.get('JobID') == job_id), None)
            if confirmed is None and getattr(getattr(exc, 'response', None), 'status_code', None) in (404, 409):
                job_id = str(uuid.uuid4())
                with db() as conn:
                    conn.execute('UPDATE wgdashboard_expirations SET job_id=?,updated=? WHERE server_id=? AND credential_id=?',
                                 (job_id, int(time.time()), server_id, credential_id))
                payload['JobID'] = job_id
                await self.request(client, 'POST', 'PeerScheduleJob', json=payload)
            elif confirmed is None:
                raise

        confirmed_peer = await self.peer(client, public_key)
        if confirmed_peer is None or confirmed_peer[0] != 'configurationPeers':
            raise PanelError('WGDashboardPeerUnavailable')
        confirmed_jobs = confirmed_peer[1].get('jobs')
        if not isinstance(confirmed_jobs, list) or any(not isinstance(job, dict) for job in confirmed_jobs):
            raise PanelError('UnsupportedWGDashboardJobsSchema')
        confirmed = [job for job in confirmed_jobs
                     if isinstance(job, dict) and job.get('JobID') == job_id]
        if len(confirmed) != 1:
            raise PanelError('WGDashboardExpiryWriteNotConfirmed')
        job = confirmed[0]
        if any(job.get(key) != value for key, value in payload.items()):
            raise PanelError('WGDashboardExpiryMismatch')
        return {'scheduled': True}

    async def download_config(self, client, private_key):
        """Получает конфиг только для подтверждённого активного peer этого ключа."""
        import configparser
        import ipaddress
        from urllib.parse import quote
        public_key = self.identity(private_key)
        await self.check(client)
        existing = await self.peer(client, public_key)
        if existing is None or existing[0] != 'configurationPeers':
            raise PanelError('WGDashboardPeerUnavailable')
        data = await self.request(client, 'GET',
            'downloadPeer/' + quote(self.profile['configuration'], safe=''), params={'id':public_key})
        if not isinstance(data, dict) or not isinstance(data.get('file'), str):
            raise PanelError('UnsupportedWGDashboardConfig')
        raw = data['file']
        if len(raw) > 16384 or '\x00' in raw:
            raise PanelError('UnsupportedWGDashboardConfig')
        parser = configparser.ConfigParser(interpolation=None, strict=True)
        parser.optionxform = str
        try:
            parser.read_string(raw)
            if set(parser.sections()) != {'Interface', 'Peer'} or parser.defaults():
                raise ValueError()
            interface, peer = parser['Interface'], parser['Peer']
            # Не выдаём исполняемые hooks из удалённого шаблона панели.
            if (set(interface) - {'PrivateKey','Address','DNS','MTU','ListenPort'}
                    or set(peer) - {'PublicKey','PresharedKey','AllowedIPs','Endpoint','PersistentKeepalive'}):
                raise ValueError()
            if interface['PrivateKey'] != private_key:
                raise ValueError()
            for value in interface['Address'].split(','):
                ipaddress.ip_interface(value.strip())
            for value in peer['AllowedIPs'].split(','):
                ipaddress.ip_network(value.strip(), strict=False)
            import base64
            if len(base64.b64decode(peer['PublicKey'], validate=True)) != 32:
                raise ValueError()
            endpoint = peer['Endpoint']
            from urllib.parse import urlsplit
            parsed = urlsplit('//' + endpoint)
            if (not parsed.hostname or not parsed.port or parsed.username or parsed.password
                    or parsed.path or parsed.query or parsed.fragment
                    or any(ch.isspace() for ch in endpoint)):
                raise ValueError()
        except (configparser.Error, KeyError, ValueError, TypeError):
            raise PanelError('WGDashboardConfigMismatch') from None
        return raw


def stored_private_key(server_id, credential_id, *, create=True):
    """Фиксирует зашифрованный ключ до сети; повторы и перезапуски получают тот же."""
    import base64
    import uuid
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PrivateFormat, NoEncryption
    from . import config as c
    from .db import db
    try:
        if str(uuid.UUID(credential_id)) != credential_id:
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise PanelError('InvalidSubscriptionIdentity') from None
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        server = conn.execute('SELECT protocol FROM servers WHERE id=?', (server_id,)).fetchone()
        if server is None or server['protocol'] != 'wg':
            raise PanelError('WireGuardServerRequired')
        row = conn.execute('SELECT private_key FROM wgdashboard_keys WHERE server_id=? AND credential_id=?',
                           (server_id, credential_id)).fetchone()
        if row is not None:
            private_key = c.decrypt(row['private_key'])
            WGDashboard.identity(private_key)
            return private_key
        if not create:
            raise PanelError('WGDashboardStoredKeyMissing')
        private_key = base64.b64encode(X25519PrivateKey.generate().private_bytes(
            Encoding.Raw, PrivateFormat.Raw, NoEncryption())).decode()
        conn.execute('INSERT INTO wgdashboard_keys(server_id,credential_id,private_key) VALUES (?,?,?)',
                     (server_id, credential_id, c.encrypt(private_key)))
    return private_key
