import pytest
from fastapi import HTTPException
from app import bank_review, payment_policy

def test_review_off_by_default(monkeypatch):
    monkeypatch.delenv('BANK_REVIEW_MODE', raising=False)
    with pytest.raises(HTTPException) as error:
        bank_review.review()
    assert error.value.status_code == 404

def test_review_catalog_and_checkout(monkeypatch):
    monkeypatch.setenv('BANK_REVIEW_MODE', 'true')
    monkeypatch.setenv('BANK_REVIEW_CODE', '<review>')
    monkeypatch.setattr(bank_review.c, 'BASE_PATH', '/shop')
    plans = [{'id':'month','name':'<Месяц>','days':30,'price':399,'is_free':0},
             {'id':'free','name':'Бесплатный','days':1,'price':1,'is_free':1},
             {'id':'over','name':'Дорогой','days':30,'price':10001,'is_free':0}]
    monkeypatch.setattr(bank_review, 'all_rows',
        lambda sql, params=(): [p for p in plans if not params or p['id']==params[0]])
    html = bank_review.review().body.decode()
    assert '399 ₽' in html and '/shop/bank-review/payment/month' in html
    assert '&lt;Месяц&gt;' in html and '&lt;review&gt;' in html
    assert 'Дорогой' not in html and 'Бесплатный' not in html
    assert bank_review.NOTICE in bank_review.review_payment('month').body.decode()
    with pytest.raises(HTTPException) as error:
        bank_review.review_payment('over')
    assert error.value.status_code == 422
    with pytest.raises(HTTPException) as error:
        bank_review.review_payment('free')
    assert error.value.status_code == 404

@pytest.mark.parametrize('amount,valid', [(0,False),(1,True),(10000,True),(10001,False)])
def test_review_rub_bounds(monkeypatch,amount,valid):
    monkeypatch.setenv('BANK_REVIEW_MODE','true')
    if valid:
        bank_review.require_rub_amount(amount)
    else:
        with pytest.raises(HTTPException):
            bank_review.require_rub_amount(amount)

def test_review_blocks_new_rub_checkout(monkeypatch):
    monkeypatch.setenv('BANK_REVIEW_MODE','true')
    for method in ('manual','yookassa','sbp','platega'):
        with pytest.raises(HTTPException) as error:
            payment_policy.require_new(None,None,method)
        assert error.value.status_code == 409

def test_privacy_escapes_owner_text(monkeypatch):
    monkeypatch.setenv('BANK_PRIVACY_TEXT','<script>alert(1)</script>')
    html = bank_review.privacy().body.decode()
    assert '&lt;script&gt;' in html and '<script>' not in html
