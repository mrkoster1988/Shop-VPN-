'use strict';
const basePath = document.querySelector('meta[name="shop-base"]').content;
const $ = id => document.getElementById(id);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let diagnosticsRequest=0;
let adsFinanceRequest=0,adsFinancePage=1,adsExpenseIntents=new Map();
let adsBookingIntents=new Map(),adsBookingRequest=0,adsBookingPage=1;
let adsStopRequest=0,adsStopPage=1;
let adsSitesCache=[],adsPrivateSites=[],adsSitesRequest=0,adsSiteEditor=null,adsSiteMutation=0,adsPlacementIntents=new Map();
let adsPolicy=null,adsPage=1,adsRequest=0,adsPaymentIntents=new Map();
let token = '', authEpoch = 0, usersPage = 1, usersQuery = '', usersRequest = 0;
let selectedUser = null, profilePage = 1, profileRequest = 0;
let serverCache = [], editingServer = null;
let ordersPage = 1, ordersRequest = 0, ordersFilters = {q:'', status:'', method:'', protocol:''};
async function api(path, {method = 'GET', data} = {}) {
  const epoch = authEpoch;
  const response = await fetch(basePath + '/api/admin/' + path, {
    method, headers: {Authorization: 'Bearer ' + token, ...(data ? {'Content-Type':'application/json'} : {})},
    body: data ? JSON.stringify(data) : undefined,
  });
  const result = await response.json();
  if (epoch !== authEpoch) throw new Error('Сессия администратора завершена');
  if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : 'Проверьте введённые данные');
  return result;
}
function planForm(plan) {
  return `<form class="plan-editor" data-plan-id="${esc(plan.id)}" data-version="${plan.version}">
    <div class="plan-editor-heading"><h3>${esc(plan.name)}</h3><span class="status">${plan.enabled ? 'В продаже' : 'Скрыт'}</span></div>
    <p class="muted">Код: ${esc(plan.id)}</p>
    <label>Название<input name="name" value="${esc(plan.name)}" maxlength="80" required></label>
    <div class="plan-editor-fields">
      <label>Срок, дней<input name="days" type="number" value="${plan.days}" min="1" max="3650" step="1" required></label>
      <label>Цена на сайте, ₽<input name="price" type="number" value="${plan.price}" min="1" max="1000000" step="1" required></label>
      <label>Цена в Telegram, Stars<input name="stars" type="number" value="${plan.stars}" min="1" max="100000" step="1" required></label>
      <label>Дополнительных профилей (0 — только основной)<input name="device_limit" type="number" value="${plan.device_limit ?? 4}" min="0" max="20" step="1" required></label>
    </div>
    <label class="plan-enabled"><input name="enabled" type="checkbox" ${plan.enabled ? 'checked' : ''}> Доступен для новых покупок</label>
    <button type="submit" class="small">Сохранить тариф</button>
  </form>`;
}
async function refresh({plans = true} = {}) {
  const [data, catalog] = await Promise.all([api('overview'), plans ? api('plans') : Promise.resolve(null)]);
  $('stats').innerHTML = `<div>Пользователи<strong>${data.users}</strong></div><div>Подписки<strong>${data.active}</strong></div><div>Подтверждено<strong>${data.revenue} ₽ / ${data.stars} ★</strong></div>`;
  if (catalog) $('plan-editors').innerHTML = catalog.plans.map(planForm).join('');
  await Promise.all([refreshAdsAdmin(),refreshDiagnostics(),refreshOrders(), refreshUsers(), plans ? refreshServers() : Promise.resolve(), plans ? refreshTrialPolicy() : Promise.resolve(), plans ? refreshCoupons() : Promise.resolve(), plans ? refreshReferralPolicy() : Promise.resolve()]);
}
$('admin-login').onsubmit = async event => {
  event.preventDefault();
  const button = event.target.querySelector('button'); button.disabled = true;
  authEpoch++; token = $('key').value;
  try { await refresh(); $('key').value = ''; $('admin-login').hidden = true; $('dashboard').hidden = false; $('error').textContent = ''; }
  catch (error) { authEpoch++; token = ''; $('error').textContent = error.message; }
  finally { button.disabled = false; }
};
$('reload').onclick = () => refresh().catch(error => $('error').textContent = error.message);
$('exit').onclick = () => {
  adsFinanceRequest++;adsFinancePage=1;adsExpenseIntents.clear();
  for(const id of ['ads-finance-summary','ads-finance-obligations','ads-finance-error','ads-expense-list','ads-expense-status','ads-finance-page'])$(id).replaceChildren();
  for(const id of ['ads-finance-start','ads-finance-end','ads-expense-amount','ads-expense-date','ads-expense-reference','ads-expense-note'])$(id).value='';
  $('ads-expense-category').value='server';$('ads-expense-save').disabled=false;$('ads-finance-prev').disabled=true;$('ads-finance-next').disabled=true;
  adsBookingRequest++;adsBookingPage=1;adsBookingIntents.clear();
  for(const id of ['ads-bookings-list','ads-bookings-page','ads-bookings-error'])$(id).replaceChildren();
  $('ads-bookings-filter').value='reserved';$('ads-bookings-prev').disabled=true;$('ads-bookings-next').disabled=true;
  adsStopRequest++;adsStopPage=1;
  for(const id of ['ads-stop-list','ads-stop-counts','ads-stop-error','ads-stop-page'])$(id).replaceChildren();
  $('ads-stop-filter').value='due';$('ads-stop-prev').disabled=true;$('ads-stop-next').disabled=true;
  adsRequest++;adsPolicy=null;adsPage=1;adsPaymentIntents.clear();
  adsSitesRequest++;adsSitesCache=[];adsPrivateSites=[];adsPlacementIntents.clear();clearAdsSite();
  $('ads-sites-list').replaceChildren();$('ads-sites-error').textContent='';
  for(const id of ['ads-admin-list','ads-admin-error','ads-admin-page'])$(id).replaceChildren();
  for(const id of ['ads-price','ads-hours','ads-instructions'])$(id).value='';
  $('ads-accepting').checked=false;$('ads-policy-save').disabled=false;$('ads-filter').value='';$('ads-admin-prev').disabled=true;$('ads-admin-next').disabled=true;
  diagnosticsRequest++;$('diagnostics-report').replaceChildren();$('diagnostics-error').textContent='';$('diagnostics-json').value='';
  authEpoch++; token = ''; usersRequest++; usersPage = 1; usersQuery = '';
  ordersRequest++; ordersPage = 1; ordersFilters = {q:'', status:'', method:'', protocol:''};
  serverCache = []; editingServer = null; $('server-list').replaceChildren(); $('servers-notice').textContent = '';
  if ($('server-dialog').open) $('server-dialog').close();
  $('server-form').reset();
  $('orders-search').reset(); $('orders-state').textContent = ''; $('orders-page').textContent = '';
  $('orders-prev').disabled = true; $('orders-next').disabled = true;
  $('users-query').value = ''; $('users-list').replaceChildren(); $('users-page').textContent = ''; $('users-state').textContent = '';
  if ($('user-dialog').open) $('user-dialog').close();
  clearUser();
  $('referral-policy-editor').replaceChildren();$('referral-policy-count').textContent='';$('referral-policy-error').textContent='';
  $('coupons-list').replaceChildren();$('coupons-error').textContent='';$('coupon-create').reset();
  $('trial-policy-editor').replaceChildren();$('trial-policy-count').textContent='';$('trial-policy-error').textContent='';
  $('list').replaceChildren(); $('stats').replaceChildren(); $('plan-editors').replaceChildren();
  $('admin-notice').textContent = ''; $('error').textContent = ''; $('dashboard').hidden = true; $('admin-login').hidden = false;
};
$('plan-editors').onsubmit = async event => {
  event.preventDefault();
  const form = event.target.closest('[data-plan-id]');
  if (!form) return;
  const values = new FormData(form);
  const button = form.querySelector('button'); button.disabled = true;
  $('error').textContent = ''; $('admin-notice').textContent = '';
  try {
    const saved = await api('plans/' + encodeURIComponent(form.dataset.planId), {method: 'PUT', data: {
      name: values.get('name'), days: Number(values.get('days')), price: Number(values.get('price')),
      stars: Number(values.get('stars')), device_limit:Number(values.get('device_limit') ?? 4), enabled: values.has('enabled'), version: Number(form.dataset.version),
    }});
    form.outerHTML = planForm(saved);
    $('admin-notice').textContent = 'Тариф сохранён. Новые условия действуют для новых заказов.';
  } catch (error) { $('error').textContent = error.message; button.disabled = false; }
};
$('list').onclick = async event => {
  const buyer = event.target.closest('[data-order-user]');
  if (buyer) { openUser(buyer.dataset.orderUser); return; }
  const button = event.target.closest('[data-action]'); if (!button) return;
  if (button.dataset.action === 'confirm' && !confirm('Вы проверили фактическое поступление денег по этому заказу?')) return;
  button.disabled = true;
  try { await api('orders/' + encodeURIComponent(button.dataset.id) + '/' + button.dataset.action, {method: 'POST'}); await refresh({plans: false}); }
  catch (error) { $('error').textContent = error.message; button.disabled = false; }
};

$('new-plan').onclick = () => {
  $('create-plan-form').reset(); $('create-plan-error').textContent = '';
  $('create-plan-dialog').showModal();
};
$('create-plan-cancel').onclick = () => $('create-plan-dialog').close();
$('create-plan-form').onsubmit = async event => {
  event.preventDefault();
  const form = event.target;
  const values = new FormData(form);
  const button = form.querySelector('button[type="submit"]'); button.disabled = true;
  $('create-plan-error').textContent = '';
  try {
    const saved = await api('plans', {method: 'POST', data: {
      id: values.get('id'), name: values.get('name'), days: Number(values.get('days')),
      price: Number(values.get('price')), stars: Number(values.get('stars')), device_limit:Number(values.get('device_limit') ?? 4), enabled: values.has('enabled'),
    }});
    $('plan-editors').insertAdjacentHTML('beforeend', planForm(saved));
    $('create-plan-dialog').close();
    $('error').textContent = '';
    $('admin-notice').textContent = saved.enabled ? 'Тариф создан и доступен покупателям.' : 'Тариф создан и пока скрыт. Включите его после проверки условий.';
  } catch (error) { $('create-plan-error').textContent = error.message; }
  finally { button.disabled = false; }
};

const orderStatus = {pending:'Ожидает оплаты', checkout:'Оплата не завершена', paid:'Готовим доступ', active:'Доступ выдан', canceled:'Отменён'};
const protocolLabel = value => value === 'wg' ? 'WireGuard' : value === 'xui' ? 'VLESS · 3x-ui' : value;
const dateTime = value => value ? new Date(value * 1000).toLocaleString('ru-RU') : '—';
async function refreshUsers() {
  const request = ++usersRequest;
  $('users-prev').disabled = true; $('users-next').disabled = true;
  $('users-state').textContent = 'Загрузка пользователей…';
  try {
    const params = new URLSearchParams({q:usersQuery, page:String(usersPage), page_size:'25'});
    const data = await api('users?' + params);
    if (request !== usersRequest) return;
    const pages = Math.max(1, Math.ceil(data.total / data.page_size));
    $('users-state').textContent = data.total ? 'Найдено пользователей: ' + data.total : 'Пользователей не найдено.';
    $('users-page').textContent = 'Страница ' + data.page + ' из ' + pages;
    $('users-prev').disabled = data.page <= 1;
    $('users-next').disabled = data.page * data.page_size >= data.total;
    $('users-list').innerHTML = data.users.map(user => `<article class="admin-user"><div><b>${esc(user.email || 'Telegram ' + user.telegram_id)}</b><p>№ ${user.id} · Telegram: ${esc(user.telegram_id ?? '—')}</p><p>Активных подписок: ${user.active_subscriptions} · Заказов: ${user.orders_count}</p><p>Регистрация: ${esc(dateTime(user.created))}</p></div><button class="small secondary" data-user="${user.id}">Открыть карточку</button></article>`).join('');
  } catch (error) {
    if (request === usersRequest) { $('users-list').replaceChildren(); $('users-state').textContent = error.message; }
    throw error;
  }
}
function showUsers(page) {
  usersPage = page;
  refreshUsers().catch(() => {});
}
$('users-search').onsubmit = event => { event.preventDefault(); usersQuery = $('users-query').value.trim(); showUsers(1); };
$('users-reset').onclick = () => { usersQuery = ''; $('users-query').value = ''; showUsers(1); };
$('users-prev').onclick = () => showUsers(Math.max(1, usersPage - 1));
$('users-next').onclick = () => showUsers(usersPage + 1);
function clearUser() {
  selectedUser = null; profileRequest++; profilePage = 1;
  for (const id of ['user-details', 'user-subscriptions', 'user-orders']) $(id).replaceChildren();
  $('user-error').textContent = ''; $('user-title').textContent = 'Пользователь'; $('user-orders-page').textContent = '';
  $('user-orders-prev').disabled = true; $('user-orders-next').disabled = true;
}
async function loadUser(page) {
  if (selectedUser === null) return;
  const userId = selectedUser, request = ++profileRequest;
  $('user-error').textContent = 'Загрузка карточки…';
  $('user-orders-prev').disabled = true; $('user-orders-next').disabled = true;
  try {
    const data = await api('users/' + userId + '?' + new URLSearchParams({orders_page:String(page), orders_page_size:'20'}));
    if (request !== profileRequest || selectedUser !== userId || !$('user-dialog').open) return;
    profilePage = data.orders_page;
    const user = data.user;
    $('user-title').textContent = 'Пользователь № ' + user.id;
    $('user-details').innerHTML = `<p>Email: ${esc(user.email || '—')}</p><p>Telegram ID: ${esc(user.telegram_id ?? '—')}</p><p>Регистрация: ${esc(dateTime(user.created))}</p>`;
    $('user-subscriptions').innerHTML = data.subscriptions.length ? data.subscriptions.map(sub => {
      const status = sub.access_active ? 'Активна' : sub.state === 'pending' ? 'Готовится' : 'Неактивна';
      return `<article class="subscription"><b>${esc(protocolLabel(sub.protocol))}</b><p>${status} · Срок: ${esc(dateTime(sub.expires))}</p><p>Дополнительных профилей: ${sub.device_limit ?? 4}</p></article>`;
    }).join('') : '<p>Подписок пока нет.</p>';
    $('user-orders').innerHTML = data.orders.length ? data.orders.map(order => `<article class="admin-order"><b>${esc(order.plan_name || order.plan)} · ${order.days} дней · ${order.method==='referral'?'Бонус · '+order.days+' дней':order.amount+' '+(order.currency === 'XTR' ? '★' : '₽')}</b><p>${esc(protocolLabel(order.protocol))} · ${esc(orderStatus[order.status] || order.status)}</p><p>Заказ: ${esc(order.id)}</p>${order.promo_code?`<p>Код ${esc(order.promo_code)} · скидка ${order.discount} ${order.currency==='XTR'?'★':'₽'} от ${order.base_amount}</p>`:''}<p>${esc(dateTime(order.created))} · ${esc(order.method==='referral'?'Бонусное продление':order.method==='trial'?'Пробный доступ':order.method)}</p>${order.error ? `<p>Ошибка выдачи: ${esc(order.error)} · Попыток: ${order.attempts}</p>` : ''}</article>`).join('') : '<p>Заказов пока нет.</p>';
    const pages = Math.max(1, Math.ceil(data.orders_total / data.orders_page_size));
    $('user-orders-page').textContent = 'Заказов: ' + data.orders_total + ' · Страница ' + profilePage + ' из ' + pages;
    $('user-orders-prev').disabled = profilePage <= 1;
    $('user-orders-next').disabled = profilePage * data.orders_page_size >= data.orders_total;
    $('user-error').textContent = '';
  } catch (error) { if (request === profileRequest && selectedUser === userId) $('user-error').textContent = error.message; }
}
$('users-list').onclick = event => {
  const button = event.target.closest('[data-user]'); if (!button) return;
  openUser(button.dataset.user);
};
$('user-close').onclick = () => $('user-dialog').close();
$('user-dialog').addEventListener('close', clearUser);
$('user-orders-prev').onclick = () => loadUser(Math.max(1, profilePage - 1));
$('user-orders-next').onclick = () => loadUser(profilePage + 1);
$('user-refresh').onclick = () => loadUser(profilePage);

function openUser(id) {
  clearUser(); selectedUser = Number(id);
  $('user-dialog').showModal(); loadUser(1);
}
async function refreshOrders() {
  const request = ++ordersRequest;
  $('orders-prev').disabled = true; $('orders-next').disabled = true;
  $('orders-state').textContent = 'Загрузка заказов…';
  try {
    const params = new URLSearchParams({...ordersFilters, page:String(ordersPage), page_size:'25'});
    const data = await api('orders?' + params);
    if (request !== ordersRequest) return;
    const pages = Math.max(1, Math.ceil(data.total / data.page_size));
    if (data.page > pages) { ordersPage = pages; return await refreshOrders(); }
    $('orders-state').textContent = data.total ? 'Найдено заказов: ' + data.total : 'Заказов по выбранным условиям нет.';
    $('orders-page').textContent = 'Страница ' + data.page + ' из ' + pages;
    $('orders-prev').disabled = data.page <= 1;
    $('orders-next').disabled = data.page * data.page_size >= data.total;
    $('list').innerHTML = data.orders.map(order => `<article class="admin-order"><b>${esc(order.plan_name || order.plan)} · ${esc(protocolLabel(order.protocol))} · ${order.method==='referral'?'Бонус · '+order.days+' дней':order.amount+' '+(order.currency === 'XTR' ? '★' : '₽')}</b><p><button class="text-link" data-order-user="${order.user_id}">${esc(order.email || 'Telegram ' + order.telegram_id)} · № ${order.user_id}</button></p><p>Заказ: ${esc(order.id)}</p>${order.promo_code?`<p>Код ${esc(order.promo_code)} · скидка ${order.discount} ${order.currency==='XTR'?'★':'₽'} от ${order.base_amount}</p>`:''}<p>${esc(orderStatus[order.status] || order.status)} · ${esc(order.method==='referral'?'Бонусное продление':order.method==='trial'?'Пробный доступ':order.method)} · ${esc(dateTime(order.created))}</p>${order.error ? `<p>Ошибка выдачи: ${esc(order.error)} · Попыток: ${order.attempts}</p>` : ''}<div class="actions">${order.can_confirm ? `<button data-action="confirm" data-id="${esc(order.id)}">Подтвердить поступление оплаты</button>` : ''}${order.can_reject ? `<button class="secondary" data-action="reject" data-id="${esc(order.id)}">Отменить заказ</button>` : ''}${order.can_retry ? `<button data-action="retry" data-id="${esc(order.id)}">Повторить выдачу</button>` : ''}</div></article>`).join('');
  } catch (error) {
    if (request === ordersRequest) { $('list').replaceChildren(); $('orders-state').textContent = error.message; }
    throw error;
  }
}
function showOrders(page) { ordersPage = page; refreshOrders().catch(() => {}); }
$('orders-search').onsubmit = event => {
  event.preventDefault();
  const fields = new FormData(event.target);
  ordersFilters = {q:String(fields.get('q') || '').trim(), status:String(fields.get('status') || ''), method:String(fields.get('method') || ''), protocol:String(fields.get('protocol') || '')};
  showOrders(1);
};
$('orders-reset').onclick = () => { $('orders-search').reset(); ordersFilters = {q:'', status:'', method:'', protocol:''}; showOrders(1); };
$('orders-prev').onclick = () => showOrders(Math.max(1, ordersPage-1));
$('orders-next').onclick = () => showOrders(ordersPage+1);

const serverProfileLabel = {legacy_vless:'Legacy API', clients_vless:'Clients JSON API', wireguard_agent:'WG-агент'};
const serverState = {active:'Принимать новых', draining:'Только продления', maintenance:'Обслуживание', disabled:'Выдача выключена'};
function serverCard(server) {
  const health = server.health === 'ok' ? 'API доступен' : server.health === 'error' ? 'Ошибка API' : 'Ещё не проверен';
  return `<form class="plan-editor" data-server="${esc(server.id)}" data-version="${server.version}"><h3>${esc(server.name)}</h3><p>${esc(protocolLabel(server.protocol))} · ${esc(serverProfileLabel[server.profile] || server.profile)} · ${health}</p><p>Проверка: ${esc(dateTime(server.checked_at))}</p><p>Занято: ${server.occupied} · Всего привязано: ${server.bound_subscriptions}</p><label>Название<input name="name" value="${esc(server.name)}" maxlength="80" required></label><label>Локация<input name="location" value="${esc(server.location)}" maxlength="80"></label><label>Лимит занятых подписок (0 — без лимита)<input name="capacity" type="number" min="0" max="1000000" value="${server.capacity}" required></label><label>Режим<select name="state">${Object.entries(serverState).map(([id,label])=>`<option value="${id}" ${server.state===id?'selected':''}>${label}</option>`).join('')}</select></label><button type="submit" class="small">Сохранить режим</button><button type="button" class="small secondary" data-server-check="${esc(server.id)}">Проверить API</button><button type="button" class="text-link" data-server-edit="${esc(server.id)}">Профиль подключения</button></form>`;
}
async function refreshServers() {
  const data = await api('servers');
  serverCache = data.servers;
  $('server-list').innerHTML = data.servers.map(serverCard).join('');
  $('servers-notice').textContent = data.unassigned_subscriptions ? 'Есть подписки без привязки: '+data.unassigned_subscriptions+'. Автоматический перенос запрещён; проверьте настройки миграции.' : data.servers.length ? '' : 'Добавьте первый сервер для продажи VPN.';
}
function showServerFields() {
  const xui = $('server-protocol').value === 'xui';
  $('server-xui-fields').hidden = !xui; $('server-wg-fields').hidden = xui;
  const modern = $('server-api').value === 'clients';
  $('server-legacy-auth').hidden = modern; $('server-clients-auth').hidden = !modern;
}
$('server-protocol').onchange = showServerFields;
$('server-api').onchange = showServerFields;
function openServer(server = null) {
  editingServer = server;
  const form = $('server-form'); form.reset();
  form.elements.name.value = server?.name || ''; form.elements.location.value = server?.location || '';
  form.elements.capacity.value = server?.capacity || 0; form.elements.protocol.value = server?.protocol || 'xui';
  form.elements.api.value = server?.profile === 'clients_vless' ? 'clients' : 'legacy';
  form.elements.protocol.disabled = !!server; form.elements.url.required = !server;
  $('server-title').textContent = server ? 'Профиль сервера' : 'Новый сервер';
  $('server-form-error').textContent = ''; showServerFields(); $('server-dialog').showModal();
}
$('new-server').onclick = () => openServer();
$('server-close').onclick = () => $('server-dialog').close();
$('server-dialog').addEventListener('close', () => { $('server-form').reset(); editingServer = null; });
$('server-form').onsubmit = async event => {
  event.preventDefault();
  const form = event.target, values = new FormData(form), button = form.querySelector('button[type="submit"]');
  button.disabled = true; $('server-form-error').textContent = '';
  const original = editingServer, protocol = original?.protocol || String(values.get('protocol'));
  try {
    const url = String(values.get('url') || '').trim();
    const mode = String(values.get('api') || 'legacy');
    const fields = protocol === 'xui' ? ['username','password','api_token','inbound_id','link_template','flow'] : ['token'];
    if (original?.protocol === 'xui' && !url && mode !== (original.profile === 'clients_vless' ? 'clients' : 'legacy')) throw new Error('Для смены режима API введите полный профиль.');
    if (!url && fields.some(key=>values.get(key))) throw new Error('Для замены профиля укажите адрес и все данные подключения.');
    const profile = url ? protocol === 'xui' ? {url,api:mode,...(mode === 'clients' ? {token:String(values.get('api_token') || '')} : {username:String(values.get('username') || ''),password:String(values.get('password') || '')}),inbound_id:Number(values.get('inbound_id')),link_template:String(values.get('link_template') || ''),flow:String(values.get('flow') || '')} : {url,token:String(values.get('token') || '')} : null;
    const data = {name:String(values.get('name')),location:String(values.get('location') || ''),capacity:Number(values.get('capacity'))};
    if (original) { data.version=original.version; data.state=profile ? 'disabled' : original.state; if (profile) data.settings=profile; }
    else { data.protocol=protocol; data.settings=profile; }
    await api(original ? 'servers/'+encodeURIComponent(original.id) : 'servers', {method:original?'PUT':'POST',data});
    $('server-dialog').close(); await refreshServers();
    $('admin-notice').textContent = profile ? 'Профиль сохранён. Проверьте API сервера перед включением продаж.' : 'Сервер сохранён.';
  } catch (error) { $('server-form-error').textContent=error.message; }
  finally { button.disabled=false; }
};
$('server-list').onsubmit = async event => {
  event.preventDefault(); const form=event.target.closest('[data-server]'); if (!form) return;
  const values=new FormData(form), button=form.querySelector('button[type="submit"]'); button.disabled=true;
  try {
    await api('servers/'+encodeURIComponent(form.dataset.server), {method:'PUT',data:{name:String(values.get('name')),location:String(values.get('location') || ''),capacity:Number(values.get('capacity')),state:String(values.get('state')),version:Number(form.dataset.version)}});
    await refreshServers(); $('error').textContent='';
  } catch (error) { $('error').textContent=error.message; button.disabled=false; }
};
$('server-list').onclick = async event => {
  const edit=event.target.closest('[data-server-edit]');
  if (edit) { openServer(serverCache.find(s=>s.id===edit.dataset.serverEdit)); return; }
  const check=event.target.closest('[data-server-check]'); if (!check) return;
  check.disabled=true;
  try { await api('servers/'+encodeURIComponent(check.dataset.serverCheck)+'/check',{method:'POST'}); await refreshServers(); }
  catch (error) { $('error').textContent=error.message; check.disabled=false; }
};

async function refreshTrialPolicy() {
  const p=await api('trial');
  $('trial-policy-count').textContent='Принято активаций: '+p.used+' из '+p.max_claims+'. Осталось: '+p.remaining;
  $('trial-policy-editor').innerHTML=`<form class="plan-editor" data-trial-version="${p.version}"><label class="plan-enabled"><input name="enabled" type="checkbox" ${p.enabled?'checked':''}> Включить пробный доступ</label><label>Срок, дней<input name="days" type="number" min="1" max="3" value="${p.days}" required></label><label>Общий лимит активаций<input name="max_claims" type="number" min="1" max="100000" value="${p.max_claims}" required></label><button type="submit" class="small">Сохранить</button></form>`;
}
$('trial-policy-editor').onsubmit=async event=>{
  event.preventDefault();const form=event.target.closest('[data-trial-version]');if(!form)return;
  const values=new FormData(form),button=form.querySelector('button');button.disabled=true;
  try {await api('trial',{method:'PUT',data:{enabled:values.has('enabled'),days:Number(values.get('days')),max_claims:Number(values.get('max_claims')),version:Number(form.dataset.trialVersion)}});await refreshTrialPolicy();$('trial-policy-error').textContent='';}
  catch(e){$('trial-policy-error').textContent=e.message;button.disabled=false;}
};

function couponDate(ts) {
  if(!ts)return '';
  const d=new Date(ts*1000);return new Date(d.getTime()-d.getTimezoneOffset()*60000).toISOString().slice(0,16);
}
function couponForm(p) {
  return `<form class="plan-editor" data-coupon="${esc(p.id)}" data-version="${p.version}"><h3>${esc(p.code)}</h3><p>Оформлено: ${p.used} из ${p.max_uses}. Применения не сбрасываются.</p><label>Скидка, %<input name="percent" type="number" min="1" max="90" required value="${p.percent}"></label><label>Лимит заказов<input name="max_uses" type="number" min="1" max="100000" required value="${p.max_uses}"></label><label>Окончание (часовой пояс браузера)<input name="expires_at" type="datetime-local" value="${couponDate(p.expires_at)}"></label><label>Код тарифа (пусто — все)<input name="plan_id" maxlength="32" value="${esc(p.plan_id || '')}"></label><label>Протокол<select name="protocol">${[['all','Все'],['xui','VLESS'],['wg','WireGuard']].map(([id,label])=>`<option value="${id}" ${p.protocol===id?'selected':''}>${label}</option>`).join('')}</select></label><label class="plan-enabled"><input name="enabled" type="checkbox" ${p.enabled?'checked':''}> Включён</label><button type="submit" class="small">Сохранить</button></form>`;
}
async function refreshCoupons(){const result=await api('coupons');$('coupons-list').innerHTML=result.coupons.map(couponForm).join('');}
function couponFields(values){return {percent:Number(values.get('percent')),max_uses:Number(values.get('max_uses')),expires_at:values.get('expires_at')?Math.floor(new Date(values.get('expires_at')).getTime()/1000):0,plan_id:String(values.get('plan_id') || '').trim() || null,protocol:String(values.get('protocol')),enabled:values.has('enabled')};}
$('coupon-create').onsubmit=async event=>{
  event.preventDefault();const button=event.target.querySelector('button'),values=new FormData(event.target);button.disabled=true;
  try {await api('coupons',{method:'POST',data:{...couponFields(values),code:String(values.get('code')).trim().toUpperCase()}});$('coupon-create').reset();await refreshCoupons();$('coupons-error').textContent='';}
  catch(e){$('coupons-error').textContent=e.message;}finally{button.disabled=false;}
};
$('coupons-list').onsubmit=async event=>{
  event.preventDefault();const form=event.target.closest('[data-coupon]');if(!form)return;
  const button=form.querySelector('button'),values=new FormData(form);button.disabled=true;
  try {await api('coupons/'+encodeURIComponent(form.dataset.coupon),{method:'PUT',data:{...couponFields(values),version:Number(form.dataset.version)}});await refreshCoupons();$('coupons-error').textContent='';}
  catch(e){$('coupons-error').textContent=e.message;button.disabled=false;}
};

function referralPolicyForm(p) {
  return `<form class="plan-editor" data-referral-version="${p.version}"><label class="plan-enabled"><input name="enabled" type="checkbox" ${p.enabled?'checked':''}> Принимать новые приглашения</label><label>Бонус пригласившему, дней<input name="days" type="number" min="1" max="30" required value="${p.days}"></label><label>Лимит принятых приглашений за всё время<input name="max_invites" type="number" min="1" max="100000" required value="${p.max_invites}"></label><label>Минимальная оплата на сайте, ₽<input name="min_rub" type="number" min="1" max="1000000" required value="${p.min_rub}"></label><label>Минимальная оплата в Telegram, ★<input name="min_stars" type="number" min="1" max="100000" required value="${p.min_stars}"></label><button type="submit" class="small">Сохранить условия приглашений</button></form>`;
}
async function refreshReferralPolicy() {
  const p=await api('referrals');$('referral-policy-editor').innerHTML=referralPolicyForm(p);
  $('referral-policy-count').textContent='Принято: '+p.used+' · свободно: '+p.remaining+' · версия '+p.version;
}
$('referral-policy-editor').onsubmit=async event=>{
  const form=event.target.closest('form[data-referral-version]');if(!form)return;event.preventDefault();
  const values=new FormData(form),button=form.querySelector('button');button.disabled=true;
  try {await api('referrals',{method:'PUT',data:{enabled:values.has('enabled'),days:Number(values.get('days')),max_invites:Number(values.get('max_invites')),min_rub:Number(values.get('min_rub')),min_stars:Number(values.get('min_stars')),version:Number(form.dataset.referralVersion)}});await refreshReferralPolicy();$('referral-policy-error').textContent='';}
  catch(e){$('referral-policy-error').textContent=e.message;button.disabled=false;}
};

const diagnosticWarnings={worker_disabled:'Фоновые задачи выключены настройками',encryption_key_invalid:'Неверный формат ключа шифрования',worker_failed:'Последний цикл worker завершился ошибкой',worker_not_observed:'Цикл worker ещё не наблюдался в этом процессе',worker_slow:'Цикл worker выполняется больше 3 минут',worker_stale:'Нет свежего успешного цикла worker',queue_errors:'В очереди есть задания с ошибками',queue_wait_over_10m:'Задания выдачи или устройств ожидают больше 10 минут',expiry_wait_over_10m:'Есть доступы с просроченным отзывом больше 10 минут',queues_paused:'Фоновые задачи выключены при непустой очереди',server_probe_not_recent_ok:'Не все включённые серверы имеют успешную API-проверку за сутки',no_new_assignments_xui:'Нет сервера VLESS для новых назначений',no_new_assignments_wg:'Нет сервера WireGuard для новых назначений',no_active_plans:'Нет опубликованных тарифов',no_assignable_servers:'Нет серверов для новых назначений',payment_settings_incomplete:'Не заполнены настройки оплаты',telegram_sales_settings_incomplete:'Не заполнены настройки продаж в Telegram'};
const diagnosticWorker={disabled:'Выключен настройками',not_observed:'Ещё не наблюдался',failed:'Ошибка последнего цикла',busy:'Выполняет цикл',slow:'Длительный цикл',stale:'Нет свежего успешного цикла',recent_success:'Недавно завершил цикл'};
function diagnosticsMarkup(r) {
  const labels={issuance:'Выдача и продление',payment_poll:'Сверка ЮKassa',devices:'Создание/отзыв устройств',device_rotations:'Смена ключей устройств',primary_rotations:'Смена основного ключа'};
  return `<p><b>${r.status==='local_checks_ok'?'Локальные проверки без замечаний':'Требуется внимание'}</b> · ${esc(new Date(r.generated_at*1000).toLocaleString('ru-RU'))}</p><p>Worker: ${esc(diagnosticWorker[r.worker.state] || r.worker.state)}. Последний успешный цикл: ${r.worker.last_success?esc(new Date(r.worker.last_success*1000).toLocaleString('ru-RU')):'не наблюдался'}.</p>${r.warnings.length?'<ul>'+r.warnings.map(w=>'<li>'+esc(diagnosticWarnings[w] || w)+'</li>').join('')+'</ul>':''}<div class="table-scroll"><table><thead><tr><th>Очередь</th><th>Ожидает</th><th>Срок повтора наступил</th><th>С ошибкой</th><th>Возраст старейшего, сек.</th></tr></thead><tbody>${Object.entries(r.queues).map(([key,q])=>`<tr><td>${esc(labels[key] || key)}</td><td>${esc(q.pending)}</td><td>${esc(q.due)}</td><td>${esc(q.with_error)}</td><td>${esc(q.oldest_age_seconds ?? '—')}</td></tr>`).join('')}</tbody></table></div><p>Подписок с истёкшим сроком в состоянии active: ${esc(r.expiry.pending)}. Это очередь проверки отзыва, а не доказательство текущего доступа на узле.</p><p>Серверы VLESS / WireGuard для новых назначений: ${esc(r.servers.xui.assignable)} / ${esc(r.servers.wg.assignable)}. Недавние успешные API-проверки: ${esc(r.servers.xui.cached_api_ok_24h)} / ${esc(r.servers.wg.cached_api_ok_24h)}.</p><p>Статус относится только к локальным наблюдениям. Проверка трафика, платежа и восстановления копии выполняется отдельно.</p>`;
}
async function refreshDiagnostics() {
  const request=++diagnosticsRequest,epoch=authEpoch;$('diagnostics-refresh').disabled=true;
  try {
    const r=await api('diagnostics');if(request!==diagnosticsRequest || epoch!==authEpoch)return;
    $('diagnostics-report').innerHTML=diagnosticsMarkup(r);$('diagnostics-json').value=JSON.stringify(r,null,2);$('diagnostics-error').textContent='';
  }catch(e){if(request===diagnosticsRequest && epoch===authEpoch){$('diagnostics-report').replaceChildren();$('diagnostics-json').value='';$('diagnostics-error').textContent=e.message;}}
  finally {if(request===diagnosticsRequest && epoch===authEpoch)$('diagnostics-refresh').disabled=false;}
}
$('diagnostics-refresh').onclick=()=>refreshDiagnostics();


const adsLabels={pending:'На модерации',approved:'Модерация пройдена',rejected:'Отклонено',canceled:'Отменено'};
function adsPaymentForm(r){
  const p=r.payment || {status:'unpaid',version:0,entries:[]};
  const label=p.status==='confirmed'?'Поступление учтено вручную':p.status==='refunded'?'Полный возврат учтён вручную':'Поступление не учтено';
  const history=(p.entries || []).map(e=>`<p>${e.kind==='receipt'?'Поступление':'Возврат'}: ${e.amount} ₽ · ${esc(e.reference)} · ${esc(e.note)}</p>`).join('');
  const kind=p.status==='confirmed'?'refund':'receipt';
  return `<p>${label}. Это учёт владельца, без автоматической сверки с банком.</p>${history}${r.state==='approved' && p.status!=='refunded' && r.booking?.state!=='reserved' && !['active','paused'].includes(r.placement?.state)?`<form data-ads-payment="${esc(r.id)}" data-kind="${kind}" data-version="${r.version}" data-payment-version="${p.version}"><h4>${kind==='receipt'?'Учесть поступление':'Учесть полный возврат'}</h4><label>Сумма, ₽<input name="amount" type="number" min="1" max="1000000" value="${r.amount}" required></label><label>ID банковской операции (без ФИО и реквизитов)<input name="reference" minlength="3" maxlength="120" required autocomplete="off"></label><label>Основание проверки (видно только владельцу)<textarea name="note" maxlength="500" required></textarea></label><label><input name="checked" type="checkbox" required> Я проверил фактическое ${kind==='receipt'?'поступление':'возвращение'} полной суммы в банке или платёжном сервисе</label><button>Сохранить проверенную операцию</button></form>`:''}`;
}
async function refreshAdsAdmin() {
  const request=++adsRequest,epoch=authEpoch,page=adsPage;
  const [policy,listing]=await Promise.all([api('ads/policy'),api('ads/requests?page='+page+($('ads-filter').value?'&state='+encodeURIComponent($('ads-filter').value):''))]);
  if(request!==adsRequest || epoch!==authEpoch)return;
  adsSitesCache=listing.sites || [];
  adsPolicy=policy;$('ads-accepting').checked=policy.accepting;$('ads-price').value=policy.amount;$('ads-hours').value=policy.hours;$('ads-instructions').value=policy.instructions;
  $('ads-admin-list').innerHTML=listing.requests.map(r=>`<article class="subscription"><b>${esc('@'+r.channel)}</b><p>${esc(adsLabels[r.state] || r.state)} · клиент ${r.user_id}</p><p>${r.amount} ₽ / ${r.hours} ч</p><p>${esc(r.comment)}</p><p>${esc(r.reason)}</p><code>${esc(r.id)}</code>${r.state==='pending'?`<form data-ads-id="${esc(r.id)}" data-version="${r.version}"><label>Решение<select name="state"><option value="approved">Одобрить канал</option><option value="rejected">Отклонить</option></select></label><label>Причина для клиента<textarea name="reason" maxlength="500"></textarea></label><button>Сохранить решение</button></form>`:''}${adsPaymentForm(r)}${adsBookingForm(r)}${adsPlacementForm(r)}</article>`).join('') || '<p>Заявок нет.</p>';
  $('ads-admin-page').textContent='Страница '+page+' · всего '+listing.total;$('ads-admin-prev').disabled=page===1;$('ads-admin-next').disabled=page*listing.page_size>=listing.total;
}
$('ads-policy-form').onsubmit=async event=>{
  event.preventDefault();if(!adsPolicy)return;
  const epoch=authEpoch,request=adsRequest;$('ads-policy-save').disabled=true;
  try{await api('ads/policy',{method:'PUT',data:{version:adsPolicy.version,accepting:$('ads-accepting').checked,amount:Number($('ads-price').value),hours:Number($('ads-hours').value),instructions:$('ads-instructions').value}});if(epoch===authEpoch && request===adsRequest)await refreshAdsAdmin();}
  catch(e){if(epoch===authEpoch && request===adsRequest)$('ads-admin-error').textContent=e.message;}
  finally{if(epoch===authEpoch)$('ads-policy-save').disabled=false;}
};
$('ads-admin-list').onsubmit=async event=>{
  const bookingForm=event.target.closest('[data-ads-booking]');if(bookingForm){event.preventDefault();return saveAdsBooking(bookingForm);}
  const placementForm=event.target.closest('[data-ads-placement]');if(placementForm){event.preventDefault();return saveAdsPlacement(placementForm);}
  const paymentForm=event.target.closest('[data-ads-payment]');if(paymentForm){event.preventDefault();return saveAdsPayment(paymentForm);}
  const form=event.target.closest('[data-ads-id]');if(!form)return;event.preventDefault();
  const data=new FormData(form),epoch=authEpoch,request=adsRequest;const button=form.querySelector('button');button.disabled=true;
  try{await api('ads/requests/'+encodeURIComponent(form.dataset.adsId),{method:'PUT',data:{version:Number(form.dataset.version),state:data.get('state'),reason:data.get('reason')}});if(epoch===authEpoch && request===adsRequest)await refreshAdsAdmin();}
  catch(e){if(epoch===authEpoch && request===adsRequest)$('ads-admin-error').textContent=e.message;}
  finally{button.disabled=false;}
};
async function reloadAdsAdmin() {
  const epoch=authEpoch;
  try{await refreshAdsAdmin();}catch(e){if(epoch===authEpoch)$('ads-admin-error').textContent=e.message;}
}
$('ads-admin-refresh').onclick=()=>reloadAdsAdmin();
$('ads-filter').onchange=()=>{adsPage=1;return reloadAdsAdmin();};
$('ads-admin-prev').onclick=()=>{if(adsPage>1){adsPage--;return reloadAdsAdmin();}};
$('ads-admin-next').onclick=()=>{if(!$('ads-admin-next').disabled){adsPage++;return reloadAdsAdmin();}};

async function saveAdsPayment(form){
  const button=form.querySelector('button');if(button.disabled)return;
  const fields=new FormData(form),epoch=authEpoch,request=adsRequest;
  const data={request_version:Number(form.dataset.version),payment_version:Number(form.dataset.paymentVersion),amount:Number(fields.get('amount')),reference:String(fields.get('reference') || '').trim(),note:String(fields.get('note') || '').trim(),checked:fields.has('checked')};
  const path='ads/requests/'+encodeURIComponent(form.dataset.adsPayment)+'/payments/'+form.dataset.kind;
  const key=JSON.stringify([path,data]);if(!adsPaymentIntents.has(key))adsPaymentIntents.set(key,crypto.randomUUID());
  button.disabled=true;$('ads-admin-error').textContent='';
  try{
    await api(path,{method:'POST',data:{...data,operation_id:adsPaymentIntents.get(key)}});
    if(epoch!==authEpoch)return;
    adsPaymentIntents.delete(key);if(request===adsRequest)await refreshAdsAdmin();
  }catch(e){if(epoch===authEpoch && request===adsRequest)$('ads-admin-error').textContent=e.message;}
  finally{if(epoch===authEpoch)button.disabled=false;}
}

function adsPlacementForm(r){
  const p=r.placement;
  const labels={active:'Запуск подтверждён владельцем',paused:'Пауза подтверждена · площадка удерживается',finished:'Остановка подтверждена · площадка освобождена'};
  const history=(p?.actions || []).map(a=>`<p>${esc(({start:'Запуск',pause:'Пауза',resume:'Продолжение',finish:'Остановка'})[a.action] || a.action)} · ${esc(a.note)}</p>`).join('');
  const status=p?`<p>${labels[p.state] || esc(p.state)} · ${esc(p.site_name)} · осталось ${p.remaining_seconds} сек.${p.attention_due?' Срок истёк: требуется подтвердить фактическую остановку.':''}</p>`:'<p>Размещение не запущено.</p>';
  if(r.state!=='approved' || r.payment?.status!=='confirmed' || p?.state==='finished')return status+history;
  if(!p && r.booking?.state==='reserved' && !r.booking.can_start)return status+history+'<p>Окно старта брони ещё не наступило или уже истекло. Обновите данные; истёкшую бронь можно отменить.</p>';
  const options=adsSitesCache.filter(s=>s.enabled && s.verified && !s.occupied && (r.booking?.state!=='reserved' || s.id===r.booking.site_id)).map(s=>`<option value="${esc(s.id)}" data-version="${s.version}">${esc(s.name)}</option>`).join('');
  const actions=p?(p.state==='active'?'<option value="pause">Подтвердить паузу</option><option value="finish">Подтвердить остановку</option>':`${p.remaining_seconds>0?'<option value="resume">Подтвердить продолжение</option>':''}<option value="finish">Подтвердить остановку</option>`):'<option value="start">Подтвердить фактический запуск</option>';
  if(!p && !options)return status+history+'<p>Нет свободной включённой проверенной площадки.</p>';
  return status+history+`<form data-ads-placement="${esc(r.id)}" data-request-version="${r.version}" data-payment-version="${r.payment.version}" data-placement-version="${p?.version || 0}" data-booking-version="${!p && r.booking?.state==='reserved'?r.booking.version:''}"><label>Действие<select name="action">${actions}</select></label>${!p?`<label>Площадка<select name="site_id" required>${options}</select></label>`:''}<label>Основание проверки (только владельцу)<textarea name="note" maxlength="500" required></textarea></label><label><input type="checkbox" name="checked" required> Я проверил фактический показ либо остановку в Telegram; сохранение только фиксирует результат</label><button>Сохранить подтверждение</button></form>`;
}
async function saveAdsPlacement(form){
  const button=form.querySelector('button');if(button.disabled)return;
  const fields=new FormData(form),epoch=authEpoch,request=adsRequest,queueRequest=adsStopRequest,calendarRequest=adsBookingRequest,fromCalendar=form.dataset.adsCalendar==='true',fromQueue=form.dataset.adsQueue==='true',action=String(fields.get('action'));
  const data={request_version:Number(form.dataset.requestVersion),payment_version:Number(form.dataset.paymentVersion),placement_version:Number(form.dataset.placementVersion),note:String(fields.get('note') || '').trim(),checked:fields.has('checked')};
  if(action==='start'){
    const site=adsSitesCache.find(s=>s.id===fields.get('site_id'));
    if(!site){$('ads-admin-error').textContent='Обновите список площадок';return;}
    data.site_id=site.id;data.site_version=site.version;
    if(form.dataset.bookingVersion)data.booking_version=Number(form.dataset.bookingVersion);
  }
  const path='ads/requests/'+encodeURIComponent(form.dataset.adsPlacement)+'/placement/'+action;
  const key=JSON.stringify([path,data]);if(!adsPlacementIntents.has(key))adsPlacementIntents.set(key,crypto.randomUUID());
  button.disabled=true;$(fromCalendar?'ads-bookings-error':fromQueue?'ads-stop-error':'ads-admin-error').textContent='';
  try{
    await api(path,{method:'POST',data:{...data,operation_id:adsPlacementIntents.get(key)}});if(epoch!==authEpoch)return;
    adsPlacementIntents.delete(key);
    if(fromCalendar){if(calendarRequest===adsBookingRequest)await refreshBookingCalendar();}
    else if(fromQueue){if(queueRequest===adsStopRequest)await refreshStopQueue();}
    else if(request===adsRequest)await refreshAdsAdmin();
  }catch(e){if(epoch===authEpoch && (fromCalendar?calendarRequest===adsBookingRequest:fromQueue?queueRequest===adsStopRequest:request===adsRequest))$(fromCalendar?'ads-bookings-error':fromQueue?'ads-stop-error':'ads-admin-error').textContent=e.message;}
  finally{if(epoch===authEpoch)button.disabled=false;}
}
function clearAdsSite(){
  adsSiteMutation++;adsSiteEditor=null;$('ads-site-title').textContent='Новая площадка';
  $('ads-site-name').value='';$('ads-site-note').value='';$('ads-site-enabled').checked=false;$('ads-site-verified').checked=false;$('ads-site-save').disabled=false;
}
async function refreshAdsSites(){
  const epoch=authEpoch,request=++adsSitesRequest;
  try{const result=await api('ads/sites');if(epoch!==authEpoch || request!==adsSitesRequest)return;
    adsPrivateSites=result.sites;
    $('ads-sites-list').innerHTML=result.sites.map(s=>`<article><b>${esc(s.name)}</b><p>${s.enabled?'Включена':'Выключена'} · ${s.verified?'проверена владельцем':'не проверена'} · ${s.occupied?'занята':'свободна'}</p><p>${esc(s.note)}</p><button data-ads-site="${esc(s.id)}">Редактировать</button></article>`).join('') || '<p>Площадок нет.</p>';
  }catch(e){if(epoch===authEpoch && request===adsSitesRequest)$('ads-sites-error').textContent=e.message;}
}
$('ads-sites-refresh').onclick=()=>refreshAdsSites();
$('ads-site-new').onclick=()=>clearAdsSite();
$('ads-sites-list').onclick=event=>{
  const button=event.target.closest('[data-ads-site]');if(!button)return;
  const site=adsPrivateSites.find(s=>s.id===button.dataset.adsSite);if(!site)return;
  adsSiteMutation++;$('ads-site-save').disabled=false;adsSiteEditor={id:site.id,version:site.version};$('ads-site-title').textContent='Изменить площадку';
  $('ads-site-name').value=site.name;$('ads-site-note').value=site.note;$('ads-site-enabled').checked=site.enabled;$('ads-site-verified').checked=site.verified;
};
$('ads-site-form').onsubmit=async event=>{
  event.preventDefault();if($('ads-site-save').disabled)return;
  const epoch=authEpoch;if(!adsSiteEditor)adsSiteEditor={id:crypto.randomUUID(),version:0};const editor=adsSiteEditor,mutation=++adsSiteMutation;
  $('ads-site-save').disabled=true;$('ads-sites-error').textContent='';
  try{await api('ads/sites/'+editor.id,{method:'PUT',data:{version:editor.version,name:$('ads-site-name').value,enabled:$('ads-site-enabled').checked,verified:$('ads-site-verified').checked,note:$('ads-site-note').value}});
    if(epoch!==authEpoch)return;if(editor===adsSiteEditor)clearAdsSite();await Promise.all([refreshAdsSites(),refreshAdsAdmin()]);
  }catch(e){if(epoch===authEpoch && mutation===adsSiteMutation)$('ads-sites-error').textContent=e.message;}
  finally{if(epoch===authEpoch && mutation===adsSiteMutation)$('ads-site-save').disabled=false;}
};

async function refreshStopQueue(){
  const epoch=authEpoch,request=++adsStopRequest,page=adsStopPage,view=$('ads-stop-filter').value || 'due';
  $('ads-stop-error').textContent='';
  try{
    const result=await api('ads/stop-queue?view='+encodeURIComponent(view)+'&page='+page);
    if(epoch!==authEpoch || request!==adsStopRequest)return;
    $('ads-stop-counts').textContent='Требуют остановки: '+result.counts.due+' · предстоящие: '+result.counts.upcoming+' · на паузе: '+result.counts.paused+' · снимок: '+new Date(result.generated_at*1000).toLocaleString('ru-RU')+' (время браузера)';
    const labels={due:'Требуется проверка и остановка',upcoming:'Предстоящее окончание',paused:'На паузе · площадка удерживается'};
    $('ads-stop-list').innerHTML=result.requests.map(r=>'<article class="subscription"><h3>'+esc('@'+r.channel)+'</h3><p>'+esc(labels[r.queue_status] || r.queue_status)+'</p><p>Клиент '+r.user_id+' · заявка '+esc(r.id)+'</p>'+(r.placement.expected_stop_at?'<p>План остановки: '+esc(new Date(r.placement.expected_stop_at*1000).toLocaleString('ru-RU'))+' (время браузера)</p>':'')+(r.queue_status==='due'?'<p>Просрочка: '+r.overdue_seconds+' сек.</p>':'')+adsPlacementForm(r).replace('<form data-ads-placement','<form data-ads-queue="true" data-ads-placement')+'</article>').join('') || '<p>В выбранной очереди размещений нет.</p>';
    $('ads-stop-page').textContent='Страница '+page+' · всего '+result.total;
    $('ads-stop-prev').disabled=page===1;$('ads-stop-next').disabled=page*result.page_size>=result.total;
  }catch(e){
    if(epoch!==authEpoch || request!==adsStopRequest)return;
    $('ads-stop-list').replaceChildren();$('ads-stop-counts').textContent='';$('ads-stop-page').textContent='';
    $('ads-stop-prev').disabled=true;$('ads-stop-next').disabled=true;$('ads-stop-error').textContent=e.message;
  }
}
$('ads-stop-refresh').onclick=()=>refreshStopQueue();
$('ads-stop-filter').onchange=()=>{adsStopPage=1;return refreshStopQueue();};
$('ads-stop-prev').onclick=()=>{if(adsStopPage>1){adsStopPage--;return refreshStopQueue();}};
$('ads-stop-next').onclick=()=>{if(!$('ads-stop-next').disabled){adsStopPage++;return refreshStopQueue();}};
$('ads-stop-list').onsubmit=event=>{
  const form=event.target.closest('[data-ads-placement]');if(!form)return;event.preventDefault();return saveAdsPlacement(form);
};

function bookingTime(value){return new Date(value*1000).toLocaleString('ru-RU');}
function adsBookingForm(r){
  const b=r.booking;
  const labels={reserved:'Бронь ожидает запуска',canceled:'Бронь отменена',consumed:'Бронь использована при запуске'};
  const status=b?'<p>'+esc(labels[b.state] || b.state)+' · '+esc(b.site_name)+' · '+esc(bookingTime(b.starts_at))+' — '+esc(bookingTime(b.ends_at))+' (время браузера)'+(b.expired?' · окно старта истекло':'')+'</p>':'<p>Плановой брони нет.</p>';
  const history=(b?.actions || []).map(a=>'<p>'+esc(({reserve:'Резерв',cancel:'Отмена',reschedule:'Перенос'})[a.action] || a.action)+' · '+esc(a.note)+'</p>').join('');
  if(r.placement || r.state!=='approved' || r.payment?.status!=='confirmed')return status+history;
  const cancel=b?.state==='reserved';
  const options=adsSitesCache.filter(s=>s.enabled && s.verified && !s.occupied).map(s=>'<option value="'+esc(s.id)+'">'+esc(s.name)+'</option>').join('');
  if(!cancel && !options)return status+history+'<p>Нет свободной проверенной площадки для брони.</p>';
  const form=(action,title,period)=>'<form data-ads-booking="'+esc(r.id)+'" data-action="'+action+'" data-request-version="'+r.version+'" data-payment-version="'+r.payment.version+'" data-booking-version="'+(b?.version || 0)+'">'+(period?'<label>Новая площадка<select name="site_id" required>'+options+'</select></label><label>Новое начало, местное время браузера<input type="datetime-local" name="starts_at" required></label>':'')+'<label>Основание (только владельцу)<textarea name="note" maxlength="500" required></textarea></label><p>Перенос проверяется целиком; при конфликте прежняя бронь сохраняется. Запуска узла и движения денег нет.</p><button>'+title+'</button></form>';
  return status+history+(cancel?form('cancel','Отменить бронь',false)+(options?form('reschedule','Перенести бронь одним действием',true):'<p>Нет свободной проверенной площадки для переноса.</p>'):form('reserve','Забронировать период',true));
}
async function saveAdsBooking(form){
  const button=form.querySelector('button');if(button.disabled)return;
  const epoch=authEpoch,request=adsRequest,calendarRequest=adsBookingRequest,fromCalendar=form.dataset.adsCalendar==='true',fields=new FormData(form);
  const errorId=fromCalendar?'ads-bookings-error':'ads-admin-error';
  const data={request_version:Number(form.dataset.requestVersion),payment_version:Number(form.dataset.paymentVersion),booking_version:Number(form.dataset.bookingVersion),note:String(fields.get('note') || '').trim()};
  if(['reserve','reschedule'].includes(form.dataset.action)){
    const site=adsSitesCache.find(s=>s.id===fields.get('site_id')),date=Date.parse(String(fields.get('starts_at') || ''));
    if(!site || !Number.isFinite(date)){$(errorId).textContent='Выберите площадку и корректное время';return;}
    data.site_id=site.id;data.site_version=site.version;data.starts_at=Math.floor(date/1000);
  }
  const path='ads/requests/'+encodeURIComponent(form.dataset.adsBooking)+'/booking/'+form.dataset.action;
  const key=JSON.stringify([path,data]);if(!adsBookingIntents.has(key))adsBookingIntents.set(key,crypto.randomUUID());
  button.disabled=true;$(errorId).textContent='';
  try{
    await api(path,{method:'POST',data:{...data,operation_id:adsBookingIntents.get(key)}});if(epoch!==authEpoch)return;
    adsBookingIntents.delete(key);
    if(fromCalendar){if(calendarRequest===adsBookingRequest)await refreshBookingCalendar();}
    else if(request===adsRequest)await refreshAdsAdmin();
  }catch(e){if(epoch===authEpoch && (fromCalendar?calendarRequest===adsBookingRequest:request===adsRequest))$(errorId).textContent=e.message;}
  finally{if(epoch===authEpoch)button.disabled=false;}
}
async function refreshBookingCalendar(){
  const epoch=authEpoch,request=++adsBookingRequest,page=adsBookingPage,view=$('ads-bookings-filter').value || 'reserved';
  $('ads-bookings-error').textContent='';
  try{
    const result=await api('ads/bookings?view='+encodeURIComponent(view)+'&page='+page);
    if(epoch!==authEpoch || request!==adsBookingRequest)return;
    adsSitesCache=result.sites;
    $('ads-bookings-list').innerHTML=result.requests.map(r=>'<article class="subscription"><h3>'+esc('@'+r.channel)+'</h3><code>'+esc(r.id)+'</code>'+adsBookingForm(r).replaceAll('<form data-ads-booking','<form data-ads-calendar="true" data-ads-booking')+adsPlacementForm(r).replace('<form data-ads-placement','<form data-ads-calendar="true" data-ads-placement')+'</article>').join('') || '<p>Броней в выбранном списке нет.</p>';
    $('ads-bookings-page').textContent='Страница '+page+' · всего '+result.total;
    $('ads-bookings-prev').disabled=page===1;$('ads-bookings-next').disabled=page*result.page_size>=result.total;
  }catch(e){
    if(epoch!==authEpoch || request!==adsBookingRequest)return;
    $('ads-bookings-list').replaceChildren();$('ads-bookings-page').textContent='';
    $('ads-bookings-prev').disabled=true;$('ads-bookings-next').disabled=true;$('ads-bookings-error').textContent=e.message;
  }
}
$('ads-bookings-refresh').onclick=()=>refreshBookingCalendar();
$('ads-bookings-filter').onchange=()=>{adsBookingPage=1;return refreshBookingCalendar();};
$('ads-bookings-prev').onclick=()=>{if(adsBookingPage>1){adsBookingPage--;return refreshBookingCalendar();}};
$('ads-bookings-next').onclick=()=>{if(!$('ads-bookings-next').disabled){adsBookingPage++;return refreshBookingCalendar();}};
$('ads-bookings-list').onsubmit=event=>{
  event.preventDefault();const booking=event.target.closest('[data-ads-booking]');if(booking)return saveAdsBooking(booking);
  const placement=event.target.closest('[data-ads-placement]');if(placement)return saveAdsPlacement(placement);
};

function expenseDate(value){const n=Date.parse(value);return Number.isFinite(n)?Math.floor(n/1000):null;}
function expenseInput(){return {amount:Number($('ads-expense-amount').value),category:$('ads-expense-category').value,incurred_at:expenseDate($('ads-expense-date').value),reference:$('ads-expense-reference').value.trim(),note:$('ads-expense-note').value.trim()};}
async function refreshAdsFinance(){
  const epoch=authEpoch,request=++adsFinanceRequest,page=adsFinancePage;
  const start=$('ads-finance-start').value,end=$('ads-finance-end').value;
  $('ads-finance-error').textContent='';
  try{
    if(Boolean(start)!==Boolean(end))throw new Error('Укажите обе границы периода');
    const query=new URLSearchParams({page:String(page)});
    if(start){const a=expenseDate(start),b=expenseDate(end);if(a===null || b===null || a>=b)throw new Error('Проверьте границы периода');query.set('start',String(a));query.set('end',String(b));}
    const result=await api('ads/finance?'+query.toString());
    if(epoch!==authEpoch || request!==adsFinanceRequest)return;
    $('ads-finance-summary').textContent='Период: '+bookingTime(result.period.start)+' — '+bookingTime(result.period.end)+' (конец не включается, время браузера). Поступления: '+result.cash.received+' ₽; возвраты: '+result.cash.refunded+' ₽; учтённые расходы: '+result.expenses.amount+' ₽; остаток после учтённых расходов: '+result.cash_after_recorded_expenses+' ₽. Чистая прибыль не рассчитывается.';
    const labels={not_started:'Не запущены',active:'Активны по ручному журналу',paused:'На паузе',early_stopped:'Остановлены с неоказанным остатком',completed:'Полный срок и остановка подтверждены'};
    $('ads-finance-obligations').innerHTML=Object.entries(result.current_paid_requests).map(([key,r])=>'<p>'+esc(labels[key] || key)+': '+r.requests+' заявок · сумма заявок '+r.contract_amount+' ₽ · неоказанный остаток '+r.remaining_seconds+' сек.</p>').join('')+'<p>Суммы заявок не являются суммой к возврату или заработанной выручкой. Здесь только заявки без учтённого полного возврата, независимо от выбранного периода.</p>';
    const categories={server:'Сервер',commission:'Комиссия',marketing:'Привлечение',other:'Прочее'};
    $('ads-expense-list').innerHTML=result.expenses.entries.map(r=>'<article class="subscription"><b>'+r.amount+' ₽ · '+esc(categories[r.category] || r.category)+'</b><p>'+esc(bookingTime(r.incurred_at))+' · '+esc(r.reference)+'</p><p>'+esc(r.note)+'</p>'+(r.state==='void'?'<p>Исключено из расчёта: '+esc(r.void_reason)+'</p>':'<form data-ads-expense="'+esc(r.id)+'" data-version="'+r.version+'"><label>Причина исключения ошибочной записи<textarea name="note" maxlength="500" required></textarea></label><button>Исключить ошибочную запись</button></form>')+'</article>').join('') || '<p>Расходов в периоде нет.</p>';
    $('ads-finance-page').textContent='Страница '+page+' · записей '+result.expenses.total;
    $('ads-finance-prev').disabled=page===1;$('ads-finance-next').disabled=page*result.expenses.page_size>=result.expenses.total;
  }catch(e){if(epoch!==authEpoch || request!==adsFinanceRequest)return;for(const id of ['ads-finance-summary','ads-finance-obligations','ads-expense-list','ads-finance-page'])$(id).replaceChildren();$('ads-finance-prev').disabled=true;$('ads-finance-next').disabled=true;$('ads-finance-error').textContent=e.message;}
}
$('ads-finance-period').onsubmit=event=>{event.preventDefault();adsFinancePage=1;return refreshAdsFinance();};
$('ads-finance-prev').onclick=()=>{if(adsFinancePage>1){adsFinancePage--;return refreshAdsFinance();}};
$('ads-finance-next').onclick=()=>{if(!$('ads-finance-next').disabled){adsFinancePage++;return refreshAdsFinance();}};
async function saveAdsExpense(path,data,button){
  if(button.disabled)return;
  const epoch=authEpoch,request=adsFinanceRequest,key=JSON.stringify([path,data]);
  if(!adsExpenseIntents.has(key))adsExpenseIntents.set(key,crypto.randomUUID());
  button.disabled=true;$('ads-expense-status').textContent='';
  try{
    await api(path,{method:'POST',data:{...data,operation_id:adsExpenseIntents.get(key)}});
    if(epoch!==authEpoch)return;
    adsExpenseIntents.delete(key);
    if(path==='ads/expenses' && JSON.stringify(expenseInput())===JSON.stringify(data)){
      for(const id of ['ads-expense-amount','ads-expense-date','ads-expense-reference','ads-expense-note'])$(id).value='';
    }
    if(request===adsFinanceRequest)await refreshAdsFinance();
    if(epoch===authEpoch)$('ads-expense-status').textContent='Запись учёта сохранена. Сводка относится к выбранному периоду; движения денег нет.';
  }catch(e){if(epoch===authEpoch && request===adsFinanceRequest)$('ads-expense-status').textContent=e.message;}
  finally{if(epoch===authEpoch)button.disabled=false;}
}
$('ads-expense-form').onsubmit=event=>{
  event.preventDefault();const data=expenseInput();if(data.incurred_at===null){$('ads-expense-status').textContent='Проверьте дату расхода';return;}
  return saveAdsExpense('ads/expenses',data,$('ads-expense-save'));
};
$('ads-expense-list').onsubmit=event=>{
  const form=event.target.closest('[data-ads-expense]');if(!form)return;event.preventDefault();const fields=new FormData(form);
  return saveAdsExpense('ads/expenses/'+encodeURIComponent(form.dataset.adsExpense)+'/void',{version:Number(form.dataset.version),note:String(fields.get('note') || '').trim()},form.querySelector('button'));
};
