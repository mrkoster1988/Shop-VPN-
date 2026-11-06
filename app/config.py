import os
import re
from urllib.parse import urlsplit
from pathlib import Path
from dotenv import load_dotenv
from cryptography.fernet import Fernet

load_dotenv()
DATA = Path(os.getenv('DATA_DIR', 'data'))
DATA.mkdir(parents=True, exist_ok=True)
DB = DATA / 'shop.db'
PUBLIC_URL = os.getenv('PUBLIC_URL', 'http://localhost:8000').rstrip('/')
_public = urlsplit(PUBLIC_URL)
if (_public.scheme not in ('http', 'https') or not _public.netloc
        or _public.username or _public.password or _public.query or _public.fragment
        or not re.fullmatch(r'(?:/[A-Za-z0-9_-]+)*', _public.path)):
    raise RuntimeError('PUBLIC_URL must be an http(s) URL with an optional path such as /shop')
BASE_PATH = _public.path
COOKIE_PATH = BASE_PATH + '/'
SECURE_COOKIE = _public.scheme == 'https'
BOT_TOKEN = os.getenv('BOT_TOKEN', '')
BOT_USERNAME = os.getenv('BOT_USERNAME', '').lstrip('@')
ADMIN_TOKEN = os.getenv('ADMIN_TOKEN', '')
PAYMENT_MODE = os.getenv('PAYMENT_MODE', 'manual')
PARTNER_WEBHOOK_SECRET = os.getenv('PARTNER_WEBHOOK_SECRET', '')
PARTNER_PROMO_CODE = os.getenv('PARTNER_PROMO_CODE', '').strip().upper()
if PARTNER_PROMO_CODE and not re.fullmatch(r'[A-Z0-9][A-Z0-9_-]{3,31}', PARTNER_PROMO_CODE):
    raise RuntimeError('PARTNER_PROMO_CODE must be 4–32 ASCII letters, digits, hyphens or underscores')
PLANS = [
    {'id': 'month', 'name': 'На месяц', 'days': 30, 'price': 199, 'stars': 150},
    {'id': 'quarter', 'name': 'На три месяца', 'days': 90, 'price': 499, 'stars': 350},
    {'id': 'year', 'name': 'На год', 'days': 365, 'price': 1499, 'stars': 1000},
]

def cipher():
    key = os.getenv('ENCRYPTION_KEY', '')
    if not key:
        raise RuntimeError('Set ENCRYPTION_KEY: python scripts/setup.py')
    return Fernet(key.encode())

def encrypt(text):
    return cipher().encrypt(text.encode()).decode()

def decrypt(text):
    return cipher().decrypt(text.encode()).decode()
