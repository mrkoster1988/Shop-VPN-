"""Учётные записи сотрудников и роли доступа."""
import hmac, re, secrets, time
from fastapi import HTTPException, Request
from . import config as c
from .db import db
from .security import digest, password_hash, password_valid
from . import staff_permissions as permissions
ROLES=("owner","administrator","moderator","panel_operator","mtproxy_manager","finance","support")
ROLE_LABELS={"owner":"Разработчик","administrator":"Администратор","moderator":"Модератор","panel_operator":"Оператор панелей","mtproxy_manager":"Менеджер MTProxy","finance":"Финансовый сотрудник","support":"Поддержка"}
EMAIL_RE=re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
EFFECTIVE="""SELECT u.id,u.email,u.password,COALESCE(a.role,u.role) AS role,
    COALESCE(a.enabled,u.enabled) AS enabled,u.created,u.updated,u.created_by,u.last_login
    FROM staff_users u LEFT JOIN staff_role_assignments a ON a.staff_id=u.id"""

def effective(conn,staff_id):
    return conn.execute(EFFECTIVE+' WHERE u.id=?',(staff_id,)).fetchone()

def active_count(conn):
    return conn.execute("""SELECT count(*) FROM staff_users u
        LEFT JOIN staff_role_assignments a ON a.staff_id=u.id
        WHERE COALESCE(a.enabled,u.enabled)=1""").fetchone()[0]

def safe_staff(row):
    result={k:row[k] for k in ('id','email','role','enabled','created','updated','last_login')}
    result['enabled']=bool(result['enabled'])
    result['role_label']=ROLE_LABELS[result['role']]
    return result

def role_response(conn,row):
    result={'ok':True,'id':row['id'],'role':row['role'],'role_label':ROLE_LABELS[row['role']]}
    if row['role'] in permissions.ALLOWED:
        result['permissions']=permissions.effective_permissions(conn,row['role'],row['id'])
    return result

def clean_email(v):
    v=v.strip().lower()
    if not EMAIL_RE.fullmatch(v): raise HTTPException(422,"Укажите корректный email")
    return v
def legacy_owner(request):
    token=request.headers.get("authorization","")
    if not c.ADMIN_TOKEN or not hmac.compare_digest(token,"Bearer "+c.ADMIN_TOKEN): return None
    if not c.DB.is_file(): raise HTTPException(503,"База данных магазина временно недоступна")
    with db() as conn: count=active_count(conn)
    return {"id":None,"email":"Первоначальная настройка","role":"owner"} if count==0 else None
def authorize(request: Request):
    actor=None; raw=request.cookies.get("staff_session","")
    if raw:
        with db() as conn:
            row=conn.execute(EFFECTIVE+""" JOIN staff_sessions s ON s.staff_id=u.id
                WHERE s.token_hash=? AND s.expires>? AND COALESCE(a.enabled,u.enabled)=1""",
                (digest(raw),int(time.time()))).fetchone()
            if row:
                actor={k:row[k] for k in ('id','email','role')}
                if row['role'] in permissions.ALLOWED:
                    actor['permissions']=permissions.effective_permissions(conn,row['role'],row['id'])
    if actor is None: actor=legacy_owner(request)
    if actor is None: raise HTTPException(401,"Войдите под учётной записью сотрудника")
    path=request.url.path.removeprefix(c.BASE_PATH); method=request.method.upper()
    request.state.admin_actor=actor
    r=actor["role"]
    if r in permissions.ALLOWED:
        if not permissions.permits(actor.get('permissions',[]),path,method):
            raise HTTPException(403,"Для этого действия недостаточно прав")
        return actor
    if path=='/api/admin/auth/me' and method in ('GET','HEAD'):
        return actor
    if r!="owner" and method in ("GET","HEAD","OPTIONS"):
        if r in ("panel_operator","support"):
            ok=path in ("/api/admin/overview","/api/admin/diagnostics") or path.startswith(("/api/admin/orders","/api/admin/users")) or (r=="panel_operator" and path.startswith("/api/admin/servers")) or (r=="support" and path.startswith("/api/admin/support/tickets"))
        elif r=="finance":
            ok=path in ("/api/admin/overview","/api/admin/commerce-analytics") or path.startswith("/api/admin/orders") or path.startswith("/api/admin/ads/finance") or path.startswith("/api/admin/ads/expenses") or path in ("/api/admin/ads/policy","/api/admin/ads/requests") or bool(re.fullmatch(r"/api/admin/ads/requests/[^/]+/payments",path))
        elif r=="mtproxy_manager":
            ok=path.startswith("/api/admin/ads/") and not any(x in path for x in ("/finance","/expenses","/payments"))
        else: ok=False
        if r=="panel_operator" and (path in ("/api/admin/protocols","/api/admin/server-routing") or re.fullmatch(r"/api/admin/server-routing/history/[^/]+",path)): ok=True
        if not ok: raise HTTPException(403,"Просмотр этих данных для вашей роли не разрешён")
    if r!="owner" and method not in ("GET","HEAD","OPTIONS"):
        ok=False
        if r=="panel_operator": ok=path.startswith("/api/admin/servers") or bool(re.fullmatch(r"/api/admin/orders/[^/]+/retry",path))
        elif r=="mtproxy_manager": ok=path.startswith("/api/admin/ads/") and not any(x in path for x in ("/finance","/expenses","/payments"))
        elif r=="finance": ok=bool(re.fullmatch(r"/api/admin/orders/[^/]+/(confirm|reject)",path)) or (path.startswith("/api/admin/ads/") and any(x in path for x in ("/finance","/expenses","/payments")))
        elif r=="support": ok=bool(re.fullmatch(r"/api/admin/orders/[^/]+/retry",path)) or path.startswith("/api/admin/support/tickets/")
        if not ok: raise HTTPException(403,"Для этого действия недостаточно прав")
    return actor
def login(email,password,response):
    email=clean_email(email)
    with db() as conn: row=conn.execute(EFFECTIVE+" WHERE u.email=?",(email,)).fetchone()
    encoded=row["password"] if row else password_hash("dummy-password")
    if not row or not row["enabled"] or not password_valid(password,encoded): raise HTTPException(401,"Неверный email или пароль")
    raw=secrets.token_urlsafe(32); now=int(time.time())
    with db() as conn:
        fresh=effective(conn,row["id"])
        if not fresh or not fresh["enabled"] or fresh["password"]!=encoded: raise HTTPException(401,"Неверный email или пароль")
        conn.execute("INSERT INTO staff_sessions(token_hash,staff_id,created,expires) VALUES (?,?,?,?)",(digest(raw),row["id"],now,now+43200))
        conn.execute("UPDATE staff_users SET last_login=? WHERE id=?",(now,row["id"]))
        result=role_response(conn,fresh)
    response.set_cookie("staff_session",raw,max_age=43200,httponly=True,secure=c.SECURE_COOKIE,samesite="lax",path=c.COOKIE_PATH)
    return result
def bootstrap(email,password,authorization):
    if not c.ADMIN_TOKEN or not hmac.compare_digest(authorization,"Bearer "+c.ADMIN_TOKEN): raise HTTPException(403,"Неверный ключ первоначальной настройки")
    if not 12<=len(password)<=128: raise HTTPException(422,"Пароль владельца должен содержать 12–128 символов")
    email=clean_email(email); now=int(time.time())
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if active_count(conn): raise HTTPException(409,"Учётная запись владельца уже создана")
        try: cur=conn.execute("INSERT INTO staff_users(email,password,role,enabled,created,updated) VALUES (?,?,'owner',1,?,?)",(email,password_hash(password),now,now))
        except Exception as e:
            if "UNIQUE" in str(e).upper(): raise HTTPException(409,"Email уже зарегистрирован") from None
            raise
        return {"ok":True,"staff_id":cur.lastrowid,"role":"owner","role_label":ROLE_LABELS["owner"]}
def list_staff():
    with db() as conn:
        return {"staff":[safe_staff(row) for row in conn.execute(EFFECTIVE+' ORDER BY u.created,u.id')]}

def create_staff(email,password,role,actor_id):
    if role not in ROLES or role=='owner':
        raise HTTPException(422,"Неизвестная роль")
    if not 12<=len(password)<=128:
        raise HTTPException(422,"Пароль сотрудника должен содержать 12–128 символов")
    now=int(time.time())
    extended=role in permissions.ALLOWED
    try:
        with db() as conn:
            conn.execute('BEGIN IMMEDIATE')
            cur=conn.execute("""INSERT INTO staff_users(email,password,role,enabled,created,updated,created_by)
                VALUES (?,?,?,?,?,?,?)""",
                (clean_email(email),password_hash(password),'support' if extended else role,
                 0 if extended else 1,now,now,actor_id))
            if extended:
                conn.execute('INSERT INTO staff_role_assignments(staff_id,role,enabled) VALUES (?,?,1)',
                             (cur.lastrowid,role))
            return safe_staff(effective(conn,cur.lastrowid))
    except Exception as e:
        if "UNIQUE" in str(e).upper():
            raise HTTPException(409,"Email уже зарегистрирован") from None
        raise

def update_staff(staff_id,enabled,role,actor_id):
    if role is not None and role not in ROLES:
        raise HTTPException(422,"Неизвестная роль")
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        row=effective(conn,staff_id)
        if not row: raise HTTPException(404,"Сотрудник не найден")
        en=int(row['enabled'] if enabled is None else enabled)
        ro=row['role'] if role is None else role
        if staff_id==actor_id and (not en or ro!='owner'):
            raise HTTPException(409,"Нельзя отключить или понизить собственную учётную запись")
        if row['role']=='owner' and row['enabled'] and (ro!='owner' or not en):
            count=conn.execute("SELECT count(*) FROM staff_users WHERE role='owner' AND enabled=1").fetchone()[0]
            if count<=1:raise HTTPException(409,"Нельзя отключить последнего активного разработчика")
        if ro in permissions.ALLOWED:
            conn.execute("UPDATE staff_users SET role='support',enabled=0,updated=? WHERE id=?",
                         (int(time.time()),staff_id))
            conn.execute("""INSERT INTO staff_role_assignments(staff_id,role,enabled) VALUES (?,?,?)
                ON CONFLICT(staff_id) DO UPDATE SET role=excluded.role,enabled=excluded.enabled""",
                (staff_id,ro,en))
        else:
            conn.execute('DELETE FROM staff_role_assignments WHERE staff_id=?',(staff_id,))
            conn.execute('UPDATE staff_users SET role=?,enabled=?,updated=? WHERE id=?',
                         (ro,en,int(time.time()),staff_id))
        if ro!=row['role']:
            permissions.invalidate_on_role_change(conn,staff_id)
        if not en or ro!=row['role']:
            conn.execute('DELETE FROM staff_sessions WHERE staff_id=?',(staff_id,))
        return safe_staff(effective(conn,staff_id))
def audit(actor,method,path,status):
    with db() as conn:
        conn.execute("INSERT INTO admin_audit(created,staff_id,actor_email,role,action,resource,status) VALUES (?,?,?,?,?,?,?)",(int(time.time()),actor.get("id"),actor["email"],actor["role"],method.upper(),path[:240],int(status)))
        conn.execute("DELETE FROM admin_audit WHERE id NOT IN (SELECT id FROM admin_audit ORDER BY id DESC LIMIT 5000)")
def audit_rows(limit):
    with db() as conn: rows=conn.execute("SELECT id,created,staff_id,actor_email,role,action,resource,status FROM admin_audit ORDER BY id DESC LIMIT ?",(min(500,max(1,int(limit))),)).fetchall()
    return {"events":[dict(r) for r in rows]}
