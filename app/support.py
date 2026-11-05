"""Обращения пользователей и переписка с поддержкой."""
import hashlib
import json
import time
import uuid
from typing import Literal
from fastapi import HTTPException
from pydantic import BaseModel, Field
from .db import db

class TicketCreate(BaseModel):
    model_config={"extra":"forbid"}
    request_id: uuid.UUID
    subject: str=Field(min_length=4,max_length=120)
    message: str=Field(min_length=5,max_length=2000)
    order_id: str|None=Field(default=None,max_length=80)
    def normalized(self):
        data=self.model_dump()
        for key in ("subject","message"):
            data[key]=data[key].strip()
            if not data[key]: raise HTTPException(422,"Заполните тему и сообщение")
        return data

class UserReply(BaseModel):
    model_config={"extra":"forbid"}
    body: str=Field(min_length=1,max_length=2000)
    version: int=Field(ge=1,strict=True)

class StaffReply(UserReply):
    pass

class TicketStatus(BaseModel):
    model_config={"extra":"forbid"}
    status: Literal["open","waiting_customer","waiting_staff","closed"]
    version: int=Field(ge=1,strict=True)

def _case(conn, case_id, user_id=None):
    if user_id is None:
        row=conn.execute("SELECT * FROM support_cases WHERE id=?",(case_id,)).fetchone()
    else:
        row=conn.execute("SELECT * FROM support_cases WHERE id=? AND user_id=?",(case_id,user_id)).fetchone()
    if not row: raise HTTPException(404,"Обращение не найдено")
    result=dict(row)
    result.pop("fingerprint",None);result.pop("request_id",None)
    return result

def _messages(conn, case_id):
    rows=conn.execute("SELECT id,actor_type,body,created FROM support_messages WHERE case_id=? ORDER BY id",(case_id,)).fetchall()
    return [{"id":r["id"],"author":"Вы" if r["actor_type"]=="customer" else "Поддержка","body":r["body"],"created":r["created"]} for r in rows]

def create(user_id, data):
    values=data.normalized()
    request_id=str(values["request_id"])
    canonical={k:(str(v) if isinstance(v,uuid.UUID) else v) for k,v in values.items()}
    fingerprint=hashlib.sha256(json.dumps(canonical,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    now=int(time.time())
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if values["order_id"] and not conn.execute("SELECT 1 FROM orders WHERE id=? AND user_id=?",(values["order_id"],user_id)).fetchone():
            raise HTTPException(404,"Заказ не найден в вашем кабинете")
        prior=conn.execute("SELECT id,fingerprint FROM support_cases WHERE user_id=? AND request_id=?",(user_id,request_id)).fetchone()
        if prior:
            if prior["fingerprint"]!=fingerprint: raise HTTPException(409,"Повторный запрос отличается от первого. Обновите страницу.")
            return _case(conn,prior["id"],user_id)
        case_id=str(uuid.uuid4())
        conn.execute("""INSERT INTO support_cases(id,user_id,order_id,request_id,fingerprint,subject,status,version,created,updated)
            VALUES (?,?,?,?,?,?,'open',1,?,?)""",(case_id,user_id,values["order_id"],request_id,fingerprint,values["subject"],now,now))
        conn.execute("INSERT INTO support_messages(case_id,actor_type,actor_id,body,created) VALUES (?,'customer',?,?,?)",(case_id,user_id,values["message"],now))
        return _case(conn,case_id,user_id)

def list_user(user_id, page=1, page_size=20):
    page_size=min(50,max(1,page_size)); offset=(page-1)*page_size
    with db() as conn:
        total=conn.execute("SELECT count(*) FROM support_cases WHERE user_id=?",(user_id,)).fetchone()[0]
        rows=conn.execute("""SELECT c.id,c.order_id,c.subject,c.status,c.version,c.created,c.updated,
            (SELECT body FROM support_messages m WHERE m.case_id=c.id ORDER BY m.id DESC LIMIT 1) AS last_message
            FROM support_cases c WHERE c.user_id=? ORDER BY c.updated DESC,c.id DESC LIMIT ? OFFSET ?""",
            (user_id,page_size,offset)).fetchall()
    return {"cases":[dict(r) for r in rows],"total":total,"page":page,"page_size":page_size}

def get_user(case_id,user_id):
    with db() as conn:
        case=_case(conn,case_id,user_id)
        return {**case,"messages":_messages(conn,case_id)}

def reply_user(case_id,user_id,data):
    body=data.body.strip()
    if not body: raise HTTPException(422,"Сообщение не может быть пустым")
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        case=_case(conn,case_id,user_id)
        if case["version"]!=data.version: raise HTTPException(409,"Обращение обновлено. Перезагрузите переписку.")
        now=int(time.time())
        conn.execute("INSERT INTO support_messages(case_id,actor_type,actor_id,body,created) VALUES (?,'customer',?,?,?)",(case_id,user_id,body,now))
        conn.execute("UPDATE support_cases SET status='waiting_staff',version=version+1,updated=? WHERE id=?",(now,case_id))
        return _case(conn,case_id,user_id)

def list_staff(status=None,page=1,page_size=25):
    page_size=min(100,max(1,page_size)); offset=(page-1)*page_size
    where=" WHERE status=?" if status else ""
    args=[status] if status else []
    with db() as conn:
        total=conn.execute("SELECT count(*) FROM support_cases"+where,args).fetchone()[0]
        rows=conn.execute("""SELECT id,user_id,order_id,subject,status,version,created,updated,
            (SELECT body FROM support_messages m WHERE m.case_id=c.id ORDER BY m.id DESC LIMIT 1) AS last_message
            FROM support_cases c"""+where+" ORDER BY updated DESC,id DESC LIMIT ? OFFSET ?",args+[page_size,offset]).fetchall()
    return {"cases":[dict(r) for r in rows],"total":total,"page":page,"page_size":page_size}

def get_staff(case_id):
    with db() as conn:
        case=_case(conn,case_id)
        return {**case,"messages":_messages(conn,case_id)}

def reply_staff(case_id,data):
    body=data.body.strip()
    if not body: raise HTTPException(422,"Сообщение не может быть пустым")
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        case=_case(conn,case_id)
        if case["version"]!=data.version: raise HTTPException(409,"Обращение обновлено. Обновите карточку.")
        now=int(time.time())
        conn.execute("INSERT INTO support_messages(case_id,actor_type,actor_id,body,created) VALUES (?,'staff',NULL,?,?)",(case_id,body,now))
        conn.execute("UPDATE support_cases SET status='waiting_customer',version=version+1,updated=? WHERE id=?",(now,case_id))
        return _case(conn,case_id)

def set_status(case_id,data):
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        case=_case(conn,case_id)
        if case["version"]!=data.version: raise HTTPException(409,"Обращение обновлено. Обновите карточку.")
        now=int(time.time())
        conn.execute("UPDATE support_cases SET status=?,version=version+1,updated=? WHERE id=?",(data.status,now,case_id))
        return _case(conn,case_id)
