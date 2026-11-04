'use strict';
const basePath = document.querySelector('meta[name="shop-base"]').content;
const $ = id => document.getElementById(id);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let token = '';
async function api(path, {method = 'GET', data} = {}) {
  const response = await fetch(basePath + '/api/admin/' + path, {
    method, headers: {Authorization: 'Bearer ' + token, ...(data ? {'Content-Type':'application/json'} : {})},
    body: data ? JSON.stringify(data) : undefined,
  });
  const result = await response.json();
  if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : 'Проверьте название, цены и срок тарифа');
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
  $('list').innerHTML = data.orders.map(o => `<article class="admin-order"><b>${esc(o.email || 'Telegram ' + o.telegram_id)} · ${esc(o.protocol)} · ${o.amount} ${o.currency === 'XTR' ? '★' : '₽'}</b><p>Заказ: ${esc(o.id)}</p><p>Статус: ${esc(o.status)} / ${new Date(o.created * 1000).toLocaleString('ru-RU')}</p>${o.error ? `<p>Ошибка выдачи: ${esc(o.error)} / попыток: ${o.attempts}</p>` : ''}<div class="actions">${o.status === 'pending' && o.method === 'manual' && !o.payment_id ? `<button data-action="confirm" data-id="${o.id}">Подтвердить поступление оплаты</button><button class="secondary" data-action="reject" data-id="${o.id}">Отменить заказ</button>` : ''}${o.status === 'paid' ? `<button data-action="retry" data-id="${o.id}">Повторить выдачу</button>` : ''}</div></article>`).join('');
}
$('admin-login').onsubmit = async event => {
  event.preventDefault(); token = $('key').value;
  try { await refresh(); $('key').value = ''; $('admin-login').hidden = true; $('dashboard').hidden = false; $('error').textContent = ''; }
  catch (error) { token = ''; $('error').textContent = error.message; }
};
$('reload').onclick = () => refresh().catch(error => $('error').textContent = error.message);
$('exit').onclick = () => {
  token = ''; $('list').replaceChildren(); $('stats').replaceChildren(); $('plan-editors').replaceChildren();
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
  const button = event.target.closest('[data-action]'); if (!button) return;
  if (button.dataset.action === 'confirm' && !confirm('Вы проверили фактическое поступление денег по этому заказу?')) return;
  button.disabled = true;
  try { await api('orders/' + button.dataset.id + '/' + button.dataset.action, {method: 'POST'}); await refresh({plans: false}); }
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
