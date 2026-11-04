'use strict';
const basePath = document.querySelector('meta[name="shop-base"]').content;
const $ = id => document.getElementById(id);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
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
  await Promise.all([refreshOrders(), refreshUsers(), plans ? refreshServers() : Promise.resolve(), plans ? refreshTrialPolicy() : Promise.resolve()]);
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
    $('user-orders').innerHTML = data.orders.length ? data.orders.map(order => `<article class="admin-order"><b>${esc(order.plan_name || order.plan)} · ${order.days} дней · ${order.amount} ${order.currency === 'XTR' ? '★' : '₽'}</b><p>${esc(protocolLabel(order.protocol))} · ${esc(orderStatus[order.status] || order.status)}</p><p>Заказ: ${esc(order.id)}</p><p>${esc(dateTime(order.created))} · ${esc(order.method==='trial'?'Пробный доступ':order.method)}</p>${order.error ? `<p>Ошибка выдачи: ${esc(order.error)} · Попыток: ${order.attempts}</p>` : ''}</article>`).join('') : '<p>Заказов пока нет.</p>';
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
    $('list').innerHTML = data.orders.map(order => `<article class="admin-order"><b>${esc(order.plan_name || order.plan)} · ${esc(protocolLabel(order.protocol))} · ${order.amount} ${order.currency === 'XTR' ? '★' : '₽'}</b><p><button class="text-link" data-order-user="${order.user_id}">${esc(order.email || 'Telegram ' + order.telegram_id)} · № ${order.user_id}</button></p><p>Заказ: ${esc(order.id)}</p><p>${esc(orderStatus[order.status] || order.status)} · ${esc(order.method==='trial'?'Пробный доступ':order.method)} · ${esc(dateTime(order.created))}</p>${order.error ? `<p>Ошибка выдачи: ${esc(order.error)} · Попыток: ${order.attempts}</p>` : ''}<div class="actions">${order.can_confirm ? `<button data-action="confirm" data-id="${esc(order.id)}">Подтвердить поступление оплаты</button>` : ''}${order.can_reject ? `<button class="secondary" data-action="reject" data-id="${esc(order.id)}">Отменить заказ</button>` : ''}${order.can_retry ? `<button data-action="retry" data-id="${esc(order.id)}">Повторить выдачу</button>` : ''}</div></article>`).join('');
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
