'use strict';
const basePath = document.querySelector('meta[name="shop-base"]').content;
const $ = id => document.getElementById(id);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const statusLabel = {pending:'Ожидает оплаты',checkout:'Оплата не завершена',paid:'Оплачено · готовим VPN',active:'Доступ выдан',canceled:'Отменён'};
const money=(amount,currency)=>amount.toLocaleString('ru-RU')+' '+(currency==='XTR'?'★':'₽');
let checkoutQuote=null,quoteRequest=0,promoIntent='';
let referralIntents=new Map();
let passwordOperation=null, logoutPending=false;
let paymentOrder=null,paymentRequest=0,paymentBusy=false;
let catalog, me, protocol = 'xui', selectedPlan, checkoutTrial=null, register = false, toastTimer;
let linkSubscription=null, linkRequest=0, linkState=null;
let sessionEpoch = 0, meRequest = 0, connectionRequest = 0, connection = null, connectionTimer;
let deviceSubscription=null, deviceRequest=0, deviceIntent=null, deviceCache=[], rotationIntents=new Map(), primaryRotationIntents=new Map();
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
function displayOrderStatus(order) { if(order.progress?.title)return order.progress.title; return order.kind==='referral'&&order.status==='paid'?'Добавляем бонусные дни':order.kind==='referral'&&order.status==='active'?'Бонусные дни добавлены':order.kind==='trial'&&order.status==='paid'?'Готовим пробный VPN':order.kind==='trial'&&order.status==='active'?'Пробный доступ выдан':statusLabel[order.status] || order.status; }
function renderPlans() {
  $('protocols').innerHTML = catalog.protocols.map(p => `<button class="${p.id===protocol?'chosen':''}" data-protocol="${p.id}" aria-pressed="${p.id===protocol}">${esc(protocolName(p.id))}${p.available?'':' · скоро'}</button>`).join('');
  const available = catalog.protocols.find(p => p.id===protocol).available;
  $('provider-note').textContent = available ? '' : 'Подключение этого протокола ещё не настроено. Покупки временно недоступны.';
  $('plan-list').innerHTML = catalog.plans.length ? catalog.plans.map((p,i) => `<article class="plan ${i===1?'featured':''}">${i===1?'<span class="badge">УДОБНЫЙ ВЫБОР</span>':''}<h3>${esc(p.name)}</h3><div class="price">${p.price.toLocaleString('ru-RU')} <small>${catalog.currency==='XTR'?'★':'₽'}</small></div><p>${p.days} дней доступа · ${esc(protocolName(protocol))}</p><ul><li>${esc(profileAllowance(p.device_limit ?? 4))}</li><li>Настройки и QR-код в кабинете</li><li>Продление с сохранением дней</li></ul><button data-plan="${p.id}" ${available?'':'disabled'}>${available?'Выбрать тариф ↗':'Скоро'}</button></article>`).join('') : '<p class="plans-empty">Тарифы временно недоступны. Пожалуйста, обратитесь в поддержку.</p>';
  renderTrial();
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
function clearPassword() {
  for(const id of ['password-current','password-new','password-repeat']) $(id).value='';
  $('password-status').textContent='';
}
$('account-dialog').addEventListener('close',clearPassword);
function clearPayment() {
  paymentOrder=null;paymentRequest++;paymentBusy=false;$('payment-result').replaceChildren();$('checkout-submit').disabled=false;
}
function clearPrivate(clearPromoIntent=true) {
  if(clearPromoIntent)promoIntent='';
  clearPayment();
  clearPassword(); $('password-settings').hidden=true; $('password-settings').open=false;
  me = null; selectedPlan = null; checkoutTrial=null;resetQuote(); primaryRotationIntents.clear();referralIntents.clear();$('referral-url').value='';$('referral-code').value='';$('referral-accept').checked=false;$('referral-status').textContent='';$('referral-info').textContent='';$('referral-history').replaceChildren();$('referrals-panel').hidden=true;
  $('link-dialog').close(); clearLink();
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
  else if (!customer) clearPrivate(false);
  me = customer; catalog = nextCatalog; renderPlans();
  $('account-btn').textContent = me ? 'Мой кабинет ↗' : 'Личный кабинет ↗';
  if (connection) {
    const current = me?.subscriptions.find(s=>s.id===connection.id);
    if (!current || current.state!=='active' || current.expires*1000<=Date.now() || (!connection.deviceId && (current.primary_key_state || 'active')!=='active')) {
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
  $('password-settings').hidden=!me.can_change_password;
  renderReferrals();
  $('identity').textContent = me.email || 'Telegram · ' + me.telegram_id;
  $('subscriptions').innerHTML = me.subscriptions.length ? me.subscriptions.map(s => {
    const active = s.state === 'active' && s.expires*1000 > Date.now();
    const primaryReady=active && (s.primary_key_state || 'active')==='active';
    return `<article class="subscription"><h3>${esc(protocolName(s.protocol))}</h3>${s.access_source==='trial'?'<span class="status">Пробный доступ</span>':''}<p>${active?'Доступ до '+date(s.expires):s.state==='pending'?'Подключение готовится':'Подписка закончилась'}</p><p>${esc(profileAllowance(s.device_limit ?? 4))}</p>${primaryReady?`<button class="small" data-config="${esc(s.id)}">Основной профиль ↗</button>${s.access_source!=='trial'?`<button class="text-link" data-primary-rotate="${esc(s.id)}">Сменить основной ключ</button>`:''}`:s.primary_key_state==='rotating'?`<p>Меняем основной ключ${s.primary_retrying?' · повторим после ошибки':''}. Дополнительные профили не меняются.</p>`:''}${s.protocol==='xui'&&s.state!=='pending'&&s.access_source!=='trial'?`<button class="text-link" data-link="${esc(s.id)}">Ссылка подписки</button>`:''}${active&&s.access_source!=='trial'&&me.referrals?.balance_days>0?`<button class="text-link" data-referral="${esc(s.id)}">Потратить бонус</button>`:''}${s.state!=='pending'?` ${s.access_source!=='trial'?`<button class="small secondary" data-devices="${esc(s.id)}">Устройства</button>`:''}<button class="text-link" data-renew="${esc(s.protocol)}">${active?'Продлить подписку':'Возобновить подписку'}</button>`:''}</article>`;
  }).join('') : '<p>Подписок пока нет. Выберите тариф на главной странице.</p>';
  $('orders').innerHTML = me.orders.length ? me.orders.map(o => `<div class="order"><div><b>${esc(protocolName(o.protocol))} · ${o.kind==='referral'?'Бонус · '+o.days+' дней':o.kind==='trial'?'Без оплаты':money(o.amount,o.currency)}</b><br><code>${esc(o.id)}</code><br>${date(o.created)}</div><div><span class="status ${esc(o.status)} ${esc(o.progress?.stage || '')}">${esc(displayOrderStatus(o))}</span><p>${esc(o.progress?.message || '')}</p><br><button class="text-link" data-order="${esc(o.id)}">${o.status==='pending'?'Оплатить / реквизиты':'Статус и помощь'}</button></div></div>`).join('') : '<p>Заказов пока нет.</p>';
}
async function beginCheckout(id) {
  checkoutTrial=null;resetQuote();
  selectedPlan = catalog.plans.find(p => p.id===id);
  await refreshMe();
  selectedPlan = catalog.plans.find(p => p.id===id);
  if (!selectedPlan) throw new Error('Тариф больше недоступен. Выберите другой тариф.');
  if (!me) { show('auth-dialog'); return; }
  $('promo-controls').hidden=false;renderCheckoutAmount();
  if(promoIntent){$('promo-code').value=promoIntent;$('promo-status').textContent='Промокод из ссылки. Нажмите «Применить», чтобы проверить скидку и итоговую цену.';}
  clearPayment(); $('checkout-form').hidden = false;
  $('terms-text').textContent=catalog.terms; $('accept-terms').checked=false; $('accept-terms').required=catalog.payment_mode==='stars';
  $('receipt-label').hidden = !catalog.receipts; $('receipt-label').querySelector('input').required = catalog.receipts;
  $('receipt-label').querySelector('input').value = me.email || '';
  show('checkout-dialog');
}
function paymentResult(order) {
  paymentOrder=order;
  $('checkout-form').hidden = true;
  $('payment-result').replaceChildren();
  const paragraph = document.createElement('p');
  paragraph.textContent = `Заказ: ${order.id}\nТариф: ${order.plan_name || protocolName(order.protocol)} · ${order.days} дней\n${profileAllowance(order.device_limit ?? 4)}\nСумма: ${order.kind==='referral'?'Бонусные дни':order.kind==='trial'?'Без оплаты':money(order.amount,order.currency)}\n${displayOrderStatus(order)}${order.promo_code?'\nПромокод: '+order.promo_code+' · скидка '+money(order.discount,order.currency)+' от '+money(order.base_amount,order.currency):''}\n\n${order.progress?.message || ''}\n${order.instructions || ''}`;
  $('payment-result').append(paragraph);
  if (order.payment_url && order.status === 'pending') {
    const link = document.createElement('a');
    let url;try{url=new URL(order.payment_url);}catch{}
    if (url?.protocol === 'https:') { link.href = url.href; link.textContent = 'Перейти к оплате ↗'; link.className = 'button'; link.target = '_blank'; link.rel = 'noopener noreferrer'; if(order.method==='stars' && window.Telegram?.WebApp?.initData) { link.onclick=event=>{event.preventDefault(); window.Telegram.WebApp.openInvoice(url.href,()=>refreshMe());}; } $('payment-result').append(link); }
  }
  const button = document.createElement('button'); button.className = 'text-link'; button.textContent = 'Проверить статус';
  button.disabled=paymentBusy; button.onclick=async()=>{button.disabled=true;try{await refreshPaymentOrder();}finally{button.disabled=false;}};
  $('payment-result').append(document.createElement('br'), button);
  const support=document.createElement('p');support.textContent=catalog?.support || 'Обратитесь в поддержку магазина.';$('payment-result').append(support);
  const copy=document.createElement('button');copy.className='text-link';copy.textContent='Скопировать номер заказа';
  copy.onclick=async()=>{
    const epoch=sessionEpoch;
    if(!paymentOrder || paymentOrder.id!==order.id || !$('checkout-dialog').open)return;
    try {await navigator.clipboard.writeText('Заказ: '+order.id);if(epoch===sessionEpoch && paymentOrder?.id===order.id)toast('Номер заказа скопирован. Отправьте его поддержке.');}
    catch {if(epoch===sessionEpoch && paymentOrder?.id===order.id)toast('Скопируйте номер заказа из деталей вручную.');}
  };
  $('payment-result').append(copy);
  if(order.status==='active') {
    const connect=document.createElement('button');connect.className='small';connect.textContent='Перейти к подключениям';
    connect.onclick=async()=>{
      const epoch=sessionEpoch,request=paymentRequest;
      if(paymentOrder?.id!==order.id || !$('checkout-dialog').open)return;
      try {await refreshMe();if(epoch===sessionEpoch && request===paymentRequest && me){$('checkout-dialog').close();show('account-dialog');}}
      catch(e){if(epoch===sessionEpoch && request===paymentRequest)toast(e.message);}
    };
    $('payment-result').append(connect);
  }
}
async function openOrder(id) {
  clearPayment();const request=paymentRequest,epoch=sessionEpoch;
  $('checkout-form').hidden=true;$('checkout-info').textContent='';$('payment-result').textContent='Проверяем заказ…';show('checkout-dialog');
  try {
    const order=await api('/api/orders/'+encodeURIComponent(id));
    if(request!==paymentRequest || epoch!==sessionEpoch || !$('checkout-dialog').open)return;
    $('checkout-info').textContent=protocolName(order.protocol);paymentResult(order);
  } catch(e){if(request===paymentRequest && epoch===sessionEpoch && $('checkout-dialog').open){$('payment-result').textContent='Не удалось открыть заказ. Закройте окно и повторите.';toast(e.message);}}
}
async function refreshPaymentOrder(silent=false) {
  if(!paymentOrder || paymentBusy || !$('checkout-dialog').open)return;
  if(silent && !['pending','checkout','paid'].includes(paymentOrder.status))return;
  const id=paymentOrder.id,request=++paymentRequest,epoch=sessionEpoch;paymentBusy=true;
  try {
    const order=await api('/api/orders/'+encodeURIComponent(id));
    if(request!==paymentRequest || epoch!==sessionEpoch || !$('checkout-dialog').open)return;
    paymentBusy=false;if(JSON.stringify(order)!==JSON.stringify(paymentOrder))paymentResult(order);
  } catch(e){if(!silent && request===paymentRequest && epoch===sessionEpoch && $('checkout-dialog').open)toast(e.message);}
  finally {if(request===paymentRequest)paymentBusy=false;}
}
$('protocols').onclick = event => { const b=event.target.closest('[data-protocol]'); if(b) { protocol=b.dataset.protocol; renderPlans(); } };
$('plan-list').onclick = event => { const b=event.target.closest('[data-plan]'); if(b) beginCheckout(b.dataset.plan).catch(e=>toast(e.message)); };
document.querySelectorAll('[data-close]').forEach(button => button.onclick=()=>$(button.dataset.close).close());
$('account-btn').onclick = async () => { if(logoutPending)return; try { await refreshMe(); show(me?'account-dialog':'auth-dialog'); } catch(e) { toast(e.message); } };
$('auth-switch').onclick = () => { register=!register; $('auth-title').textContent=register?'Добро пожаловать.':'С возвращением.'; $('auth-submit').textContent=register?'Создать аккаунт →':'Войти →'; $('auth-switch').textContent=register?'Уже есть аккаунт? Войти':'Создать аккаунт'; $('auth-form').password.autocomplete=register?'new-password':'current-password'; };
$('auth-form').onsubmit = async event => {
  event.preventDefault(); if(logoutPending || passwordOperation)return; const b=$('auth-submit'); b.disabled=true;
  try { const form=new FormData(event.target); await api('/api/auth/'+(register?'register':'login'),Object.fromEntries(form)); sessionEpoch++; $('auth-form').reset(); $('auth-dialog').close(); await refreshMe(); if(selectedPlan) await beginCheckout(selectedPlan.id); else show('account-dialog'); }
  catch(e) { toast(e.message); } finally { b.disabled=false; }
};
$('checkout-form').onsubmit = async event => {
  event.preventDefault(); const b=$('checkout-submit'); b.disabled=true; const request=paymentRequest,epoch=sessionEpoch;
  try { if(!checkoutTrial && $('promo-code').value.trim() && !checkoutQuote)throw new Error('Сначала нажмите «Применить» и проверьте сумму скидки.'); const email=new FormData(event.target).get('receipt_email'); const path=checkoutTrial?'/api/trial':'/api/orders';const payload=checkoutTrial?{protocol:checkoutTrial.protocol,policy_version:checkoutTrial.version,accept_terms:$('accept-terms').checked}:{plan:selectedPlan.id,plan_version:selectedPlan.version,protocol,receipt_email:email||null,accept_terms:$('accept-terms').checked,...(checkoutQuote?{promo_code:checkoutQuote.promo_code,promo_version:checkoutQuote.promo_version,quoted_amount:checkoutQuote.amount,quote_currency:checkoutQuote.currency}:{})};const order=await api(path,payload);if(request!==paymentRequest || epoch!==sessionEpoch || !$('checkout-dialog').open)return;promoIntent='';checkoutTrial=null;paymentResult(order);await refreshMe(); }
  catch(e) {
    if(request!==paymentRequest || epoch!==sessionEpoch || !$('checkout-dialog').open)return;
    if (e.status===409) {
      selectedPlan=null; checkoutTrial=null; $('checkout-dialog').close();
      try { catalog=await api('/api/catalog'); renderPlans(); } catch {}
    }
    toast(e.message);
  } finally { if(request===paymentRequest && epoch===sessionEpoch)b.disabled=false; }
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
  if (!sub || sub.state!=='active' || sub.expires*1000<=Date.now() || (!device && (sub.primary_key_state || 'active')!=='active')) throw new Error('Обновите кабинет и проверьте срок подписки.');
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
$('subscriptions').onclick = async event => {
  const bonus=event.target.closest('[data-referral]');if(bonus){bonus.disabled=true;try {await redeemReferral(bonus.dataset.referral);}catch(e){toast(e.message);}finally{bonus.disabled=false;}return;}
  const link=event.target.closest('[data-link]');
  if(link){await openLink(link.dataset.link).catch(e=>toast(e.message));return;}
  const rotate=event.target.closest('[data-primary-rotate]');
  if(rotate){
    const sub=me?.subscriptions.find(s=>s.id===rotate.dataset.primaryRotate);
    const linkNote=sub?.link_enabled ? (sub.link_follows_rotation ? ' По сохранённой ссылке подписки будет доступен новый ключ.' : ' Ссылка подписки будет отключена.') : '';
    if(!window.confirm('Основной ключ будет отключён, новые настройки нужно импортировать заново. Дополнительные профили не меняются. Продление дождётся завершения операции.'+linkNote+' Продолжить?'))return;
    const sid=rotate.dataset.primaryRotate;rotate.disabled=true;
    try {
      if(!primaryRotationIntents.has(sid))primaryRotationIntents.set(sid,crypto.randomUUID());
      await api('/api/subscriptions/'+encodeURIComponent(sid)+'/rotate',{request_id:primaryRotationIntents.get(sid)});
      primaryRotationIntents.delete(sid);await refreshMe();
    } catch(e){toast(e.message);rotate.disabled=false;}
    return;
  }

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
function renderCheckoutAmount() {
  if(!selectedPlan || checkoutTrial)return;
  const summary=selectedPlan.name+' / '+selectedPlan.days+' дней / '+protocolName(protocol)+' / '+profileAllowance(selectedPlan.device_limit ?? 4);
  $('checkout-info').textContent=summary+' / '+(checkoutQuote?'Было '+money(checkoutQuote.base_amount,checkoutQuote.currency)+' · скидка '+money(checkoutQuote.discount,checkoutQuote.currency)+' · к оплате '+money(checkoutQuote.amount,checkoutQuote.currency):money(selectedPlan.price,catalog.currency));
}
function resetQuote(clearInput=true) {
  quoteRequest++;checkoutQuote=null;
  if(clearInput)$('promo-code').value='';
  $('promo-status').textContent='';$('promo-apply').disabled=false;renderCheckoutAmount();
}
$('checkout-dialog').addEventListener('close',()=>{resetQuote();clearPayment();});
$('promo-code').oninput=()=>{promoIntent='';resetQuote(false);if($('promo-code').value.trim())$('promo-status').textContent='Нажмите «Применить», чтобы рассчитать скидку.';};
$('promo-clear').onclick=()=>{promoIntent='';resetQuote();};
async function applyPromo() {
  if(!selectedPlan || checkoutTrial)return;
  const code=$('promo-code').value.trim(),request=++quoteRequest,plan=selectedPlan.id,version=selectedPlan.version,chosen=protocol;
  checkoutQuote=null;$('promo-apply').disabled=true;renderCheckoutAmount();
  try {
    const result=await api('/api/quote',{plan,plan_version:version,protocol:chosen,promo_code:code});
    if(request!==quoteRequest || !$('checkout-dialog').open || selectedPlan?.id!==plan || protocol!==chosen)return;
    checkoutQuote=result;$('promo-code').value=result.promo_code;
    $('promo-status').textContent='Код '+result.promo_code+': '+result.promo_percent+'%, скидка '+money(result.discount,result.currency)+'. Итоговая сумма показана выше.';renderCheckoutAmount();
  } catch(e){if(request===quoteRequest)$('promo-status').textContent=e.message;}
  finally {if(request===quoteRequest)$('promo-apply').disabled=false;}
}
$('promo-apply').onclick=()=>applyPromo();
function renderTrial() {
  const trial=catalog.trial;
  $('trial-offer').hidden=!trial || (!trial.enabled&&!trial.claim);
  if(!trial)return;
  const available=!!catalog.protocols.find(p=>p.id===protocol)?.available;
  const messages={telegram_required:'Войдите через Telegram, чтобы попробовать VPN.',existing_customer:'Пробный доступ доступен аккаунтам без прежних заказов и подписок.',limit_reached:'Пробные активации временно недоступны.',not_configured:'Пробный доступ готовится к запуску.',disabled:'Пробный доступ отключён.'};
  $('trial-info').textContent=trial.claim?'Пробный доступ уже выбран: '+protocolName(trial.claim.protocol)+'. Статус и настройки — в кабинете.':trial.eligible?trial.days+' дн. · '+protocolName(protocol)+' · только основной профиль.':messages[trial.reason] || 'Пробный доступ уже использован.';
  $('trial-start').textContent=trial.claim?'Открыть кабинет':trial.reason==='telegram_required'?'Войти через Telegram':'Попробовать бесплатно';
  $('trial-start').disabled=!trial.claim && !(trial.eligible&&available) && trial.reason!=='telegram_required';
}
async function beginTrial() {
  selectedPlan=null;checkoutTrial=null;resetQuote();
  await refreshMe();const trial=catalog.trial;
  if(trial?.claim){show('account-dialog');return;}
  if(!me?.telegram_id){show('auth-dialog');toast('Пробный доступ доступен при входе через Telegram-бота.');return;}
  if(!trial?.eligible || !catalog.protocols.find(p=>p.id===protocol)?.available)throw new Error('Пробный доступ сейчас недоступен.');
  selectedPlan=null;checkoutTrial={protocol,version:trial.version};$('promo-controls').hidden=true;
  $('checkout-info').textContent='Пробный доступ / '+trial.days+' дн. / '+protocolName(protocol)+' / без оплаты / только основной профиль';
  clearPayment();$('checkout-form').hidden=false;$('receipt-label').hidden=true;$('receipt-label').querySelector('input').required=false;
  $('terms-text').textContent=catalog.terms;$('accept-terms').checked=false;$('accept-terms').required=true;show('checkout-dialog');
}
$('trial-start').onclick=()=>beginTrial().catch(e=>toast(e.message));
function clearLink() {
  linkRequest++;linkSubscription=null;linkState=null;
  $('link-value').value='';$('link-status').textContent='';$('link-follow').checked=false;
  for(const id of ['copy-link','link-enable','link-rotate','link-disable'])$(id).disabled=true;
}
function renderLink(state) {
  linkState=state;$('link-value').value=state.url || '';$('link-follow').checked=state.follow_rotation;
  $('copy-link').disabled=!state.enabled;$('link-rotate').disabled=!state.enabled;$('link-disable').disabled=!state.enabled;
  const parent=me?.subscriptions.find(s=>s.id===linkSubscription);
  $('link-enable').disabled=!parent || parent.state!=='active' || parent.expires*1000<=Date.now() || (parent.primary_key_state || 'active')!=='active';
  $('link-enable').textContent=state.enabled?'Сохранить настройки':'Создать ссылку';
  $('link-status').textContent=state.enabled?'Ссылка включена. Импортируйте её как URL подписки.':'Ссылка отключена.';
}
async function openLink(sid) {
  clearLink();linkSubscription=sid;$('link-status').textContent='Загружаем…';show('link-dialog');await refreshLink();
}
async function refreshLink() {
  if(!linkSubscription)return;
  const sid=linkSubscription,request=++linkRequest;
  linkState=null;$('link-value').value='';
  for(const id of ['copy-link','link-enable','link-rotate','link-disable'])$(id).disabled=true;
  const state=await api('/api/subscriptions/'+encodeURIComponent(sid)+'/link');
  if(request===linkRequest && $('link-dialog').open)renderLink(state);
}
async function changeLink(action) {
  if(!linkSubscription || !linkState)return;
  const sid=linkSubscription,version=linkState.version,request=++linkRequest;
  for(const id of ['copy-link','link-enable','link-rotate','link-disable'])$(id).disabled=true;
  $('link-value').value='';
  try {
    const state=await api('/api/subscriptions/'+encodeURIComponent(sid)+'/link',{action,version,follow_rotation:$('link-follow').checked},'PUT');
    if(request!==linkRequest || !$('link-dialog').open)return;
    renderLink(state);await refreshMe();
  } catch(e) { if(request===linkRequest) $('link-status').textContent=e.message+' Нажмите «Обновить» перед повтором.'; }
}
$('link-dialog').addEventListener('close',clearLink);
$('link-form').onsubmit=event=>{event.preventDefault();changeLink('enable');};
$('link-refresh').onclick=()=>refreshLink().catch(e=>toast(e.message));
$('link-rotate').onclick=()=>{if(window.confirm('Заменить ссылку? Прежний URL перестанет работать. Его нужно заменить в VPN-клиенте.'))changeLink('rotate');};
$('link-disable').onclick=()=>{if(window.confirm('Отключить обновление по этой ссылке? Уже скачанный VPN-ключ останется действующим.'))changeLink('disable');};
$('copy-link').onclick=async()=>{
  if(!linkState?.enabled || !$('link-value').value)return;
  const request=linkRequest;
  try {await navigator.clipboard.writeText($('link-value').value);if(request===linkRequest)toast('Ссылка скопирована. Добавьте её как подписку в VPN-клиент.');}
  catch {if(request===linkRequest){$('link-value').select();toast('Ссылка выделена для ручного копирования.');}}
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
$('orders').onclick=event=>{const button=event.target.closest('[data-order]');if(button)return openOrder(button.dataset.order);};
$('copy-config').onclick = async () => {
  if (!connectionReady()) return;
  const request = connectionRequest;
  try { await navigator.clipboard.writeText($('config-value').value); if (request===connectionRequest) toast('Настройки скопированы. Импортируйте их в VPN-клиент.'); }
  catch { if (request===connectionRequest) { $('config-details').open=true; $('config-value').select(); toast('Настройки выделены. Скопируйте их вручную.'); } }
};
$('refresh').onclick = async () => { try { await refreshMe(); toast('Кабинет обновлён'); } catch(e) { toast(e.message); } };
$('password-form').onsubmit = event => {
  event.preventDefault();
  if(passwordOperation || logoutPending || !me?.can_change_password)return;
  if($('password-new').value!==$('password-repeat').value){$('password-status').textContent='Новые пароли не совпадают.';return;}
  const data={current_password:$('password-current').value,new_password:$('password-new').value};
  const epoch=sessionEpoch, actor=me.id;
  $('password-submit').disabled=true; $('password-status').textContent='Меняем пароль…';
  passwordOperation=(async()=>{
    try {
      await api('/api/auth/password',data);
      if(epoch!==sessionEpoch || me?.id!==actor)return;
      const reopen=$('account-dialog').open && !logoutPending;
      sessionEpoch++; meRequest++; const nextEpoch=sessionEpoch; clearPrivate();
      await refreshMe();
      if(nextEpoch===sessionEpoch && me?.id===actor && !logoutPending){
        if(reopen)show('account-dialog');
        toast('Пароль изменён. Другие сеансы и ссылки входа отозваны.');
      }
    } catch(e) {
      if(epoch===sessionEpoch && me?.id===actor){clearPassword();$('password-status').textContent=e.message+' Если ответ потерян, попробуйте войти с новым паролем.';}
      else if(!logoutPending)toast('Обновите кабинет. При необходимости войдите с новым паролем.');
    } finally {for(const id of ['password-current','password-new','password-repeat'])$(id).value='';$('password-submit').disabled=false;passwordOperation=null;}
  })();
  return passwordOperation;
};
$('logout').onclick = async () => {
  if(logoutPending)return;
  logoutPending=true; $('logout').disabled=true;
  const pending=passwordOperation;
  sessionEpoch++; meRequest++; clearPrivate();
  try { if(pending)await pending; await api('/api/auth/logout',{}); catalog=await api('/api/catalog'); renderPlans(); }
  catch(e) { toast('Не удалось подтвердить выход. '+e.message); }
  finally {logoutPending=false;$('logout').disabled=false;}
};
(async () => {
  try {
    catalog=await api('/api/catalog'); protocol=catalog.protocols.find(p=>p.available)?.id || 'xui'; renderPlans(); $('support').textContent=catalog.support;
    if(catalog.bot_username) { for(const id of ['bot-link','auth-bot']) { $(id).href='https://t.me/'+encodeURIComponent(catalog.bot_username); $(id).hidden=false; } }
    const tg=window.Telegram?.WebApp;
    if(tg?.initData) { tg.ready(); tg.expand(); await api('/api/auth/telegram',{init_data:tg.initData}); }
    await refreshMe();
    // Ссылка передаёт только код: процент и право на скидку проверяет сервер после явного применения.
    const incomingPromos=new URLSearchParams(location.search).getAll('promo');
    const incomingPromo=incomingPromos.length===1?incomingPromos[0].trim().toUpperCase():'';
    if(/^[A-Z0-9][A-Z0-9_-]{3,31}$/.test(incomingPromo)){
      promoIntent=incomingPromo;toast('В ссылке есть промокод. Выберите тариф и примените его перед оформлением.');
    }
    if(/^[a-f0-9]{24}$/.test(new URLSearchParams(location.search).get('ref') || '')) {show(me?'account-dialog':'auth-dialog');toast('Примите приглашение в кабинете до первого заказа или пробного доступа.');}
    if(new URLSearchParams(location.search).has('payment')) { history.replaceState(null,'',basePath+'/'); show(me?'account-dialog':'auth-dialog'); }
    setInterval(async()=>{ if(me && ( $('account-dialog').open || $('checkout-dialog').open || $('config-dialog').open || $('devices-dialog').open )) { try { await refreshMe(); await refreshPaymentOrder(true); } catch {} } },15000);
  } catch(e) { toast(e.message); }
})();

function renderReferrals() {
  const r=me?.referrals;$('referrals-panel').hidden=!r;if(!r)return;
  const p=r.policy;
  $('referral-info').textContent='Бонусных дней: '+r.balance_days+' · начислено '+r.earned_days+' · использовано '+r.spent_days+'. Приглашений: '+r.invited+', с бонусом: '+r.rewarded+'. '+(p.enabled?'За одну подходящую оплату друга: '+p.days+' дней пригласившему. Минимум фактической оплаты: '+p.min_rub+' ₽ или '+p.min_stars+' ★.':'Новые приглашения сейчас отключены.')+(r.bound?' Ваше приглашение принято: '+r.bound.days+' дней пригласившему после оплаты от '+r.bound.min_rub+' ₽ или '+r.bound.min_stars+' ★.'+(r.bound.rewarded?' Бонус начислен.':''): ' Приглашение принимают до первого заказа или trial.');
  $('referral-code-create').hidden=!r.can_share || !!r.url;
  $('referral-share').hidden=!r.url;$('referral-url').value=r.url || '';
  $('referral-bind').hidden=!r.can_bind;
  const initial=new URLSearchParams(location.search).get('ref');
  if(r.can_bind && !$('referral-code').value && /^[a-f0-9]{24}$/.test(initial || ''))$('referral-code').value=initial;
  $('referral-days').max=String(Math.max(1,Math.min(365,r.balance_days)));
  $('referral-days').disabled=r.balance_days<1;
  $('referral-history').innerHTML=r.history.map(h=>'<p>+'+h.days+' дней · '+date(h.created)+'</p>').join('');
}
$('referral-code-create').onclick=async()=>{
  const b=$('referral-code-create');b.disabled=true;
  try {const r=await api('/api/referrals/code',{});if(me){me.referrals=r;renderReferrals();}}
  catch(e){toast(e.message);}finally{b.disabled=false;}
};
$('referral-copy').onclick=async()=>{try {if(me?.referrals?.url){await navigator.clipboard.writeText(me.referrals.url);toast('Ссылка скопирована');}}catch {toast('Выделите и скопируйте ссылку вручную.');}};
$('referral-bind').onsubmit=async event=>{
  event.preventDefault();if(!me?.referrals)return;const b=$('referral-bind-submit');b.disabled=true;
  try {await api('/api/referrals/bind',{code:$('referral-code').value.trim(),policy_version:me.referrals.policy.version,accept_terms:$('referral-accept').checked});await refreshMe();toast('Приглашение принято');}
  catch(e){toast(e.message);}finally{b.disabled=false;}
};
async function redeemReferral(sid) {
  const epoch=sessionEpoch,uid=me?.id;
  const days=Number($('referral-days').value);
  if(!Number.isInteger(days)||days<1||days>365||days>(me?.referrals?.balance_days || 0))throw new Error('Проверьте число бонусных дней');
  const key=sid+':'+days;if(!referralIntents.has(key))referralIntents.set(key,crypto.randomUUID());
  const order=await api('/api/referrals/redeem',{subscription_id:sid,days,request_id:referralIntents.get(key)});
  await refreshMe();if(epoch!==sessionEpoch || uid!==me?.id)return;referralIntents.delete(key);$('referral-status').textContent='Бонусное продление в очереди. Заказ: '+order.id;
}
