'use strict';
const basePath = document.querySelector('meta[name="shop-base"]').content;
const $ = id => document.getElementById(id);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let token = '', authEpoch = 0, usersPage = 1, usersQuery = '', usersRequest = 0;
let selectedUser = null, profilePage = 1, profileRequest = 0;
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
    </div>
    <label class="plan-enabled"><input name="enabled" type="checkbox" ${plan.enabled ? 'checked' : ''}> Доступен для новых покупок</label>
    <button type="submit" class="small">Сохранить тариф</button>
  </form>`;
}
async function refresh({plans = true} = {}) {
  const [data, catalog] = await Promise.all([api('overview'), plans ? api('plans') : Promise.resolve(null)]);
  $('stats').innerHTML = `<div>Пользователи<strong>${data.users}</strong></div><div>Подписки<strong>${data.active}</strong></div><div>Подтверждено<strong>${data.revenue} ₽ / ${data.stars} ★</strong></div>`;
  if (catalog) $('plan-editors').innerHTML = catalog.plans.map(planForm).join('');
  await Promise.all([refreshOrders(), refreshUsers()]);
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
  $('orders-search').reset(); $('orders-state').textContent = ''; $('orders-page').textContent = '';
  $('orders-prev').disabled = true; $('orders-next').disabled = true;
  $('users-query').value = ''; $('users-list').replaceChildren(); $('users-page').textContent = ''; $('users-state').textContent = '';
  if ($('user-dialog').open) $('user-dialog').close();
  clearUser();
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
      stars: Number(values.get('stars')), enabled: values.has('enabled'), version: Number(form.dataset.version),
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
      price: Number(values.get('price')), stars: Number(values.get('stars')), enabled: values.has('enabled'),
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
      return `<article class="subscription"><b>${esc(protocolLabel(sub.protocol))}</b><p>${status} · Срок: ${esc(dateTime(sub.expires))}</p></article>`;
    }).join('') : '<p>Подписок пока нет.</p>';
    $('user-orders').innerHTML = data.orders.length ? data.orders.map(order => `<article class="admin-order"><b>${esc(order.plan_name || order.plan)} · ${order.days} дней · ${order.amount} ${order.currency === 'XTR' ? '★' : '₽'}</b><p>${esc(protocolLabel(order.protocol))} · ${esc(orderStatus[order.status] || order.status)}</p><p>Заказ: ${esc(order.id)}</p><p>${esc(dateTime(order.created))} · ${esc(order.method)}</p>${order.error ? `<p>Ошибка выдачи: ${esc(order.error)} · Попыток: ${order.attempts}</p>` : ''}</article>`).join('') : '<p>Заказов пока нет.</p>';
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
    $('list').innerHTML = data.orders.map(order => `<article class="admin-order"><b>${esc(order.plan_name || order.plan)} · ${esc(protocolLabel(order.protocol))} · ${order.amount} ${order.currency === 'XTR' ? '★' : '₽'}</b><p><button class="text-link" data-order-user="${order.user_id}">${esc(order.email || 'Telegram ' + order.telegram_id)} · № ${order.user_id}</button></p><p>Заказ: ${esc(order.id)}</p><p>${esc(orderStatus[order.status] || order.status)} · ${esc(order.method)} · ${esc(dateTime(order.created))}</p>${order.error ? `<p>Ошибка выдачи: ${esc(order.error)} · Попыток: ${order.attempts}</p>` : ''}<div class="actions">${order.can_confirm ? `<button data-action="confirm" data-id="${esc(order.id)}">Подтвердить поступление оплаты</button>` : ''}${order.can_reject ? `<button class="secondary" data-action="reject" data-id="${esc(order.id)}">Отменить заказ</button>` : ''}${order.can_retry ? `<button data-action="retry" data-id="${esc(order.id)}">Повторить выдачу</button>` : ''}</div></article>`).join('');
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
