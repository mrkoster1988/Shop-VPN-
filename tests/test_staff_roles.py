"""New management roles, live policy checks and restricted moderation."""
import uuid

from app import config as c
from app.db import one, init_db
from test_shop import client, register
from test_ads import policy, submit

PASSWORD='fixture-staff-password-123'
ROOT='/api/admin/'


def bootstrap(client):
    response=client.post(ROOT+'auth/bootstrap',headers={'Authorization':'Bearer '+c.ADMIN_TOKEN},
                         json={'email':'developer@example.com','password':PASSWORD})
    assert response.status_code==200,response.text
    assert response.json()['role']=='owner'
    assert response.json()['role_label']=='Разработчик'
    return client.cookies.get('staff_session')


def login(client,role):
    result=client.post(ROOT+'auth/login',json={'email':role+'@example.com','password':PASSWORD})
    assert result.status_code==200,result.text
    assert result.json()['role']==role
    return client.cookies.get('staff_session')


def add_role(client,role):
    result=client.post(ROOT+'staff',json={'email':role+'@example.com','password':PASSWORD,'role':role})
    assert result.status_code==201,result.text
    assert result.json()['role']==role and result.json()['enabled'] is True
    return result.json()['id']


def use(client,token):
    client.cookies.clear()
    client.cookies.set('staff_session',token,domain='testserver.local',path='/')


def test_developer_assigns_roles_without_legacy_fallback(client):
    owner=bootstrap(client)
    aid=add_role(client,'administrator');mid=add_role(client,'moderator')
    init_db()
    assert one('SELECT enabled,role FROM staff_users WHERE id=?',(mid,))=={'enabled':0,'role':'support'}
    assert one('SELECT role,enabled FROM staff_role_assignments WHERE staff_id=?',(aid,))=={'role':'administrator','enabled':1}
    listing=client.get(ROOT+'staff').json()['staff']
    assert {x['role_label'] for x in listing}=={'Разработчик','Администратор','Модератор'}
    assert all('password' not in x for x in listing)
    login(client,'administrator')
    assert client.get(ROOT+'auth/me').json()['role_label']=='Администратор'
    for path in ('dashboard-summary','plans','servers','shop-settings','flow/wireguard/auto'):
        assert client.get(ROOT+path).status_code==200,path
    for path in ('staff','audit','role-policies','updates','ads/finance','ads/requests','bot-menu'):
        assert client.get(ROOT+path).status_code==403,path
    assert client.post(ROOT+'orders/'+str(uuid.uuid4())+'/confirm').status_code==403
    assert client.patch(ROOT+'staff/'+str(mid),json={'role':'owner'}).status_code==403
    assert client.put(ROOT+'role-policies/administrator',json={'version':0,'permissions':['payments']}).status_code==403
    use(client,owner)
    assert client.get(ROOT+'updates').status_code==200


def test_live_permission_matrix_version_and_role_boundaries(client):
    owner=bootstrap(client);add_role(client,'administrator');add_role(client,'moderator')
    initial=client.get(ROOT+'role-policies').json()
    assert len(initial['roles'])==2 and 'catalog' in initial['labels']
    administrator=login(client,'administrator')
    assert client.get(ROOT+'plans').status_code==200
    use(client,owner)
    changed=client.put(ROOT+'role-policies/administrator',json={'version':0,'permissions':['moderation','dashboard']})
    assert changed.status_code==200 and changed.json()['version']==1
    assert client.put(ROOT+'role-policies/administrator',json={'version':0,'permissions':[]}).status_code==409
    assert client.put(ROOT+'role-policies/moderator',json={'version':0,'permissions':['payments']}).status_code==422
    assert client.put(ROOT+'role-policies/moderator',json={'version':0,'permissions':['moderation','moderation']}).status_code==422
    assert client.put(ROOT+'role-policies/administrator',json={'version':1,'permissions':['developer']}).status_code==422
    use(client,administrator)
    assert client.get(ROOT+'plans').status_code==403
    assert client.get(ROOT+'moderation/requests').status_code==200
    assert client.get(ROOT+'auth/me').json()['permissions']==['dashboard','moderation']
    use(client,owner)
    assert client.put(ROOT+'role-policies/administrator',json={'version':1,'permissions':[]}).status_code==200
    use(client,administrator)
    assert client.get(ROOT+'dashboard-summary').status_code==403
    assert client.get(ROOT+'auth/me').json()['permissions']==[]


def test_moderation_does_not_expose_financial_or_customer_details(client):
    register(client)
    assert policy(client,accepting=True).status_code==200
    request=submit(client,channel='@channel_name',comment='fixture-comment').json()
    owner=bootstrap(client);add_role(client,'moderator');login(client,'moderator')
    queue=client.get(ROOT+'moderation/requests')
    assert queue.status_code==200
    row=queue.json()['requests'][0]
    assert set(row)=={'id','channel','comment','state','reason','version','created','updated'}
    assert row['id']==request['id']
    for path in ('users','orders','plans','ads/requests','ads/finance','servers','updates','staff','role-policies'):
        assert client.get(ROOT+path).status_code==403,path
    url=ROOT+'moderation/requests/'+row['id']
    assert client.put(url,json={'version':row['version'],'state':'rejected','reason':''}).status_code==422
    rejected=client.put(url,json={'version':row['version'],'state':'rejected','reason':'Не соответствует правилам'})
    assert rejected.status_code==200
    assert rejected.json()['state']=='rejected' and 'payment' not in rejected.json()
    assert client.put(url,json={'version':row['version'],'state':'approved','reason':''}).status_code==409
    assert client.get(ROOT+'moderation/requests').json()['total']==0
    assert client.get(ROOT+'moderation/requests?state=rejected').json()['total']==1
    assert one("SELECT role FROM admin_audit WHERE resource=? ORDER BY id DESC LIMIT 1",
               ('/api/admin/moderation/requests/'+row['id'],))['role']=='moderator'
    use(client,owner)
    assert client.get(ROOT+'ads/requests').json()['requests'][0]['state']=='rejected'


def test_role_change_revokes_old_session_and_last_developer_protected(client):
    owner=bootstrap(client);mid=add_role(client,'moderator')
    dev_id=one("SELECT id FROM staff_users WHERE role='owner'")['id']
    assert client.patch(ROOT+'staff/'+str(dev_id),json={'enabled':False}).status_code==409
    assert client.patch(ROOT+'staff/'+str(dev_id),json={'role':'moderator'}).status_code==409
    mod=login(client,'moderator')
    use(client,owner)
    changed=client.patch(ROOT+'staff/'+str(mid),json={'role':'administrator'})
    assert changed.status_code==200 and changed.json()['role']=='administrator'
    use(client,mod)
    assert client.get(ROOT+'auth/me').status_code==401
    new_session=client.post(ROOT+'auth/login',
        json={'email':'moderator@example.com','password':PASSWORD})
    assert new_session.status_code==200 and new_session.json()['role']=='administrator'
    fresh=client.cookies.get('staff_session')
    use(client,owner)
    assert client.patch(ROOT+'staff/'+str(mid),json={'enabled':False}).json()['enabled'] is False
    use(client,fresh)
    assert client.get(ROOT+'auth/me').status_code==401


def test_existing_developer_and_staff_sessions_survive_additive_init(client):
    owner=bootstrap(client)
    legacy=add_role(client,'support')
    before=one('SELECT role,enabled FROM staff_users WHERE id=?',(legacy,))
    init_db();init_db()
    assert one('SELECT role,enabled FROM staff_users WHERE id=?',(legacy,))==before
    use(client,owner)
    assert client.get(ROOT+'auth/me').json()['role_label']=='Разработчик'
    assert client.get(ROOT+'staff').status_code==200


def test_role_editor_does_not_echo_invalid_staff_password(client):
    bootstrap(client)
    response=client.post(ROOT+'staff',json={'email':'wrong@example.com','password':'short-key','role':'moderator'})
    assert response.status_code==422 and 'short-key' not in response.text
    assert one('SELECT id FROM staff_users WHERE email=?',('wrong@example.com',)) is None


def test_individual_ceiling_only_limits_selected_employee_and_updates_live(client):
    owner=bootstrap(client);aid=add_role(client,'administrator')
    other=client.post(ROOT+'staff',json={'email':'other-admin@example.com','password':PASSWORD,'role':'administrator'}).json()
    admin_session=login(client,'administrator')
    assert client.get(ROOT+'plans').status_code==200
    url=ROOT+'staff/'+str(aid)+'/permissions'
    assert client.get(url).status_code==403
    use(client,owner)
    card=client.get(url).json()
    assert card['inherit'] is True and card['role_version']==0
    body={'version':card['version'],'role_version':card['role_version'],'role':'administrator','inherit':False,'permissions':['customers']}
    assert client.put(url,json={**body,'permissions':['payments']}).status_code==422
    changed=client.put(url,json=body)
    assert changed.status_code==200 and changed.json()['effective']==['customers']
    assert client.put(url,json=body).status_code==409
    assert client.get(ROOT+'staff/'+str(other['id'])+'/permissions').json()['inherit'] is True
    use(client,admin_session)
    assert client.get(ROOT+'plans').status_code==403
    assert client.get(ROOT+'users').status_code==200
    assert client.get(ROOT+'auth/me').json()['permissions']==['customers']
    use(client,owner)
    assert client.put(ROOT+'role-policies/administrator',json={'version':0,'permissions':['catalog']}).status_code==200
    assert client.put(url,json={**body,'version':1,'inherit':True,'permissions':[]}).status_code==409
    use(client,admin_session)
    assert client.get(ROOT+'users').status_code==403
    assert client.get(ROOT+'auth/me').json()['permissions']==[]
    use(client,owner)
    fresh=client.get(url).json()
    assert client.put(url,json={'version':fresh['version'],'role_version':fresh['role_version'],'role':'administrator','inherit':True,'permissions':[]}).status_code==200
    use(client,admin_session)
    assert client.get(ROOT+'plans').status_code==200


def test_role_specific_settings_access_and_individual_restrictions(client):
    owner=bootstrap(client);aid=add_role(client,'administrator');add_role(client,'moderator')
    admin_session=login(client,'administrator')
    assert client.get(ROOT+'runtime/appearance').status_code==200
    assert client.get(ROOT+'runtime/sales').status_code==403
    assert client.get(ROOT+'runtime/notifications').status_code==403
    use(client,owner)
    policy={'version':0,'permissions':['store_settings','sales_controls','notifications']}
    assert client.put(ROOT+'role-policies/administrator',json=policy).status_code==200
    use(client,admin_session)
    assert client.get(ROOT+'runtime/sales').status_code==200
    data=client.get(ROOT+'runtime/sales').json();data.pop('updated');data['new_sales']=False
    assert client.put(ROOT+'runtime/sales',json=data).status_code==200
    use(client,owner)
    url=ROOT+'staff/'+str(aid)+'/permissions'
    limits={'version':0,'role_version':1,'role':'administrator','inherit':False,'permissions':['notifications']}
    assert client.put(url,json=limits).status_code==200
    use(client,admin_session)
    assert client.get(ROOT+'runtime/sales').status_code==403
    assert client.get(ROOT+'runtime/notifications').status_code==200
    login(client,'moderator')
    assert client.get(ROOT+'runtime/appearance').status_code==403
    assert client.put(ROOT+'runtime/sales',json=data).status_code==403
