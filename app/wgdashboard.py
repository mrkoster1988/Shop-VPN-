"""Адаптер WGDashboard; методы записи пока не подключены к заказам магазина."""
from pydantic import Field
from .servers import WGProfile
from .panel import PanelError


class CheckProfile(WGProfile):
    configuration: str = Field(min_length=1, max_length=64, pattern=r'^[A-Za-z0-9_=+.-]+$')


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
