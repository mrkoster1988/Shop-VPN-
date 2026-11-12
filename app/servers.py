"""Server registry. Connection profiles stay encrypted and never enter API responses."""
import ipaddress
import json
import os
import time
import uuid
from urllib.parse import urlsplit
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from pydantic import BaseModel, Field, field_validator, model_validator
from fastapi import HTTPException
from . import config as c
from .db import db, one, all_rows


class Profile(BaseModel):
    model_config = {'extra': 'forbid'}
    url: str = Field(min_length=1, max_length=2048)

    @field_validator('url')
    @classmethod
    def url_is_private_or_tls(cls, value):
        parsed = urlsplit(value)
        try:
            host = parsed.hostname
            parsed.port
        except ValueError:
            raise ValueError('Некорректный адрес сервера')
        if (parsed.scheme not in ('http', 'https') or not host or parsed.username or parsed.password
                or parsed.query or parsed.fragment or any(ord(x) < 33 for x in value)):
            raise ValueError('Укажите HTTP(S) адрес без логина, query и fragment')
        if parsed.scheme == 'http':
            try:
                private = ipaddress.ip_address(host).is_private
            except ValueError:
                private = host == 'localhost' or ('.' not in host and ':' not in host)
            if not private:
                raise ValueError('Для публичного сервера требуется HTTPS')
        return value.rstrip('/')

class VLESSProfile(Profile):
    inbound_id: int = Field(ge=1, le=2147483647, strict=True)
    link_template: str = Field(min_length=15, max_length=4096)
    flow: str = Field(default='', max_length=100)

    @field_validator('link_template')
    @classmethod
    def vless_template(cls, value):
        if not value.startswith('vless://{uuid}@') or value.count('{uuid}') != 1 or any(ord(x) < 33 for x in value):
            raise ValueError('Ссылка должна начинаться с vless://{uuid}@')
        return value

class XUIProfile(VLESSProfile):
    api: Literal['legacy'] = 'legacy'
    username: str = Field(min_length=1, max_length=256)
    password: str = Field(min_length=1, max_length=2048)

class RemnawaveProfile(Profile):
    """Профиль API панели для выдачи VLESS-подписок."""
    api: Literal['remnawave'] = 'remnawave'
    token: str = Field(min_length=32, max_length=2048)
    squad_uuids: list[str] = Field(default_factory=list, max_length=100)

    @field_validator('token')
    @classmethod
    def valid_header(cls, value):
        if any(ord(x) < 33 or ord(x) > 126 for x in value):
            raise ValueError('Токен должен содержать печатные ASCII-символы без пробелов')
        return value

    @field_validator('squad_uuids')
    @classmethod
    def valid_squads(cls, values):
        import re
        if any(not re.fullmatch(r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}', value) for value in values):
            raise ValueError('ID групп доступа должны быть UUID')
        if len(set(values)) != len(values):
            raise ValueError('ID групп доступа не должны повторяться')
        return values


class WGProfile(Profile):
    token: str = Field(min_length=32, max_length=2048)

    @field_validator('token')
    @classmethod
    def valid_header(cls, value):
        if any(ord(x) < 33 or ord(x) > 126 for x in value):
            raise ValueError('Токен должен содержать печатные ASCII-символы без пробелов')
        return value


class WGDashboardProfile(WGProfile):
    api: Literal['wgdashboard'] = 'wgdashboard'
    configuration: str = Field(min_length=1, max_length=64, pattern=r'^[A-Za-z0-9_=+.-]+$')
    panel_timezone: str | None = Field(default=None, max_length=100)
    schedule_expiry: bool = False

    @field_validator('panel_timezone')
    @classmethod
    def valid_panel_timezone(cls, value):
        if value is not None:
            try:
                ZoneInfo(value)
            except (ZoneInfoNotFoundError, ValueError):
                raise ValueError('Укажите часовой пояс панели из базы IANA, например Europe/Moscow') from None
        return value

    @field_validator('schedule_expiry')
    @classmethod
    def timezone_required_for_panel_expiry(cls, value, info):
        if value and not info.data.get('panel_timezone'):
            raise ValueError('Для срока на панели укажите её часовой пояс')
        return value


class ClientsProfile(VLESSProfile):
    api: Literal['clients']
    token: str = Field(min_length=32, max_length=2048)
    valid_header = field_validator('token')(WGProfile.valid_header.__func__)


class VMessProfile(Profile):
    """Структурированный профиль VMess AEAD; TLS включён всегда."""
    api: Literal['clients']
    token: str = Field(min_length=32, max_length=2048)
    inbound_id: int = Field(ge=1, le=2147483647, strict=True)
    address: str = Field(min_length=1, max_length=253)
    port: int = Field(ge=1, le=65535, strict=True)
    network: Literal['tcp', 'ws']
    sni: str = Field(min_length=1, max_length=253)
    ws_host: str = Field(default='', max_length=253)
    path: str = Field(default='', max_length=256)
    valid_header = field_validator('token')(WGProfile.valid_header.__func__)

    @field_validator('address', 'sni', 'ws_host')
    @classmethod
    def hostname(cls, value, info):
        if not value and info.field_name == 'ws_host':
            return value
        try:
            ipaddress.ip_address(value)
            if '%' in value:
                raise ValueError()
            return value
        except ValueError:
            import re
            if (not value or len(value) > 253 or any(not re.fullmatch(
                    r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?', label)
                    for label in value.split('.'))):
                raise ValueError('Укажите IP или DNS-имя без схемы, порта, пути и пробелов') from None
        return value

    @model_validator(mode='after')
    def transport_fields(self):
        if self.network == 'tcp' and (self.path or self.ws_host):
            raise ValueError('Для TCP не задавайте путь и Host WebSocket')
        if self.network == 'ws' and (not self.path.startswith('/') or any(
                ord(ch) < 33 or ch in '?#\\' for ch in self.path)):
            raise ValueError('Для WebSocket укажите путь /vpn без query, fragment и пробелов')
        return self


class Hysteria2Profile(Profile):
    """S-UI: прямой Hysteria2 по QUIC/TLS, опционально Salamander."""
    api: Literal['sui']
    token: str = Field(min_length=32, max_length=2048)
    inbound_id: int = Field(ge=1, le=2147483647, strict=True)
    address: str = Field(min_length=1, max_length=253)
    port: int = Field(ge=1, le=65535, strict=True)
    sni: str = Field(min_length=1, max_length=253)
    obfs_password: str = Field(default='', max_length=256)
    valid_header = field_validator('token')(WGProfile.valid_header.__func__)
    hostname = field_validator('address', 'sni')(VMessProfile.hostname.__func__)

    @field_validator('obfs_password')
    @classmethod
    def valid_obfs(cls, value):
        if value and (len(value) < 16 or any(ord(ch) < 33 for ch in value)):
            raise ValueError('Пароль Salamander: не менее 16 символов без пробелов и управляющих знаков')
        return value


class TUICProfile(Profile):
    """TUIC v5 через S-UI, UUID + пароль, TLS и ALPN h3."""
    api: Literal['sui']
    token: str = Field(min_length=32, max_length=2048)
    inbound_id: int = Field(ge=1, le=2147483647, strict=True)
    address: str = Field(min_length=1, max_length=253)
    port: int = Field(ge=1, le=65535, strict=True)
    sni: str = Field(min_length=1, max_length=253)
    congestion_control: Literal['cubic', 'new_reno', 'bbr'] = 'cubic'
    udp_relay_mode: Literal['native', 'quic'] = 'native'
    valid_header = field_validator('token')(WGProfile.valid_header.__func__)
    hostname = field_validator('address', 'sni')(VMessProfile.hostname.__func__)


def validated(protocol, settings):
    if protocol == 'xui':
        mode = settings.get('api', 'legacy')
        model = {'legacy': XUIProfile, 'clients': ClientsProfile, 'remnawave': RemnawaveProfile}.get(mode)
        if model is None:
            raise ValueError('Неизвестный режим API VLESS')
    elif protocol == 'wg':
        mode = settings.get('api')
        model = WGDashboardProfile if mode == 'wgdashboard' else WGProfile if mode is None else None
        if model is None:
            raise ValueError('Неизвестный режим API WireGuard')
    elif protocol == 'vmess':
        model = VMessProfile
    elif protocol == 'hysteria2':
        model = Hysteria2Profile
    elif protocol == 'tuic':
        model = TUICProfile
    else:
        raise ValueError('Неизвестный VPN-протокол')
    return model.model_validate(settings).model_dump()


def decode(row):
    profile = json.loads(c.decrypt(row['settings']))
    if row['protocol']=='xui':
        profile.setdefault('api','legacy')
    return profile


def public(row):
    fields = ('id','name','location','protocol','state','capacity','version','created','health','checked_at','error_code')
    result = {key: row[key] for key in fields}
    if row['protocol'] == 'xui':
        result['profile'] = {'legacy':'legacy_vless','clients':'clients_vless','remnawave':'remnawave_vless'}.get(decode(row).get('api','legacy'),'legacy_vless')
    elif row['protocol'] == 'vmess':
        result['profile'] = 'clients_vmess'
    elif row['protocol'] == 'hysteria2':
        result['profile'] = 'sui_hysteria2'
    elif row['protocol'] == 'tuic':
        result['profile'] = 'sui_tuic'
    else:
        profile = decode(row)
        result['profile'] = 'wgdashboard' if profile.get('api') == 'wgdashboard' else 'wireguard_agent'
        result['schedule_expiry'] = bool(profile.get('schedule_expiry', False))
        result['panel_timezone'] = profile.get('panel_timezone')
    result['bound_subscriptions'] = row.get('bound_subscriptions', 0)
    result['occupied'] = row.get('occupied', 0)
    return result


def list_servers():
    now = int(time.time())
    return [public(row) for row in all_rows("""SELECT v.*,
        (SELECT count(*) FROM subscriptions s WHERE s.server_id=v.id) AS bound_subscriptions,
        (SELECT count(*) FROM subscriptions s WHERE s.server_id=v.id AND (s.state='pending' OR (s.state='active' AND s.expires>?))) AS occupied
        FROM servers v ORDER BY v.created,v.id""", (now,))]


def select(conn, protocol):
    row = conn.execute("""SELECT v.*,
        (SELECT count(*) FROM subscriptions s WHERE s.server_id=v.id AND (s.state='pending' OR (s.state='active' AND s.expires>?))) AS occupied
        FROM servers v WHERE v.protocol=? AND v.state='active' AND v.health!='error'
        ORDER BY occupied,v.created,v.id""", (int(time.time()), protocol)).fetchall()
    for candidate in row:
        if candidate['capacity'] == 0 or candidate['occupied'] < candidate['capacity']:
            return dict(candidate)
    return None


def available(protocol, user_id=None):
    with db() as conn:
        if user_id is not None:
            sub = conn.execute('SELECT server_id FROM subscriptions WHERE user_id=? AND protocol=?', (user_id,protocol)).fetchone()
            if sub:
                if not sub['server_id']:
                    return False
                row = conn.execute('SELECT state,health FROM servers WHERE id=?', (sub['server_id'],)).fetchone()
                return bool(row and row['state'] in ('active','draining') and row['health'] != 'error')
        return select(conn, protocol) is not None


def for_subscription(sub, *, revoking=False):
    sid = sub.get('server_id')
    row = one('SELECT * FROM servers WHERE id=?', (sid,)) if sid else None
    if not row or row['protocol'] != sub['protocol']:
        raise RuntimeError('ServerBindingMissing')
    if not revoking and row['state'] not in ('active','draining'):
        raise RuntimeError('ServerUnavailable')
    # Pinned attempts always use this profile, even after a failed health check.
    return row, decode(row)


def create(data):
    now, sid = int(time.time()), str(uuid.uuid4())
    profile = validated(data.protocol, data.settings)
    with db() as conn:
        conn.execute('INSERT INTO servers(id,name,location,protocol,capacity,settings,created) VALUES (?,?,?,?,?,?,?)',
            (sid,data.name,data.location,data.protocol,data.capacity,c.encrypt(json.dumps(profile)),now))
        conn.execute('INSERT INTO server_changes(server_id,created,action,version) VALUES (?,?,?,1)', (sid,now,'created'))
        row = dict(conn.execute('SELECT * FROM servers WHERE id=?',(sid,)).fetchone())
    return public(row)


def update(sid, data):
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        raw = conn.execute('SELECT * FROM servers WHERE id=?',(sid,)).fetchone()
        if not raw:
            raise HTTPException(404,'Сервер не найден')
        before = dict(raw)
        if before['version'] != data.version:
            raise HTTPException(409,'Сервер уже изменён. Обновите список.')
        profile = decode(before)
        if ((data.schedule_expiry is not None or data.panel_timezone is not None)
                and not (before['protocol'] == 'wg' and profile.get('api') == 'wgdashboard')):
            raise HTTPException(422, 'Настройки задания срока доступны только для WGDashboard.')
        if data.settings is not None:
            settings = dict(data.settings)
            if before['protocol'] == 'wg' and settings.get('api') == 'wgdashboard':
                for key, default in (('schedule_expiry', False), ('panel_timezone', None)):
                    settings.setdefault(key, profile.get(key, default))
            replacement = validated(before['protocol'], settings)
        else:
            replacement = profile
        if before['protocol'] == 'wg' and profile.get('api') == 'wgdashboard':
            if data.schedule_expiry is not None:
                replacement['schedule_expiry'] = data.schedule_expiry
            if data.panel_timezone is not None:
                replacement['panel_timezone'] = data.panel_timezone
            replacement = validated(before['protocol'], replacement)
        bound = conn.execute('SELECT count(*) FROM subscriptions WHERE server_id=?',(sid,)).fetchone()[0]
        identity = (('url','api','squad_uuids') if profile.get('api') == 'remnawave'
                    else ('url','api','inbound_id','link_template','flow')) if before['protocol']=='xui' else ('url','api','configuration')
        if before['protocol'] == 'tuic':
            identity = ('url','api','inbound_id','address','port','sni','congestion_control','udp_relay_mode')
        if before['protocol'] == 'hysteria2':
            identity = ('url','api','inbound_id','address','port','sni','obfs_password')
        if before['protocol'] == 'vmess':
            identity = ('url','api','inbound_id','address','port','network','sni','path','ws_host')
        if bound and any(profile.get(k)!=replacement.get(k) for k in identity):
            raise HTTPException(409,'У сервера есть подписки. Для другого endpoint создайте отдельный сервер.')
        requires_recheck = any(profile.get(key) != replacement.get(key)
                               for key in ('url','api','token','configuration','username','password',
                                           'inbound_id','address','port','network','sni','path','ws_host','obfs_password','congestion_control','udp_relay_mode'))
        health = 'unknown' if requires_recheck else before['health']
        if data.state == 'active' and (requires_recheck or before['state']!='active') and health != 'ok':
            raise HTTPException(409,'Сначала сохраните настройки и успешно проверьте сервер, затем включите продажи.')
        conn.execute('UPDATE servers SET name=?,location=?,state=?,capacity=?,settings=?,health=?,checked_at=?,error_code=?,version=version+1 WHERE id=?',
            (data.name,data.location,data.state,data.capacity,c.encrypt(json.dumps(replacement)),health,
             None if requires_recheck else before['checked_at'],None if requires_recheck else before['error_code'],sid))
        row = dict(conn.execute('SELECT * FROM servers WHERE id=?',(sid,)).fetchone())
        conn.execute('INSERT INTO server_changes(server_id,created,action,version) VALUES (?,?,?,?)', (sid,int(time.time()),'updated',row['version']))
    return public(row)


def update_credentials(sid, data):
    """Меняет только доступ к API, сохраняя endpoint и привязки подписок."""
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        raw = conn.execute('SELECT * FROM servers WHERE id=?', (sid,)).fetchone()
        if not raw:
            raise HTTPException(404, 'Сервер не найден')
        before = dict(raw)
        if before['version'] != data.version:
            raise HTTPException(409, 'Сервер уже изменён. Обновите список и откройте замену доступа заново.')
        profile = decode(before)
        fields = data.model_fields_set - {'version'}
        legacy = before['protocol'] == 'xui' and profile.get('api') == 'legacy'
        if legacy:
            if not fields or not fields <= {'username', 'password'}:
                raise ValueError('InvalidCredentialFields')
        elif fields != {'token'}:
            raise ValueError('InvalidCredentialFields')
        # Явный null не является командой удаления секрета.
        if any(getattr(data, key) is None for key in fields):
            raise ValueError('EmptyCredentials')
        profile.update({key:getattr(data, key) for key in fields})
        replacement = validated(before['protocol'], profile)
        conn.execute("""UPDATE servers SET settings=?, state='disabled', health='unknown',
            checked_at=NULL, error_code=NULL, version=version+1 WHERE id=?""",
            (c.encrypt(json.dumps(replacement)), sid))
        row = dict(conn.execute('SELECT * FROM servers WHERE id=?', (sid,)).fetchone())
        conn.execute('INSERT INTO server_changes(server_id,created,action,version) VALUES (?,?,?,?)',
            (sid, int(time.time()), 'credentials_updated', row['version']))
    # Счётчики и секреты здесь не вычисляем: клиент обновит обычный список серверов.
    return public(row)


def import_environment():
    # One-time snapshot pins old subscriptions before any remote operation can run.
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        if conn.execute("SELECT 1 FROM meta WHERE key='server_registry_initialized'").fetchone():
            for row in conn.execute('SELECT settings FROM servers'):
                c.decrypt(row['settings'])
            return
        definitions = []
        keys = ('XUI_URL','XUI_USERNAME','XUI_PASSWORD','XUI_INBOUND_ID','XUI_LINK_TEMPLATE')
        if all(os.getenv(k) for k in keys):
            definitions.append(('xui', {'url':os.environ['XUI_URL'],'username':os.environ['XUI_USERNAME'],
                'password':os.environ['XUI_PASSWORD'],'inbound_id':int(os.environ['XUI_INBOUND_ID']),
                'link_template':os.environ['XUI_LINK_TEMPLATE'],'flow':os.getenv('XUI_FLOW','')}))
        if all(os.getenv(k) for k in ('WG_AGENT_URL','WG_AGENT_TOKEN')):
            definitions.append(('wg',{'url':os.environ['WG_AGENT_URL'],'token':os.environ['WG_AGENT_TOKEN']}))
        legacy_protocols = {row[0] for row in conn.execute('SELECT DISTINCT protocol FROM subscriptions WHERE server_id IS NULL')}
        if legacy_protocols - {protocol for protocol, _ in definitions}:
            raise RuntimeError('Original provider settings required to bind legacy subscriptions')
        for protocol, profile in definitions:
            sid = 'legacy-' + protocol
            profile = validated(protocol, profile)
            conn.execute("INSERT INTO servers(id,name,protocol,state,settings,created) VALUES (?,?,?,'active',?,?)",
                (sid,'Импорт из .env · '+protocol,protocol,c.encrypt(json.dumps(profile)),int(time.time())))
            conn.execute('UPDATE subscriptions SET server_id=? WHERE protocol=? AND server_id IS NULL',(sid,protocol))
            conn.execute('INSERT INTO server_changes(server_id,created,action,version) VALUES (?,?,?,1)',(sid,int(time.time()),'environment_imported'))
        conn.execute("INSERT INTO meta(key,value) VALUES ('server_registry_initialized','1')")
