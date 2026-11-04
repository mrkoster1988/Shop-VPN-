'use strict';
const basePath = document.querySelector('meta[name="shop-base"]').content;
const $ = id => document.getElementById(id);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const statusLabel = {pending:'Ожидает оплаты',checkout:'Оплата не завершена',paid:'Оплачено · готовим VPN',active:'Доступ выдан',canceled:'Отменён'};
const money=(amount,currency)=>amount.toLocaleString('ru-RU')+' '+(currency==='XTR'?'★':'₽');
let catalog, me, protocol = 'xui', selectedPlan, register = false, toastTimer;
async function api(path, data, method) {
  const response = await fetch(basePath + path, {method:method || (data ? 'POST':'GET'), credentials:'same-origin',
    headers:data ? {'Content-Type':'application/json'} : {}, body:data ? JSON.stringify(data) : undefined});
  const result = await response.json();
  if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : 'Проверьте введённые данные');
  return result;
}
function toast(message) { $('toast').textContent = message; $('toast').hidden = false; clearTimeout(toastTimer); toastTimer = setTimeout(() => $('toast').hidden = true, 6000); }
function show(id) { if (!$(id).open) $(id).showModal(); }
function date(ts) { return new Date(ts*1000).toLocaleDateString('ru-RU'); }
function protocolName(id) { return id === 'wg' ? 'WireGuard' : 'VLESS · 3x-ui'; }
function renderPlans() {
  $('protocols').innerHTML = catalog.protocols.map(p => `<button class="${p.id===protocol?'chosen':''}" data-protocol="${p.id}" aria-pressed="${p.id===protocol}">${esc(p.name)}${p.available?'':' · скоро'}</button>`).join('');
  const available = catalog.protocols.find(p => p.id===protocol).available;
  $('provider-note').textContent = available ? '' : 'Подключение этого протокола ещё не настроено. Покупки временно недоступны.';
  $('plan-list').innerHTML = catalog.plans.map((p,i) => `<article class="plan ${i===1?'featured':''}">${i===1?'<span class="badge">УДОБНЫЙ ВЫБОР</span>':''}<h3>${esc(p.name)}</h3><div class="price">${p.price.toLocaleString('ru-RU')} <small>${catalog.currency==='XTR'?'★':'₽'}</small></div><p>${p.days} дней доступа · ${esc(protocolName(protocol))}</p><ul><li>Личное подключение</li><li>Настройки и QR-код в кабинете</li><li>Продление с сохранением дней</li></ul><button data-plan="${p.id}" ${available?'':'disabled'}>${available?'Выбрать тариф ↗':'Скоро'}</button></article>`).join('');
}
async function refreshMe() {
  try { me = await api('/api/me'); const updated=await api('/api/catalog'); if(!catalog || updated.currency!==catalog.currency) { catalog=updated; renderPlans(); } $('account-btn').textContent = 'Мой кабинет ↗'; renderAccount(); }
  catch { me = null; $('account-btn').textContent = 'Личный кабинет ↗'; }
}
function renderAccount() {
  if (!me) return;
  $('identity').textContent = me.email || 'Telegram · ' + me.telegram_id;
  $('subscriptions').innerHTML = me.subscriptions.length ? me.subscriptions.map(s => {
    const active = s.state === 'active' && s.expires*1000 > Date.now();
    return `<article class="subscription"><h3>${esc(protocolName(s.protocol))}</h3><p>${active?'Доступ до '+date(s.expires):s.state==='pending'?'Подключение готовится':'Подписка закончилась'}</p>${active?`<button class="small" data-config="${s.id}">Подключить устройство ↗</button>`:''}</article>`;
  }).join('') : '<p>Подписок пока нет. Выберите тариф на главной странице.</p>';
  $('orders').innerHTML = me.orders.length ? me.orders.map(o => `<div class="order"><div><b>${esc(protocolName(o.protocol))} · ${money(o.amount,o.currency)}</b><br><code>${esc(o.id)}</code><br>${date(o.created)}</div><div><span class="status ${esc(o.status)}">${esc(statusLabel[o.status] || o.status)}</span>${o.status==='pending'?`<br><button class="text-link" data-order="${o.id}">Оплатить / реквизиты</button>`:''}</div></div>`).join('') : '<p>Заказов пока нет.</p>';
}
async function beginCheckout(id) {
  selectedPlan = catalog.plans.find(p => p.id===id);
  await refreshMe();
  selectedPlan = catalog.plans.find(p => p.id===id);
  if (!me) { show('auth-dialog'); return; }
  $('checkout-info').textContent = `${selectedPlan.name} / ${protocolName(protocol)} / ${money(selectedPlan.price,catalog.currency)}`;
  $('payment-result').replaceChildren(); $('checkout-form').hidden = false;
  $('terms-text').textContent=catalog.terms; $('accept-terms').checked=false; $('accept-terms').required=catalog.payment_mode==='stars';
  $('receipt-label').hidden = !catalog.receipts; $('receipt-label').querySelector('input').required = catalog.receipts;
  $('receipt-label').querySelector('input').value = me.email || '';
  show('checkout-dialog');
}
function paymentResult(order) {
  $('checkout-form').hidden = true;
  $('payment-result').replaceChildren();
  const paragraph = document.createElement('p');
  paragraph.textContent = `Заказ: ${order.id}\nСумма: ${money(order.amount,order.currency)}\n${statusLabel[order.status] || order.status}\n\n${order.instructions || ''}`;
  $('payment-result').append(paragraph);
  if (order.payment_url && order.status === 'pending') {
    const link = document.createElement('a');
    const url = new URL(order.payment_url);
    if (url.protocol === 'https:') { link.href = url.href; link.textContent = 'Перейти к оплате ↗'; link.className = 'button'; link.target = '_blank'; link.rel = 'noopener noreferrer'; if(order.method==='stars' && window.Telegram?.WebApp?.initData) { link.onclick=event=>{event.preventDefault(); window.Telegram.WebApp.openInvoice(url.href,()=>refreshMe());}; } $('payment-result').append(link); }
  }
  const button = document.createElement('button'); button.className = 'text-link'; button.textContent = 'Проверить статус';
  button.onclick = async () => { try { paymentResult(await api('/api/orders/'+order.id)); await refreshMe(); } catch(e) { toast(e.message); } };
  $('payment-result').append(document.createElement('br'), button);
}
$('protocols').onclick = event => { const b=event.target.closest('[data-protocol]'); if(b) { protocol=b.dataset.protocol; renderPlans(); } };
$('plan-list').onclick = event => { const b=event.target.closest('[data-plan]'); if(b) beginCheckout(b.dataset.plan).catch(e=>toast(e.message)); };
document.querySelectorAll('[data-close]').forEach(button => button.onclick=()=>$(button.dataset.close).close());
$('account-btn').onclick = async () => { await refreshMe(); show(me?'account-dialog':'auth-dialog'); };
$('auth-switch').onclick = () => { register=!register; $('auth-title').textContent=register?'Добро пожаловать.':'С возвращением.'; $('auth-submit').textContent=register?'Создать аккаунт →':'Войти →'; $('auth-switch').textContent=register?'Уже есть аккаунт? Войти':'Создать аккаунт'; $('auth-form').password.autocomplete=register?'new-password':'current-password'; };
$('auth-form').onsubmit = async event => {
  event.preventDefault(); const b=$('auth-submit'); b.disabled=true;
  try { const form=new FormData(event.target); await api('/api/auth/'+(register?'register':'login'),Object.fromEntries(form)); $('auth-dialog').close(); await refreshMe(); if(selectedPlan) await beginCheckout(selectedPlan.id); else show('account-dialog'); }
  catch(e) { toast(e.message); } finally { b.disabled=false; }
};
$('checkout-form').onsubmit = async event => {
  event.preventDefault(); const b=event.target.querySelector('button'); b.disabled=true;
  try { const email=new FormData(event.target).get('receipt_email'); const order=await api('/api/orders',{plan:selectedPlan.id,protocol,receipt_email:email||null,accept_terms:$('accept-terms').checked}); paymentResult(order); await refreshMe(); }
  catch(e) { toast(e.message); } finally { b.disabled=false; }
};
$('subscriptions').onclick = async event => {
  const b=event.target.closest('[data-config]'); if(!b)return;
  try { const sid=b.dataset.config, result=await api('/api/subscriptions/'+sid+'/config'); $('config-value').value=result.config;
    $('config-qr').src=basePath+'/api/subscriptions/'+sid+'/qr'; $('download-config').href=basePath+'/api/subscriptions/'+sid+'/download';
    $('config-help').textContent=result.protocol==='wg'?'В приложении WireGuard выберите добавление туннеля: импорт файла или QR-кода.':'Установите приложение с поддержкой VLESS, например v2rayNG или Hiddify, и импортируйте ссылку либо QR-код.';
    show('config-dialog'); } catch(e){toast(e.message);}
};
$('orders').onclick = async event => { const b=event.target.closest('[data-order]'); if(!b)return; try { const order=await api('/api/orders/'+b.dataset.order); $('checkout-info').textContent=protocolName(order.protocol); paymentResult(order); show('checkout-dialog'); } catch(e){toast(e.message);} };
$('copy-config').onclick = async () => { try { await navigator.clipboard.writeText($('config-value').value); toast('Настройки скопированы'); } catch { $('config-value').select(); toast('Выделено. Скопируйте настройки вручную.'); } };
$('refresh').onclick = async () => { await refreshMe(); toast('Кабинет обновлён'); };
$('logout').onclick = async () => { try { await api('/api/auth/logout',{}); me=null; selectedPlan=null; catalog=await api('/api/catalog'); renderPlans(); $('account-dialog').close(); $('account-btn').textContent='Личный кабинет ↗'; $('config-value').value=''; $('config-qr').removeAttribute('src'); } catch(e){toast(e.message);} };
(async () => {
  try {
    catalog=await api('/api/catalog'); protocol=catalog.protocols.find(p=>p.available)?.id || 'xui'; renderPlans(); $('support').textContent=catalog.support;
    if(catalog.bot_username) { for(const id of ['bot-link','auth-bot']) { $(id).href='https://t.me/'+encodeURIComponent(catalog.bot_username); $(id).hidden=false; } }
    const tg=window.Telegram?.WebApp;
    if(tg?.initData) { tg.ready(); tg.expand(); await api('/api/auth/telegram',{init_data:tg.initData}); }
    await refreshMe();
    if(new URLSearchParams(location.search).has('payment')) { history.replaceState(null,'','/'); show(me?'account-dialog':'auth-dialog'); }
    setInterval(async()=>{ if(me && ( $('account-dialog').open || $('checkout-dialog').open )) await refreshMe(); },15000);
  } catch(e) { toast(e.message); }
})();
