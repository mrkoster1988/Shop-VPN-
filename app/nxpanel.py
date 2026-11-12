"""Предварительная диагностика NX Panel. Выдача подписок пока не подключена."""
from urllib.parse import urlsplit

from pydantic import Field, field_validator

from .panel import PanelError
from .servers import Profile, WGProfile


class NXCheckError(PanelError):
    """Только локальные коды ошибок; ответы панели не попадают в отчёт."""


class CheckProfile(Profile):
    api: str = Field(default='nxpanel', pattern=r'^nxpanel$')
    token: str = Field(min_length=1, max_length=2048, repr=False)
    template_id: int = Field(ge=1, le=2147483647, strict=True)
    valid_header = field_validator('token')(WGProfile.valid_header.__func__)

    @field_validator('url')
    @classmethod
    def secure_base(cls, value):
        parsed = urlsplit(value)
        segments = parsed.path.split('/')[1:]
        if (parsed.scheme != 'https' or '%' in parsed.path or '\\' in value
                or any(part in ('', '.', '..') for part in segments)
                or parsed.path.endswith(('/api', '/api/v1'))):
            raise ValueError('Нужен HTTPS адрес панели с base path, без /api/v1')
        return value


class NXPanel:
    # Снимок официального OpenAPI: nx-panel.github.io e2479d0c244d05aaf68846a4d9b26972192c4142.
    MAX_BODY = 1024 * 1024
    PAGE_SIZE = 100
    MAX_PAGES = 10

    def __init__(self, profile):
        self.profile = CheckProfile.model_validate(profile).model_dump()

    async def read(self, client, path, params=None):
        # Только фиксированные GET-маршруты; токен не передаётся редиректам.
        async with client.stream('GET', self.profile['url'] + '/api/v1/' + path,
                headers={'Authorization': 'Bearer ' + self.profile['token'],
                         'Accept': 'application/json'}, params=params,
                follow_redirects=False, auth=None) as response:
            response.raise_for_status()
            chunks = bytearray()
            async for chunk in response.aiter_bytes(chunk_size=65536):
                chunks.extend(chunk)
                if len(chunks) > self.MAX_BODY:
                    raise NXCheckError('NXResponseTooLarge')
        import json
        try:
            body = json.loads(chunks)
        except (ValueError, UnicodeError):
            raise NXCheckError('UnsupportedNXSchema') from None
        if not isinstance(body, dict):
            raise NXCheckError('UnsupportedNXSchema')
        return body

    @staticmethod
    def page(body, limit, offset):
        items = body.get('items')
        total = body.get('total')
        if (not isinstance(items, list) or type(total) is not int or total < 0
                or type(body.get('limit')) is not int or body['limit'] != limit
                or type(body.get('offset')) is not int or body['offset'] != offset
                or len(items) != min(limit, max(0, total - offset))
                or any(not isinstance(row, dict) or type(row.get('id')) is not int
                       or row['id'] < 1 for row in items)
                or len({row['id'] for row in items}) != len(items)):
            raise NXCheckError('UnsupportedNXSchema')
        return items, total

    async def check(self, client):
        identity = await self.read(client, 'me')
        scopes = identity.get('scopes')
        if (identity.get('token') is not True or type(identity.get('id')) is not int
                or identity['id'] < 0 or identity.get('base') not in ('sudo', 'admin', 'resale')
                or not isinstance(scopes, list)
                or any(not isinstance(scope, str) or not scope for scope in scopes)):
            raise NXCheckError('UnsupportedNXIdentity')
        if identity.get('base') == 'resale' and identity.get('blocked') is not False:
            raise NXCheckError('NXResellerUnavailable')
        if scopes and any(not {name + ':read', name + ':write'}.intersection(scopes)
                          for name in ('users', 'templates')):
            raise NXCheckError('NXReadScopesRequired')
        self.page(await self.read(client, 'users', {'limit': 1, 'offset': 0}), 1, 0)
        seen = set()
        expected_total = None
        for index in range(self.MAX_PAGES):
            offset = index * self.PAGE_SIZE
            items, total = self.page(await self.read(client, 'templates',
                {'limit': self.PAGE_SIZE, 'offset': offset}), self.PAGE_SIZE, offset)
            ids = {row['id'] for row in items}
            if (seen.intersection(ids)
                    or expected_total is not None and total != expected_total):
                raise NXCheckError('NXTemplateListChanged')
            seen.update(ids)
            expected_total = total
            if self.profile['template_id'] in ids:
                return {'checks': {'authentication': True, 'token_scopes': True,
                    'user_api': True, 'template_access': True, 'supported_schema': True},
                    'warnings': [] if scopes else ['NXUnrestrictedToken']}
            if offset + len(items) >= total:
                raise NXCheckError('NXTemplateNotFound')
        raise NXCheckError('NXTemplateScanLimit')
