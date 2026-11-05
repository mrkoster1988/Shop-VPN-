"""Проверка SMTP без отправки письма по умолчанию."""
import argparse
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import parseaddr
import re
import smtplib
import ssl
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
load_dotenv(ROOT/".env")

from app import mail

def valid_address(value):
    _,address=parseaddr(value or "")
    return bool(address and re.fullmatch(r"[^\\s@]+@[^\\s@]+\\.[^\\s@]+",address))

def connect(cfg):
    context=ssl.create_default_context()
    if cfg["security"]=="ssl":
        smtp=smtplib.SMTP_SSL(cfg["host"],cfg["port"],timeout=12,context=context)
    else:
        smtp=smtplib.SMTP(cfg["host"],cfg["port"],timeout=12)
    try:
        smtp.ehlo()
        if cfg["security"]=="starttls":
            smtp.starttls(context=context)
            smtp.ehlo()
        if cfg["username"]:
            smtp.login(cfg["username"],cfg["password"])
        code,_=smtp.noop()
        if code!=250:
            raise RuntimeError("SMTP NOOP failed")
        return smtp
    except Exception:
        smtp.close()
        raise

def main():
    parser=argparse.ArgumentParser(description="Проверка SMTP из .env магазина")
    parser.add_argument("--send-to",help="явно отправить тестовое письмо на этот адрес")
    args=parser.parse_args()
    if args.send_to and not valid_address(args.send_to):
        parser.error("--send-to должен содержать корректный email")
    try:
        cfg=mail.settings()
    except RuntimeError as error:
        print(str(error),file=sys.stderr)
        return 2
    if not cfg:
        print("SMTP не настроен. Заполните параметры SMTP в .env.")
        return 2
    try:
        with connect(cfg) as smtp:
            print("SMTP-соединение, TLS и авторизация проверены.")
            if args.send_to:
                message=EmailMessage()
                message["Subject"]="Проверка почты Pushok VPN"
                message["From"]=cfg["sender"]
                message["To"]=args.send_to
                message.set_content("Тестовое письмо. Настройки SMTP магазина принимают отправку.")
                smtp.send_message(message)
                print("SMTP-сервер принял письмо. Проверьте папку «Входящие» и «Спам».")
            else:
                print("Письмо не отправлялось. Для проверки доставки укажите --send-to.")
        return 0
    except Exception as error:
        code=getattr(error,"smtp_code",None)
        detail=("код SMTP "+str(code)) if code else type(error).__name__
        print("Проверка SMTP не пройдена: "+detail,file=sys.stderr)
        return 1

if __name__=="__main__":
    raise SystemExit(main())
