"""Отправка писем восстановления пароля через настроенный SMTP."""
import logging
import os
import re
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import parseaddr

log=logging.getLogger("pushok.mail")
def settings():
    host=os.getenv("SMTP_HOST","").strip()
    username=os.getenv("SMTP_USERNAME","")
    password=os.getenv("SMTP_PASSWORD","")
    sender=os.getenv("SMTP_FROM","").strip()
    configured=bool(host or username or password or sender)
    if not configured: return None
    if not host or not sender or bool(username)!=bool(password):
        raise RuntimeError("SMTP_HOST и SMTP_FROM обязательны; SMTP_USERNAME и SMTP_PASSWORD задаются вместе")
    if any(ch in host for ch in "\r\n"):
        raise RuntimeError("Некорректный SMTP_HOST")
    security=os.getenv("SMTP_SECURITY","starttls").strip().lower()
    if security not in ("starttls","ssl"): raise RuntimeError("SMTP_SECURITY должен быть starttls или ssl")
    try: port=int(os.getenv("SMTP_PORT","") or ("465" if security=="ssl" else "587"))
    except ValueError: raise RuntimeError("SMTP_PORT должен быть числом") from None
    if not 1<=port<=65535: raise RuntimeError("SMTP_PORT вне диапазона")
    if chr(10) in sender or chr(13) in sender: raise RuntimeError("Некорректный SMTP_FROM")
    _,address=parseaddr(sender)
    if not address or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+",address):
        raise RuntimeError("SMTP_FROM должен содержать корректный адрес отправителя")
    return {"host":host,"port":port,"security":security,"username":username,"password":password,"sender":sender}

def validate_configuration():
    return settings() is not None

def enabled():
    return settings() is not None

def send_password_reset(recipient, link):
    cfg=settings()
    if not cfg: return
    message=EmailMessage()
    message["Subject"]="Восстановление доступа к Pushok VPN"
    message["From"]=cfg["sender"]
    message["To"]=recipient
    message.set_content(
        "Поступил запрос на смену пароля для вашего аккаунта Pushok VPN.\n\n"
        "Чтобы задать новый пароль, откройте ссылку в течение 20 минут:\n"+link+
        "\n\nЕсли вы не запрашивали смену пароля, просто проигнорируйте это письмо.\n"
        "Не пересылайте ссылку: она позволяет сменить пароль аккаунта.")
    context=ssl.create_default_context()
    try:
        if cfg["security"]=="ssl":
            with smtplib.SMTP_SSL(cfg["host"],cfg["port"],timeout=12,context=context) as smtp:
                if cfg["username"]: smtp.login(cfg["username"],cfg["password"])
                smtp.send_message(message)
        else:
            with smtplib.SMTP(cfg["host"],cfg["port"],timeout=12) as smtp:
                smtp.ehlo()
                smtp.starttls(context=context)
                smtp.ehlo()
                if cfg["username"]: smtp.login(cfg["username"],cfg["password"])
                smtp.send_message(message)
    except Exception:
        # Не записываем в журнал адрес, текст письма, токен или SMTP-реквизиты.
        log.warning("Не удалось отправить письмо восстановления пароля")
