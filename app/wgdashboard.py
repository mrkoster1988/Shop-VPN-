"""Проверка WGDashboard без записи peers; выдача пока не подключена к магазину."""
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
