"""One management executor shared by Shop and the authenticated Mini App bridge."""
import hashlib
import hmac
import json
import os
import re
import secrets
import time
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, ValidationError, field_validator

from . import config as c, providers, servers, service, wg_fleet as fleet
from . import wg_fleet_health as health
from .db import db
from .security import admin

router = APIRouter(prefix='/api/admin/wg-fleet')
MUTATIONS = frozenset(('create','update','check','state','drain','retry','policy'))


class Fields(BaseModel):
    model_config = {'extra': 'forbid'}
    name: str = Field(min_length=1, max_length=80)
    location: str = Field(default='', max_length=80)
    capacity: int = Field(default=0, ge=0, le=1000000, strict=True)
    endpoint_ip: str | None = Field(default=None, max_length=45)

    @field_validator('name', 'location')
    @classmethod
    def text(cls, value):
        if any(ord(ch)<32 or ord(ch)==127 for ch in value):
            raise ValueError('InvalidText')
        return value.strip()

    @field_validator('endpoint_ip')
    @classmethod
    def endpoint(cls, value):
        if value is None or value == '':
            return None
        from . import wg_fleet_dns
        return wg_fleet_dns.validate_endpoint_ip(value)


class Create(Fields):
    protocol: str = 'wg'
    settings: dict


class Update(Fields):
    id: str = Field(min_length=1, max_length=100)
    version: int = Field(ge=1, strict=True)
    state: str | None = None
    settings: dict | None = None
    schedule_expiry: bool | None = None
    panel_timezone: str | None = Field(default=None, max_length=100)


class Command(BaseModel):
    model_config = {'extra': 'forbid'}
    action: str = Field(min_length=1, max_length=30)
    payload: dict = Field(default_factory=dict)
    request_id: uuid.UUID | None = None


class Envelope(Command):
    actor: uuid.UUID
    actor_role: str


def snapshot(conn, *, developer=False):
    nodes = []
    for raw in conn.execute("""SELECT s.*,n.domain,n.endpoint_ip,n.dns_state,
            COALESCE(n.archived,0) AS archived FROM servers s
        LEFT JOIN wg_fleet_nodes n ON n.server_id=s.id WHERE s.protocol='wg'
        ORDER BY s.created,s.id"""):
        row = dict(raw)
        if servers.decode(row).get('api') != 'wgdashboard':
            continue
        counts = fleet.counts(conn, row['id'])
        state = health.health_summary(conn, row['id'], row['version'])
        score=None
        if all(state[key] is not None for key in ('cpu_percent','rx_bps','tx_bps','bandwidth_capacity_bps')):
            score=max(state['cpu_percent']/100,max(state['rx_bps'],state['tx_bps'])/state['bandwidth_capacity_bps'],
                      (counts['occupied']+counts['reserved'])/row['capacity'] if row['capacity'] else 0)
        item = {k: row[k] for k in ('id','name','location','domain','endpoint_ip','state','health','capacity','version','error_code')}
        item.update({
            'dns_state': row['dns_state'] or 'unconfigured', 'archived': bool(row['archived']),
            'traffic_verified': state['healthy'], 'checked_at': state['checked_at'],
            'api_checked_at': row['checked_at'], 'probe_status': state['status'],
            'load_score': score,
            'occupied': counts['occupied'] + counts['reserved'],
            'primary_count': counts['primary_count'], 'standby_count': counts['standby_count'],
        })
        if developer:
            profile = servers.decode(row)
            item['settings'] = {key: profile.get(key) for key in ('api','url','configuration','schedule_expiry','panel_timezone')}
            item['settings']['token_configured'] = bool(profile.get('token'))
        nodes.append(item)
    operations = [fleet.operation(conn, r[0]) for r in conn.execute('SELECT id FROM wg_fleet_operations ORDER BY created DESC,id LIMIT 100')]
    return {'nodes': nodes, 'operations': operations, 'policy': fleet.policy(conn)}


def _node(conn, sid, developer=True):
    row = next((item for item in snapshot(conn, developer=developer)['nodes'] if item['id']==sid), None)
    if row is None:
        raise HTTPException(404, 'Сервер не найден')
    return row


def _required_id(payload):
    value = payload.get('id')
    if not isinstance(value, str) or not 1 <= len(value) <= 100:
        raise HTTPException(422, 'Укажите сервер или операцию.')
    return value


def _version(payload, row):
    if type(payload.get('version')) is not int or row['version'] != payload['version']:
        raise HTTPException(409, 'Версия изменилась. Обновите список.')


def _load_node(conn, payload):
    sid = _required_id(payload)
    row = conn.execute("SELECT * FROM servers WHERE id=? AND protocol='wg'", (sid,)).fetchone()
    if not row or servers.decode(dict(row)).get('api') != 'wgdashboard':
        raise HTTPException(404, 'Сервер не найден')
    return dict(row)


def _activate(conn, row):
    meta = conn.execute('SELECT * FROM wg_fleet_nodes WHERE server_id=?', (row['id'],)).fetchone()
    if meta and meta['archived']:
        raise HTTPException(409, 'Сервер архивирован. Для новой машины добавьте новый узел.')
    if meta and meta['domain'] and meta['dns_state'] != 'ready':
        raise HTTPException(409, 'Сначала подтвердите DNS WireGuard.')
    if row['health'] != 'ok' or not health.health_summary(conn, row['id'], row['version'])['healthy']:
        raise HTTPException(409, 'Нужны успешная проверка API и свежая проверка трафика WireGuard.')
    if conn.execute("SELECT 1 FROM wg_fleet_operations WHERE source_server_id=? AND state!='completed'", (row['id'],)).fetchone():
        raise HTTPException(409, 'Сначала завершите перенос и отзыв прежних подключений.')


def _profile(settings):
    profile = servers.validated('wg', settings)
    if profile.get('api')!='wgdashboard' or not profile.get('schedule_expiry') or not profile.get('panel_timezone'):
        raise HTTPException(422, 'Для узла нужен WGDashboard с заданием окончания доступа и часовым поясом.')
    return profile


def _create(conn, payload):
    data = Create.model_validate(payload)
    if data.protocol != 'wg' or not data.name:
        raise HTTPException(422, 'Укажите имя сервера WireGuard.')
    profile = _profile(data.settings)
    sid, now = str(uuid.uuid4()), int(time.time())
    from . import wg_fleet_dns
    base = wg_fleet_dns.domain_suffix()
    conn.execute('INSERT INTO servers(id,name,location,protocol,capacity,settings,created) VALUES (?,?,?,?,?,?,?)',
                 (sid,data.name,data.location,'wg',data.capacity,c.encrypt(json.dumps(profile)),now))
    serial = conn.execute('INSERT INTO wg_fleet_nodes(server_id,endpoint_ip,dns_state,created) VALUES (?,?,?,?)',
                          (sid,data.endpoint_ip,'pending' if data.endpoint_ip else 'unconfigured',now)).lastrowid
    conn.execute('UPDATE wg_fleet_nodes SET domain=? WHERE server_id=?', (f'wg{serial}.{base}',sid))
    conn.execute("INSERT INTO server_changes(server_id,created,action,version) VALUES (?,?,'fleet_created',1)", (sid,now))
    return {'node': _node(conn,sid)}


def _update(conn, payload):
    data = Update.model_validate(payload)
    if not data.name:
        raise HTTPException(422,'Укажите имя сервера.')
    row = _load_node(conn, payload)
    _version(payload, row)
    old_profile = servers.decode(row)
    replacement = dict(old_profile)
    if data.settings is not None:
        incoming = dict(data.settings)
        if not incoming.get('token'):
            incoming.pop('token', None)
        replacement.update(incoming)
    if data.schedule_expiry is not None:
        replacement['schedule_expiry'] = data.schedule_expiry
    if data.panel_timezone is not None:
        replacement['panel_timezone'] = data.panel_timezone
    replacement = _profile(replacement)
    occupied = fleet.counts(conn,row['id'])
    bound = occupied['occupied'] + occupied['reserved']
    if bound and any(old_profile.get(key)!=replacement.get(key) for key in ('url','api','configuration')):
        raise HTTPException(409, 'У узла есть подключения. Для новой машины добавьте отдельный сервер.')
    meta = conn.execute('SELECT * FROM wg_fleet_nodes WHERE server_id=?', (row['id'],)).fetchone()
    endpoint_changed = bool(data.endpoint_ip and meta and meta['endpoint_ip'] != data.endpoint_ip)
    if endpoint_changed:
        if bound:
            raise HTTPException(409, 'Изменение IP занятого узла выполняется через добавление и перенос.')
        conn.execute("UPDATE wg_fleet_nodes SET endpoint_ip=?,dns_state='pending' WHERE server_id=?", (data.endpoint_ip,row['id']))
    changed_profile = replacement != old_profile or endpoint_changed
    state = data.state or row['state']
    if state not in ('active','draining','maintenance','disabled'):
        raise HTTPException(422, 'Неизвестное состояние узла.')
    if state in ('disabled','maintenance') and bound and state != row['state']:
        raise HTTPException(409, 'У узла есть подключения. Используйте перенос.')
    if changed_profile:
        state = 'disabled'
    elif state=='active' and row['state']!='active':
        _activate(conn,row)
    conn.execute('''UPDATE servers SET name=?,location=?,capacity=?,state=?,settings=?,version=version+1,
        health=?,checked_at=?,error_code=? WHERE id=?''',
        (data.name,data.location,data.capacity,state,c.encrypt(json.dumps(replacement)),
         'unknown' if changed_profile else row['health'], None if changed_profile else row['checked_at'],
         None if changed_profile else row['error_code'],row['id']))
    if not changed_profile:
        fleet.carry_version(conn,row['id'],row['version'],row['version']+1)
    conn.execute("INSERT INTO server_changes(server_id,created,action,version) VALUES (?,?,'fleet_updated',?)", (row['id'],int(time.time()),row['version']+1))
    return {'node':_node(conn,row['id'])}


def _execute(conn, action, payload):
    if action=='snapshot':
        return snapshot(conn,developer=True)
    if action=='create':
        return _create(conn,payload)
    if action=='update':
        return _update(conn,payload)
    if action=='migration':
        return {'operation':fleet.operation(conn,_required_id(payload))}
    if action=='retry':
        operation = fleet.operation(conn,_required_id(payload))
        conn.execute("UPDATE wg_fleet_placements SET next_attempt=0 WHERE server_id=? AND state!='revoked'", (operation['source_server_id'],))
        conn.execute("UPDATE wg_fleet_placements SET next_attempt=0 WHERE subscription_id IN (SELECT subscription_id FROM wg_fleet_moves WHERE operation_id=?) AND state IN ('pending','revoking')", (operation['id'],))
        return {'operation':fleet.operation(conn,operation['id'])}
    if action=='policy':
        old = fleet.policy(conn)
        _version(payload, old)
        if type(payload.get('enabled')) is not bool or payload.get('standby_count') != 1:
            raise HTTPException(422, 'Резерв подписки должен быть равен одному.')
        conn.execute('UPDATE wg_fleet_policy SET enabled=?,version=version+1,updated=? WHERE id=1', (int(payload['enabled']),int(time.time())))
        return {'policy':fleet.policy(conn)}
    if action in ('state','drain'):
        row = _load_node(conn,payload)
        _version(payload,row)
        if action=='drain':
            if type(payload.get('lost',False)) is not bool:
                raise HTTPException(422, 'Проверьте режим переноса.')
            if not fleet.enabled(conn):
                raise HTTPException(409, 'Сначала включите управление пулом WireGuard.')
            return {'operation':fleet.drain(conn,row['id'],row['version'],deadline_hours=payload.get('deadline_hours',24),lost=payload.get('lost',False))}
        state = payload.get('state')
        if state not in ('active','draining','disabled','maintenance'):
            raise HTTPException(422, 'Неизвестное состояние узла.')
        if state=='active':
            _activate(conn,row)
        if state in ('disabled','maintenance') and any(fleet.counts(conn,row['id'])[k] for k in ('occupied','reserved')):
            raise HTTPException(409, 'У узла есть подключения. Используйте перенос.')
        conn.execute('UPDATE servers SET state=?,version=version+1 WHERE id=?',(state,row['id']))
        fleet.carry_version(conn,row['id'],row['version'],row['version']+1)
        return {'node':_node(conn,row['id'])}
    raise HTTPException(422, 'Неизвестное действие.')


async def command(data, actor, *, developer):
    if data.action in MUTATIONS and not developer:
        raise HTTPException(403, 'Управление узлами доступно разработчику.')
    if data.action not in MUTATIONS | {'snapshot','migration'}:
        raise HTTPException(422, 'Неизвестное действие.')
    if data.action in MUTATIONS and data.request_id is None:
        raise HTTPException(422, 'Для действия требуется request_id UUID.')
    fingerprint = hashlib.sha256(json.dumps([data.action,data.payload],sort_keys=True,separators=(',',':')).encode()).hexdigest()
    rid = str(data.request_id) if data.request_id else None
    async with service.lock:
        with db() as conn:
            cached = conn.execute('SELECT fingerprint,response FROM wg_fleet_requests WHERE actor=? AND request_id=?', (actor,rid)).fetchone() if rid else None
            if cached:
                if not hmac.compare_digest(cached['fingerprint'],fingerprint):
                    raise HTTPException(409,'Этот request_id уже использован для другого действия.')
                return json.loads(cached['response'])
        check = None
        if data.action=='check':
            with db() as conn:
                check = _load_node(conn,data.payload)
            try:
                import asyncio
                await asyncio.wait_for(providers.probe_server(check),timeout=30)
                check_result = ('ok', None)
            except Exception:
                check_result = ('error','ProviderCheckFailed')
        try:
            with db() as conn:
                conn.execute('BEGIN IMMEDIATE')
                # Protect also against a second process using the same bridge.
                cached = conn.execute('SELECT fingerprint,response FROM wg_fleet_requests WHERE actor=? AND request_id=?', (actor,rid)).fetchone() if rid else None
                if cached:
                    if cached['fingerprint']!=fingerprint:
                        raise HTTPException(409,'Этот request_id уже использован.')
                    return json.loads(cached['response'])
                if data.action=='snapshot':
                    result = snapshot(conn,developer=developer)
                elif check:
                    changed = conn.execute('UPDATE servers SET health=?,error_code=?,checked_at=? WHERE id=? AND version=?', (*check_result,int(time.time()),check['id'],check['version'])).rowcount
                    if not changed:
                        raise HTTPException(409,'Настройки сервера изменились во время проверки.')
                    result = {'node':_node(conn,check['id'])}
                else:
                    result = _execute(conn,data.action,data.payload)
                if data.action in MUTATIONS:
                    conn.execute('INSERT INTO wg_fleet_requests VALUES (?,?,?,?,?)', (actor,rid,fingerprint,json.dumps(result),int(time.time())))
                    target = data.payload.get('id') or result.get('node',{}).get('id')
                    conn.execute('INSERT INTO wg_fleet_audit(actor,action,target,created) VALUES (?,?,?,?)', (actor,data.action,target,int(time.time())))
                return result
        except (ValidationError,ValueError):
            # Pydantic error inputs may contain credentials. Never serialize them.
            raise HTTPException(422,'Проверьте поля узла и профиль подключения.') from None


@router.get('')
async def get_fleet(actor=Depends(admin)):
    with db() as conn:
        return snapshot(conn,developer=actor.get('role')=='owner')


@router.post('/commands')
async def local_command(data: Command, actor=Depends(admin)):
    return await command(data,'shop:'+str(actor.get('id','owner')),developer=actor.get('role')=='owner')


@router.post('/bridge')
async def bridge(request: Request):
    raw = await request.body()
    if len(raw)>16384:
        raise HTTPException(413,'Слишком большой запрос.')
    secret = os.getenv('FLOW_SHOP_BRIDGE_SECRET','')
    if (os.getenv('WG_FLEET_MANAGEMENT_ENABLED','false').lower()!='true'
            or not c.SECURE_COOKIE or not re.fullmatch('[0-9a-f]{64}',secret)):
        raise HTTPException(503,'Управление серверами из Mini App не настроено.')
    signature = request.headers.get('X-Pushok-Management','')
    try:
        timestamp, digest = signature.split(':',1)
        if not re.fullmatch(r'[0-9]{10}',timestamp) or abs(int(time.time())-int(timestamp))>60 or not re.fullmatch('[0-9a-f]{64}',digest):
            raise ValueError()
        expected = hmac.new(bytes.fromhex(secret),b'pushok-wg-fleet-management-v1:'+timestamp.encode()+b':'+raw,hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected,digest):
            raise ValueError()
        data = Envelope.model_validate_json(raw)
        if data.actor_role not in ('developer','admin'):
            raise ValueError()
    except (ValueError,ValidationError):
        raise HTTPException(401,'Недействительный запрос управления.') from None
    return await command(data,'miniapp:'+str(data.actor),developer=data.actor_role=='developer')


class Enrollment(BaseModel):
    model_config={'extra':'forbid'}
    id: str = Field(min_length=1,max_length=100)
    version: int = Field(ge=1,strict=True)


@router.post('/probes/enroll')
async def enroll(data: Enrollment, actor=Depends(admin)):
    if actor.get('role')!='owner':
        raise HTTPException(403,'Регистрация проверки доступна разработчику.')
    if not c.SECURE_COOKIE:
        raise HTTPException(409,'Передача ключа проверки требует HTTPS.')
    async with service.lock:
        with db() as conn:
            conn.execute('BEGIN IMMEDIATE')
            node=_load_node(conn,data.model_dump())
            _version(data.model_dump(),node)
            old=conn.execute('SELECT generation FROM wg_fleet_probes WHERE server_id=?',(node['id'],)).fetchone()
            token,probe_id=secrets.token_urlsafe(48),str(uuid.uuid4())
            enrollment=health.register_probe(conn,node['id'],node['version'],probe_id,old['generation']+1 if old else 1)
            conn.execute('DELETE FROM wg_fleet_failures WHERE server_id=?',(node['id'],))
            conn.execute('''INSERT INTO wg_fleet_probe_tokens VALUES (?,?,?,?)
                ON CONFLICT(server_id) DO UPDATE SET probe_id=excluded.probe_id,
                token_hash=excluded.token_hash,created=excluded.created''',
                (node['id'],probe_id,hashlib.sha256(token.encode()).hexdigest(),int(time.time())))
            return {**enrollment,'token':token}


def _probe_auth(conn,request):
    header=request.headers.get('authorization','')
    if not header.startswith('Bearer ') or not 32<=len(header[7:])<=200:
        raise HTTPException(401,'Недействительный ключ проверки.')
    row=conn.execute('SELECT * FROM wg_fleet_probe_tokens WHERE token_hash=?',
                     (hashlib.sha256(header[7:].encode()).hexdigest(),)).fetchone()
    if row is None:
        raise HTTPException(401,'Недействительный ключ проверки.')
    return dict(row)


@router.get('/probes/assignment')
async def probe_assignment(request: Request):
    with db() as conn:
        source=_probe_auth(conn,request)
        row=conn.execute('SELECT server_id,server_version,probe_id,generation FROM wg_fleet_probes WHERE server_id=?', (source['server_id'],)).fetchone()
        node=conn.execute('SELECT version FROM servers WHERE id=?',(source['server_id'],)).fetchone()
        if not row or row['server_version']!=node['version']:
            raise HTTPException(409,'После изменения профиля зарегистрируйте проверку заново.')
        return dict(row)


@router.post('/probes/report')
async def probe_report(request: Request):
    if len(await request.body())>4096:
        raise HTTPException(413,'Слишком большой отчёт.')
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        source=_probe_auth(conn,request)
        try:
            report=await request.json()
            result=health.ingest(conn,report,source['probe_id'])
            fleet.record_failure(conn,report)
        except (ValueError,health.HealthReportError):
            raise HTTPException(422,'Недействительный или устаревший отчёт проверки.') from None
        return {'accepted':True,'health':result}
