'use strict';
const basePath = document.querySelector('meta[name="shop-base"]').content;
const $ = id => document.getElementById(id);
const telegramApp = window.Telegram?.WebApp;
const telegramDialogStack = [];
let miniappCurrentRoute = 'home';
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const statusLabel = {pending:'Ожидает оплаты',checkout:'Оплата не завершена',paid:'Оплачено · готовим VPN',active:'Доступ выдан',canceled:'Отменён'};
const money=(amount,currency)=>amount.toLocaleString('ru-RU')+' '+(currency==='XTR'?'★':'₽');
let checkoutQuote=null,quoteRequest=0,promoIntent='',checkoutPolicyVersion=null;
let referralIntents=new Map();
let adsPolicy=null,adsPage=1,adsRequest=0,adsMutation=0,adsIntent=new Map();
let passwordOperation=null, logoutPending=false, supportRequest=0, selectedSupportCase=null;
let paymentOrder=null,paymentRequest=0,paymentBusy=false;
let catalog, me, protocol = 'xui', selectedPlan, checkoutTrial=null, register = false, toastTimer;
let linkSubscription=null, linkDeviceId=null, linkProtocol=null, linkRequest=0, linkState=null, flowLaunchUrl=null;
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
function syncTelegramTheme() {
  if (!telegramApp?.initData) return;
  document.documentElement.dataset.telegramTheme = telegramApp.colorScheme === 'dark' ? 'dark' : 'light';
}
function setMiniappActive(route) {
  if (!telegramApp?.initData) return;
  const nav = $('miniapp-nav');
  if (!nav) return;
  if (!['home', 'plans', 'help', 'account'].includes(route)) route = 'home';
  if (route !== 'account') miniappCurrentRoute = route;
  if ($('account-dialog').open) route = 'account';
  nav.querySelectorAll('[data-miniapp-route]').forEach(item => {
    if (item.dataset.miniappRoute === route) item.setAttribute('aria-current', 'page');
    else item.removeAttribute('aria-current');
  });
}
function setupMiniappNavigation() {
  const nav = $('miniapp-nav');
  if (!nav) return;
  nav.hidden = false;
  document.documentElement.dataset.miniappReady = 'true';
  nav.querySelectorAll('a[data-miniapp-route]').forEach(link => link.addEventListener('click', () => setMiniappActive(link.dataset.miniappRoute)));
  $('miniapp-account').addEventListener('click', () => $('account-btn').click());
  window.addEventListener('hashchange', () => {
    const hash = location.hash.slice(1);
    const route = hash === 'how' ? 'plans' : hash;
    if (['home', 'plans', 'help'].includes(route)) setMiniappActive(route);
  });
  if ('IntersectionObserver' in window) {
    const observer = new IntersectionObserver(entries => {
      const visible = entries.filter(entry => entry.isIntersecting).sort((a, b) => b.intersectionRatio - a.intersectionRatio)[0];
      const route = visible?.target.id;
      if (['home', 'plans', 'help'].includes(route)) setMiniappActive(route);
    }, {rootMargin: '-30% 0px -55% 0px', threshold: [0, 0.15, 0.35, 0.6]});
    for (const id of ['home', 'plans', 'help']) { const section = $(id); if (section) observer.observe(section); }
  }
  $('account-dialog').addEventListener('close', () => { if (telegramApp?.initData && nav.querySelector('[aria-current="page"]')?.dataset.miniappRoute === 'account') setMiniappActive(miniappCurrentRoute); });
  const initialHash = location.hash.slice(1);
  setMiniappActive(initialHash === 'how' ? 'plans' : initialHash || 'home');
}
function syncTelegramBackButton() {
  const button = telegramApp?.BackButton;
  if (!button) return;
  const hasOpenDialog = telegramDialogStack.some(dialog => dialog.open);
  if (hasOpenDialog && typeof button.show === 'function') button.show();
  else if (!hasOpenDialog && typeof button.hide === 'function') button.hide();
}
function closeTelegramDialog() {
  while (telegramDialogStack.length) {
    const dialog = telegramDialogStack.pop();
    if (dialog.open) { dialog.close(); return; }
  }
  const dialog = document.querySelector('dialog[open]');
  if (dialog) dialog.close();
  syncTelegramBackButton();
}
if (typeof telegramApp?.BackButton?.onClick === 'function') {
  telegramApp.BackButton.onClick(closeTelegramDialog);
  document.querySelectorAll('dialog').forEach(dialog => dialog.addEventListener('close', () => {
    // Событие close может прийти после повторного открытия того же окна.
    if (dialog.open) return;
    const index = telegramDialogStack.indexOf(dialog);
    if (index !== -1) telegramDialogStack.splice(index, 1);
    syncTelegramBackButton();
  }));
}
function show(id) {
  const dialog = $(id);
  if (!dialog.open) {
    dialog.showModal();
    dialog.scrollTop=0;
    if (typeof telegramApp?.BackButton?.show === 'function') telegramDialogStack.push(dialog);
  }
  if (id === 'account-dialog') {setMiniappActive('account');setShopMobileActive('account');}
  syncTelegramBackButton();
}
function date(ts) { return new Date(ts*1000).toLocaleDateString('ru-RU'); }
function protocolName(id) { return ({wg:'WireGuard',xui:'VLESS',vmess:'VMess',hysteria2:'Hysteria2',tuic:'TUIC v5'})[id] || id; }
function profileAllowance(limit) { return limit===0 ? 'Только основной профиль' : 'Основной + до '+limit+' дополнительных профилей'; }
function displayOrderStatus(order) { if(order.progress?.title)return order.progress.title; return order.method==='free'&&order.status==='paid'?'Бесплатный доступ готовится':order.kind==='referral'&&order.status==='paid'?'Добавляем бонусные дни':order.kind==='referral'&&order.status==='active'?'Бонусные дни добавлены':order.kind==='trial'&&order.status==='paid'?'Готовим пробный VPN':order.kind==='trial'&&order.status==='active'?'Пробный доступ выдан':statusLabel[order.status] || order.status; }
function renderAppearance(){
 const config=catalog?.appearance;
 if(!config)return;
 const textFields=[['brand',config.brand_name==='pushok.vpn'?'':config.brand_name],['headline',config.headline],['description',config.description]];
 for(const [field,value] of textFields){
  const custom=$('shop-'+field+'-custom'),original=$('shop-'+field+'-default');
  const text=typeof value==='string'?value:'';
  custom.textContent=text;custom.hidden=!text;original.hidden=!!text;
 }
 const announcement=typeof config.announcement==='string'?config.announcement:'';
 $('shop-announcement').textContent=announcement;$('shop-announcement').hidden=!announcement;
 if(document.documentElement?.dataset)document.documentElement.dataset.shopTheme=['pushok','ocean','graphite','violet'].includes(config.theme)?config.theme:'pushok';
}
function trialProtocolAvailable(){
 const item=catalog.protocols.find(p=>p.id===protocol);
 return !!(item?.trial_available??item?.available);
}
function renderPlans() {
  renderPushokIdentity();
  renderAppearance();
  $('protocols').innerHTML = catalog.protocols.map(p => `<button class="${p.id===protocol?'chosen':''}" data-protocol="${p.id}" aria-pressed="${p.id===protocol}">${esc(protocolName(p.id))}${p.available?'':' · скоро'}</button>`).join('');
  const selected=catalog.protocols.find(p=>p.id===protocol);
  const available=!!selected?.available && selected?.sales_allowed!==false;
  const paymentReady=catalog.payment_available!==false;
  $('provider-note').textContent = available && (paymentReady || catalog.plans.some(p=>p.price===0)) ? '' : !paymentReady && available ? (catalog.payment_notice||'Оплата временно недоступна. Бесплатные тарифы доступны.') : selected?.sales_allowed===false ? (catalog.sale_notice||'Продажи временно приостановлены.') : 'Подключение этого протокола ещё не настроено. Покупки временно недоступны.';
  $('plan-list').innerHTML = catalog.plans.length ? catalog.plans.map((p,i) => `<article class="plan ${i===1?'featured':''}">${i===1?'<span class="badge">УДОБНЫЙ ВЫБОР</span>':''}<h3>${esc(p.name)}</h3><div class="price">${p.price===0?'Бесплатно':p.price.toLocaleString('ru-RU')}${p.price===0?'':' <small>'+(catalog.currency==='XTR'?'★':'₽')+'</small>'}</div><p>${p.days} дней доступа · ${esc(protocolName(protocol))}</p><ul><li>${esc(profileAllowance(p.device_limit ?? 4))}</li><li>Настройки и QR-код в кабинете</li><li>${p.price===0?(p.free_auto_renew?'Автопродление: '+p.free_max_renewals+' раз(а)':'Без автоматического продления'):'Продление с сохранением дней'}</li>${p.price===0&&p.free_traffic_gb?'<li>Лимит '+p.free_traffic_gb+' ГБ на профиль 3x-ui</li>':''}${p.price===0&&p.free_activation_limit?'<li>Всего активаций: '+p.free_activation_limit+'</li>':''}</ul><button data-plan="${p.id}" ${available&&(paymentReady||p.price===0)&&!(p.price===0&&p.free_traffic_gb>0&&!['xui','vmess'].includes(protocol))?'':'disabled'}>${p.price===0&&p.free_traffic_gb>0&&!['xui','vmess'].includes(protocol)?'Только для 3x-ui':available&&(paymentReady||p.price===0)?(p.price===0?'Получить бесплатно ↗':'Выбрать тариф ↗'):'Скоро'}</button></article>`).join('') : '<p class="plans-empty">Тарифы временно недоступны. Пожалуйста, обратитесь в поддержку.</p>';
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
  paymentOrder=null;paymentRequest++;paymentBusy=false;checkoutPolicyVersion=null;$('payment-result').replaceChildren();$('checkout-submit').disabled=false;
}
function clearAds() {
  adsRequest++;adsMutation++;adsPolicy=null;adsPage=1;adsIntent.clear();
  for(const id of ['ads-channel','ads-comment'])$(id).value='';
  for(const id of ['ads-status','ads-offer','ads-page'])$(id).textContent='';
  $('ads-list').replaceChildren();$('ads-form').hidden=true;$('ads-submit').disabled=false;
}
function clearPrivate(clearPromoIntent=true) {
  if(clearPromoIntent){$('pushok-id-dialog').close();clearPushokIdentity();$('pushok-id-account').hidden=true;$('pushok-id-link-state').textContent='';}
  $('ads-dialog').close();clearAds();
  if(clearPromoIntent)promoIntent='';
  clearPayment();
  clearPassword(); $('password-settings').hidden=true; $('password-settings').open=false;
  me = null; selectedPlan = null; checkoutTrial=null;resetQuote(); primaryRotationIntents.clear();referralIntents.clear();$('referral-url').value='';$('referral-code').value='';$('referral-accept').checked=false;$('referral-status').textContent='';$('referral-info').textContent='';$('referral-history').replaceChildren();$('referrals-panel').hidden=true;
  $('link-dialog').close(); clearLink();
  $('devices-dialog').close(); clearDevices();
  $('config-dialog').close(); clearConnection();
  $('account-dialog').close(); $('checkout-dialog').close();
  for (const id of ['identity','account-summary','subscriptions','orders','payment-result','checkout-info','support-list','support-thread']) $(id).replaceChildren();
  supportRequest++;selectedSupportCase=null;$('support-create').reset();$('support-reply').reset();$('support-reply').hidden=true;$('support-status').textContent='';
  $('auth-form').reset(); $('receipt-label').querySelector('input').value = '';
  $('account-label').textContent = 'Войти';
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
  $('account-label').textContent = me ? 'Мой кабинет ↗' : 'Войти';
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
  if (linkSubscription) {
    const current = me?.subscriptions.find(s=>s.id===linkSubscription);
    if (!current || current.protocol!==linkProtocol || current.state!=='active' || current.expires*1000<=Date.now() ||
        (!linkDeviceId && (current.primary_key_state || 'active')!=='active')) {
      $('link-dialog').close(); clearLink();
    } else if (linkState) renderFlowLink(linkState, current);
  }
  if (me) renderAccount();
  if (deviceSubscription) await refreshDevices();
}
function renderAccount() {
  if (!me) return;
  renderPushokIdentity();
  $('password-settings').hidden=!me.can_change_password;
  renderReferrals();
  $('identity').textContent = me.email || 'Telegram · ' + me.telegram_id;
  const activeCount=me.subscriptions.filter(s=>s.state==='active'&&s.expires*1000>Date.now()).length;
  const pendingCount=me.subscriptions.filter(s=>s.state==='pending').length;
  $('account-summary').textContent='Активных: '+activeCount+' · готовятся: '+pendingCount;
  const bonusTab=document.querySelector('[data-account-section="referrals-panel"]');
  if(bonusTab)bonusTab.hidden=!me.referrals;
  if(!me.referrals&&$('referrals-panel').dataset?.accountActive==='true')setShopAccountPane('subscriptions');
  // Preserve expanded controls during the 15-second refresh.
  const expanded=new Set(Array.from($('subscriptions').querySelectorAll?.('details[data-subscription-options][open]')||[],el=>el.dataset.subscriptionOptions));
  $('subscriptions').innerHTML = me.subscriptions.length ? me.subscriptions.map(s => {
    const active = s.state === 'active' && s.expires*1000 > Date.now();
    const primaryReady=active && (s.primary_key_state || 'active')==='active';
    const accessLabel=active?'Активно':s.state==='pending'?'Готовится':s.state==='revoked'?'Отозвано':'Истекло';
    const stateClass=active?'active':s.state==='pending'?'pending':'canceled';
    const configure=primaryReady?'<button type="button" class="small" data-config="'+esc(s.id)+'">Настроить подключение ↗</button>':'';
    const flow=s.protocol==='wg'&&primaryReady?'<button type="button" class="small subscription-flow" data-link="'+esc(s.id)+'">Открыть в Pushok Flow</button>':'';
    const options=[
      primaryReady&&s.access_source!=='trial'?'<button type="button" class="text-link" data-primary-rotate="'+esc(s.id)+'">Сменить основной ключ</button>':'',
      s.protocol==='xui'&&s.state!=='pending'&&s.access_source!=='trial'?'<button type="button" class="text-link" data-link="'+esc(s.id)+'">Ссылка подписки</button>':'',
      active&&s.access_source!=='trial'&&me.referrals?.balance_days>0?'<button type="button" class="text-link" data-referral="'+esc(s.id)+'">Потратить бонус</button>':'',
      s.state!=='pending'&&s.access_source!=='trial'?'<button type="button" class="small secondary" data-devices="'+esc(s.id)+'">Устройства</button>':''
    ].filter(Boolean);
    const renewal=s.state!=='pending'?'<button type="button" class="small secondary" data-renew="'+esc(s.protocol)+'">'+(active?'Продлить подписку':'Возобновить подписку')+'</button>':'';
    return '<article class="subscription"><div class="subscription-top"><h3>'+esc(protocolName(s.protocol))+'</h3><span class="status '+stateClass+'">'+accessLabel+'</span></div>'+
      (s.access_source==='trial'?'<p class="subscription-kind">Пробный доступ</p>':'')+
      '<p class="subscription-expiry">'+(active?'Доступ до '+date(s.expires):s.state==='pending'?'Подключение готовится':s.state==='revoked'?'Доступ отозван':'Подписка закончилась')+'</p><p>'+esc(profileAllowance(s.device_limit ?? 4))+'</p>'+
      (s.primary_key_state==='rotating'?'<p>Меняем основной ключ'+(s.primary_retrying?' · повторим после ошибки':'')+'. Дополнительные профили не меняются.</p>':'')+
      '<div class="subscription-primary">'+flow+configure+renewal+'</div>'+
      (options.length?'<details class="subscription-options" data-subscription-options="'+esc(s.id)+'"'+(expanded.has(s.id)?' open':'')+'><summary>Управление подключением</summary><div class="actions">'+options.join('')+'</div></details>':'')+'</article>';
  }).join('') : '<article class="account-empty"><h3>Подключений пока нет</h3><p>Выберите тариф, чтобы получить настройки для вашего устройства.</p><button type="button" class="small" data-account-plans>Выбрать тариф</button></article>';
  $('orders').innerHTML = me.orders.length ? me.orders.map(o => `<div class="order"><div><b>${esc(protocolName(o.protocol))} · ${o.kind==='referral'?'Бонус · '+o.days+' дней':o.kind==='trial'?'Без оплаты':money(o.amount,o.currency)}</b><br><code>${esc(o.id)}</code><br>${date(o.created)}</div><div><span class="status ${esc(o.status)} ${esc(o.progress?.stage || '')}">${esc(displayOrderStatus(o))}</span><p>${esc(o.progress?.message || '')}</p><br><button class="text-link" data-order="${esc(o.id)}">${o.status==='pending'?'Оплатить / реквизиты':'Статус и помощь'}</button></div></div>`).join('') : '<p>Заказов пока нет.</p>';
  refreshSupportCases().catch(e=>{ $('support-status').textContent=e.message; });
}
async function refreshSupportCases(){
 const request=++supportRequest,epoch=sessionEpoch;
 const select=$('support-order'),chosenOrder=select.value;select.innerHTML='<option value="">Без привязки</option>'+me.orders.map(o=>'<option value="'+esc(o.id)+'">'+esc(o.plan_name||protocolName(o.protocol))+' · '+esc(o.id.slice(0,8))+'</option>').join('');
  if(chosenOrder&&me.orders.some(o=>o.id===chosenOrder))select.value=chosenOrder;
 const data=await api('/api/support/tickets?page=1&page_size=30');
 if(request!==supportRequest||epoch!==sessionEpoch||!me)return;
 const labels={open:'Открыто',waiting_staff:'Ждёт поддержки',waiting_customer:'Ждёт вашего ответа',closed:'Закрыто'};
 $('support-list').innerHTML=data.cases.map(c=>'<article class="order"><b>'+esc(c.subject)+'</b><p>'+esc(labels[c.status]||c.status)+' · обновлено '+date(c.updated)+'</p><p>'+esc(c.last_message||'')+'</p><button class="text-link" data-support-case="'+esc(c.id)+'">Открыть переписку</button></article>').join('')||'<p>Обращений пока нет.</p>';
}
async function openSupportCase(id){
 const request=++supportRequest,epoch=sessionEpoch;selectedSupportCase=id;
 const c=await api('/api/support/tickets/'+encodeURIComponent(id));
 if(request!==supportRequest||epoch!==sessionEpoch||!me)return;
 const labels={open:'Открыто',waiting_staff:'Ждёт поддержки',waiting_customer:'Ждёт вашего ответа',closed:'Закрыто'};
 $('support-thread').innerHTML='<article class="subscription"><h4>'+esc(c.subject)+' · '+esc(labels[c.status]||c.status)+'</h4>'+c.messages.map(m=>'<p><b>'+esc(m.author)+' · '+date(m.created)+'</b><br>'+esc(m.body)+'</p>').join('')+'</article>';
 $('support-reply').hidden=false;$('support-reply').dataset.version=String(c.version);
}
$('support-create').onsubmit=async event=>{
 event.preventDefault();if(!me)return;
 const form=event.target,values=new FormData(form),button=form.querySelector('button');button.disabled=true;
 try{
  await api('/api/support/tickets',{request_id:crypto.randomUUID(),subject:values.get('subject'),order_id:values.get('order_id')||null,message:values.get('message')});
  form.reset();$('support-status').textContent='Обращение создано. Ответ появится в этой переписке.';await refreshSupportCases();
 }catch(error){$('support-status').textContent=error.message}
 finally{button.disabled=false}
};
$('support-list').onclick=event=>{const b=event.target.closest('[data-support-case]');if(b)openSupportCase(b.dataset.supportCase).catch(e=>$('support-status').textContent=e.message)};
$('support-reply').onsubmit=async event=>{
 event.preventDefault();if(!selectedSupportCase)return;
 const form=event.target,body=new FormData(form).get('body'),button=form.querySelector('button');button.disabled=true;
 try{
  await api('/api/support/tickets/'+encodeURIComponent(selectedSupportCase)+'/messages',{body,version:Number(form.dataset.version)});
  form.reset();$('support-status').textContent='Ответ отправлен.';await openSupportCase(selectedSupportCase);await refreshSupportCases();
 }catch(error){$('support-status').textContent=error.message}
 finally{button.disabled=false}
};

async function beginCheckout(id) {
  checkoutTrial=null;resetQuote();
  selectedPlan = catalog.plans.find(p => p.id===id);
  await refreshMe();
  selectedPlan = catalog.plans.find(p => p.id===id);
  if (!selectedPlan) throw new Error('Тариф больше недоступен. Выберите другой тариф.');
  if (!me) { show('auth-dialog'); return; }
  if(catalog.protocols.find(p=>p.id===protocol)?.sales_allowed===false)throw new Error(catalog.sale_notice||'Продажи временно приостановлены.');
  if(selectedPlan.price!==0 && catalog.payment_available===false)throw new Error(catalog.payment_notice||'Оплата временно недоступна.');
  $('promo-controls').hidden=selectedPlan.price===0;$('promo-controls').open=!!promoIntent&&selectedPlan.price!==0;renderCheckoutAmount();
  if(promoIntent){$('promo-code').value=promoIntent;$('promo-status').textContent='Промокод из ссылки. Нажмите «Применить», чтобы проверить скидку и итоговую цену.';}
  clearPayment(); $('checkout-form').hidden = false;
  checkoutPolicyVersion=Number.isSafeInteger(catalog.payment_policy_version)?catalog.payment_policy_version:null;
  $('checkout-submit').textContent=selectedPlan.price===0?'Получить бесплатно →':catalog.payment_mode==='sbp'?'Создать счёт СБП →':'Оформить заказ →';
  $('terms-text').textContent=catalog.terms; $('accept-terms').checked=false; $('accept-terms').required=selectedPlan.price!==0 && catalog.payment_mode==='stars';
  $('receipt-label').hidden = selectedPlan.price===0 || !catalog.receipts; $('receipt-label').querySelector('input').required = selectedPlan.price!==0 && catalog.receipts;
  $('receipt-label').querySelector('input').value = me.email || '';
  show('checkout-dialog');
}
function paymentResult(order) {
  paymentOrder=order;
  $('checkout-form').hidden = true;
  $('payment-result').replaceChildren();
  const paragraph = document.createElement('p');
  paragraph.className='payment-summary';
  paragraph.textContent = `Заказ: ${order.id}\nТариф: ${order.plan_name || protocolName(order.protocol)} · ${order.days} дней\n${profileAllowance(order.device_limit ?? 4)}\nСумма: ${order.kind==='referral'?'Бонусные дни':order.kind==='trial'?'Без оплаты':order.amount===0?'Бесплатно':money(order.amount,order.currency)}\n${displayOrderStatus(order)}${order.promo_code?'\nПромокод: '+order.promo_code+' · скидка '+money(order.discount,order.currency)+' от '+money(order.base_amount,order.currency):''}\n\n${order.progress?.message || ''}\n${order.instructions || ''}`;
  $('payment-result').append(paragraph);
  if (order.payment_url && order.status === 'pending') {
    const link = document.createElement('a');
    let url;try{url=new URL(order.payment_url);}catch{}
    if (url?.protocol === 'https:') { link.href = url.href; link.textContent = order.method==='sbp'?'Оплатить через СБП ↗':'Перейти к оплате ↗'; link.className = 'button'; link.target = '_blank'; link.rel = 'noopener noreferrer'; if(order.method==='stars' && window.Telegram?.WebApp?.initData) { link.onclick=event=>{event.preventDefault(); window.Telegram.WebApp.openInvoice(url.href,()=>refreshMe());}; } $('payment-result').append(link); }
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
      try {await refreshMe();if(epoch===sessionEpoch && request===paymentRequest && me){$('checkout-dialog').close();setShopAccountPane('subscriptions');show('account-dialog');}}
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
// В браузер поступает только случайный номер запроса. Подпись приходит от ID на сервер Shop.
let pushokIdMode=null,pushokIdConfirmation=null,pushokIdSeq=0,pushokIdBusy=false,pushokIdFinish=null;
function renderPushokIdentity(){
 const enabled=catalog?.pushok_id?.enabled===true,linked=me?.pushok_id?.linked===true;
 $('pushok-id-signin').hidden=!enabled||!!me;
 $('pushok-id-account').hidden=!me||(!enabled&&!linked);
 $('pushok-id-link').hidden=!enabled||!me?.telegram_id||linked;
 $('pushok-id-revoke').hidden=!linked;
 $('pushok-id-link-state').textContent=linked?'Связан с Pushok ID '+me.pushok_id.subject:
  me?.telegram_id?'Аккаунт пока не связан.':'Email-аккаунт нельзя объединить автоматически. Требуется проверенный Telegram ID.';
}
function clearPushokIdentity(){
 pushokIdSeq++;pushokIdMode=null;pushokIdConfirmation=null;pushokIdBusy=false;
 $('pushok-id-portal').removeAttribute('href');$('pushok-id-portal').hidden=true;
 $('pushok-id-status').textContent='';$('pushok-id-confirm-identity').textContent='';
 $('pushok-id-confirm-form').hidden=true;$('pushok-id-consent').checked=false;$('pushok-id-confirm').disabled=true;
 $('pushok-id-start').disabled=false;$('pushok-id-check').disabled=false;$('pushok-id-cancel').disabled=false;$('pushok-id-close').disabled=false;
}
function identityControls(busy){
 pushokIdBusy=busy;
 for(const id of ['pushok-id-start','pushok-id-check','pushok-id-cancel','pushok-id-close'])$(id).disabled=busy;
 $('pushok-id-confirm').disabled=busy||!pushokIdConfirmation||!$('pushok-id-consent').checked;
}
function identityCurrent(seq,epoch){
 return seq===pushokIdSeq&&epoch===sessionEpoch&&$('pushok-id-dialog').open;
}
function safeApprovalUrl(value){
 const url=new URL(value);
 if(url.protocol!=='https:'||url.username||url.password||url.hash||!url.searchParams.get('shop_request')||!url.searchParams.get('shop_audience'))throw new Error('Некорректный адрес Pushok ID');
 return url.href;
}
async function openPushokIdentity(mode){
 if(catalog?.pushok_id?.enabled!==true||logoutPending||pushokIdFinish)return;
 if(mode==='link'&&(!me?.telegram_id||me?.pushok_id?.linked))return;
 if(mode==='signin'&&me)return;
 clearPushokIdentity();pushokIdMode=mode;
 show('pushok-id-dialog');await checkPushokIdentity();
}
async function startPushokIdentity(){
 if(!pushokIdMode||pushokIdBusy||!$('pushok-id-dialog').open)return;
 const seq=++pushokIdSeq,epoch=sessionEpoch;
 pushokIdConfirmation=null;$('pushok-id-confirm-form').hidden=true;$('pushok-id-consent').checked=false;
 $('pushok-id-portal').removeAttribute('href');$('pushok-id-portal').hidden=true;identityControls(true);
 try{
  const result=await api('/api/auth/pushok-id/start',{mode:pushokIdMode});
  if(!identityCurrent(seq,epoch))return;
  const url=safeApprovalUrl(result.approval_url);
  $('pushok-id-portal').href=url;$('pushok-id-portal').hidden=false;
  $('pushok-id-status').textContent='Запрос создан на 5 минут. Откройте Pushok ID и подтвердите его.';
 }catch(e){if(identityCurrent(seq,epoch))$('pushok-id-status').textContent=e.message;}
 finally{if(identityCurrent(seq,epoch))identityControls(false);}
}
async function checkPushokIdentity(){
 if(!pushokIdMode||pushokIdBusy||!$('pushok-id-dialog').open)return;
 const seq=++pushokIdSeq,epoch=sessionEpoch;
 pushokIdConfirmation=null;$('pushok-id-confirm-form').hidden=true;$('pushok-id-consent').checked=false;identityControls(true);
 try{
  const result=await api('/api/auth/pushok-id/status');
  if(!identityCurrent(seq,epoch))return;
  if(result.state==='approved'){
   if(result.mode!==pushokIdMode||!Number.isSafeInteger(result.telegram_id)||result.telegram_id<=0||
      !/^[a-f0-9]{64}$/.test(result.confirmation)||! /^[a-f0-9-]{36}$/.test(result.subject))throw new Error('Подтверждение не соответствует текущему запросу');
   pushokIdConfirmation={confirmation:result.confirmation,subject:result.subject};
   $('pushok-id-confirm-identity').textContent='Подтверждённый Telegram ID: '+result.telegram_id+' · Pushok ID: '+result.subject+'. Сверьте эти данные в центральном кабинете.';
   $('pushok-id-confirm-form').hidden=false;$('pushok-id-status').textContent='Личность подтверждена центральным кабинетом. Требуется ваше согласие.';
  }else if(result.state==='pending')$('pushok-id-status').textContent='Ожидается подтверждение в Pushok ID. Если вкладка потеряна, начните новый запрос.';
  else $('pushok-id-status').textContent='Нет действующего запроса. Нажмите «Начать новый запрос».';
 }catch(e){if(identityCurrent(seq,epoch))$('pushok-id-status').textContent=e.message;}
 finally{if(identityCurrent(seq,epoch))identityControls(false);}
}
function finishPushokIdentity(){
 if(!pushokIdConfirmation||!$('pushok-id-consent').checked||pushokIdBusy||pushokIdFinish||!$('pushok-id-dialog').open)return;
 const seq=++pushokIdSeq,epoch=sessionEpoch,body={...pushokIdConfirmation,confirm:true};
 identityControls(true);
 pushokIdFinish=(async()=>{
  try{
   const result=await api('/api/auth/pushok-id/finish',body);
   if(epoch!==sessionEpoch)return;
   if(result.ok!==true)throw new Error('Вход не подтверждён');
   $('pushok-id-dialog').close();clearPushokIdentity();$('auth-dialog').close();
   await refreshMe();if(epoch===sessionEpoch&&me&&!logoutPending){show('account-dialog');toast('Pushok ID связан с вашим аккаунтом Shop.');}
  }catch(e){if(identityCurrent(seq,epoch))$('pushok-id-status').textContent=e.message;}
  finally{if(identityCurrent(seq,epoch))identityControls(false);pushokIdFinish=null;}
 })();
 return pushokIdFinish;
}
$('pushok-id-signin').onclick=()=>openPushokIdentity('signin');
$('pushok-id-link').onclick=()=>openPushokIdentity('link');
$('pushok-id-start').onclick=startPushokIdentity;
$('pushok-id-check').onclick=checkPushokIdentity;
$('pushok-id-consent').onchange=()=>identityControls(pushokIdBusy);
$('pushok-id-confirm-form').onsubmit=event=>{event.preventDefault();return finishPushokIdentity();};
$('pushok-id-dialog').addEventListener('cancel',event=>{if(pushokIdBusy)event.preventDefault();});
$('pushok-id-dialog').addEventListener('close',clearPushokIdentity);
$('pushok-id-cancel').onclick=async()=>{
 if(pushokIdBusy)return;const seq=++pushokIdSeq,epoch=sessionEpoch;identityControls(true);
 try{await api('/api/auth/pushok-id/cancel',{});if(identityCurrent(seq,epoch))$('pushok-id-dialog').close();}
 catch(e){if(identityCurrent(seq,epoch))$('pushok-id-status').textContent=e.message;}
 finally{if(identityCurrent(seq,epoch))identityControls(false);}
};
$('pushok-id-revoke').onclick=async()=>{
 if(!me?.pushok_id?.linked||logoutPending||!window.confirm('Завершить сеансы Shop, созданные через Pushok ID? Подписки не будут отключены.'))return;
 const epoch=sessionEpoch,button=$('pushok-id-revoke');button.disabled=true;
 try{
  const result=await api('/api/auth/pushok-id/revoke',{});
  if(epoch!==sessionEpoch)return;
  if(result.signed_out){sessionEpoch++;meRequest++;clearPrivate();}
  await refreshMe();toast('Сеансы входа через Pushok ID завершены.');
 }catch(e){if(epoch===sessionEpoch)toast(e.message);}finally{button.disabled=false;}
};

$('account-btn').onclick = async () => { if(logoutPending)return; try { await refreshMe(); setShopAccountPane('subscriptions'); show(me?'account-dialog':'auth-dialog'); } catch(e) { toast(e.message); } };
function showForgotForm(show){
 $('auth-form').hidden=show;$('auth-switch').hidden=show;$('forgot-open').hidden=show;$('forgot-form').hidden=!show;
 $('auth-title').textContent=show?'Сменить пароль.':'С возвращением.';
}
$('auth-dialog').addEventListener('close',()=>{showForgotForm(false);$('forgot-form').reset();$('forgot-status').textContent='';register=false;});
$('forgot-open').onclick=()=>{const email=$('auth-form').email.value;$('forgot-form').email.value=email;showForgotForm(true);};
$('forgot-back').onclick=()=>showForgotForm(false);
$('forgot-form').onsubmit=async event=>{
 event.preventDefault();const button=event.target.querySelector('button[type=submit]');button.disabled=true;
 try{const email=new FormData(event.target).get('email');await api('/api/auth/password-reset/request',{email});$('forgot-status').textContent='Если аккаунт с таким email зарегистрирован и почта настроена, письмо будет отправлено.'}
 catch(error){$('forgot-status').textContent=error.message}
 finally{button.disabled=false}
};
$('auth-switch').onclick = () => { register=!register; $('auth-title').textContent=register?'Добро пожаловать.':'С возвращением.'; $('auth-submit').textContent=register?'Создать аккаунт →':'Войти →'; $('auth-switch').textContent=register?'Уже есть аккаунт? Войти':'Создать аккаунт'; $('auth-form').password.autocomplete=register?'new-password':'current-password'; $('forgot-open').hidden=register; };
$('auth-form').onsubmit = async event => {
  event.preventDefault(); if(logoutPending || passwordOperation)return; const b=$('auth-submit'); b.disabled=true;
  try { const form=new FormData(event.target); await api('/api/auth/'+(register?'register':'login'),Object.fromEntries(form)); sessionEpoch++; $('auth-form').reset(); $('auth-dialog').close(); await refreshMe(); if(selectedPlan) await beginCheckout(selectedPlan.id); else {setShopAccountPane(defaultAccountPane());show('account-dialog');} }
  catch(e) { toast(e.message); } finally { b.disabled=false; }
};
$('checkout-form').onsubmit = async event => {
  event.preventDefault(); const b=$('checkout-submit'); b.disabled=true; const request=paymentRequest,epoch=sessionEpoch;
  try { if(!checkoutTrial && $('promo-code').value.trim() && !checkoutQuote)throw new Error('Сначала нажмите «Применить» и проверьте сумму скидки.'); const email=new FormData(event.target).get('receipt_email'); const path=checkoutTrial?'/api/trial':'/api/orders';const payload=checkoutTrial?{protocol:checkoutTrial.protocol,policy_version:checkoutTrial.version,accept_terms:$('accept-terms').checked}:{plan:selectedPlan.id,plan_version:selectedPlan.version,protocol,receipt_email:email||null,accept_terms:$('accept-terms').checked,...(checkoutPolicyVersion===null?{}:{payment_policy_version:checkoutPolicyVersion}),...(checkoutQuote?{promo_code:checkoutQuote.promo_code,promo_version:checkoutQuote.promo_version,quoted_amount:checkoutQuote.amount,quote_currency:checkoutQuote.currency}:{})};const order=await api(path,payload);if(request!==paymentRequest || epoch!==sessionEpoch || !$('checkout-dialog').open)return;promoIntent='';checkoutTrial=null;paymentResult(order);await refreshMe(); }
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
  const remoteSubscription = connection.remoteSubscription;
  const install = wg ? 'Установите приложение WireGuard для выбранного устройства.' : 'Установите VPN-клиент с поддержкой '+protocolName(connection.protocol)+' для выбранного устройства.';
  const source = platform==='ios' || platform==='macos' ? 'Ищите приложение в App Store или на официальном сайте его разработчика.' : platform==='android' ? 'Используйте магазин приложений или официальный сайт разработчика.' : 'Скачайте клиент с официального сайта его разработчика для вашей системы.';
  const importing = wg ? (platform==='linux' ? 'Скачайте файл .conf. Импортируйте его в установленный клиент WireGuard; при ручной настройке обратитесь к администратору.' : 'Скачайте файл .conf. В WireGuard добавьте туннель и выберите импорт из файла.') : remoteSubscription ? 'Скопируйте HTTPS-ссылку. В VPN-клиенте выберите добавление подписки по URL или по ссылке; обновление подписки будет получать конфигурации с панели.' : 'Нажмите «Копировать настройки». В VPN-клиенте выберите добавление профиля или импорт из буфера обмена. Можно также скачать ссылку как .txt.';
  const alternative = mobile ? 'Если настройки открыты на другом экране, покажите QR-код и отсканируйте его внутри VPN-клиента.' : 'Для телефона можно открыть QR-код на этом компьютере и отсканировать его в VPN-клиенте телефона.';
  const titles = ['Установите приложение','Импортируйте настройки','Включите VPN'];
  const steps = [install+' '+source, importing+' '+alternative, 'Выберите импортированный профиль и включите подключение. Разрешите добавление VPN, если устройство запросит это. Затем откройте сайт в браузере и проверьте интернет.'];
  $('connection-steps').innerHTML = steps.map((text,i)=>`<li><h3>${titles[i]}</h3><p>${esc(text)}</p></li>`).join('');
  $('config-help').textContent = wg ? 'Импортируйте именно файл .conf или сканируйте код внутри WireGuard. Не используйте этот профиль в VLESS-клиенте.' : connection.protocol==='tuic' ? 'Импортируйте ссылку tuic:// целиком в клиент с поддержкой TUIC v5. Нужен доступный UDP-порт. Проверьте ALPN h3, режим UDP и алгоритм из ссылки. Оставьте 0-RTT выключенным и не отключайте проверку сертификата TLS.' : connection.protocol==='hysteria2' ? 'Импортируйте ссылку hysteria2:// целиком в клиент с поддержкой Hysteria2. Для подключения нужен доступный UDP-порт. Не отключайте проверку сертификата TLS; при блокировке UDP выберите другой доступный протокол.' : connection.protocol==='vmess' ? 'Импортируйте ссылку vmess:// целиком в клиент с поддержкой VMess AEAD и выбранного TCP/WS + TLS. Включите автоматическую дату и время. Не отключайте проверку сертификата TLS.' : remoteSubscription ? 'Это ссылка подписки. Добавьте её в клиент как подписку по URL; QR-код также содержит ссылку. Поддержка подписок зависит от выбранного клиента.' : 'Клиент должен поддерживать параметры вашей VLESS-ссылки. Скопируйте ссылку целиком, без лишних пробелов. Обычная камера телефона может не распознать VPN-конфигурацию.';
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
    connection = {id:sid,deviceId:device?.id,deviceExpires:device?.expires,protocol:sub.protocol,remoteSubscription:sub.protocol==='xui' && result.config.startsWith('https://'),expires:Math.min(sub.expires,device?.expires || Infinity),path};
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
  if(event.target.closest('[data-account-plans]')){
    $('account-dialog').close();setShopMobileActive('plans');setMiniappActive('plans');
    $('plans').scrollIntoView({behavior:'smooth',block:'start'});return;
  }
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
    setShopMobileActive('plans');setMiniappActive('plans');
    $('plans').scrollIntoView({behavior:'smooth',block:'start'}); toast('Выберите срок продления. Оставшиеся дни сохранятся.'); return;
  }
  const button=event.target.closest('[data-config]');
  if (button) openConnection(button.dataset.config).catch(e=>toast(e.message));
};
function renderCheckoutAmount() {
  if(!selectedPlan || checkoutTrial)return;
  const summary=selectedPlan.name+' / '+selectedPlan.days+' дней / '+protocolName(protocol)+' / '+profileAllowance(selectedPlan.device_limit ?? 4);
  $('checkout-info').textContent=summary+' / '+(checkoutQuote?'Было '+money(checkoutQuote.base_amount,checkoutQuote.currency)+' · скидка '+money(checkoutQuote.discount,checkoutQuote.currency)+' · к оплате '+money(checkoutQuote.amount,checkoutQuote.currency):selectedPlan.price===0?'Бесплатно':money(selectedPlan.price,catalog.currency));
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
  const available=trialProtocolAvailable();
  const messages={telegram_required:catalog.mtproxy_login?'Войдите через MTProxy, чтобы попробовать VPN.':'Войдите через Telegram, чтобы попробовать VPN.',existing_customer:'Пробный доступ доступен аккаунтам без прежних заказов и подписок.',limit_reached:'Пробные активации временно недоступны.',not_configured:'Пробный доступ готовится к запуску.',disabled:'Пробный доступ отключён.'};
  $('trial-info').textContent=trial.claim?'Пробный доступ уже выбран: '+protocolName(trial.claim.protocol)+'. Статус и настройки — в кабинете.':trial.eligible?trial.days+' дн. · '+protocolName(protocol)+' · только основной профиль.':messages[trial.reason] || 'Пробный доступ уже использован.';
  $('trial-start').textContent=trial.claim?'Открыть кабинет':trial.reason==='telegram_required'?(catalog.mtproxy_login?'Войти через MTProxy':'Войти через Telegram'):'Попробовать бесплатно';
  const loginAvailable=Boolean(catalog.mtproxy_login || catalog.bot_username);
  if(!trial.claim && trial.reason==='telegram_required' && !loginAvailable){
    $('trial-info').textContent='Магазин пока не подключил вход через Telegram.';
    $('trial-start').textContent='Вход через Telegram недоступен';
  }
  $('trial-start').disabled=!trial.claim && !(trial.eligible&&available) && !(trial.reason==='telegram_required' && loginAvailable);
}
async function startMtproxyLogin() {
  await api('/api/auth/mtproxy/start', {});
  location.assign('/?shop_login=1#login');
}
$('auth-mtproxy').onclick=()=>startMtproxyLogin().catch(e=>toast(e.message));
async function beginTrial() {
  selectedPlan=null;checkoutTrial=null;resetQuote();
  await refreshMe();const trial=catalog.trial;
  if(trial?.claim){setShopAccountPane('subscriptions');show('account-dialog');return;}
  if(!me?.telegram_id){
    if(catalog.mtproxy_login){
      if(me){show('account-dialog');toast('Для входа через Telegram сначала выйдите из этого аккаунта магазина.');return;}
      await startMtproxyLogin();
    } else if(catalog.bot_username){
      location.assign('https://t.me/'+encodeURIComponent(catalog.bot_username));
    } else {
      show('auth-dialog');toast('Вход через Telegram пока не настроен в этом магазине.');
    }
    return;
  }
  if(!trial?.eligible || !trialProtocolAvailable())throw new Error('Пробный доступ сейчас недоступен.');
  selectedPlan=null;checkoutTrial={protocol,version:trial.version};$('promo-controls').hidden=true;
  $('checkout-info').textContent='Пробный доступ / '+trial.days+' дн. / '+protocolName(protocol)+' / без оплаты / только основной профиль';
  clearPayment();$('checkout-form').hidden=false;$('receipt-label').hidden=true;$('receipt-label').querySelector('input').required=false;
  $('terms-text').textContent=catalog.terms;$('accept-terms').checked=false;$('accept-terms').required=true;$('checkout-submit').textContent='Получить пробный доступ →';show('checkout-dialog');
}
$('trial-start').onclick=()=>beginTrial().catch(e=>toast(e.message));
function clearFlowLink() {
  flowLaunchUrl=null;
  $('flow-open').disabled=true;
  $('flow-open-note').textContent='Для Pushok Flow 0.41 и новее нужна включённая HTTPS-ссылка подписки.';
}
function linkTargetReady(parent) {
  if (!parent || parent.protocol!==linkProtocol || parent.state!=='active' ||
      !Number.isFinite(parent.expires) || parent.expires*1000<=Date.now()) return false;
  if (!linkDeviceId) return (parent.primary_key_state || 'active')==='active';
  const device=deviceCache.find(d=>d.id===linkDeviceId);
  return parent.protocol==='wg' && deviceSubscription===parent.id && $('devices-dialog').open &&
    device?.protocol==='wg' && device.ready===true && device.state==='active' &&
    Number.isFinite(device.expires) && device.expires*1000>Date.now();
}
function flowUrlFor(state, parent) {
  if (typeof PushokFlowLinks==='undefined' || state?.enabled!==true ||
      !['xui','wg'].includes(parent?.protocol) || !linkTargetReady(parent)) return null;
  return PushokFlowLinks.landingUrl(state.url, 'Подписка Pushok', location.origin, basePath);
}
function renderFlowLink(state, parent) {
  flowLaunchUrl=flowUrlFor(state,parent);
  $('flow-open').disabled=!flowLaunchUrl;
  $('flow-open-note').textContent=flowLaunchUrl
    ? 'Откроется страница передачи подписки. Подтвердите добавление в Pushok Flow.'
    : linkIsWireGuard() ? 'Для WireGuard нужна версия Pushok Flow 0.57 или новее и HTTPS-ссылка.'
      : 'Для VLESS нужна версия Pushok Flow 0.41 или новее и HTTPS-ссылка.';
}
function linkApiPath(sid) {
  if(linkDeviceId)return '/api/flow/wireguard/devices/'+encodeURIComponent(linkDeviceId)+'/link';
  const encoded=encodeURIComponent(sid);
  return linkProtocol==='wg' ? '/api/flow/wireguard/subscriptions/'+encoded+'/link'
    : '/api/subscriptions/'+encoded+'/link';
}
function linkIsWireGuard() {
  return linkProtocol==='wg';
}
function clearLink() {
  linkRequest++;linkSubscription=null;linkDeviceId=null;linkProtocol=null;linkState=null;
  clearFlowLink();
  $('link-value').value='';$('link-status').textContent='';$('link-follow').checked=false;
  for(const id of ['copy-link','link-enable','link-rotate','link-disable'])$(id).disabled=true;
}
function renderLink(state) {
  const parent=me?.subscriptions.find(s=>s.id===linkSubscription);
  if(linkDeviceId && !linkTargetReady(parent)){ $('link-dialog').close();clearLink();return; }
  linkState=state;$('link-value').value=state.url || '';$('link-follow').checked=!!state.follow_rotation;
  const wireguard=linkIsWireGuard();
  $('link-title').textContent=linkDeviceId?'Ссылка WireGuard для устройства':wireguard?'Ссылка WireGuard для Pushok Flow':'Ссылка подписки VLESS';
  $('link-description').textContent='Личная ссылка отдаёт настройки только '+(linkDeviceId?'выбранного устройства':'основного профиля')+'. Любой, кто получил ссылку, сможет получить VPN-ключ — храните её приватно.';
  $('link-revoke-note').textContent=linkDeviceId?'Отключение ссылки не отзывает скачанный VPN-ключ. Для отзыва используйте управление устройством. Перевыпуск ключа отключит прежнюю ссылку.':'Отключение или замена ссылки не отзывает уже скачанный VPN-ключ. Для его отзыва используйте смену основного ключа. Частота обновления зависит от VPN-клиента.';
  $('link-follow-settings').hidden=wireguard;
  $('copy-link').disabled=!state.enabled;$('link-rotate').disabled=!state.enabled;$('link-disable').disabled=!state.enabled;
  renderFlowLink(state,parent);
  $('link-enable').disabled=!linkTargetReady(parent) || (wireguard && state.enabled);
  $('link-enable').textContent=state.enabled?(wireguard?'Ссылка включена':'Сохранить настройки'):'Создать ссылку';
  $('link-status').textContent=state.enabled?'Ссылка включена. Импортируйте её как URL подписки.':'Ссылка отключена.';
}
async function openLink(sid,did=null) {
  clearLink();
  const parent=me?.subscriptions.find(s=>s.id===sid);
  if (!parent || !['xui','wg'].includes(parent.protocol)) return;
  linkSubscription=sid;linkProtocol=parent.protocol;linkDeviceId=did;
  if(did && !linkTargetReady(parent)){clearLink();return;}
  $('link-status').textContent='Загружаем…';show('link-dialog');await refreshLink();
}
async function refreshLink() {
  if(!linkSubscription || !['xui','wg'].includes(linkProtocol))return;
  const parent=me?.subscriptions.find(s=>s.id===linkSubscription);
  if(!parent || parent.protocol!==linkProtocol || (linkDeviceId && !linkTargetReady(parent))) {
    clearFlowLink();clearLink();$('link-dialog').close();return;
  }
  const sid=linkSubscription,request=++linkRequest;
  linkState=null;$('link-value').value='';clearFlowLink();
  for(const id of ['copy-link','link-enable','link-rotate','link-disable'])$(id).disabled=true;
  const state=await api(linkApiPath(sid));
  if(request===linkRequest && $('link-dialog').open)renderLink(state);
}
async function changeLink(action) {
  if(!linkSubscription || !linkState)return;
  const parent=me?.subscriptions.find(s=>s.id===linkSubscription);
  if (!parent || parent.protocol!==linkProtocol || parent.state!=='active' ||
      !Number.isFinite(parent.expires) || parent.expires*1000<=Date.now() ||
      (linkDeviceId && !linkTargetReady(parent))) {
    clearFlowLink();return;
  }
  const sid=linkSubscription,version=linkState.version,request=++linkRequest;
  for(const id of ['copy-link','link-enable','link-rotate','link-disable'])$(id).disabled=true;
  linkState=null;$('link-value').value='';clearFlowLink();
  try {
    const state=await api(linkApiPath(sid),linkIsWireGuard()?{action,version}:{action,version,follow_rotation:$('link-follow').checked},'PUT');
    if(request!==linkRequest || !$('link-dialog').open)return;
    renderLink(state);await refreshMe();
  } catch(e) {
    if(request===linkRequest) {
      linkState=null;$('link-value').value='';clearFlowLink();
      for(const id of ['copy-link','link-enable','link-rotate','link-disable'])$(id).disabled=true;
      $('link-status').textContent=e.message+' Нажмите «Обновить» перед повтором.';
    }
  }
}
$('link-dialog').addEventListener('close',clearLink);
$('flow-open').onclick=()=>{
  const parent=me?.subscriptions.find(s=>s.id===linkSubscription);
  const target=flowUrlFor(linkState,parent);
  if (!target || target!==flowLaunchUrl || !$('link-dialog').open || $('flow-open').disabled) {
    clearFlowLink();return;
  }
  try {
    if (telegramApp?.initData && typeof telegramApp.openLink==='function') {
      // Telegram Mini Apps accept HTTP(S) openLink URLs; the landing owns the explicit native-app action.
      telegramApp.openLink(target,{try_instant_view:false});
    } else window.open(target,'_blank','noopener,noreferrer');
  } catch { toast('Не удалось открыть страницу. Скопируйте ссылку подписки и добавьте её в Pushok Flow.'); }
};
$('link-form').onsubmit=event=>{event.preventDefault();changeLink('enable');};
$('link-refresh').onclick=()=>refreshLink().catch(e=>toast(e.message));
$('link-rotate').onclick=()=>{if(window.confirm('Заменить ссылку? Прежний URL перестанет работать. Его нужно заменить в VPN-клиенте.'))changeLink('rotate');};
$('link-disable').onclick=()=>{if(window.confirm('Отключить обновление по этой ссылке? Уже скачанный VPN-ключ останется действующим.'))changeLink('disable');};
$('copy-link').onclick=async()=>{
  if(!linkState?.enabled || !$('link-value').value ||
      (linkDeviceId && !linkTargetReady(me?.subscriptions.find(s=>s.id===linkSubscription))))return;
  const request=linkRequest;
  try {await navigator.clipboard.writeText($('link-value').value);if(request===linkRequest)toast('Ссылка скопирована. Добавьте её как подписку в VPN-клиент.');}
  catch {if(request===linkRequest){$('link-value').select();toast('Ссылка выделена для ручного копирования.');}}
};
function clearDevices() {
  if(linkDeviceId){$('link-dialog').close();clearLink();}
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
  let result;
  try {result=await api('/api/subscriptions/'+encodeURIComponent(sid)+'/devices');}
  catch(e){if(request===deviceRequest && linkDeviceId && linkSubscription===sid){$('link-dialog').close();clearLink();}throw e;}
  if (request!==deviceRequest || sid!==deviceSubscription) return;
  deviceCache=result.devices;
  const parent=me?.subscriptions.find(s=>s.id===sid);
  $('device-form').hidden=!parent || parent.state!=='active' || parent.expires*1000<=Date.now() || result.devices.length>=result.max_extra;
  const labels={pending:'Готовим профиль',active:'Профиль выдан',revoking:'Отключаем доступ',revoked:'Доступ отозван',rotating:'Меняем ключ'};
  $('devices-list').innerHTML=result.devices.map(d=>`<article class="subscription"><h3>${esc(d.name)}</h3><p>${esc(labels[d.state] || d.state)}${d.retrying?' · повторим после ошибки':''}${d.state==='active'&&!d.ready?' · срок доступа истёк':''}</p><div class="actions">${d.ready?`<button class="small" data-device-config="${esc(d.id)}">Подключить ↗</button><button class="small secondary" data-device-rotate="${esc(d.id)}">Перевыпустить ключ</button>${d.protocol==='wg'?`<button class="small secondary" data-device-link="${esc(d.id)}">Открыть в Pushok Flow</button>`:''}`:''}${['pending','active','rotating'].includes(d.state)?`<button class="small secondary" data-device-revoke="${esc(d.id)}">Отозвать доступ</button>`:''}</div></article>`).join('') || '<p>Дополнительных профилей пока нет.</p>';
  $('devices-status').textContent='Дополнительные профили: '+result.devices.length+' из '+result.max_extra+'. Основной профиль — отдельно.'+(result.devices.length>result.max_extra?' Уже выданные профили работают; новые недоступны до освобождения лимита.':'');
  if (linkDeviceId && linkSubscription===sid) {
    if(!linkTargetReady(parent)){ $('link-dialog').close();clearLink(); }
    else if(linkState)renderFlowLink(linkState,parent);
  }
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
  const link=event.target.closest('[data-device-link]');
  if(link){await openLink(deviceSubscription,link.dataset.deviceLink).catch(e=>toast(e.message));return;}
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
        if(reopen){setShopAccountPane('settings');show('account-dialog');}
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
  const pending=Promise.allSettled([passwordOperation,pushokIdFinish]);
  sessionEpoch++; meRequest++; clearPrivate();
  try { if(pending)await pending; await api('/api/auth/logout',{}); catalog=await api('/api/catalog'); renderPlans(); }
  catch(e) { toast('Не удалось подтвердить выход. '+e.message); }
  finally {logoutPending=false;$('logout').disabled=false;}
};
(async () => {
  try {
    catalog=await api('/api/catalog'); protocol=catalog.protocols.find(p=>p.available)?.id || 'xui'; renderPlans(); $('support').textContent=catalog.support;
    $('auth-mtproxy').hidden=!catalog.mtproxy_login;
    $('auth-methods').textContent=catalog.mtproxy_login?'Подтвердите вход в кабинете MTProxy или войдите по email.':catalog.bot_username?'Войдите по email или откройте магазин из Telegram-бота.':'Войдите по email.';
    if(catalog.bot_username) { for(const id of ['bot-link','auth-bot']) { $(id).href='https://t.me/'+encodeURIComponent(catalog.bot_username); $(id).hidden=false; } }
    const tg=window.Telegram?.WebApp;
    if(tg?.initData) {
      tg.ready(); tg.expand(); syncTelegramTheme(); tg.onEvent?.('themeChanged',syncTelegramTheme);
      try { await api('/api/auth/telegram',{init_data:tg.initData}); }
      catch(e) {
        // A Mini App opened by MTProxy has a different bot signature. Only the
        // separately authenticated Shop cookie can resume the cabinet here.
        if(e.status!==401 && e.status!==403 && e.status!==503)throw e;
      }
      setupMiniappNavigation();
    }
    await refreshMe();
    // Переход из уведомления не даёт прав: вход уже проверен выше.
    if(new URLSearchParams(location.search).get('view')==='account')show(me?'account-dialog':'auth-dialog');
    // Ссылка передаёт только код: процент и право на скидку проверяет сервер после явного применения.
    const incomingPromos=new URLSearchParams(location.search).getAll('promo');
    const incomingPromo=incomingPromos.length===1?incomingPromos[0].trim().toUpperCase():'';
    if(/^[A-Z0-9][A-Z0-9_-]{3,31}$/.test(incomingPromo)){
      promoIntent=incomingPromo;toast('В ссылке есть промокод. Выберите тариф и примените его перед оформлением.');
    }
    if(/^[a-f0-9]{24}$/.test(new URLSearchParams(location.search).get('ref') || '')) {if(me)setShopAccountPane('referrals-panel');show(me?'account-dialog':'auth-dialog');toast('Примите приглашение в кабинете до первого заказа или пробного доступа.');}
    if(new URLSearchParams(location.search).has('payment')) { if(me)setShopAccountPane('orders');history.replaceState(null,'',basePath+'/'); show(me?'account-dialog':'auth-dialog'); }
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


const adsLabels={pending:'На модерации',approved:'Модерация пройдена',rejected:'Отклонено',canceled:'Отменено'};
function adsPaymentLabel(p){return p?.status==='confirmed'?'Поступление учтено владельцем вручную.':p?.status==='refunded'?'Полный возврат учтён владельцем вручную.':'Поступление не учтено.';}
function adsPlacementLabel(p){
  if(!p)return 'Размещение не запущено.';
  const labels={active:'Запуск подтверждён владельцем',paused:'Пауза подтверждена владельцем',finished:'Остановка подтверждена владельцем'};
  return (labels[p.state] || p.state)+' · '+p.site_name+' · осталось '+p.remaining_seconds+' сек.'+(p.attention_due?' Срок истёк, ожидается подтверждение остановки.':'');
}
function adsBookingLabel(b){
  if(!b)return 'Плановой брони нет.';
  const labels={reserved:'Забронировано владельцем',canceled:'Бронь отменена',consumed:'Бронь использована при запуске'};
  return (labels[b.state] || b.state)+' · '+b.site_name+' · '+new Date(b.starts_at*1000).toLocaleString('ru-RU')+' — '+new Date(b.ends_at*1000).toLocaleString('ru-RU')+' (время браузера)'+(b.expired?' · ожидается перенос или отмена':'');
}
async function refreshAds() {
  const request=++adsRequest,epoch=sessionEpoch,page=adsPage;
  const [policy,listing]=await Promise.all([api('/api/ads/policy'),api('/api/ads/requests?page='+page)]);
  if(request!==adsRequest || epoch!==sessionEpoch || !$('ads-dialog').open)return;
  adsPolicy=policy;
  $('ads-offer').textContent=(policy.accepting?'Приём заявок открыт. ':'Приём новых заявок закрыт. ')+policy.amount+' ₽ за '+policy.hours+' ч. '+policy.instructions;
  $('ads-form').hidden=!policy.accepting;
  $('ads-list').innerHTML=listing.requests.map(r=>`<article class="subscription"><b>${esc('@'+r.channel)}</b><p>${esc(adsLabels[r.state] || r.state)}</p><p>${r.amount} ₽ / ${r.hours} ч · условия сохранены при подаче</p><p>${esc(r.comment)}</p><p>${esc(r.reason)}</p><code>${esc(r.id)}</code><p>${esc(adsPaymentLabel(r.payment))}</p><p>${esc(adsPlacementLabel(r.placement))}</p><p>${esc(adsBookingLabel(r.booking))}</p>${['pending','approved'].includes(r.state) && r.payment?.status!=='confirmed' && r.booking?.state!=='reserved' && !['active','paused'].includes(r.placement?.state)?`<br><button class="text-link" data-ads-cancel="${esc(r.id)}" data-version="${r.version}">Отменить заявку</button>`:''}</article>`).join('') || '<p>Заявок пока нет.</p>';
  $('ads-page').textContent='Страница '+page+' · всего '+listing.total;
  $('ads-prev').disabled=page===1;$('ads-next').disabled=page*listing.page_size>=listing.total;
}
$('ads-dialog').addEventListener('close',clearAds);
$('ads-open').onclick=async()=>{
  if(!me || logoutPending)return;
  const epoch=sessionEpoch;show('ads-dialog');try{await refreshAds();}catch(e){if(epoch===sessionEpoch && $('ads-dialog').open)toast(e.message);}
};
async function reloadAds(){const epoch=sessionEpoch;try{await refreshAds();}catch(e){if(epoch===sessionEpoch && $('ads-dialog').open)toast(e.message);}}
$('ads-refresh').onclick=()=>reloadAds();
$('ads-prev').onclick=()=>{if(adsPage>1){adsPage--;return reloadAds();}};
$('ads-next').onclick=()=>{if(!$('ads-next').disabled){adsPage++;return reloadAds();}};
$('ads-form').onsubmit=async event=>{
  event.preventDefault();if(!adsPolicy?.accepting || $('ads-submit').disabled)return;
  const epoch=sessionEpoch,request=adsRequest,mutation=++adsMutation;
  const channel=$('ads-channel').value.trim(),comment=$('ads-comment').value.trim();
  const key=JSON.stringify([channel,comment,adsPolicy.version]);if(!adsIntent.has(key))adsIntent.set(key,crypto.randomUUID());
  $('ads-submit').disabled=true;
  try {
    await api('/api/ads/requests',{request_id:adsIntent.get(key),policy_version:adsPolicy.version,channel,comment});
    if(epoch!==sessionEpoch || request!==adsRequest || !$('ads-dialog').open)return;
    adsIntent.delete(key);$('ads-channel').value='';$('ads-comment').value='';adsPage=1;
    await refreshAds();if(epoch===sessionEpoch && mutation===adsMutation && $('ads-dialog').open)$('ads-status').textContent='Заявка отправлена. Оплата не выполнялась.';
  }catch(e){if(epoch===sessionEpoch && request===adsRequest && $('ads-dialog').open)$('ads-status').textContent=e.message;}
  finally{if(epoch===sessionEpoch && mutation===adsMutation)$('ads-submit').disabled=false;}
};
$('ads-list').onclick=async event=>{
  const b=event.target.closest('[data-ads-cancel]');if(!b)return;
  const epoch=sessionEpoch,request=adsRequest;b.disabled=true;
  try{await api('/api/ads/requests/'+encodeURIComponent(b.dataset.adsCancel)+'/cancel',{version:Number(b.dataset.version)});if(epoch===sessionEpoch && request===adsRequest && $('ads-dialog').open)await refreshAds();}
  catch(e){if(epoch===sessionEpoch && request===adsRequest && $('ads-dialog').open)$('ads-status').textContent=e.message;}
  finally{b.disabled=false;}
};

// Compact account workspace: only one panel is shown at a time. Navigation
// changes presentation only; existing subscription/order/support APIs remain.
const SHOP_ACCOUNT_PANES = {
  subscriptions: 'subscriptions-pane',
  orders: 'orders-pane',
  'referrals-panel': 'referrals-panel',
  'support-panel': 'support-panel',
  settings: 'account-settings-pane',
};
function defaultAccountPane(){
  const params=new URLSearchParams(location.search);
  return params.has('payment')?'orders':/^[a-f0-9]{24}$/.test(params.get('ref')||'')?'referrals-panel':'subscriptions';
}
function setShopAccountPane(name, focus = false) {
  const chosen = Object.hasOwn(SHOP_ACCOUNT_PANES, name) && (name!=='referrals-panel'||me?.referrals) ? name : 'subscriptions';
  for (const [key, id] of Object.entries(SHOP_ACCOUNT_PANES)) {
    const pane = $(id);
    pane.dataset ||= {};
    pane.dataset.accountActive = key === chosen ? 'true' : 'false';
  }
  for (const button of document.querySelectorAll('[data-account-section]')) {
    const selected = button.dataset.accountSection === chosen;
    button.setAttribute('aria-pressed', String(selected));
  }
  if (focus && $('account-dialog').open) {
    const pane = $(SHOP_ACCOUNT_PANES[chosen]);
    pane.setAttribute('tabindex','-1');
    pane.focus?.({preventScroll:true});
    $('account-dialog').scrollTop=0;
  }
}
document.querySelectorAll('[data-account-section]').forEach(button => {
  button.addEventListener('click', () => setShopAccountPane(button.dataset.accountSection,true));
});

// One browser tab bar; Telegram keeps its dedicated Mini App navigation.
let shopCurrentRoute='home';
function setShopMobileActive(route){
  if(!['home','plans','help','account'].includes(route))route='home';
  if(route!=='account')shopCurrentRoute=route;
  if($('account-dialog').open)route='account';
  for(const item of document.querySelectorAll('[data-shop-route]')){
    if(item.dataset.shopRoute===route)item.setAttribute('aria-current','page');
    else item.removeAttribute('aria-current');
  }
}
$('hero-account-btn').onclick = () => $('account-btn').click();
$('shop-mobile-account').onclick = () => $('account-btn').click();
document.querySelectorAll('a[data-shop-route]').forEach(link => {
  link.addEventListener('click', () => setShopMobileActive(link.dataset.shopRoute));
});
$('account-dialog').addEventListener('close',()=>{if(!$('account-dialog').open)setShopMobileActive(shopCurrentRoute);});
function syncShopRoute(){
  const hash=(location.hash||'').slice(1);
  const route=hash==='how'?'plans':hash;
  if(['home','plans','help'].includes(route))setShopMobileActive(route);
}
window.addEventListener?.('hashchange',syncShopRoute);
syncShopRoute();
if('IntersectionObserver' in window){
  const observer=new IntersectionObserver(entries=>{
    if($('account-dialog').open)return;
    const visible=entries.filter(e=>e.isIntersecting).sort((a,b)=>b.intersectionRatio-a.intersectionRatio)[0];
    if(visible)setShopMobileActive(visible.target.id);
  },{rootMargin:'-15% 0px -65% 0px',threshold:[0,.15,.35]});
  for(const id of ['home','plans','help'])observer.observe($(id));
}
