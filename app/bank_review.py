"""Bank review storefront: no payment creation and no simulated success."""
import os
from html import escape
from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse
from . import config as c, shop_settings
from .db import all_rows

router = APIRouter()
NOTICE = 'Оплата временно недоступна. Платёжная система проходит подключение.'
MIN_RUB = 1
MAX_RUB = 10_000

def enabled():
    return os.getenv('BANK_REVIEW_MODE', '').lower() == 'true'

def require_rub_amount(amount):
    if enabled() and not MIN_RUB <= amount <= MAX_RUB:
        raise HTTPException(422, 'Допустимая сумма операции: от 1 до 10 000 ₽.')

def link(path, label):
    return f'<a href="{escape(c.BASE_PATH + path, quote=True)}">{escape(label)}</a>'

def page(title, content):
    nav = ' · '.join(link(p, t) for p, t in [
        ('/', 'Магазин'), ('/legal/privacy', 'Политика конфиденциальности'),
        ('/legal/terms', 'Пользовательское соглашение'), ('/legal/support', 'Поддержка')])
    return HTMLResponse(f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{escape(title)} — Pushok</title>
<style>body{{margin:0;background:#f5f8fa;color:#13283d;font:16px/1.65 system-ui,sans-serif}}main{{max-width:800px;margin:auto;padding:24px}}nav{{margin-bottom:24px}}a{{color:#006b70}}article{{background:white;padding:20px;margin:12px 0;border-radius:12px}}button,.buy{{display:inline-block;background:#006b70;color:white;padding:10px 18px;border-radius:9px;border:0;text-decoration:none}}pre{{white-space:pre-wrap;overflow-wrap:anywhere;font:inherit}}.price{{font-size:1.4rem;font-weight:bold}}</style></head>
<body><main><nav>{nav}</nav><h1>{escape(title)}</h1>{content}</main></body></html>""",
headers={'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'})

@router.get('/legal/privacy')
def privacy():
    value = os.getenv('BANK_PRIVACY_TEXT', '').strip()
    return page('Политика конфиденциальности', '<pre>' + escape(value or 'Документ ещё не опубликован владельцем.') + '</pre>')

@router.get('/legal/terms')
def terms():
    value = shop_settings.terms().strip()
    return page('Пользовательское соглашение', '<pre>' + escape(value or 'Документ ещё не опубликован владельцем.') + '</pre>')

@router.get('/legal/support')
def support():
    value = shop_settings.public()['support']
    return page('Поддержка', '<pre>' + escape(value) + '</pre>')

@router.get('/bank-review')
def review():
    if not enabled():
        raise HTTPException(404)
    plans = all_rows('SELECT id,name,days,price,is_free FROM plans WHERE enabled=1 ORDER BY position,id')
    cards = []
    for plan in plans:
        if plan['is_free'] or not MIN_RUB <= plan['price'] <= MAX_RUB:
            continue
        cards.append(f'<article><h2>{escape(str(plan["name"]))}</h2><p>Подписка на VPN-подключение, срок {int(plan["days"])} дн.</p><p class="price">{int(plan["price"])} ₽</p><a class="buy" href="{escape(c.BASE_PATH, quote=True)}/bank-review/payment/{int(plan["id"])}">Оплатить</a></article>')
    marker = os.getenv('BANK_REVIEW_CODE', '').strip()
    return page('Тарифы и оплата', '<p>Проверка подключения платёжной системы. Реальные списания здесь не выполняются.</p>' +
                ''.join(cards or ['<p>Платные тарифы пока не опубликованы.</p>']) +
                (f'<p>Код проверки: {escape(marker)}</p>' if marker else ''))

@router.get('/bank-review/payment/{plan_id}')
def review_payment(plan_id: int):
    if not enabled():
        raise HTTPException(404)
    plans = all_rows('SELECT name,days,price,is_free FROM plans WHERE id=? AND enabled=1', (plan_id,))
    if not plans or plans[0]['is_free']:
        raise HTTPException(404)
    plan = plans[0]
    require_rub_amount(plan['price'])
    return page('Оплата', f'<article><h2>{escape(str(plan["name"]))}</h2><p>Срок: {int(plan["days"])} дн.</p><p class="price">{int(plan["price"])} ₽</p><p role="status">{NOTICE}</p>{link("/bank-review", "Вернуться к тарифам")}</article>')
