"""Учётные записи сотрудников и роли доступа."""
import hmac, re, secrets, time
from fastapi import HTTPException, Request
from . import config as c
from .db import db
from .security import digest, password_hash, password_valid
ROLES=("owner","panel_operator","mtproxy_manager","finance","support")
ROLE_LABELS={"owner":"Владелец","panel_operator":"Оператор панелей","mtproxy_manager":"Менеджер MTProxy","finance":"Финансовый сотрудник","support":"Поддержка"}
EMAIL_RE=re.compile(r"^[^\\s@]+@[^\\s@]+\\.[^\\s@]+$")
def clean_email(v):
    v=v.strip().lower()
    if not EMAIL_RE.fullmatch(v): raise HTTPException(422,"Укажите корректный email")
    return v
def legacy_owner(request):
    token=request.headers.get("authorization","")
    if not c.ADMIN_TOKEN or not hmac.compare_digest(token,"Bearer "+c.ADMIN_TOKEN): return None
    with db() as conn: count=conn.execute("SELECT count(*) FROM staff_users WHERE enabled=1").fetchone()[0]
    return {"id":None,"email":"Первоначальная настройка","role":"owner"} if count==0 else None
def authorize(request: Request):
    actor=None; raw=request.cookies.get("staff_session","")
    if raw:
        with db() as conn:
            row=conn.execute("SELECT u.id,u.email,u.role FROM staff_sessions s JOIN staff_users u ON u.id=s.staff_id WHERE s.token_hash=? AND s.expires>? AND u.enabled=1",(digest(raw),int(time.time()))).fetchone()
        if row: actor=dict(row)
    if actor is None: actor=legacy_owner(request)
    if actor is None: raise HTTPException(401,"Войдите под учётной записью сотрудника")
    path=request.url.path.removeprefix(c.BASE_PATH); method=request.method.upper()
    request.state.admin_actor=actor
    r=actor["role"]
    if r!="owner" and method in ("GET","HEAD","OPTIONS"):
        if r in ("panel_operator","support"):
            ok=path in ("/api/admin/overview","/api/admin/diagnostics") or path.startswith(("/api/admin/orders","/api/admin/users")) or (r=="panel_operator" and path.startswith("/api/admin/servers")) or (r=="support" and path.startswith("/api/admin/support/tickets"))
        elif r=="finance":
            ok=path in ("/api/admin/overview",) or path.startswith("/api/admin/orders") or path.startswith("/api/admin/ads/finance") or path.startswith("/api/admin/ads/expenses") or path in ("/api/admin/ads/policy","/api/admin/ads/requests") or bool(re.fullmatch(r"/api/admin/ads/requests/[^/]+/payments",path))
        elif r=="mtproxy_manager":
            ok=path.startswith("/api/admin/ads/") and not any(x in path for x in ("/finance","/expenses","/payments"))
        else: ok=False
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
    with db() as conn: row=conn.execute("SELECT * FROM staff_users WHERE email=?",(email,)).fetchone()
    encoded=row["password"] if row else password_hash("dummy-password")
    if not row or not row["enabled"] or not password_valid(password,encoded): raise HTTPException(401,"Неверный email или пароль")
    raw=secrets.token_urlsafe(32); now=int(time.time())
    with db() as conn:
        fresh=conn.execute("SELECT enabled,password FROM staff_users WHERE id=?",(row["id"],)).fetchone()
        if not fresh or not fresh["enabled"] or fresh["password"]!=encoded: raise HTTPException(401,"Неверный email или пароль")
        conn.execute("INSERT INTO staff_sessions(token_hash,staff_id,created,expires) VALUES (?,?,?,?)",(digest(raw),row["id"],now,now+43200))
        conn.execute("UPDATE staff_users SET last_login=? WHERE id=?",(now,row["id"]))
    response.set_cookie("staff_session",raw,max_age=43200,httponly=True,secure=c.SECURE_COOKIE,samesite="lax",path=c.COOKIE_PATH)
    return {"ok":True,"role":row["role"],"role_label":ROLE_LABELS[row["role"]]}
def bootstrap(email,password,authorization):
    if not c.ADMIN_TOKEN or not hmac.compare_digest(authorization,"Bearer "+c.ADMIN_TOKEN): raise HTTPException(403,"Неверный ключ первоначальной настройки")
    if not 12<=len(password)<=128: raise HTTPException(422,"Пароль владельца должен содержать 12–128 символов")
    email=clean_email(email); now=int(time.time())
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute("SELECT 1 FROM staff_users WHERE enabled=1 LIMIT 1").fetchone(): raise HTTPException(409,"Учётная запись владельца уже создана")
        try: cur=conn.execute("INSERT INTO staff_users(email,password,role,enabled,created,updated) VALUES (?,?,'owner',1,?,?)",(email,password_hash(password),now,now))
        except Exception as e:
            if "UNIQUE" in str(e).upper(): raise HTTPException(409,"Email уже зарегистрирован") from None
            raise
        return {"ok":True,"staff_id":cur.lastrowid,"role":"owner","role_label":ROLE_LABELS["owner"]}
def list_staff():
    with db() as conn: rows=conn.execute("SELECT id,email,role,enabled,created,updated,last_login FROM staff_users ORDER BY created,id").fetchall()
    return {"staff":[{**dict(x),"enabled":bool(x["enabled"]),"role_label":ROLE_LABELS[x["role"]]} for x in rows]}
def create_staff(email,password,role,actor_id):
    if role not in ROLES or role=="owner": raise HTTPException(422,"Неизвестная роль")
    if not 12<=len(password)<=128: raise HTTPException(422,"Пароль сотрудника должен содержать 12–128 символов")
    now=int(time.time())
    try:
        with db() as conn:
            cur=conn.execute("INSERT INTO staff_users(email,password,role,enabled,created,updated,created_by) VALUES (?,?,?,1,?,?,?)",(clean_email(email),password_hash(password),role,now,now,actor_id))
            row=conn.execute("SELECT id,email,role,enabled,created,updated,last_login FROM staff_users WHERE id=?",(cur.lastrowid,)).fetchone()
    except Exception as e:
        if "UNIQUE" in str(e).upper(): raise HTTPException(409,"Email уже зарегистрирован") from None
        raise
    result=dict(row); result["enabled"]=bool(result["enabled"]); result["role_label"]=ROLE_LABELS[result["role"]]; return result
def update_staff(staff_id,enabled,role,actor_id):
    if role is not None and role not in ROLES: raise HTTPException(422,"Неизвестная роль")
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE"); row=conn.execute("SELECT * FROM staff_users WHERE id=?",(staff_id,)).fetchone()
        if not row: raise HTTPException(404,"Сотрудник не найден")
        if staff_id==actor_id and (enabled is False or (role and role!="owner")): raise HTTPException(409,"Нельзя отключить или понизить собственную учётную запись")
        en=int(row["enabled"] if enabled is None else enabled); ro=row["role"] if role is None else role
        if row["role"]=="owner" and (ro!="owner" or not en):
            count=conn.execute("SELECT count(*) FROM staff_users WHERE role='owner' AND enabled=1").fetchone()[0]
            if count<=1: raise HTTPException(409,"Нельзя отключить последнего активного владельца")
        conn.execute("UPDATE staff_users SET role=?,enabled=?,updated=? WHERE id=?",(ro,en,int(time.time()),staff_id))
        if not en: conn.execute("DELETE FROM staff_sessions WHERE staff_id=?",(staff_id,))
        saved=conn.execute("SELECT id,email,role,enabled,created,updated,last_login FROM staff_users WHERE id=?",(staff_id,)).fetchone()
    result=dict(saved); result["enabled"]=bool(result["enabled"]); result["role_label"]=ROLE_LABELS[result["role"]]; return result
def audit(actor,method,path,status):
    with db() as conn:
        conn.execute("INSERT INTO admin_audit(created,staff_id,actor_email,role,action,resource,status) VALUES (?,?,?,?,?,?,?)",(int(time.time()),actor.get("id"),actor["email"],actor["role"],method.upper(),path[:240],int(status)))
        conn.execute("DELETE FROM admin_audit WHERE id NOT IN (SELECT id FROM admin_audit ORDER BY id DESC LIMIT 5000)")
def audit_rows(limit):
    with db() as conn: rows=conn.execute("SELECT id,created,staff_id,actor_email,role,action,resource,status FROM admin_audit ORDER BY id DESC LIMIT ?",(min(500,max(1,int(limit))),)).fetchall()
    return {"events":[dict(r) for r in rows]}
