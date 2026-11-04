'use strict';
const basePath = document.querySelector('meta[name="shop-base"]').content;
const $ = id => document.getElementById(id);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const statusLabel = {pending:'Ожидает оплаты',checkout:'Оплата не завершена',paid:'Оплачено · готовим VPN',active:'Доступ выдан',canceled:'Отменён'};
const money=(amount,currency)=>amount.toLocaleString('ru-RU')+' '+(currency==='XTR'?'★':'₽');
let catalog, me, protocol = 'xui', selectedPlan, register = false, toastTimer;
let sessionEpoch = 0, meRequest = 0, connectionRequest = 0, connection = null, connectionTimer;
let deviceSubscription=null, deviceRequest=0, deviceIntent=null, deviceCache=[], rotationIntents=new Map();
async function api(path, data, method) {
  const epoch = sessionEpoch;
  const response = await fetch(basePath + path, {method:method || (data ? 'POST':'GET'), credentials:'same-origin',
    headers:data ? {'Content-Type':'application/json'} : {}, body:data ? JSON.stringify(data) : undefined});
  const result = await response.json();
  if (epoch !== sessionEpoch) throw new Error('Сессия изменилась. Повторите действие.');
  if (!response.ok) throw Object.assign(new Error(typeof result.detail === 'string' ? result.detail : 'Проверьте введённые данные'), {status:response.status});
  return result;
}
function toast(message) { $('toast').textContent = message; $('toast').hidden = false; clearTimeout(toastTimer); toastTimer = setTimeout(() => $('toast').hidden = true, 6000); }
function show(id) { if (!$(id).open) $(id).showModal(); }
function date(ts) { return new Date(ts*1000).toLocaleDateString('ru-RU'); }
function protocolName(id) { return id === 'wg' ? 'WireGuard' : 'VLESS'; }
function profileAllowance(limit) { return limit===0 ? 'Только основной профиль' : 'Основной + до '+limit+' дополнительных профилей'; }
function renderPlans() {
  $('protocols').innerHTML = catalog.protocols.map(p => `<button class="${p.id===protocol?'chosen':''}" data-protocol="${p.id}" aria-pressed="${p.id===protocol}">${esc(protocolName(p.id))}${p.available?'':' · скоро'}</button>`).join('');
  const available = catalog.protocols.find(p => p.id===protocol).available;
  $('provider-note').textContent = available ? '' : 'Подключение этого протокола ещё не настроено. Покупки временно недоступны.';
  $('plan-list').innerHTML = catalog.plans.length ? catalog.plans.map((p,i) => `<article class="plan ${i===1?'featured':''}">${i===1?'<span class="badge">УДОБНЫЙ ВЫБОР</span>':''}<h3>${esc(p.name)}</h3><div class="price">${p.price.toLocaleString('ru-RU')} <small>${catalog.currency==='XTR'?'★':'₽'}</small></div><p>${p.days} дней доступа · ${esc(protocolName(protocol))}</p><ul><li>${esc(profileAllowance(p.device_limit ?? 4))}</li><li>Настройки и QR-код в кабинете</li><li>Продление с сохранением дней</li></ul><button data-plan="${p.id}" ${available?'':'disabled'}>${available?'Выбрать тариф ↗':'Скоро'}</button></article>`).join('') : '<p class="plans-empty">Тарифы временно недоступны. Пожалуйста, обратитесь в поддержку.</p>';
}
function clearConnection() {
  connectionRequest++; connection = null; clearTimeout(connectionTimer);
  $('config-value').value = ''; $('config-qr').removeAttribute('src');
  $('config-qr-panel').hidden = true; $('config-details').open = false;
  $('download-config').removeAttribute('href'); $('download-config').setAttribute('aria-disabled','true');
  $('copy-config').disabled = true; $('toggle-config-qr').disabled = true;
  $('toggle-config-qr').textContent = 'Показать QR-код';
  for (const id of ['config-summary','config-status','config-help','config-support']) $(id).textContent = '';
  $('connection-steps').replaceChildren();
}
function clearPrivate() {
  me = null; selectedPlan = null;
  $('devices-dialog').close(); clearDevices();
  $('config-dialog').close(); clearConnection();
  $('account-dialog').close(); $('checkout-dialog').close();
  for (const id of ['identity','subscriptions','orders','payment-result','checkout-info']) $(id).replaceChildren();
  $('auth-form').reset(); $('receipt-label').querySelector('input').value = '';
  $('account-btn').textContent = 'Личный кабинет ↗';
}
async function refreshMe() {
  const request = ++meRequest, epoch = sessionEpoch;
  let customer;
  try { customer = await api('/api/me'); }
  catch (error) { if (error.status === 401) customer = null; else throw error; }
  const nextCatalog = await api('/api/catalog');
  if (request !== meRequest || epoch !== sessionEpoch) return;
  if (me && me.id !== customer?.id) { sessionEpoch++; clearPrivate(); }
  else if (!customer) clearPrivate();
  me = customer; catalog = nextCatalog; renderPlans();
  $('account-btn').textContent = me ? 'Мой кабинет ↗' : 'Личный кабинет ↗';
  if (connection) {
    const current = me?.subscriptions.find(s=>s.id===connection.id);
    if (!current || current.state!=='active' || current.expires*1000<=Date.now()) {
      $('config-dialog').close(); clearConnection();
    } else if (Math.min(current.expires,connection.deviceExpires || Infinity)!==connection.expires) {
      connection.expires=Math.min(current.expires,connection.deviceExpires || Infinity);
      $('config-summary').textContent=protocolName(current.protocol)+' · доступ до '+date(connection.expires);
      scheduleConnectionExpiry();
    }
  }
  if (me) renderAccount();
  if (deviceSubscription) await refreshDevices();
}
function renderAccount() {
  if (!me) return;
  $('identity').textContent = me.email || 'Telegram · ' + me.telegram_id;
  $('subscriptions').innerHTML = me.subscriptions.length ? me.subscriptions.map(s => {
    const active = s.state === 'active' && s.expires*1000 > Date.now();
    return `<article class="subscription"><h3>${esc(protocolName(s.protocol))}</h3><p>${active?'Доступ до '+date(s.expires):s.state==='pending'?'Подключение готовится':'Подписка закончилась'}</p><p>${esc(profileAllowance(s.device_limit ?? 4))}</p>${active?`<button class="small" data-config="${esc(s.id)}">Основной профиль ↗</button>`:''}${s.state!=='pending'?` <button class="small secondary" data-devices="${esc(s.id)}">Устройства</button><button class="text-link" data-renew="${esc(s.protocol)}">${active?'Продлить подписку':'Возобновить подписку'}</button>`:''}</article>`;
  }).join('') : '<p>Подписок пока нет. Выберите тариф на главной странице.</p>';
  $('orders').innerHTML = me.orders.length ? me.orders.map(o => `<div class="order"><div><b>${esc(protocolName(o.protocol))} · ${money(o.amount,o.currency)}</b><br><code>${esc(o.id)}</code><br>${date(o.created)}</div><div><span class="status ${esc(o.status)}">${esc(statusLabel[o.status] || o.status)}</span>${o.status==='pending'?`<br><button class="text-link" data-order="${esc(o.id)}">Оплатить / реквизиты</button>`:''}</div></div>`).join('') : '<p>Заказов пока нет.</p>';
}
async function beginCheckout(id) {
  selectedPlan = catalog.plans.find(p => p.id===id);
  await refreshMe();
  selectedPlan = catalog.plans.find(p => p.id===id);
  if (!selectedPlan) throw new Error('Тариф больше недоступен. Выберите другой тариф.');
  if (!me) { show('auth-dialog'); return; }
  $('checkout-info').textContent = `${selectedPlan.name} / ${selectedPlan.days} дней / ${protocolName(protocol)} / ${money(selectedPlan.price,catalog.currency)} / ${profileAllowance(selectedPlan.device_limit ?? 4)}`;
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
  paragraph.textContent = `Заказ: ${order.id}\nТариф: ${order.plan_name || protocolName(order.protocol)} · ${order.days} дней\n${profileAllowance(order.device_limit ?? 4)}\nСумма: ${money(order.amount,order.currency)}\n${statusLabel[order.status] || order.status}\n\n${order.instructions || ''}`;
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
$('account-btn').onclick = async () => { try { await refreshMe(); show(me?'account-dialog':'auth-dialog'); } catch(e) { toast(e.message); } };
$('auth-switch').onclick = () => { register=!register; $('auth-title').textContent=register?'Добро пожаловать.':'С возвращением.'; $('auth-submit').textContent=register?'Создать аккаунт →':'Войти →'; $('auth-switch').textContent=register?'Уже есть аккаунт? Войти':'Создать аккаунт'; $('auth-form').password.autocomplete=register?'new-password':'current-password'; };
$('auth-form').onsubmit = async event => {
  event.preventDefault(); const b=$('auth-submit'); b.disabled=true;
  try { const form=new FormData(event.target); await api('/api/auth/'+(register?'register':'login'),Object.fromEntries(form)); sessionEpoch++; $('auth-form').reset(); $('auth-dialog').close(); await refreshMe(); if(selectedPlan) await beginCheckout(selectedPlan.id); else show('account-dialog'); }
  catch(e) { toast(e.message); } finally { b.disabled=false; }
};
$('checkout-form').onsubmit = async event => {
  event.preventDefault(); const b=event.target.querySelector('button'); b.disabled=true;
  try { const email=new FormData(event.target).get('receipt_email'); const order=await api('/api/orders',{plan:selectedPlan.id,plan_version:selectedPlan.version,protocol,receipt_email:email||null,accept_terms:$('accept-terms').checked}); paymentResult(order); await refreshMe(); }
  catch(e) {
    if (e.status===409) {
      selectedPlan=null; $('checkout-dialog').close();
      try { catalog=await api('/api/catalog'); renderPlans(); } catch {}
    }
    toast(e.message);
  } finally { b.disabled=false; }
};
function detectedPlatform() {
  const device = (navigator.userAgentData?.platform || navigator.platform || '') + ' ' + (navigator.userAgent || '');
  if (/android/i.test(device)) return 'android';
  if (/iphone|ipad|ipod/i.test(device) || (/mac/i.test(device) && navigator.maxTouchPoints>1)) return 'ios';
  if (/win/i.test(device)) return 'windows';
  if (/mac/i.test(device)) return 'macos';
  if (/linux/i.test(device)) return 'linux';
  return 'android';
}
function connectionInstructions() {
  if (!connection) return;
  const platform = $('config-platform').value;
  const mobile = platform==='android' || platform==='ios';
  const wg = connection.protocol==='wg';
  const install = wg ? 'Установите приложение WireGuard для выбранного устройства.' : 'Установите VPN-клиент с поддержкой VLESS для выбранного устройства.';
  const source = platform==='ios' || platform==='macos' ? 'Ищите приложение в App Store или на официальном сайте его разработчика.' : platform==='android' ? 'Используйте магазин приложений или официальный сайт разработчика.' : 'Скачайте клиент с официального сайта его разработчика для вашей системы.';
  const importing = wg ? (platform==='linux' ? 'Скачайте файл .conf. Импортируйте его в установленный клиент WireGuard; при ручной настройке обратитесь к администратору.' : 'Скачайте файл .conf. В WireGuard добавьте туннель и выберите импорт из файла.') : 'Нажмите «Копировать настройки». В VPN-клиенте выберите добавление профиля или импорт из буфера обмена. Можно также скачать ссылку как .txt.';
  const alternative = mobile ? 'Если настройки открыты на другом экране, покажите QR-код и отсканируйте его внутри VPN-клиента.' : 'Для телефона можно открыть QR-код на этом компьютере и отсканировать его в VPN-клиенте телефона.';
  const titles = ['Установите приложение','Импортируйте настройки','Включите VPN'];
  const steps = [install+' '+source, importing+' '+alternative, 'Выберите импортированный профиль и включите подключение. Разрешите добавление VPN, если устройство запросит это. Затем откройте сайт в браузере и проверьте интернет.'];
  $('connection-steps').innerHTML = steps.map((text,i)=>`<li><h3>${titles[i]}</h3><p>${esc(text)}</p></li>`).join('');
  $('config-help').textContent = wg ? 'Импортируйте именно файл .conf или сканируйте код внутри WireGuard. Не используйте этот профиль в VLESS-клиенте.' : 'Клиент должен поддерживать параметры вашей VLESS-ссылки. Скопируйте ссылку целиком, без лишних пробелов. Обычная камера телефона может не распознать VPN-конфигурацию.';
}
function connectionReady() {
  if (!connection || connection.expires*1000<=Date.now()) {
    clearConnection(); $('config-status').textContent = 'Подписка недоступна. Обновите кабинет и проверьте срок доступа.';
    return false;
  }
  return true;
}
function scheduleConnectionExpiry() {
  clearTimeout(connectionTimer);
  const request=connectionRequest;
  connectionTimer=setTimeout(()=>{
    if (request!==connectionRequest || !connection) return;
    if (connection.expires*1000>Date.now()) { scheduleConnectionExpiry(); return; }
    clearConnection(); $('config-status').textContent='Срок подписки истёк. Обновите кабинет или продлите доступ.';
  },Math.min(2147483647,Math.max(0,connection.expires*1000-Date.now())));
}
async function openConnection(sid, device=null) {
  clearConnection();
  const sub = me?.subscriptions.find(s=>s.id===sid);
  if (!sub || sub.state!=='active' || sub.expires*1000<=Date.now()) throw new Error('Обновите кабинет и проверьте срок подписки.');
  const request = connectionRequest;
  $('config-platform').value = detectedPlatform();
  $('config-status').textContent = 'Загружаем настройки…'; show('config-dialog');
  try {
    const path=device ? '/api/devices/'+encodeURIComponent(device.id) : '/api/subscriptions/'+encodeURIComponent(sid);
    const result = await api(path+'/config');
    if (request!==connectionRequest || !$('config-dialog').open) return;
    if (result.protocol!==sub.protocol || typeof result.config!=='string' || !result.config) throw new Error('Не удалось получить настройки. Обновите кабинет.');
    connection = {id:sid,deviceId:device?.id,deviceExpires:device?.expires,protocol:sub.protocol,expires:Math.min(sub.expires,device?.expires || Infinity),path};
    if (!connectionReady()) return;
    $('config-value').value = result.config;
    $('config-summary').textContent = (device ? device.name+' · ' : '')+protocolName(sub.protocol)+' · доступ до '+date(connection.expires);
    $('config-status').textContent = 'Настройки готовы. Следуйте трём шагам ниже.';
    $('config-support').textContent = catalog.support;
    $('download-config').href = basePath+path+'/download';
    $('download-config').setAttribute('aria-disabled','false');
    $('copy-config').disabled = false; $('toggle-config-qr').disabled = false;
    connectionInstructions();
    scheduleConnectionExpiry();
  } catch(error) {
    if (request===connectionRequest && $('config-dialog').open) { clearConnection(); $('config-status').textContent = error.message; }
  }
}
$('config-dialog').addEventListener('close', clearConnection);
$('config-platform').onchange = connectionInstructions;
$('download-config').onclick = event => { if (!connectionReady()) event.preventDefault(); };
$('toggle-config-qr').onclick = () => {
  if (!connectionReady()) return;
  const visible = $('config-qr-panel').hidden;
  $('config-qr-panel').hidden = !visible;
  $('toggle-config-qr').textContent = visible ? 'Скрыть QR-код' : 'Показать QR-код';
  if (visible) $('config-qr').src = basePath+connection.path+'/qr';
  else $('config-qr').removeAttribute('src');
};
$('config-qr').onerror = () => {
  if (!connection) return;
  $('config-qr').removeAttribute('src'); $('config-qr-panel').hidden=true;
  $('toggle-config-qr').textContent='Показать QR-код';
  $('config-status').textContent='QR-код не загрузился. Используйте копирование или скачивание настроек.';
};
$('subscriptions').onclick = event => {
  const devices=event.target.closest('[data-devices]');
  if (devices) { openDevices(devices.dataset.devices).catch(e=>toast(e.message)); return; }
  const renew=event.target.closest('[data-renew]');
  if (renew) {
    const item=catalog.protocols.find(p=>p.id===renew.dataset.renew);
    if (!item?.available) { toast('Продление временно недоступно. Обратитесь в поддержку.'); return; }
    protocol=item.id; selectedPlan=null; renderPlans(); $('account-dialog').close();
    $('plans').scrollIntoView({behavior:'smooth',block:'start'}); toast('Выберите срок продления. Оставшиеся дни сохранятся.'); return;
  }
  const button=event.target.closest('[data-config]');
  if (button) openConnection(button.dataset.config).catch(e=>toast(e.message));
};
function clearDevices() {
  deviceRequest++; deviceSubscription=null; deviceIntent=null; deviceCache=[]; rotationIntents.clear();
  $('device-form').reset(); $('devices-list').replaceChildren(); $('devices-status').textContent='';
}
$('devices-dialog').addEventListener('close',clearDevices);
async function openDevices(sid) {
  clearDevices();deviceSubscription=sid;show('devices-dialog');await refreshDevices();
}
async function refreshDevices() {
  if (!deviceSubscription) return;
  const request=++deviceRequest,sid=deviceSubscription;
  const result=await api('/api/subscriptions/'+encodeURIComponent(sid)+'/devices');
  if (request!==deviceRequest || sid!==deviceSubscription) return;
  deviceCache=result.devices;
  const parent=me?.subscriptions.find(s=>s.id===sid);
  $('device-form').hidden=!parent || parent.state!=='active' || parent.expires*1000<=Date.now() || result.devices.length>=result.max_extra;
  const labels={pending:'Готовим профиль',active:'Профиль выдан',revoking:'Отключаем доступ',revoked:'Доступ отозван',rotating:'Меняем ключ'};
  $('devices-list').innerHTML=result.devices.map(d=>`<article class="subscription"><h3>${esc(d.name)}</h3><p>${esc(labels[d.state] || d.state)}${d.retrying?' · повторим после ошибки':''}${d.state==='active'&&!d.ready?' · срок доступа истёк':''}</p><div class="actions">${d.ready?`<button class="small" data-device-config="${esc(d.id)}">Подключить ↗</button><button class="small secondary" data-device-rotate="${esc(d.id)}">Перевыпустить ключ</button>`:''}${['pending','active','rotating'].includes(d.state)?`<button class="small secondary" data-device-revoke="${esc(d.id)}">Отозвать доступ</button>`:''}</div></article>`).join('') || '<p>Дополнительных профилей пока нет.</p>';
  $('devices-status').textContent='Дополнительные профили: '+result.devices.length+' из '+result.max_extra+'. Основной профиль — отдельно.'+(result.devices.length>result.max_extra?' Уже выданные профили работают; новые недоступны до освобождения лимита.':'');
  if (connection?.deviceId) {
    const current=result.devices.find(d=>d.id===connection.deviceId);
    if (!current?.ready || !parent) { $('config-dialog').close();clearConnection(); }
    else { connection.deviceExpires=current.expires;connection.expires=Math.min(current.expires,parent.expires);$('config-summary').textContent=current.name+' · '+protocolName(current.protocol)+' · доступ до '+date(connection.expires);scheduleConnectionExpiry(); }
  }
}
$('devices-refresh').onclick=()=>refreshDevices().catch(e=>toast(e.message));
$('device-form').onsubmit=async event=>{
  event.preventDefault();const button=event.target.querySelector('button');button.disabled=true;
  const name=String(new FormData(event.target).get('name') || '').trim(),sid=deviceSubscription;
  if (!deviceIntent || deviceIntent.name!==name) deviceIntent={name,request_id:crypto.randomUUID()};
  const intent=deviceIntent;
  try {
    await api('/api/subscriptions/'+encodeURIComponent(sid)+'/devices',intent);
    if (sid!==deviceSubscription || intent!==deviceIntent) return;
    deviceIntent=null;$('device-form').reset();await refreshDevices();
  } catch(e) { if(sid===deviceSubscription) $('devices-status').textContent=e.message; }
  finally { button.disabled=false; }
};
$('devices-list').onclick=async event=>{
  const connect=event.target.closest('[data-device-config]');
  if(connect){const device=deviceCache.find(d=>d.id===connect.dataset.deviceConfig);if(device?.ready)await openConnection(deviceSubscription,device).catch(e=>toast(e.message));return;}
  const rotate=event.target.closest('[data-device-rotate]');
  if(rotate){
    if(!window.confirm('Старый ключ будет отключён. Новые настройки придётся импортировать заново. При сбое потребуется дождаться повтора. Перевыпустить?'))return;
    const did=rotate.dataset.deviceRotate;rotate.disabled=true;
    try {
      if(!rotationIntents.has(did))rotationIntents.set(did,crypto.randomUUID());
      await api('/api/devices/'+encodeURIComponent(did)+'/rotate',{request_id:rotationIntents.get(did)});
      rotationIntents.delete(did);await refreshDevices();
    } catch(e){toast(e.message);rotate.disabled=false;}
    return;
  }
  const revoke=event.target.closest('[data-device-revoke]');if(!revoke)return;
  if(!window.confirm('Отозвать доступ этого устройства? Его текущий ключ будет отключён.'))return;
  revoke.disabled=true;
  try {await api('/api/devices/'+encodeURIComponent(revoke.dataset.deviceRevoke),null,'DELETE');await refreshDevices();}
  catch(e){toast(e.message);revoke.disabled=false;}
};
$('orders').onclick = async event => { const b=event.target.closest('[data-order]'); if(!b)return; try { const order=await api('/api/orders/'+b.dataset.order); $('checkout-info').textContent=protocolName(order.protocol); paymentResult(order); show('checkout-dialog'); } catch(e){toast(e.message);} };
$('copy-config').onclick = async () => {
  if (!connectionReady()) return;
  const request = connectionRequest;
  try { await navigator.clipboard.writeText($('config-value').value); if (request===connectionRequest) toast('Настройки скопированы. Импортируйте их в VPN-клиент.'); }
  catch { if (request===connectionRequest) { $('config-details').open=true; $('config-value').select(); toast('Настройки выделены. Скопируйте их вручную.'); } }
};
$('refresh').onclick = async () => { try { await refreshMe(); toast('Кабинет обновлён'); } catch(e) { toast(e.message); } };
$('logout').onclick = async () => {
  sessionEpoch++; meRequest++; clearPrivate();
  try { await api('/api/auth/logout',{}); catalog=await api('/api/catalog'); renderPlans(); }
  catch(e) { toast('Не удалось подтвердить выход. '+e.message); }
};
(async () => {
  try {
    catalog=await api('/api/catalog'); protocol=catalog.protocols.find(p=>p.available)?.id || 'xui'; renderPlans(); $('support').textContent=catalog.support;
    if(catalog.bot_username) { for(const id of ['bot-link','auth-bot']) { $(id).href='https://t.me/'+encodeURIComponent(catalog.bot_username); $(id).hidden=false; } }
    const tg=window.Telegram?.WebApp;
    if(tg?.initData) { tg.ready(); tg.expand(); await api('/api/auth/telegram',{init_data:tg.initData}); }
    await refreshMe();
    if(new URLSearchParams(location.search).has('payment')) { history.replaceState(null,'',basePath+'/'); show(me?'account-dialog':'auth-dialog'); }
    setInterval(async()=>{ if(me && ( $('account-dialog').open || $('checkout-dialog').open || $('config-dialog').open || $('devices-dialog').open )) { try { await refreshMe(); } catch {} } },15000);
  } catch(e) { toast(e.message); }
})();
