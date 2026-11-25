'use strict';
const basePath = document.querySelector('meta[name="shop-base"]').content;
const $ = id => document.getElementById(id);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let updatesRequest = 0, updatesVersion = 0, updatesCache = null, updatesBusy = false;
let updatesDirty = false, updatesTimer = null;
const updatesErrors = {network_error:'Нет связи с GitHub. Повторим проверку автоматически.',
  rate_limited:'GitHub временно ограничил запросы. Дождитесь следующей попытки.',
  source_unavailable:'Источник релизов временно недоступен.', invalid_release:'Не удалось подтвердить стабильный релиз.',
  interrupted:'Предыдущая проверка была прервана. Она будет повторена.', check_error:'Проверка не завершилась. Повторите позже.'};
function updatesDate(value, zone) {
  if (!value) return 'ещё не выполнялась';
  const date = new Date(value * 1000);
  try {return date.toLocaleString('ru-RU', {timeZone:zone});}
  catch {return date.toLocaleString('ru-RU', {timeZone:'UTC'})+' UTC';}
}
function updateLinks(tag) {
  if (typeof tag !== 'string' || !/^v(?:0|[1-9][0-9]{0,5})\.[0-9]{3}$/.test(tag)) return null;
  const root = 'https://github.com/mrkoster1988/Shop-VPN-';
  return {release:root+'/releases/tag/'+tag, guide:root+'/blob/'+tag+'/docs/UPGRADE.md'};
}
function setUpdatesBusy(busy) {
  updatesBusy = busy;
  for (const id of ['updates-check','updates-refresh','updates-save','updates-reset','updates-enabled','updates-time','updates-timezone']) $(id).disabled = busy;
}
function clearUpdates() {
  clearHostUpdate();
  updatesRequest++; updatesCache = null; updatesVersion = 0; updatesDirty = false;
  if (updatesTimer !== null) clearTimeout(updatesTimer);
  updatesTimer = null; setUpdatesBusy(false);
  $('updates-panel').hidden = true; $('updates-open').hidden = true; $('updates-action-note').hidden = true;
  $('updates-release').hidden = true;
  for (const id of ['updates-status','updates-timing','updates-notes','updates-error']) $(id).textContent = '';
  for (const id of ['updates-current','updates-latest']) $(id).textContent = '—';
  $('updates-release-link').href = ''; $('updates-guide-link').href = '';
  if ($('updates-dialog').open) $('updates-dialog').close();
}
function renderUpdates(data) {
  updatesCache = data;
  if (data.installer) renderHostUpdate(data.installer);
  const labels = {not_checked:'Ещё не проверено',current:'Установлена последняя версия',available:'Доступно обновление',
    ahead:'Установленная версия новее релиза',check_failed:'Не удалось проверить',stale:'Нужна свежая проверка',unknown_version:'Версия не определена'};
  $('updates-badge').textContent = data.checking ? 'Проверяем…' : labels[data.state] || 'Статус неизвестен';
  $('updates-current').textContent = data.current_version;
  $('updates-latest').textContent = data.release?.tag || '—';
  $('updates-status').textContent = data.checking ? 'Проверяем опубликованный стабильный релиз…' :
    data.error_code ? updatesErrors[data.error_code] || updatesErrors.check_error : labels[data.state] || '';
  const zone = data.settings.timezone;
  const schedule = !data.settings.enabled ? 'Проверка по расписанию выключена.' : !data.scheduler_running ?
    'Фоновые задачи магазина выключены. Доступна ручная проверка.' : 'Следующая проверка: '+updatesDate(data.next_check,zone)+'.';
  $('updates-timing').textContent = 'Последняя попытка: '+updatesDate(data.last_attempt,zone)+
    '. Успешная проверка: '+updatesDate(data.last_success,zone)+'. '+schedule+' Часовой пояс: '+zone+'.'+
    (data.stale ? ' Сведения о релизе сохранены ранее; доступность обновления нужно подтвердить.' : '');
  if (!updatesDirty) {
    updatesVersion = data.settings.version;
    $('updates-enabled').checked = data.settings.enabled;
    $('updates-time').value = data.settings.check_time;
    $('updates-timezone').value = zone;
  }
  const links = updateLinks(data.release?.tag);
  $('updates-release').hidden = !links;
  $('updates-notes').textContent = links ? (data.release.notes || 'Описание не заполнено.')+
    (data.release.notes_truncated ? '\n\nПолное описание доступно на странице релиза.' : '') : '';
  $('updates-release-link').href = links ? links.release : '';
  $('updates-open').hidden = !links || !data.can_open_guide;
  $('updates-action-note').hidden = $('updates-open').hidden;
  if ($('updates-dialog').open && (!links || !data.can_open_guide)) $('updates-dialog').close();
}
function scheduleUpdatesPoll() {
  if (updatesTimer !== null) clearTimeout(updatesTimer);
  updatesTimer = null;
  if (role !== 'owner' || typeof setTimeout !== 'function') return;
  updatesTimer = setTimeout(() => {
    updatesTimer = null;
    if (document.visibilityState === 'visible' && !updatesBusy) void refreshUpdates();
    else scheduleUpdatesPoll();
  }, 60000);
}
async function refreshUpdates() {
  if (role !== 'owner') {clearUpdates(); return;}
  if (updatesBusy) return;
  $('updates-panel').hidden = false;
  const request = ++updatesRequest, epoch = authEpoch;
  try {
    const data = await api('updates');
    if (request !== updatesRequest || epoch !== authEpoch) return;
    renderUpdates(data); $('updates-error').textContent = '';
  } catch (error) {
    if (request !== updatesRequest || epoch !== authEpoch) return;
    updatesCache = null; $('updates-open').hidden = true; $('updates-action-note').hidden = true;
    $('updates-badge').textContent = 'Статус недоступен';
    if ($('updates-dialog').open) $('updates-dialog').close();
    $('updates-error').textContent = error.message;
  } finally {
    if (request === updatesRequest && epoch === authEpoch) scheduleUpdatesPoll();
  }
}
$('updates-schedule').oninput = () => {updatesDirty = true;};
$('updates-refresh').onclick = () => refreshUpdates();
$('updates-reset').onclick = () => {if (updatesBusy) return; updatesDirty = false; return refreshUpdates();};
async function mutateUpdates(path, options) {
  if (role !== 'owner' || updatesBusy) return;
  const request = ++updatesRequest, epoch = authEpoch;
  setUpdatesBusy(true); $('updates-error').textContent = ''; $('updates-open').hidden = true;
  if (path.endsWith('/check')) $('updates-status').textContent = 'Проверяем опубликованный стабильный релиз…';
  try {
    const data = await api(path, options);
    if (request !== updatesRequest || epoch !== authEpoch) return;
    if (path.endsWith('/schedule')) updatesDirty = false;
    renderUpdates(data);
    if (path.endsWith('/schedule')) $('updates-error').textContent = 'Расписание сохранено.';
  } catch (error) {
    if (request !== updatesRequest || epoch !== authEpoch) return;
    $('updates-error').textContent = error.message;
    $('updates-status').textContent = 'Действие не завершено. Обновите статус перед следующей попыткой.';
  } finally {
    if (request === updatesRequest && epoch === authEpoch) {setUpdatesBusy(false); scheduleUpdatesPoll();}
  }
}
$('updates-check').onclick = () => mutateUpdates('updates/check', {method:'POST'});
$('updates-schedule').onsubmit = event => {
  event.preventDefault();
  return mutateUpdates('updates/schedule', {method:'PUT',data:{enabled:$('updates-enabled').checked,
    check_time:$('updates-time').value,timezone:$('updates-timezone').value,version:updatesVersion}});
};
$('updates-open').onclick = () => {
  const data = updatesCache, links = updateLinks(data?.release?.tag);
  if (role !== 'owner' || updatesBusy || !data?.can_open_guide || !links) return;
  $('updates-dialog-title').textContent = 'Обновление до '+data.release.tag;
  $('updates-guide-link').href = links.guide;
  renderHostUpdate(hostUpdate);
  $('updates-dialog').showModal();
};
$('updates-dialog-close').onclick = () => $('updates-dialog').close();
// Установка выполняется службой VPS; в браузере остаются только план и статус.
let hostUpdate=null, hostUpdateRequest=0, hostUpdateBusy=false, hostUpdateTimer=null;
let hostPlanIntent=null, hostApplyIntent=null, hostReviewedPlan=null;
const hostStates={queued:'Запрос принят',running:'Операция выполняется',ready:'План готов',succeeded:'Обновление завершено',
  failed:'Операция не выполнена',rolled_back:'Возвращена предыдущая версия',recovery_required:'Нужно восстановление на VPS',interrupted:'Служба перезапущена: проверьте состояние на VPS'};
const hostPhases={building:'Собираем образ',stopping:'Останавливаем магазин',copying:'Создаём копию',verifying_backup:'Проверяем копию',
  backed_up:'Копия проверена',migrating:'Проверяем данные новой версии',switching:'Запускаем новую версию',ready:'Версия готова к активации',
  activating:'Активируем магазин',committed:'Версия активирована',rolled_back:'Откат завершён',activation_failed:'Активация не подтверждена',recovery_required:'Нужна проверка на VPS'};
function clearHostUpdate(){
  hostUpdateRequest++;hostUpdate=null;hostUpdateBusy=false;hostPlanIntent=null;hostApplyIntent=null;hostReviewedPlan=null;
  if(hostUpdateTimer!==null && typeof clearTimeout==='function')clearTimeout(hostUpdateTimer);
  hostUpdateTimer=null;$('host-update-panel').hidden=true;$('host-update-controls').hidden=true;
  $('host-update-confirm').hidden=true;$('host-update-ack').checked=false;$('host-update-apply').disabled=true;
  for(const id of ['host-update-status','host-update-detail','host-update-review','host-update-error'])$(id).textContent='';
}
function hostCanApply(){
 const job=hostUpdate?.job;
 return role==='owner' && !hostUpdateBusy && hostUpdate?.reachable && job?.kind==='plan' && job.state==='ready'
   && job.can_apply && job.expires*1000>Date.now() && updatesCache?.can_open_guide && job.tag===updatesCache?.release?.tag;
}
function renderHostUpdate(data){
 hostUpdate=data;
 const enabled=!!data?.enabled,job=data?.job,active=['queued','running'].includes(job?.state);
 $('host-update-panel').hidden=!enabled;$('host-update-controls').hidden=!enabled;$('updates-manual').hidden=enabled;
 $('updates-action-note').textContent=enabled?'Сначала проверьте план, затем подтвердите установку.':'Обновление выполняется через команды на VPS. Кнопка открывает порядок действий.';
 $('host-update-status').textContent=!data?.reachable?'Связь со службой не подтверждена. Принятая операция может продолжаться.':job?
   (hostStates[job.state]||'Неизвестное состояние')+(job.phase?' · '+(hostPhases[job.phase]||'Проверка состояния'):''):'Служба подключена. Обновление не запущено.';
 $('host-update-detail').textContent=job?'Версия: '+job.tag+' · операция '+job.id+'.'+
   (job.activation_started?' Активация началась: возврат старой базы заблокирован.':'')+
   (['interrupted','recovery_required'].includes(job.state)?' Используйте status службы на VPS; автоматического повтора нет.':''):'';
 $('host-update-plan').disabled=hostUpdateBusy || !data?.reachable || active || !updatesCache?.can_open_guide;
 if(hostReviewedPlan!==job?.id){hostReviewedPlan=job?.id;$('host-update-ack').checked=false;}
 const ready=hostCanApply();$('host-update-confirm').hidden=!ready;
 $('host-update-apply').disabled=!ready || !$('host-update-ack').checked;
 $('host-update-review').textContent=job?.kind==='plan'&&job.state==='ready'?
  job.previous+' → '+job.tag+' · коммит '+job.commit+' · план до '+updatesDate(job.expires,'Europe/Moscow')+'.'+(ready?'':' План сейчас недоступен для установки; подготовьте новый.'):
  job ? (hostStates[job.state]||'Проверяем состояние') : '';
 scheduleHostPoll();
}
function scheduleHostPoll(){
 if(hostUpdateTimer!==null && typeof clearTimeout==='function')clearTimeout(hostUpdateTimer);
 hostUpdateTimer=null;
 if(role!=='owner'||!hostUpdate?.enabled||typeof setTimeout!=='function')return;
 hostUpdateTimer=setTimeout(()=>{hostUpdateTimer=null;void refreshHostUpdate();},5000);
}
async function refreshHostUpdate(){
 if(role!=='owner'||hostUpdateBusy)return;
 const request=++hostUpdateRequest,epoch=authEpoch;
 try{
  const data=await api('updates/operation');if(request!==hostUpdateRequest||epoch!==authEpoch)return;
  renderHostUpdate(data);
 }catch(error){
  if(request!==hostUpdateRequest||epoch!==authEpoch)return;
  renderHostUpdate({...hostUpdate,reachable:false});
  $('host-update-error').textContent='Связь с магазином прервана. Дождитесь запуска и войдите снова при необходимости. Принятая службой операция продолжается; повторная установка не отправлена.';
 }
}
async function mutateHostUpdate(action){
 if(role!=='owner'||hostUpdateBusy||!hostUpdate?.reachable)return;
 let data;
 if(action==='plan'){
  if(!updatesCache?.can_open_guide)return;
  const tag=updatesCache.release.tag;
  if(!hostPlanIntent||hostPlanIntent.tag!==tag||hostUpdate.job?.id===hostPlanIntent.request_id)hostPlanIntent={tag,request_id:crypto.randomUUID()};
  data=hostPlanIntent;
 }else{
  if(!hostCanApply()||!$('host-update-ack').checked)return;
  const job=hostUpdate.job;
  if(!hostApplyIntent||hostApplyIntent.plan_id!==job.id)hostApplyIntent={plan_id:job.id,tag:job.tag,request_id:crypto.randomUUID()};
  data=hostApplyIntent;
 }
 const request=++hostUpdateRequest,epoch=authEpoch;hostUpdateBusy=true;
 renderHostUpdate(hostUpdate);$('host-update-error').textContent='';
 try{
  const result=await api('updates/'+action,{method:'POST',data});
  if(request!==hostUpdateRequest||epoch!==authEpoch)return;
  hostUpdate=result;
 }catch(error){
  if(request!==hostUpdateRequest||epoch!==authEpoch)return;
  hostUpdate={...hostUpdate,reachable:false};$('host-update-error').textContent=error.message+' Сначала проверьте ход операции.';
 }finally{
  if(request===hostUpdateRequest&&epoch===authEpoch){hostUpdateBusy=false;renderHostUpdate(hostUpdate);}
 }
}
$('host-update-refresh').onclick=()=>refreshHostUpdate();
$('host-update-plan').onclick=()=>mutateHostUpdate('plan');
$('host-update-apply').onclick=()=>mutateHostUpdate('apply');
$('host-update-ack').onchange=()=>{$('host-update-apply').disabled=!hostCanApply()||!$('host-update-ack').checked;};
let diagnosticsRequest=0;
let adsFinanceRequest=0,adsFinancePage=1,adsExpenseIntents=new Map();
let adsBookingIntents=new Map(),adsBookingRequest=0,adsBookingPage=1;
let adsStopRequest=0,adsStopPage=1;
let adsSitesCache=[],adsPrivateSites=[],adsSitesRequest=0,adsSiteEditor=null,adsSiteMutation=0,adsPlacementIntents=new Map();
let adsPolicy=null,adsPage=1,adsRequest=0,adsPaymentIntents=new Map();
let token = '', role = '', authEpoch = 0, usersPage = 1, usersQuery = '', usersRequest = 0, shopSettingsVersion = 0, commerceAnalyticsRequest = 0;
let delegatedPermissions=new Set(),staffActorId=null,moderationRequest=0,moderationPage=1,rolePolicyRequest=0;
const STAFF_LABELS={owner:'Разработчик',administrator:'Администратор',moderator:'Модератор',panel_operator:'Оператор панелей',mtproxy_manager:'Менеджер MTProxy',finance:'Финансовый сотрудник',support:'Поддержка'};
const DELEGATED_ROLES=['administrator','moderator'];
const LEGACY_GROUPS={panel_operator:['dashboard','customers','orders','servers_read','diagnostics'],support:['dashboard','customers','orders','support','diagnostics'],finance:['dashboard','orders','payments','analytics','advertising','ad_finance'],mtproxy_manager:['advertising','placements']};
function delegatedRole(){return DELEGATED_ROLES.includes(role);}
function canGroup(group){
  return role==='owner' || (delegatedRole()?delegatedPermissions.has(group):(LEGACY_GROUPS[role]||[]).includes(group));
}
function acceptStaffRole(result){
  role=typeof result.role==='string'?result.role:'';
  if(!Object.hasOwn(STAFF_LABELS,role))throw new Error('Неизвестная роль сотрудника');
  staffActorId=Number.isSafeInteger(result.id)?result.id:staffActorId;
  delegatedPermissions=new Set(Array.isArray(result.permissions)?result.permissions.filter(x=>typeof x==='string'):[]);
}
const MANAGEMENT_SECTIONS=[
  ['shop-settings','store_settings','Настройки'],['appearance-section','store_settings','Оформление'],['pushok-id-settings','developer','Pushok ID'],['sales-controls-section','sales_controls','Продажи'],['payment-policy-section','payment_settings','Оплата'],['shop-finance-section','finance_read','Финансы Shop'],['notifications-section','notifications','Уведомления'],['updates-panel','developer','Обновления'],
  ['bot-menu-panel','bot_menu','Меню Telegram'],['staff-management','developer','Сотрудники'],
  ['role-policy-section','developer','Полномочия'],['moderation-section','moderation','Модерация'],
  ['support-admin','support','Поддержка'],['commerce-analytics','analytics','Аналитика'],
  ['plans-section','catalog','Тарифы'],['users-section','customers','Пользователи'],
  ['orders-section','orders','Заказы'],['servers-section','servers_read','Серверы'],['server-routing-section','servers_read','Распределение'],
  ['protocols-panel','servers_read','Протоколы'],['flow-auto-section','starter','Автовыдача'],
  ['coupons-section','promotions','Промокоды'],['referrals-section','promotions','Бонусы'],
  ['trial-section','promotions','Пробный доступ'],['ads-requests-section','advertising','Реклама MTProxy'],
  ['ads-finance-section','ad_finance','Финансы рекламы'],['ads-bookings-section','placements','Брони'],
  ['ads-stop-section','placements','Остановки'],['ads-sites-section','placements','Площадки'],
  ['diagnostics-section','diagnostics','Диагностика']
];
// Keep navigation metadata separate from permissions: the API remains the
// sole authority. All menu items resolve to existing approved sections.
const MANAGEMENT_NAV_GROUPS = [
  ['Подключения', ['servers-section','server-routing-section','protocols-panel','flow-auto-section','trial-section']],
  ['Пользователи', ['users-section','orders-section','moderation-section','staff-management','support-admin']],
  ['Финансы', ['plans-section','payment-policy-section','shop-finance-section','commerce-analytics','coupons-section','referrals-section']],
  ['Реклама', ['ads-requests-section','ads-finance-section','ads-bookings-section','ads-stop-section','ads-sites-section']],
  ['Система', ['shop-settings','appearance-section','sales-controls-section','notifications-section','updates-panel','bot-menu-panel','role-policy-section','diagnostics-section','pushok-id-settings']]
];
const MANAGEMENT_SEARCH_TAGS={
  'servers-section':'wgdashboard wireguard wg0 3x-ui сервер api ключ',
  'server-routing-section':'узлы маршрутизация',
  'flow-auto-section':'wireguard flow 30 дней бесплатная выдача',
  'diagnostics-section':'состояние api здоровье',
  'users-section':'аккаунты регистрация',
  'updates-panel':'версия релиз обновление',
  'bot-menu-panel':'telegram бот кнопки'
};
const MANAGEMENT_QUICK = [
  ['servers-section','Подключить WGDashboard','Добавить WireGuard-сервер и проверить API','⚙'],
  ['flow-auto-section','Flow · 30 дней','Настроить лимит бесплатных подключений','◈'],
  ['users-section','Пользователи','Аккаунты и персональные подключения','♙'],
  ['orders-section','Заказы','Ожидающие действия и выдача','▤'],
  ['diagnostics-section','Диагностика','Проверка работы компонентов','ⓘ'],
  ['updates-panel','Обновления','Версия и безопасное обновление Shop','↻']
];
let currentManagementSection='management-overview';
let managementMenuOpen=false;
const MANAGEMENT_MOBILE_TABS=[['users-section','Клиенты','♙'],['orders-section','Заказы','▤'],['servers-section','Серверы','◈'],['moderation-section','Модерация','✓'],['support-admin','Помощь','ⓘ'],['plans-section','Тарифы','★'],['shop-finance-section','Финансы','₽']];
function managementMeta(id){
  return MANAGEMENT_SECTIONS.find(section=>section[0]===id);
}
function canViewManagementSection(id){
  return id==='management-overview' || !!(managementMeta(id)&&canGroup(managementMeta(id)[1]));
}
function managementAnchor(id,title,extraClass=''){
  const active=currentManagementSection===id?' aria-current="page"':'';
  return '<a href="#'+id+'" data-admin-target="'+id+'"'+active+' class="'+extraClass+'">'+esc(title)+'</a>';
}
function renderManagementNavItems(){
  const query=($('management-search').value||'').trim().toLocaleLowerCase('ru-RU');
  let markup=query?'':managementAnchor('management-overview','⌂ Рабочий стол','management-overview-link');
  const visited=new Set();
  for(const [label,ids] of MANAGEMENT_NAV_GROUPS){
    const matches=[];
    for(const id of ids){
      const item=managementMeta(id);
      if(!item||!canGroup(item[1]))continue;
      visited.add(id);
      if(!query || (item[2]+' '+(MANAGEMENT_SEARCH_TAGS[id]||'')).toLocaleLowerCase('ru-RU').includes(query)){
        matches.push(managementAnchor(id,item[2]));
      }
    }
    if(!matches.length)continue;
    const expanded=!!query || ids.includes(currentManagementSection);
    markup+='<details class="management-nav-group"'+(expanded?' open':'')+'><summary>'+esc(label)+' <span>'+matches.length+'</span></summary>'+
      '<div class="management-nav-items">'+matches.join('')+'</div></details>';
  }
  // Future sections remain reachable even before they are categorized.
  const extra=MANAGEMENT_SECTIONS.filter(item=>canGroup(item[1])&&!visited.has(item[0])&&
    (!query||item[2].toLocaleLowerCase('ru-RU').includes(query)));
  if(extra.length){
    markup+='<details class="management-nav-group" open><summary>Другое <span>'+extra.length+'</span></summary>'+
      '<div class="management-nav-items">'+extra.map(item=>managementAnchor(item[0],item[2])).join('')+'</div></details>';
  }
  $('management-nav').innerHTML=markup||'<p class="management-empty">Разделы не найдены.</p>';
}
function renderManagementMobileNav(){
  const tabs=[['management-overview','Главная','⌂'],...MANAGEMENT_MOBILE_TABS.filter(([id])=>canViewManagementSection(id)).slice(0,3)];
  $('management-mobile-nav').innerHTML=tabs.map(([id,title,icon])=>
    '<button type="button" data-admin-target="'+id+'"'+(currentManagementSection===id&&!managementMenuOpen?' aria-current="page"':'')+'><span aria-hidden="true">'+icon+'</span>'+esc(title)+'</button>'
  ).join('')+'<button type="button" data-admin-menu aria-controls="management-menu-content" aria-expanded="'+String(managementMenuOpen)+'"'+(managementMenuOpen?' aria-current="page"':'')+'><span aria-hidden="true">☰</span>Разделы</button>';
}
function setManagementMenu(open,{scroll=false}={}){
  managementMenuOpen=!!open;
  const header=$('management-header');header.dataset ||= {};
  header.dataset.mobileMenuOpen=String(managementMenuOpen);
  $('management-menu-toggle').setAttribute?.('aria-expanded',String(managementMenuOpen));
  renderManagementMobileNav();
  if(managementMenuOpen&&scroll)header.scrollIntoView?.({behavior:'auto',block:'start'});
}
function showManagementSection(id,{scroll=false}={}){
  currentManagementSection=canViewManagementSection(id)?id:'management-overview';
  $('dashboard').dataset ||= {};
  $('dashboard').dataset.adminFocused='true';
  const overview=$('management-overview');
  overview.dataset ||= {};
  overview.dataset.adminActive=currentManagementSection==='management-overview'?'true':'false';
  for(const [section,group] of MANAGEMENT_SECTIONS){
    const node=$(section);
    node.dataset ||= {};
    node.dataset.adminActive=section===currentManagementSection&&canGroup(group)?'true':'false';
  }
  const info=managementMeta(currentManagementSection);
  $('management-current').textContent=currentManagementSection==='management-overview'
    ?'Раздел: рабочий стол':'Раздел: '+info[2];
  if(scroll)$('management-search').value='';
  renderManagementNavItems();setManagementMenu(false);
  if(scroll){
    $(currentManagementSection).scrollIntoView?.({behavior:'smooth',block:'start'});
  }
}
function renderManagementNav(){
  $('management-role').textContent=STAFF_LABELS[role]||'Кабинет сотрудника';
  $('management-scope').textContent=role==='owner'?'Все настройки и полномочия разработчика.':'Доступ только к назначенным разделам.';
  for(const [id,group] of MANAGEMENT_SECTIONS) $(id).hidden=!canGroup(group);
  $('management-overview').hidden=false;
  $('stats').hidden=!canGroup('dashboard');
  $('new-server').hidden=delegatedRole();
  const quick=MANAGEMENT_QUICK.filter(([id])=>canViewManagementSection(id));
  $('management-quick').innerHTML=quick.map(([id,title,description,icon])=>
    '<a href="#'+id+'" class="management-quick-card" data-admin-target="'+id+'"><span aria-hidden="true">'+icon+'</span><strong>'+esc(title)+'</strong><small>'+esc(description)+'</small></a>'
  ).join('')||'<p>Быстрые действия пока недоступны вашей роли.</p>';
  showManagementSection(currentManagementSection);
}
// Only authorized areas are navigable. A crafted hash/link never grants access.
$('dashboard').addEventListener('click',event=>{
  const menu=event.target?.closest?.('[data-admin-menu]');
  if(menu){event.preventDefault();setManagementMenu(!managementMenuOpen,{scroll:true});return;}
  const link=event.target?.closest?.('[data-admin-target]');
  if(!link)return;
  const destination=link.dataset?.adminTarget;
  if(!canViewManagementSection(destination)){
    event.preventDefault();
    return;
  }
  event.preventDefault();
  showManagementSection(destination,{scroll:true});
});
$('management-home').onclick=()=>showManagementSection('management-overview',{scroll:true});
$('management-search').oninput=()=>renderManagementNavItems();

async function refreshModeration(){
  const seq=++moderationRequest,epoch=authEpoch;
  if(!canGroup('moderation')){$('moderation-section').hidden=true;$('moderation-list').replaceChildren();return;}
  $('moderation-section').hidden=false;
  try{
    const filter=$('moderation-state').value||'pending';
    const data=await api('moderation/requests?page='+moderationPage+'&state='+encodeURIComponent(filter));
    if(seq!==moderationRequest||epoch!==authEpoch||!canGroup('moderation'))return;
    const labels={pending:'На модерации',approved:'Одобрено',rejected:'Отклонено',canceled:'Отменено'};
    $('moderation-list').innerHTML=data.requests.map(r=>'<article class="admin-order"><h3>'+esc('@'+r.channel)+'</h3><p>'+esc(labels[r.state]||r.state)+'</p><p>'+esc(r.comment||'')+'</p><p>'+esc(r.reason||'')+'</p>'+
      (r.state==='pending'?'<form data-moderation-id="'+esc(r.id)+'" data-version="'+r.version+'"><label>Решение<select name="state"><option value="approved">Одобрить</option><option value="rejected">Отклонить</option></select></label><label>Причина<textarea name="reason" maxlength="500"></textarea></label><button type="submit" class="small">Сохранить решение</button></form>':'')+'</article>').join('')||'<p>Заявок нет.</p>';
    $('moderation-page').textContent='Страница '+data.page+' · всего '+data.total;
    $('moderation-prev').disabled=data.page<=1;$('moderation-next').disabled=data.page*data.page_size>=data.total;
    $('moderation-status').textContent='';
  }catch(e){if(seq===moderationRequest&&epoch===authEpoch){$('moderation-list').replaceChildren();$('moderation-status').textContent=e.message;}}
}
$('moderation-refresh').onclick=()=>refreshModeration();
$('moderation-state').onchange=()=>{moderationPage=1;return refreshModeration();};
$('moderation-prev').onclick=()=>{if(moderationPage>1){moderationPage--;return refreshModeration();}};
$('moderation-next').onclick=()=>{if(!$('moderation-next').disabled){moderationPage++;return refreshModeration();}};
$('moderation-list').onsubmit=async event=>{
  event.preventDefault();const f=event.target.closest('[data-moderation-id]');if(!f||!canGroup('moderation'))return;
  const values=new FormData(f),epoch=authEpoch,seq=moderationRequest,button=f.querySelector('button');
  if(button.disabled)return;button.disabled=true;
  try{await api('moderation/requests/'+encodeURIComponent(f.dataset.moderationId),{method:'PUT',data:{version:Number(f.dataset.version),state:values.get('state'),reason:String(values.get('reason')||'')}});
    if(epoch===authEpoch&&seq===moderationRequest&&canGroup('moderation'))await refreshModeration();
  }catch(e){if(epoch===authEpoch&&seq===moderationRequest)$('moderation-status').textContent=e.message;}
  finally{button.disabled=false;}
};
async function refreshRolePolicies(){
  const seq=++rolePolicyRequest,epoch=authEpoch;
  if(role!=='owner'){$('role-policy-section').hidden=true;$('role-policy-list').replaceChildren();return;}
  $('role-policy-section').hidden=false;
  try{
    const data=await api('role-policies');
    if(seq!==rolePolicyRequest||epoch!==authEpoch||role!=='owner')return;
    $('role-policy-list').innerHTML=data.roles.map(p=>'<form class="plan-editor" data-role-policy="'+esc(p.role)+'" data-version="'+p.version+'"><h3>'+esc(STAFF_LABELS[p.role]||p.role)+'</h3><div class="permission-grid">'+p.allowed.map(key=>'<label class="permission-choice"><input type="checkbox" name="permissions" value="'+esc(key)+'" '+(p.permissions.includes(key)?'checked':'')+'>'+esc(data.labels[key]||key)+'</label>').join('')+'</div><button class="small" type="submit">Сохранить полномочия</button></form>').join('');
    $('role-policy-status').textContent='';
  }catch(e){if(seq===rolePolicyRequest&&epoch===authEpoch)$('role-policy-status').textContent=e.message;}
}
$('role-policy-list').onsubmit=async event=>{
  event.preventDefault();const f=event.target.closest('[data-role-policy]');if(!f||role!=='owner')return;
  const data=new FormData(f),epoch=authEpoch,button=f.querySelector('button');if(button.disabled)return;button.disabled=true;
  try{await api('role-policies/'+encodeURIComponent(f.dataset.rolePolicy),{method:'PUT',data:{version:Number(f.dataset.version),permissions:data.getAll('permissions')}});
    if(epoch===authEpoch&&role==='owner'){await refreshRolePolicies();$('role-policy-status').textContent='Полномочия сохранены.';}
  }catch(e){if(epoch===authEpoch)$('role-policy-status').textContent=e.message;}finally{button.disabled=false;}
};
function clearManagement(){
  delegatedPermissions.clear();staffActorId=null;moderationRequest++;rolePolicyRequest++;moderationPage=1;
  currentManagementSection='management-overview';managementMenuOpen=false;
  $('management-search').value='';$('management-header').dataset ||= {};$('management-header').dataset.mobileMenuOpen='false';
  $('management-menu-toggle').setAttribute?.('aria-expanded','false');$('management-mobile-nav').replaceChildren();
  for(const id of ['management-nav','management-role','management-scope','moderation-list','moderation-status','moderation-page','role-policy-list','role-policy-status'])$(id).replaceChildren();
  $('moderation-state').value='pending';$('moderation-section').hidden=true;$('role-policy-section').hidden=true;
}
async function refreshDelegated({plans=true}={}){
  const epoch=authEpoch;
  const identity=await api('auth/me');
  if(epoch!==authEpoch)return;
  acceptStaffRole(identity);
  if(!delegatedRole())throw new Error('Роль изменилась. Войдите заново.');
  renderManagementNav();
  $('stats').replaceChildren();
  const tasks=[];
  if(canGroup('dashboard'))tasks.push(api('dashboard-summary').then(data=>{if(epoch===authEpoch)$('stats').innerHTML='<div>Пользователи<strong>'+esc(data.users)+'</strong></div><div>Подписки<strong>'+esc(data.active)+'</strong></div><div>Заказы в работе<strong>'+esc(data.pending_orders)+'</strong></div>';}));
  if(canGroup('catalog')&&plans)tasks.push(api('plans').then(data=>{if(epoch===authEpoch)$('plan-editors').innerHTML=data.plans.map(planForm).join('');}));
  if(canGroup('orders'))tasks.push(refreshOrders());
  if(canGroup('customers'))tasks.push(refreshUsers());
  if(canGroup('servers_read'))tasks.push(refreshServers(),refreshProtocols(),refreshRouting());else clearRouting();
  if(canGroup('promotions'))tasks.push(refreshTrialPolicy(),refreshCoupons(),refreshReferralPolicy());
  if(canGroup('starter'))tasks.push(refreshFlowAutoPolicy());
  if(canGroup('moderation'))tasks.push(refreshModeration());
  if(canGroup('support'))tasks.push(refreshSupportAdmin());
  if(canGroup('store_settings'))tasks.push(refreshShopSettings(),refreshRuntime('appearance'));
  if(canGroup('sales_controls'))tasks.push(refreshRuntime('sales'));
  if(canGroup('notifications'))tasks.push(refreshRuntime('notifications'));
  if(canGroup('payment_settings'))tasks.push(refreshPaymentPolicy());else clearPaymentPolicy();
  if(canGroup('finance_read'))tasks.push(refreshShopFinance());else clearShopFinance();
  if(canGroup('bot_menu'))tasks.push(refreshBotMenu());
  if(canGroup('analytics'))tasks.push(refreshCommerceAnalytics());
  if(canGroup('diagnostics'))tasks.push(refreshDiagnostics());
  await Promise.all(tasks);
  if(epoch===authEpoch)renderManagementNav();
}

let selectedUser = null, profilePage = 1, profileRequest = 0;
let flowExtensionAttempts = new Map();
let serverCache = [], editingServer = null;
let serverWizardStep = 3, serverDialogEpoch = 0, serverSubmitting = false;
let credentialsServer = null, credentialsEpoch = 0, credentialsSubmitting = false;
let protocolCache = [], protocolsRequest = 0;
let ordersPage = 1, ordersRequest = 0, ordersFilters = {q:'', status:'', method:'', protocol:''};
async function api(path, {method = 'GET', data} = {}) {
  const epoch = authEpoch;
  const response = await fetch(basePath + '/api/admin/' + path, {
    method, credentials: 'same-origin', headers: {...(data ? {'Content-Type':'application/json'} : {})},
    body: data ? JSON.stringify(data) : undefined,
  });
  const result = await response.json();
  if (epoch !== authEpoch) throw new Error('Сессия администратора завершена');
  if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : 'Проверьте введённые данные');
  return result;
}

// Справочник не включает продажи: доступность проверяется отдельно для каждого заказа.
function renderProtocols() {
  const filter = $('protocols-filter').value || 'all';
  const items = protocolCache.filter(item => filter === 'all' || item.implementation === filter);
  $('protocols-list').innerHTML = items.map(item => {
    const implemented = item.implementation === 'in_code';
    const status = implemented ? 'Адаптер в коде' : item.implementation === 'planned' ? 'В плане' : 'Статус неизвестен';
    const url = typeof item.documentation_url === 'string' && /^https:\/\/[a-z0-9.-]+(?:\/[^\s\\]*)?$/i.test(item.documentation_url) ? item.documentation_url : '';
    const adapters = implemented && Array.isArray(item.adapters) ? item.adapters.join(' · ') : 'Выдача магазином пока не реализована';
    return '<article class="protocol-card"><div class="protocol-card-heading"><h3>' + esc(item.name) + '</h3>' +
      '<span class="protocol-badge' + (implemented ? ' implemented' : '') + '">' + status + '</span></div>' +
      '<p>' + esc(item.description) + '</p><dl><dt>Сервер / панель</dt><dd>' + esc(item.server_options) +
      '</dd><dt>Адаптеры магазина</dt><dd>' + esc(adapters) + '</dd><dt>Для подключения</dt><dd>' +
      esc(item.client_requirements) + '</dd></dl>' +
      (url ? '<a href="' + esc(url) + '" target="_blank" rel="noopener noreferrer">Официальная документация ↗</a>' : '') + '</article>';
  }).join('') || '<p>В этой группе пока нет протоколов.</p>';
  $('protocols-status').textContent = 'Показано: ' + items.length + ' из ' + protocolCache.length + '.';
}
async function refreshProtocols() {
  const section = $('protocols-panel'), epoch = authEpoch, request = ++protocolsRequest;
  if (!canGroup('servers_read')) {
    section.hidden = true; protocolCache = []; $('protocols-list').replaceChildren();
    $('protocols-status').textContent = ''; return;
  }
  section.hidden = false; $('protocols-refresh').disabled = true;
  $('protocols-status').textContent = 'Загружаем справочник…';
  try {
    const data = await api('protocols');
    if (epoch !== authEpoch || request !== protocolsRequest) return;
    if (data.schema_version !== 1 || !Array.isArray(data.protocols)) throw new Error('Не удалось прочитать справочник протоколов.');
    protocolCache = data.protocols; renderProtocols();
  } catch (error) {
    if (epoch !== authEpoch || request !== protocolsRequest) return;
    protocolCache = []; $('protocols-list').replaceChildren(); $('protocols-status').textContent = error.message;
  } finally {
    if (epoch === authEpoch && request === protocolsRequest) $('protocols-refresh').disabled = false;
  }
}
$('protocols-refresh').onclick = () => refreshProtocols();
$('protocols-filter').onchange = () => renderProtocols();
function planForm(plan) {
  return `<form class="plan-editor" data-plan-id="${esc(plan.id)}" data-version="${plan.version}">
    <div class="plan-editor-heading"><h3>${esc(plan.name)}</h3><span class="status">${plan.enabled ? 'В продаже' : 'Скрыт'}</span></div>
    <p class="muted">Код: ${esc(plan.id)}</p>
    <label>Название<input name="name" value="${esc(plan.name)}" maxlength="80" required></label>
    <div class="plan-editor-fields">
      <label>Срок, дней<input name="days" type="number" value="${plan.days}" min="1" max="3650" step="1" required></label>
      <label>Цена на сайте, ₽<input name="price" type="number" value="${plan.price}" min="0" max="1000000" step="1" required></label>
      <label>Цена в Telegram, Stars (для бесплатного тарифа обе цены — 0)<input name="stars" type="number" value="${plan.stars}" min="0" max="100000" step="1" required></label>
      <label>Бесплатных активаций всего (0 — без ограничения)<input name="free_activation_limit" type="number" value="${plan.free_activation_limit ?? 0}" min="0" max="1000000" step="1" required></label>
      <label>Трафик на профиль 3x-ui, ГБ (0 — без ограничения)<input name="free_traffic_gb" type="number" value="${plan.free_traffic_gb ?? 0}" min="0" max="10000" step="1" required></label>
      <label class="plan-enabled"><input name="free_auto_renew" type="checkbox" ${plan.free_auto_renew ? 'checked' : ''}> Автоматически продлевать бесплатный доступ</label>
      <label>Максимум продлений (1–12, если включено)<input name="free_max_renewals" type="number" value="${plan.free_max_renewals ?? 0}" min="0" max="12" step="1" required></label>
      <label>Дополнительных профилей (0 — только основной)<input name="device_limit" type="number" value="${plan.device_limit ?? 4}" min="0" max="20" step="1" required></label>
    </div>
    <label class="plan-enabled"><input name="enabled" type="checkbox" ${plan.enabled ? 'checked' : ''}> Доступен для новых покупок</label>
    <div class="record-actions"><button type="submit" class="small">Сохранить тариф</button>
    ${role==='owner' && !plan.enabled && !['month','quarter','year'].includes(plan.id)
      ? '<button type="button" class="danger-action" data-plan-delete="'+esc(plan.id)+'">Удалить неиспользуемый тариф</button>' : ''}</div>
  </form>`;
}
async function refreshShopSettings(){
 const section=$('shop-settings');if(!canGroup('store_settings')){section.hidden=true;return}section.hidden=false;
 try{
  const data=await api('shop-settings');shopSettingsVersion=data.version;
  $('shop-support-text').value=data.support_text;$('shop-terms-text').value=data.terms_text;
  $('shop-settings-status').textContent='Настройки загружены. Версия '+data.version+'.';
 }catch(error){$('shop-settings-status').textContent=error.message}
}
$('shop-settings-form').onsubmit=async event=>{
 event.preventDefault();const button=$('shop-settings-save');button.disabled=true;
 try{
  const data=await api('shop-settings',{method:'PUT',data:{
   support_text:$('shop-support-text').value,terms_text:$('shop-terms-text').value,version:shopSettingsVersion
  }});
  shopSettingsVersion=data.version;$('shop-settings-status').textContent='Настройки сохранены. Новые значения уже доступны покупателям и Telegram-боту.';
 }catch(error){$('shop-settings-status').textContent=error.message}
 finally{button.disabled=false}
};
// Независимые версии формы: сохраняем только выбранный раздел.
const RUNTIME_GROUPS={
 appearance:{permission:'store_settings',fields:{brand_name:'text',headline:'text',description:'text',announcement:'text',theme:'text'}},
 sales:{permission:'sales_controls',fields:{new_sales:'bool',renewals:'bool',web_sales:'bool',telegram_sales:'bool',enabled_protocols:'protocols',closed_notice:'text'}},
 notifications:{permission:'notifications',fields:{order_ready_enabled:'bool',expiry_enabled:'bool',expiry_hours:'number',purchase_text:'text',trial_text:'text',bonus_text:'text',expiry_text:'text'}}
};
const RUNTIME_PROTOCOLS=['xui','wg','vmess','hysteria2','tuic'];
const runtimeState={},runtimeSeq={},runtimeBusy={};
function runtimeControl(key){return $('runtime-'+key);}
function runtimeFields(group,callback){
 for(const [key,type] of Object.entries(RUNTIME_GROUPS[group].fields)){
  if(type==='protocols')for(const protocol of RUNTIME_PROTOCOLS)callback(runtimeControl('protocol-'+protocol),key,type,protocol);
  else callback(runtimeControl(key),key,type);
 }
}
function runtimeControls(group,disabled){
 runtimeFields(group,element=>{element.disabled=disabled;});
 $('runtime-'+group+'-save').disabled=disabled;
}
function clearRuntimeGroup(group){
 runtimeSeq[group]=(runtimeSeq[group]||0)+1;runtimeState[group]=null;runtimeBusy[group]=false;
 runtimeFields(group,(el,key,type)=>{if(type==='bool'||type==='protocols')el.checked=false;else el.value='';});
 runtimeControls(group,true);$('runtime-'+group+'-status').textContent='';
}
function clearRuntime(){for(const group of Object.keys(RUNTIME_GROUPS))clearRuntimeGroup(group);}
function applyRuntime(group,data){
 if(!Number.isInteger(data.version)||data.version<0)throw new Error('Не удалось прочитать версию настроек');
 for(const [key,type] of Object.entries(RUNTIME_GROUPS[group].fields)){
  const value=data[key];
  if(type==='protocols'?!Array.isArray(value):type==='bool'?typeof value!=='boolean':type==='number'?!Number.isInteger(value):typeof value!=='string')throw new Error('Не удалось прочитать настройки');
 }
 runtimeFields(group,(el,key,type,protocol)=>{
  if(type==='protocols')el.checked=data[key].includes(protocol);
  else if(type==='bool')el.checked=data[key];
  else el.value=String(data[key]);
 });
 runtimeState[group]={version:data.version};
 runtimeControls(group,false);
}
async function refreshRuntime(group){
 const def=RUNTIME_GROUPS[group];
 if(!def||!canGroup(def.permission)){if(def)clearRuntimeGroup(group);return;}
 if(runtimeBusy[group])return;
 const seq=runtimeSeq[group]=(runtimeSeq[group]||0)+1,epoch=authEpoch;
 runtimeState[group]=null;runtimeControls(group,true);
 $('runtime-'+group+'-status').textContent='Загрузка…';
 try{
  const data=await api('runtime/'+group);
  if(seq!==runtimeSeq[group]||epoch!==authEpoch||!canGroup(def.permission))return;
  applyRuntime(group,data);
  $('runtime-'+group+'-status').textContent='Настройки загружены. Версия '+data.version+'.';
 }catch(e){if(seq===runtimeSeq[group]&&epoch===authEpoch)$('runtime-'+group+'-status').textContent=e.message;}
}
function runtimePayload(group){
 const payload={version:runtimeState[group].version};
 for(const [key,type] of Object.entries(RUNTIME_GROUPS[group].fields)){
  if(type==='protocols')payload[key]=RUNTIME_PROTOCOLS.filter(p=>runtimeControl('protocol-'+p).checked);
  else if(type==='bool')payload[key]=runtimeControl(key).checked;
  else if(type==='number')payload[key]=Number(runtimeControl(key).value);
  else payload[key]=runtimeControl(key).value;
 }
 return payload;
}
async function saveRuntime(group){
 const def=RUNTIME_GROUPS[group];
 if(!def||!canGroup(def.permission)||!runtimeState[group]||runtimeBusy[group])return;
 const payload=runtimePayload(group),epoch=authEpoch,seq=runtimeSeq[group]=(runtimeSeq[group]||0)+1;
 runtimeBusy[group]=true;runtimeState[group]=null;runtimeControls(group,true);
 try{
  const data=await api('runtime/'+group,{method:'PUT',data:payload});
  if(seq!==runtimeSeq[group]||epoch!==authEpoch||!canGroup(def.permission))return;
  applyRuntime(group,data);
  $('runtime-'+group+'-status').textContent='Сохранено. Версия '+data.version+'.';
 }catch(e){
  if(seq===runtimeSeq[group]&&epoch===authEpoch)$('runtime-'+group+'-status').textContent=e.message+' Обновите настройки перед повтором.';
 }finally{if(seq===runtimeSeq[group]&&epoch===authEpoch)runtimeBusy[group]=false;}
}
for(const group of Object.keys(RUNTIME_GROUPS)){
 $('runtime-'+group+'-form').onsubmit=event=>{event.preventDefault();return saveRuntime(group);};
 $('runtime-'+group+'-refresh').onclick=()=>refreshRuntime(group);
}

// Платёжная конфигурация не хранит ключи и не изменяет открытые счета.
let paymentPolicyVersion=null,paymentPolicySeq=0,paymentPolicyBusy=false;
function clearPaymentPolicy(){
 paymentPolicySeq++;paymentPolicyVersion=null;paymentPolicyBusy=false;
 for(const id of ['shop-payment-mode','shop-payment-manual'])$(id).value='';
 for(const id of ['shop-payment-web','shop-payment-stars'])$(id).checked=false;
 for(const id of ['shop-payment-readiness','shop-payment-status'])$(id).textContent='';
 $('shop-payment-save').disabled=true;
}
function renderPaymentPolicy(data){
 if(!Number.isSafeInteger(data.version)||data.version<0||!['inherit','manual','sbp','yookassa'].includes(data.web_mode))throw new Error('Не удалось прочитать политику оплаты');
 paymentPolicyVersion=data.version;
 $('shop-payment-mode').value=data.web_mode;$('shop-payment-web').checked=data.web_enabled===true;
 $('shop-payment-stars').checked=data.stars_enabled===true;$('shop-payment-manual').value=data.manual_text;
 $('shop-payment-readiness').textContent='Текущий метод: '+data.effective_web_mode+' · Незакрытые счета: '+data.open_web_orders+
 ' · ЮKassa/СБП (локальные реквизиты и HTTPS): '+(data.merchant_configured?'настроены':'не настроены')+
 ' · Telegram-бот: '+(data.stars_bot_configured?'настроен':'не настроен')+'. Реальные платежи требуют проверки.';
 $('shop-payment-save').disabled=false;
}
async function refreshPaymentPolicy(){
 if(!canGroup('payment_settings')){clearPaymentPolicy();return;}
 if(paymentPolicyBusy)return;
 const seq=++paymentPolicySeq,epoch=authEpoch;paymentPolicyVersion=null;$('shop-payment-save').disabled=true;
 try{const data=await api('payment-policy');if(seq===paymentPolicySeq&&epoch===authEpoch&&canGroup('payment_settings')){renderPaymentPolicy(data);$('shop-payment-status').textContent='';}}
 catch(e){if(seq===paymentPolicySeq&&epoch===authEpoch)$('shop-payment-status').textContent=e.message;}
}
$('shop-payment-refresh').onclick=refreshPaymentPolicy;
$('shop-payment-form').onsubmit=async event=>{
 event.preventDefault();if(!canGroup('payment_settings')||paymentPolicyVersion===null||paymentPolicyBusy)return;
 const data={version:paymentPolicyVersion,web_mode:$('shop-payment-mode').value,web_enabled:$('shop-payment-web').checked,stars_enabled:$('shop-payment-stars').checked,manual_text:$('shop-payment-manual').value};
 const epoch=authEpoch,seq=++paymentPolicySeq;paymentPolicyBusy=true;paymentPolicyVersion=null;$('shop-payment-save').disabled=true;
 try{const result=await api('payment-policy',{method:'PUT',data});if(epoch===authEpoch&&seq===paymentPolicySeq&&canGroup('payment_settings')){renderPaymentPolicy(result);$('shop-payment-status').textContent='Сохранено.';}}
 catch(e){if(epoch===authEpoch&&seq===paymentPolicySeq)$('shop-payment-status').textContent=e.message+' Обновите настройки перед повтором.';}
 finally{if(epoch===authEpoch&&seq===paymentPolicySeq)paymentPolicyBusy=false;}
};

let shopFinanceSeq=0,shopFinancePage=1,shopFinanceBusy=false,shopFinanceIntent=null,shopFinanceVoidIntents=new Map(),shopFinanceRows=[];
const financeLabels={expense:'Расход',fee:'Комиссия',refund:'Возврат',server:'Серверы',commission:'Комиссии',marketing:'Маркетинг',tax:'Налоги',other:'Прочее'};
function financeMoney(amount,currency){
 return currency==='RUB'?(amount/100).toLocaleString('ru-RU',{minimumFractionDigits:2,maximumFractionDigits:2})+' ₽':Number(amount).toLocaleString('ru-RU')+' ★';
}
function parseFinanceAmount(raw,currency){
 const value=String(raw).trim().replace(',','.');
 if(currency==='XTR'){if(!/^\d{1,11}$/.test(value))return null;const n=Number(value);return n>0&&n<=10000000000?n:null;}
 const match=/^(\d{1,9})(?:\.(\d{1,2}))?$/.exec(value);
 if(!match)return null;const n=Number(match[1])*100+Number((match[2]||'').padEnd(2,'0'));return n>0&&n<=10000000000?n:null;
}
function clearShopFinance(){
 shopFinanceSeq++;shopFinancePage=1;shopFinanceBusy=false;shopFinanceIntent=null;shopFinanceVoidIntents.clear();shopFinanceRows=[];
 for(const id of ['shop-finance-summary','shop-finance-coverage','shop-finance-methods','shop-finance-status','shop-finance-list','shop-finance-page'])$(id).replaceChildren();
 for(const id of ['shop-finance-start','shop-finance-end','shop-finance-amount','shop-finance-date','shop-finance-order','shop-finance-reference','shop-finance-note'])$(id).value='';
 $('shop-finance-verified').checked=false;$('shop-finance-kind').value='expense';$('shop-finance-category').value='server';$('shop-finance-currency').value='RUB';$('shop-finance-save').disabled=false;
 $('shop-finance-prev').disabled=true;$('shop-finance-next').disabled=true;$('shop-finance-entry-form').hidden=true;
}
function shopFinanceKind(){
 const kind=$('shop-finance-kind').value;
 if(kind==='fee')$('shop-finance-category').value='commission';
 else if(kind==='refund')$('shop-finance-category').value='refund';
 else if(['commission','refund'].includes($('shop-finance-category').value))$('shop-finance-category').value='server';
 $('shop-finance-category').disabled=kind!=='expense';
 $('shop-finance-order').required=kind==='refund';
}
$('shop-finance-kind').onchange=shopFinanceKind;
async function refreshShopFinance(){
 if(!canGroup('finance_read')){clearShopFinance();return;}
 const seq=++shopFinanceSeq,epoch=authEpoch;
 $('shop-finance-entry-form').hidden=!canGroup('finance_write');
 try{
  const query=new URLSearchParams({page:String(shopFinancePage)});
  const start=$('shop-finance-start').value,end=$('shop-finance-end').value;
  if(Boolean(start)!==Boolean(end))throw new Error('Укажите обе границы периода');
  if(start){const a=expenseDate(start),b=expenseDate(end);if(a===null||b===null||a>=b)throw new Error('Проверьте период');query.set('start',String(a));query.set('end',String(b));}
  const data=await api('shop-finance?'+query);
  if(seq!==shopFinanceSeq||epoch!==authEpoch||!canGroup('finance_read'))return;
  shopFinanceRows=data.entries;
  $('shop-finance-summary').innerHTML=['RUB','XTR'].map(currency=>{
    const t=data.totals[currency],label=currency==='RUB'?'Рубли':'Stars';
    return '<article class="admin-order"><h3>'+label+'</h3>'+[['Поступления',t.receipts],['Возвраты',t.refunds],['Комиссии',t.fees],['Расходы',t.expenses],['Разница учтённых сумм',t.net_recorded]].map(([name,n])=>'<p>'+name+': <b>'+esc(financeMoney(n,currency))+'</b></p>').join('')+'</article>';
  }).join('');
  $('shop-finance-coverage').textContent='Период '+dateTime(data.period.start)+' — '+dateTime(data.period.end)+'. Старых оплаченных заказов без даты подтверждения: '+data.missing_payment_timestamp+' (не включены в сумму периода).';
  $('shop-finance-methods').textContent=data.receipts_by_method.map(r=>r.method+': '+financeMoney(r.amount_minor,r.currency)+' ('+r.count+' заказов)').join(' · ');
  $('shop-finance-list').innerHTML=data.entries.map(e=>'<article class="admin-order"><h3>'+esc(financeLabels[e.kind]||e.kind)+' · '+esc(financeMoney(e.amount_minor,e.currency))+'</h3><p>'+esc(dateTime(e.occurred_at))+' · '+esc(e.reference)+' · '+(e.state==='void'?'Аннулирована в учёте':'Учтена')+'</p><p>'+esc(e.note)+'</p>'+(e.order_id?'<p>Заказ: '+esc(e.order_id)+'</p>':'')+(e.void_reason?'<p>Исправление: '+esc(e.void_reason)+'</p>':'')+(e.state==='recorded'&&canGroup('finance_write')?'<form data-finance-void="'+esc(e.id)+'"><label>Причина исправления<input name="note" maxlength="500" required></label><button type="submit" class="small secondary">Аннулировать запись</button></form>':'')+'</article>').join('')||'<p>Нет записей за период.</p>';
  $('shop-finance-page').textContent='Страница '+data.page+' · всего '+data.total;
  $('shop-finance-prev').disabled=data.page<=1;$('shop-finance-next').disabled=data.page*data.page_size>=data.total;
  $('shop-finance-status').textContent='';
 }catch(e){if(seq===shopFinanceSeq&&epoch===authEpoch){shopFinanceRows=[];for(const id of ['shop-finance-summary','shop-finance-coverage','shop-finance-methods','shop-finance-list'])$(id).replaceChildren();$('shop-finance-status').textContent=e.message;}}
}
$('shop-finance-filter').onsubmit=event=>{event.preventDefault();shopFinancePage=1;return refreshShopFinance();};
$('shop-finance-prev').onclick=()=>{if(shopFinancePage>1){shopFinancePage--;return refreshShopFinance();}};
$('shop-finance-next').onclick=()=>{if(!$('shop-finance-next').disabled){shopFinancePage++;return refreshShopFinance();}};
$('shop-finance-entry-form').onsubmit=async event=>{
 event.preventDefault();if(!canGroup('finance_write')||shopFinanceBusy)return;
 const kind=$('shop-finance-kind').value||'expense',currency=$('shop-finance-currency').value||'RUB';
 const amount=parseFinanceAmount($('shop-finance-amount').value,currency),occurred_at=expenseDate($('shop-finance-date').value);
 if(amount===null||occurred_at===null||!$('shop-finance-verified').checked){$('shop-finance-status').textContent='Проверьте сумму, дату и подтверждение сверки.';return;}
 const payload={kind,category:kind==='fee'?'commission':kind==='refund'?'refund':$('shop-finance-category').value,currency,amount_minor:amount,occurred_at,order_id:$('shop-finance-order').value.trim()||null,reference:$('shop-finance-reference').value.trim(),note:$('shop-finance-note').value.trim(),verified:true};
 const signature=JSON.stringify(payload);
 if(!shopFinanceIntent||shopFinanceIntent.signature!==signature)shopFinanceIntent={signature,id:crypto.randomUUID()};
 const intent=shopFinanceIntent,epoch=authEpoch;
 shopFinanceBusy=true;$('shop-finance-save').disabled=true;
 try{
  await api('shop-finance/entries',{method:'POST',data:{...payload,operation_id:intent.id}});
  if(epoch!==authEpoch||!canGroup('finance_write'))return;
  shopFinanceIntent=null;event.target.reset();shopFinanceKind();$('shop-finance-verified').checked=false;
  await refreshShopFinance();if(epoch===authEpoch)$('shop-finance-status').textContent='Операция учтена. Денежный перевод не выполнялся.';
 }catch(e){if(epoch===authEpoch)$('shop-finance-status').textContent=e.message+' При повторе без изменений используется прежний идентификатор.';}
 finally{if(epoch===authEpoch){shopFinanceBusy=false;$('shop-finance-save').disabled=false;}}
};
$('shop-finance-list').onsubmit=async event=>{
 event.preventDefault();if(!canGroup('finance_write')||shopFinanceBusy)return;
 const form=event.target.closest('[data-finance-void]');if(!form)return;
 const entry=shopFinanceRows.find(e=>e.id===form.dataset.financeVoid);if(!entry||entry.state!=='recorded')return;
 const note=String(new FormData(form).get('note')||'').trim();if(!note)return;
 if(!confirm('Аннулировать только ошибочную запись учёта? Деньги и подписка не изменятся.'))return;
 const sig=JSON.stringify([entry.version,note]);let intent=shopFinanceVoidIntents.get(entry.id);
 if(!intent||intent.sig!==sig){intent={sig,id:crypto.randomUUID()};shopFinanceVoidIntents.set(entry.id,intent);}
 const epoch=authEpoch;shopFinanceBusy=true;
 try{await api('shop-finance/entries/'+encodeURIComponent(entry.id)+'/void',{method:'POST',data:{operation_id:intent.id,version:entry.version,note}});if(epoch===authEpoch){shopFinanceVoidIntents.delete(entry.id);await refreshShopFinance();}}
 catch(e){if(epoch===authEpoch)$('shop-finance-status').textContent=e.message;}
 finally{if(epoch===authEpoch)shopFinanceBusy=false;}
};

// Распределение и наблюдение не редактируют секреты или привязки действующих подписок.
let routingSeq=0,routingState=null,routingBusy=false,routingHistorySeq=0;
const routingReasons={not_active:'Узел не активен',new_assignments_paused:'Новые назначения приостановлены',api_error:'Ошибка API',capacity:'Лимит заполнен',stale_check:'Нет свежей успешной проверки',reserve_disabled:'Резерв отключён политикой'};
function clearRoutingHistory(){
 routingHistorySeq++;$('routing-history-panel').hidden=true;
 $('routing-history-list').replaceChildren();$('routing-history-title').textContent='История проверок';
}
function clearRouting(){
 routingSeq++;routingState=null;routingBusy=false;clearRoutingHistory();
 $('routing-summary').textContent='';$('routing-status').textContent='';$('routing-nodes').replaceChildren();
 for(const id of ['routing-strategy','routing-max-age','routing-monitor-interval','routing-monitor-batch'])$(id).value='';
 for(const id of ['routing-reserve-enabled','routing-require-recent','routing-monitor-enabled'])$(id).checked=false;
 $('server-routing-policy').hidden=true;$('routing-policy-save').disabled=true;
}
function renderRouting(data){
 const p=data.policy;
 if(!Number.isSafeInteger(p?.version)||!Array.isArray(data.nodes)||!data.next_assignment)throw new Error('Не удалось прочитать правила');
 routingState=data;const edit=canGroup('server_routing');
 $('server-routing-policy').hidden=!edit;
 $('routing-strategy').value=p.strategy;$('routing-reserve-enabled').checked=p.reserve_enabled===true;
 $('routing-require-recent').checked=p.require_recent===true;$('routing-max-age').value=String(p.max_age_minutes);
 $('routing-monitor-enabled').checked=p.monitor_enabled===true;$('routing-monitor-interval').value=String(p.monitor_interval_minutes);
 $('routing-monitor-batch').value=String(p.monitor_batch);$('routing-policy-save').disabled=!edit;
 const names=new Map(data.nodes.map(n=>[n.id,n.name]));
 const choice=Object.entries(data.next_assignment).map(([proto,id])=>protocolLabel(proto)+': '+(id?names.get(id)||'Узел':'нет свободного узла')).join(' · ');
 $('routing-summary').textContent=(p.monitor_enabled?'Автомониторинг включён':'Автомониторинг выключен')+'. Следующее назначение по текущему снимку: '+choice+'.';
 $('routing-nodes').innerHTML=data.nodes.map(n=>{
  const load=n.capacity>0?Math.min(100,Math.round(n.occupied*100/n.capacity)):null;
  const last=n.last_probe?' · '+esc(n.last_probe.duration_ms)+' мс (API, '+(n.last_probe.source==='automatic'?'авто':'вручную')+')':'';
  const status=n.blocked_reason?routingReasons[n.blocked_reason]||n.blocked_reason:'Подходит для нового назначения';
  const settings=edit?'<form data-routing-node="'+esc(n.id)+'"><label>Приоритет (0–1000, выше — раньше)<input name="priority" type="number" min="0" max="1000" step="1" required value="'+esc(n.priority)+'"></label><label class="permission-choice"><input name="reserve" type="checkbox" '+(n.reserve?'checked':'')+'> Резервный узел</label><label class="permission-choice"><input name="accept_new" type="checkbox" '+(n.accept_new?'checked':'')+'> Разрешать новые привязки</label><button type="submit" class="small">Сохранить распределение</button></form>':'';
  return '<article class="admin-order"><h3>'+esc(n.name)+'</h3><p>'+esc(protocolLabel(n.protocol))+' · '+esc(n.location||'')+' · '+(n.reserve?'Резерв':'Основной')+'</p><p>Приоритет: '+esc(n.priority)+' · '+esc(status)+'</p><p>Слоты: '+esc(n.occupied)+' / '+esc(n.capacity||'без лимита')+(load===null?'':' · '+load+'%')+'</p>'+(load===null?'':'<progress max="100" value="'+load+'" aria-label="Занятость подписок"></progress>')+'<p>API: '+esc(n.health)+' · '+esc(dateTime(n.checked_at))+last+'</p>'+settings+'<button type="button" class="small secondary" data-routing-history="'+esc(n.id)+'">История API</button></article>';
 }).join('')||'<p>Серверы ещё не добавлены.</p>';
}
async function refreshRouting(){
 if(!canGroup('servers_read')){clearRouting();return;}
 const seq=++routingSeq,epoch=authEpoch;
 routingState=null;$('routing-policy-save').disabled=true;$('routing-nodes').replaceChildren();
 try{const data=await api('server-routing');if(seq===routingSeq&&epoch===authEpoch&&canGroup('servers_read')){renderRouting(data);$('routing-status').textContent='';}}
 catch(e){if(seq===routingSeq&&epoch===authEpoch){$('routing-summary').textContent='';$('routing-status').textContent=e.message;}}
}
$('routing-refresh').onclick=()=>{if(!routingBusy)return refreshRouting();};
async function changeRouting(path,data){
 if(routingBusy||!routingState||!canGroup('server_routing'))return;
 const epoch=authEpoch,seq=routingSeq;routingBusy=true;$('routing-policy-save').disabled=true;
 try{
  await api(path,{method:'PUT',data});
  if(epoch!==authEpoch||seq!==routingSeq||!canGroup('servers_read'))return;
  await refreshRouting();if(epoch===authEpoch&&canGroup('servers_read')&&routingState)$('routing-status').textContent='Правила сохранены.';
 }catch(e){if(epoch===authEpoch&&seq===routingSeq){routingState=null;$('routing-nodes').replaceChildren();$('routing-status').textContent=e.message+' Обновите список перед повтором.';}}
 finally{if(epoch===authEpoch)routingBusy=false;}
}
$('server-routing-policy').onsubmit=event=>{
 event.preventDefault();if(!routingState)return;
 return changeRouting('server-routing/policy',{version:routingState.policy.version,strategy:$('routing-strategy').value,
 reserve_enabled:$('routing-reserve-enabled').checked,require_recent:$('routing-require-recent').checked,
 max_age_minutes:Number($('routing-max-age').value),monitor_enabled:$('routing-monitor-enabled').checked,
 monitor_interval_minutes:Number($('routing-monitor-interval').value),monitor_batch:Number($('routing-monitor-batch').value)});
};
$('routing-nodes').onsubmit=event=>{
 event.preventDefault();if(!routingState||!canGroup('server_routing')||routingBusy)return;
 const form=event.target.closest('[data-routing-node]');if(!form)return;
 const row=routingState.nodes.find(n=>n.id===form.dataset.routingNode);if(!row)return;
 const values=new FormData(form);
 return changeRouting('server-routing/nodes/'+encodeURIComponent(row.id),{version:row.routing_version,
 server_version:row.version,priority:Number(values.get('priority')),reserve:values.has('reserve'),accept_new:values.has('accept_new')});
};
async function openRoutingHistory(id){
 if(!routingState||!canGroup('servers_read'))return;
 const node=routingState.nodes.find(n=>n.id===id);if(!node)return;
 clearRoutingHistory();const seq=routingHistorySeq,epoch=authEpoch;
 $('routing-history-panel').hidden=false;$('routing-history-title').textContent='История API · '+node.name;
 try{
  const data=await api('server-routing/history/'+encodeURIComponent(id));
  if(seq!==routingHistorySeq||epoch!==authEpoch||!canGroup('servers_read'))return;
  $('routing-history-list').innerHTML=data.checks.map(r=>'<p>'+esc(dateTime(r.checked_at))+' · '+(r.status==='ok'?'Доступен':'Ошибка')+' · '+esc(r.duration_ms)+' мс · '+(r.source==='automatic'?'Авто':'Вручную')+' · конфигурация v'+esc(r.server_version)+'</p>').join('')||'<p>Проверок пока нет.</p>';
 }catch(e){if(seq===routingHistorySeq&&epoch===authEpoch)$('routing-history-list').textContent=e.message;}
}
$('routing-nodes').onclick=event=>{
 const button=event.target.closest('[data-routing-history]');if(button)return openRoutingHistory(button.dataset.routingHistory);
};
$('routing-history-close').onclick=clearRoutingHistory;

// Интеграционные ключи и запуск доверенной авторизации — только разработчик.
let idPolicySeq=0,idPolicyVersion=null,idPolicyBusy=false;
function clearIdPolicy(){
 idPolicySeq++;idPolicyVersion=null;idPolicyBusy=false;
 $('id-policy-enabled').checked=false;$('id-policy-registration').checked=false;
 $('id-policy-readiness').textContent='';$('id-policy-status').textContent='';$('id-policy-save').disabled=true;
}
function renderIdPolicy(data){
 if(!Number.isSafeInteger(data.version)||data.version<0)throw new Error('Не удалось прочитать версию');
 idPolicyVersion=data.version;
 $('id-policy-enabled').checked=data.enabled===true;$('id-policy-registration').checked=data.allow_new_accounts===true;
 $('id-policy-readiness').textContent='Локальная конфигурация: '+(data.configured?'настроена':'не настроена')+' · Связано: '+data.linked_accounts+
 ' · Ожидающие запросы: '+data.pending_requests+' · Сеансы: '+data.active_id_sessions+
 ' · Срок Shop-сеанса: '+Math.floor(data.session_ttl/60)+' мин. · Общий кошелёк не подключён. Центральный issuer требует отдельной проверки.'+(data.reapproval_required?' Конфигурация изменилась. Сохраните настройки после проверки новой пары ключей; прежние ID-сеансы будут отозваны.':'');
 $('id-policy-save').disabled=false;
}
async function refreshIdPolicy(){
 if(role!=='owner'){clearIdPolicy();return;}
 if(idPolicyBusy)return;const seq=++idPolicySeq,epoch=authEpoch;idPolicyVersion=null;$('id-policy-save').disabled=true;
 try{const data=await api('pushok-id');if(seq===idPolicySeq&&epoch===authEpoch&&role==='owner'){renderIdPolicy(data);$('id-policy-status').textContent='';}}
 catch(e){if(seq===idPolicySeq&&epoch===authEpoch)$('id-policy-status').textContent=e.message;}
}
$('id-policy-refresh').onclick=refreshIdPolicy;
$('id-policy-form').onsubmit=async event=>{
 event.preventDefault();if(role!=='owner'||idPolicyBusy||idPolicyVersion===null)return;
 const data={version:idPolicyVersion,enabled:$('id-policy-enabled').checked,allow_new_accounts:$('id-policy-registration').checked};
 const seq=++idPolicySeq,epoch=authEpoch;idPolicyBusy=true;idPolicyVersion=null;$('id-policy-save').disabled=true;
 try{const result=await api('pushok-id',{method:'PUT',data});if(seq===idPolicySeq&&epoch===authEpoch&&role==='owner'){renderIdPolicy(result);$('id-policy-status').textContent='Настройки сохранены.';}}
 catch(e){if(seq===idPolicySeq&&epoch===authEpoch)$('id-policy-status').textContent=e.message+' Обновите раздел перед повтором.';}
 finally{if(seq===idPolicySeq&&epoch===authEpoch)idPolicyBusy=false;}
};

// Настройки меню бот получает из базы при отправке очередного /start.
let botMenuVersion=0,botMenuButtons=[],botMenuRequest=0;
function menuFields(){
 return botMenuButtons.map(b=>({
  id:b.id,
  text:$('bot-menu-text-'+b.id).value,
  enabled:$('bot-menu-enabled-'+b.id).checked,
  row:Number($('bot-menu-row-'+b.id).value),
  url:(b.id.startsWith('link')?$('bot-menu-url-'+b.id).value:'')
 }));
}
function menuPreview(buttons){
 const rows=new Map();
 for(const b of buttons){
  if(!b.enabled)continue;
  if(!rows.has(b.row))rows.set(b.row,[]);
  rows.get(b.row).push(b.text);
 }
 $('bot-menu-preview').innerHTML=[...rows].sort((a,b)=>a[0]-b[0])
   .map(([,texts])=>'<div class="bot-menu-preview-row">'+texts.map(t=>'<span class="bot-menu-preview-button">'+esc(t)+'</span>').join('')+'</div>').join('') || '<p>Кнопки скрыты.</p>';
}
function renderBotMenu(){
 const titles={buy:'Покупка / продление',account:'Браузерный кабинет',support:'Поддержка',terms:'Условия продажи',link1:'Дополнительная ссылка № 1',link2:'Дополнительная ссылка № 2'};
 $('bot-menu-rows').innerHTML=botMenuButtons.map(b=>'<div class="bot-menu-editor-row"><h3>'+esc(titles[b.id]||b.id)+'</h3>'+
  '<label>Название кнопки<input id="bot-menu-text-'+esc(b.id)+'" maxlength="60" value="'+esc(b.text)+'" required></label>'+
  '<label><input id="bot-menu-enabled-'+esc(b.id)+'" type="checkbox" '+(b.enabled?'checked ':'')+(b.id==='buy'?'disabled ':'')+'> Показывать кнопку'+(b.id==='buy'?' (обязательная)':'')+'</label>'+
  '<label>Номер строки<select id="bot-menu-row-'+esc(b.id)+'">'+[1,2,3,4,5,6].map(n=>'<option value="'+n+'" '+(n===b.row?'selected':'')+'>'+n+'</option>').join('')+'</select></label>'+
  (b.id.startsWith('link')?'<label>Публичная HTTPS-ссылка<input type="url" id="bot-menu-url-'+esc(b.id)+'" maxlength="512" placeholder="https://t.me/..." value="'+esc(b.url||'')+'"></label>':'')+
  '</div>').join('');
 menuPreview(botMenuButtons);
}
async function refreshBotMenu(){
 const section=$('bot-menu-panel');
 if(!canGroup('bot_menu')){section.hidden=true;return}
 section.hidden=false;
 const epoch=authEpoch,request=++botMenuRequest;
 try{
  const data=await api('bot-menu');
  if(epoch!==authEpoch||request!==botMenuRequest)return;
  botMenuVersion=data.version;botMenuButtons=data.buttons;renderBotMenu();
  $('bot-menu-status').textContent='Меню загружено. Версия '+data.version+'.';
 }catch(error){if(epoch===authEpoch&&request===botMenuRequest)$('bot-menu-status').textContent=error.message}
}
$('bot-menu-rows').oninput=()=>menuPreview(menuFields());
$('bot-menu-rows').onchange=()=>menuPreview(menuFields());
$('bot-menu-form').onsubmit=async event=>{
 event.preventDefault();if(!canGroup('bot_menu'))return;
 const button=$('bot-menu-save'),epoch=authEpoch;
 button.disabled=true;
 try{
  const data=await api('bot-menu',{method:'PUT',data:{buttons:menuFields(),version:botMenuVersion}});
  if(epoch!==authEpoch)return;
  botMenuVersion=data.version;botMenuButtons=data.buttons;renderBotMenu();
  $('bot-menu-status').textContent='Меню сохранено. Для проверки откройте /start в Telegram.';
 }catch(error){if(epoch===authEpoch)$('bot-menu-status').textContent=error.message}
 finally{if(epoch===authEpoch)button.disabled=false}
};
async function refreshStaff(){
 const section=$('staff-management'),epoch=authEpoch;
 if(role!=='owner'){section.hidden=true;return}section.hidden=false;
 try{
  const [data,audit]=await Promise.all([api('staff'),api('audit?limit=50')]);
  if(epoch!==authEpoch||role!=='owner')return;
  const developers=data.staff.filter(x=>x.enabled&&x.role==='owner').length;
  $('staff-list').innerHTML=data.staff.map(s=>{
    const protectedRole=s.id===staffActorId||(s.role==='owner'&&s.enabled&&developers<=1);
    const select=Object.entries(STAFF_LABELS).map(([key,label])=>'<option value="'+key+'" '+(s.role===key?'selected':'')+'>'+esc(label)+'</option>').join('');
    return '<article class="admin-order"><b>'+esc(s.email)+'</b><p class="staff-role staff-role-'+esc(s.role)+'">'+esc(s.role_label)+' · '+(s.enabled?'активен':'отключён')+'</p><p>Последний вход: '+(s.last_login?esc(dateTime(s.last_login)):'—')+'</p>'+
      (DELEGATED_ROLES.includes(s.role)?'<button type="button" class="small secondary" data-staff-permissions="'+s.id+'">Индивидуальные права</button>':'')+
      (protectedRole?'<p>Защищённая учётная запись разработчика.</p>':'<form data-staff-role="'+s.id+'"><label>Роль<select name="role">'+select+'</select></label><button type="submit" class="small">Изменить роль</button></form><button class="small secondary" data-staff-id="'+s.id+'" data-enabled="'+(!s.enabled)+'">'+(s.enabled?'Отключить':'Включить')+'</button>')+'</article>';
  }).join('');
  $('audit-list').innerHTML=audit.events.map(e=>'<article class="admin-order"><b>'+esc(e.actor_email)+' · '+esc(STAFF_LABELS[e.role]||e.role)+'</b><p>'+esc(e.action)+' '+esc(e.resource)+' · HTTP '+e.status+' · '+esc(dateTime(e.created))+'</p></article>').join('')||'<p>Записей пока нет.</p>';
 }catch(error){if(epoch===authEpoch)$('staff-error').textContent=error.message}
}
$('staff-create').onsubmit=async event=>{
 event.preventDefault();if(role!=='owner')return;
 const form=event.target,values=new FormData(form),epoch=authEpoch;
 try{await api('staff',{method:'POST',data:{email:values.get('email'),password:values.get('password'),role:values.get('role')}});if(epoch!==authEpoch)return;form.reset();$('staff-error').textContent='Сотрудник добавлен.';await refreshStaff()}
 catch(error){if(epoch===authEpoch)$('staff-error').textContent=error.message}
};
$('staff-list').onclick=async event=>{
 const edit=event.target.closest('[data-staff-permissions]');
 if(edit&&role==='owner')return openStaffIndividual(Number(edit.dataset.staffPermissions));
 const button=event.target.closest('[data-staff-id]');if(!button||role!=='owner')return;
 const epoch=authEpoch;
 try{await api('staff/'+encodeURIComponent(button.dataset.staffId),{method:'PATCH',data:{enabled:button.dataset.enabled==='true'}});if(epoch===authEpoch)await refreshStaff()}
 catch(error){if(epoch===authEpoch)$('staff-error').textContent=error.message}
};
$('staff-list').onsubmit=async event=>{
 event.preventDefault();const form=event.target.closest('[data-staff-role]');if(!form||role!=='owner')return;
 const next=new FormData(form).get('role'),epoch=authEpoch,button=form.querySelector('button');
 if(!Object.hasOwn(STAFF_LABELS,next)||button.disabled)return;
 if(next==='owner'&&!confirm('Назначить разработчика с полным доступом к магазину?'))return;
 button.disabled=true;
 try{await api('staff/'+encodeURIComponent(form.dataset.staffRole),{method:'PATCH',data:{role:next}});if(epoch===authEpoch){clearStaffIndividual();await refreshStaff();$('staff-error').textContent='Роль изменена. Сеансы прежней роли отозваны.';}}
 catch(error){if(epoch===authEpoch)$('staff-error').textContent=error.message}finally{button.disabled=false;}
};

let individualState=null,individualSeq=0,individualBusy=false;
function clearStaffIndividual(){
 individualSeq++;individualState=null;individualBusy=false;
 $('staff-individual-editor').hidden=true;$('staff-individual-form').replaceChildren();$('staff-individual-status').textContent='';
}
function renderStaffIndividual(data){
 if(!DELEGATED_ROLES.includes(data.role)||!Number.isInteger(data.version)||!Number.isInteger(data.role_version)||!Array.isArray(data.allowed)||!Array.isArray(data.permissions)||!Array.isArray(data.effective))throw new Error('Не удалось прочитать полномочия');
 individualState=data;
 $('staff-individual-editor').hidden=false;
 const selected=data.inherit?data.allowed:data.permissions;
 $('staff-individual-form').innerHTML='<form id="individual-policy-form"><p>Сотрудник № '+esc(data.staff_id)+' · '+esc(STAFF_LABELS[data.role])+'</p>'+
   '<label class="permission-choice"><input id="individual-inherit" type="checkbox" '+(data.inherit?'checked':'')+'> Наследовать права роли</label>'+
   '<div class="permission-grid" id="individual-choices">'+data.allowed.map(key=>'<label class="permission-choice"><input name="permissions" type="checkbox" value="'+esc(key)+'" '+(selected.includes(key)?'checked':'')+' '+(data.inherit?'disabled':'')+'>'+esc(data.labels[key]||key)+'</label>').join('')+'</div><p>Действующие права: '+data.effective.map(key=>esc(data.labels[key]||key)).join(', ')+'</p><button class="small" type="submit">Сохранить индивидуальные права</button></form>';
}
async function openStaffIndividual(id){
 if(role!=='owner'||!Number.isSafeInteger(id)||id<1)return;
 clearStaffIndividual();const seq=individualSeq,epoch=authEpoch;
 $('staff-individual-editor').hidden=false;$('staff-individual-status').textContent='Загрузка…';
 try{const data=await api('staff/'+id+'/permissions');if(seq!==individualSeq||epoch!==authEpoch||role!=='owner')return;renderStaffIndividual(data);$('staff-individual-status').textContent='';}
 catch(e){if(seq===individualSeq&&epoch===authEpoch)$('staff-individual-status').textContent=e.message;}
}
$('staff-individual-form').onchange=event=>{
 if(event.target.id!=='individual-inherit')return;
 for(const input of $('individual-choices').querySelectorAll('input'))input.disabled=event.target.checked;
};
$('staff-individual-form').onsubmit=async event=>{
 event.preventDefault();if(role!=='owner'||!individualState||individualBusy)return;
 const form=event.target.closest('#individual-policy-form');if(!form)return;
 const before=individualState,seq=individualSeq,epoch=authEpoch;
 const inherit=$('individual-inherit').checked,values=new FormData(form);
 const data={version:before.version,role_version:before.role_version,role:before.role,inherit,permissions:inherit?[]:values.getAll('permissions')};
 individualBusy=true;const button=form.querySelector('button');button.disabled=true;
 try{
  const result=await api('staff/'+before.staff_id+'/permissions',{method:'PUT',data});
  if(seq===individualSeq&&epoch===authEpoch&&role==='owner'){renderStaffIndividual(result);$('staff-individual-status').textContent='Сохранено.';}
 }catch(e){if(seq===individualSeq&&epoch===authEpoch){$('staff-individual-status').textContent=e.message+' Откройте карточку заново.';individualState=null;}}
 finally{if(seq===individualSeq&&epoch===authEpoch)individualBusy=false;}
};
$('staff-individual-close').onclick=clearStaffIndividual;

let selectedAdminCase=null,adminCaseVersion=0;
async function refreshSupportAdmin(){
 const panel=$('support-admin');if(!canGroup('support')){panel.hidden=true;return}panel.hidden=false;
 try{
  const q=$('support-admin-filter').value,listing=await api('support/tickets?page=1'+(q?'&status='+encodeURIComponent(q):''));
  $('support-admin-list').innerHTML=listing.cases.map(c=>'<article class="admin-order"><b>'+esc(c.subject)+'</b><p>'+esc(c.status)+' · клиент № '+c.user_id+(c.order_id?' · заказ '+esc(c.order_id):'')+'</p><p>'+esc(c.last_message||'')+'</p><button class="small secondary" data-admin-case="'+esc(c.id)+'">Открыть</button></article>').join('')||'<p>Обращений нет.</p>';
  $('support-admin-error').textContent='';
 }catch(error){$('support-admin-error').textContent=error.message}
}
async function openAdminCase(id){
 selectedAdminCase=id;const c=await api('support/tickets/'+encodeURIComponent(id));adminCaseVersion=c.version;
 $('support-admin-thread').innerHTML='<article class="admin-order"><h3>'+esc(c.subject)+' · '+esc(c.status)+'</h3>'+c.messages.map(m=>'<p><b>'+esc(m.author)+' · '+esc(dateTime(m.created))+'</b><br>'+esc(m.body)+'</p>').join('')+'</article>';
 $('support-admin-reply').hidden=false;
}
$('support-admin-list').onclick=event=>{const b=event.target.closest('[data-admin-case]');if(b)openAdminCase(b.dataset.adminCase).catch(e=>$('support-admin-error').textContent=e.message)};
$('support-admin-refresh').onclick=()=>refreshSupportAdmin();
$('support-admin-filter').onchange=()=>refreshSupportAdmin();
$('support-admin-reply').onsubmit=async event=>{
 event.preventDefault();if(!selectedAdminCase)return;const form=event.target,body=new FormData(form).get('body'),button=form.querySelector('button[type=submit]');if(!body.trim()){$('support-admin-error').textContent='Введите ответ.';return}button.disabled=true;
 try{await api('support/tickets/'+selectedAdminCase+'/messages',{body,version:adminCaseVersion});form.reset();await openAdminCase(selectedAdminCase);await refreshSupportAdmin();$('support-admin-error').textContent='Ответ отправлен.'}
 catch(error){$('support-admin-error').textContent=error.message}finally{button.disabled=false}
};
$('support-admin-close').onclick=async()=>{
 if(!selectedAdminCase)return;
 try{await api('support/tickets/'+selectedAdminCase,{status:'closed',version:adminCaseVersion},'PATCH');$('support-admin-reply').hidden=true;$('support-admin-thread').replaceChildren();await refreshSupportAdmin()}
 catch(error){$('support-admin-error').textContent=error.message}
};

// Счётчики событий относятся к времени записи, а не к когорте оформленных заказов.
function commerceEventsMarkup(result){
 const events=result.events;
 if(!events||events.source!=='first_observed_transition'||
    !Number.isSafeInteger(events.recording_started_at)||events.recording_started_at<0||
    !Number.isSafeInteger(events.paid)||events.paid<0||
    !Number.isSafeInteger(events.issued)||events.issued<0){
  return '<h3>События за период</h3><p>История событий недоступна. Обновите отчёт позже.</p>';
 }
 const since=events.recording_started_at;
 const coverage=result.period.end<=since?'Выбранный период целиком предшествует началу истории. Нулевые счётчики не означают отсутствие прежних оплат и выдач.':
  result.period.start<since?'История покрывает только часть выбранного периода. События до начала записи неизвестны.':
  'Выбранный период находится в пределах записываемой истории.';
 return '<h3>События за период</h3><div class="stats"><div>Подтверждения оплаты<strong>'+esc(events.paid)+
  '</strong></div><div>Выдачи доступа<strong>'+esc(events.issued)+
  '</strong></div></div><p>История записывается с '+esc(bookingTime(since))+'. '+esc(coverage)+
  '</p><p>Счётчики отражают первые события коммерческих заказов по времени записи магазином. Они могут относиться к разным заказам, поэтому их отношение не является конверсией. Старые события не восстанавливаются по статусам. Время записи может отличаться от времени банковского платежа.</p>';
}
async function refreshCommerceAnalytics(){
 const section=$('commerce-analytics');
 if(!canGroup('analytics')){section.hidden=true;return}
 section.hidden=false;
 const epoch=authEpoch,request=++commerceAnalyticsRequest;
 const start=$('commerce-analytics-start').value,end=$('commerce-analytics-end').value;
 $('commerce-analytics-error').textContent='';
 try{
  if(Boolean(start)!==Boolean(end))throw new Error('Укажите обе границы периода');
  const query=new URLSearchParams();
  if(start){const a=expenseDate(start),b=expenseDate(end);if(a===null||b===null||a>=b)throw new Error('Проверьте границы периода');query.set('start',String(a));query.set('end',String(b));}
  const suffix=query.toString()?('?'+query.toString()):'';
  const result=await api('commerce-analytics'+suffix);
  if(epoch!==authEpoch||request!==commerceAnalyticsRequest)return;
  const percent=value=>value===null?'—':String(value)+'%';
  const cards=[
   ['Регистрации',result.accounts_registered],
   ['Коммерческие заказы',result.orders.created],
   ['Оплата подтверждена',result.orders.paid+' · '+percent(result.orders.payment_rate_percent)],
   ['Доступ выдан',result.orders.issued+' · '+percent(result.orders.issuance_rate_percent)],
   ['Повторные покупки',result.orders.renewals],
   ['Пробный → платный',result.trials.converted_to_paid+' из '+result.trials.issued+' · '+percent(result.trials.conversion_rate_percent)]
  ];
  $('commerce-analytics-cards').innerHTML=cards.map(([label,value])=>'<div>'+esc(label)+'<strong>'+esc(value)+'</strong></div>').join('');
  const currencies=result.orders.confirmed_amount_by_currency;
  const currencyText=Object.entries(currencies).map(([key,value])=>esc(key==='RUB'?value+' ₽':key==='XTR'?value+' ★':value+' '+key)).join(' · ')||'нет';
  const statuses=Object.entries(result.orders.by_status).map(([key,value])=>esc(key)+': '+value).join(' · ')||'нет';
  $('commerce-analytics-details').innerHTML='<p>Пробные заявки: '+result.trials.claimed+' · выдано: '+result.trials.issued+'. Заказы по статусам: '+statuses+'.</p><p>Сумма заказов с подтверждённой оплатой по валютам: '+currencyText+'. Это не банковская сверка и не прибыль.</p><p>Период: '+esc(bookingTime(result.period.start))+' — '+esc(bookingTime(result.period.end))+' (конец не включается). Статусы отражают момент отчёта.</p>'+commerceEventsMarkup(result);
 }catch(error){
  if(epoch!==authEpoch||request!==commerceAnalyticsRequest)return;
  $('commerce-analytics-cards').replaceChildren();$('commerce-analytics-details').replaceChildren();$('commerce-analytics-error').textContent=error.message;
 }
}
$('commerce-analytics-period').onsubmit=event=>{event.preventDefault();return refreshCommerceAnalytics();};

async function refresh({plans = role==='owner' || canGroup('catalog')} = {}) {
  if(delegatedRole())return refreshDelegated({plans});
  const data = role==='mtproxy_manager' ? {users:'—',active:'—',revenue:'—',stars:'—'} : await api('overview');
  const catalog = plans ? await api('plans') : null;
  $('stats').innerHTML = `<div>Пользователи<strong>${data.users}</strong></div><div>Подписки<strong>${data.active}</strong></div><div>Подтверждено<strong>${data.revenue} ₽ / ${data.stars} ★</strong></div>`;
  if (catalog) $('plan-editors').innerHTML = catalog.plans.map(planForm).join('');
  await refreshProtocols();
  $('flow-auto-section').hidden=role!=='owner';
  if(role==='owner') await Promise.all([refreshAdsAdmin(),refreshDiagnostics(),refreshOrders(),refreshUsers(),refreshServers(),refreshTrialPolicy(),refreshFlowAutoPolicy(),refreshCoupons(),refreshReferralPolicy(),refreshCommerceAnalytics()]);
  else if(role==='mtproxy_manager') await refreshAdsAdmin();
  else if(role==='finance') await Promise.all([refreshAdsAdmin(),refreshOrders(),refreshCommerceAnalytics()]);
  else if(role==='panel_operator') await Promise.all([refreshDiagnostics(),refreshOrders(),refreshUsers(),refreshServers(),refreshRouting()]);
  else if(role==='support') await Promise.all([refreshDiagnostics(),refreshOrders(),refreshUsers(),refreshSupportAdmin()]);
  if(role==='owner'){await Promise.all([refreshStaff(),refreshSupportAdmin(),refreshShopSettings(),refreshBotMenu(),refreshUpdates(),refreshRolePolicies(),refreshModeration(),refreshRuntime('appearance'),refreshRuntime('sales'),refreshRuntime('notifications'),refreshPaymentPolicy(),refreshShopFinance(),refreshRouting(),refreshIdPolicy()]);}
  renderManagementNav();
}
// Never store a staff password or session token in browser storage.
async function showStaffSession(identity, epoch) {
  if(epoch!==authEpoch)return;
  acceptStaffRole(identity);
  token='';
  $('key').value=''; $('staff-password').value='';
  $('admin-login').hidden=true; $('dashboard').hidden=false;
  $('error').textContent='';
  try { await refresh(); }
  catch(error) { if(epoch===authEpoch)$('error').textContent='Не удалось загрузить разделы. Нажмите «Обновить»: '+error.message; }
}
async function restoreStaffSession() {
  const epoch=++authEpoch;
  $('admin-login').hidden=true;
  try {
    const response=await fetch(basePath+'/api/admin/auth/me',{
      credentials:'same-origin',cache:'no-store'
    });
    if(epoch!==authEpoch)return;
    if(response.status===401) {
      $('admin-login').hidden=false;
      return;
    }
    if(!response.ok)throw new Error('Проверка сессии временно недоступна');
    const identity=await response.json();
    if(epoch!==authEpoch)return;
    await showStaffSession(identity,epoch);
  } catch(error) {
    if(epoch!==authEpoch)return;
    role='';token='';staffActorId=null;delegatedPermissions.clear();
    $('dashboard').hidden=true;$('admin-login').hidden=false;
    $('error').textContent=error.message;
  }
}
$('admin-login').onsubmit = async event => {
  event.preventDefault(); const button=event.target.querySelector('button'); button.disabled=true; authEpoch++;
  const credentials={email:$('staff-email').value,password:$('staff-password').value};
  try {
    let response=await fetch(basePath+'/api/admin/auth/login',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json'},body:JSON.stringify(credentials)});
    let result=await response.json();
    if(!response.ok&&$('key').value){
      response=await fetch(basePath+'/api/admin/auth/bootstrap',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json',Authorization:'Bearer '+$('key').value},body:JSON.stringify(credentials)});
      result=await response.json();
    }
    if(!response.ok)throw new Error(typeof result.detail==='string'?result.detail:'Не удалось войти');
    await showStaffSession(result,authEpoch);
  } catch(error){authEpoch++;token='';$('error').textContent=error.message}
  finally{button.disabled=false}
};
$('reload').onclick = () => refresh().catch(error => $('error').textContent = error.message);
$('exit').onclick = () => { void fetch(basePath+'/api/admin/auth/logout',{method:'POST',credentials:'same-origin'}).catch(()=>{}); role='';
  flowExtensionAttempts.clear();
  clearUpdates(); clearManagement(); clearRuntime(); clearStaffIndividual(); clearPaymentPolicy(); clearShopFinance(); clearRouting(); clearIdPolicy();
  protocolsRequest++; protocolCache = []; $('protocols-list').replaceChildren();
  $('protocols-status').textContent = ''; $('protocols-filter').value = 'all';
  $('protocols-panel').hidden = true; $('protocols-refresh').disabled = false;
  adsFinanceRequest++;adsFinancePage=1;adsExpenseIntents.clear();commerceAnalyticsRequest++;
  for(const id of ['commerce-analytics-cards','commerce-analytics-details','commerce-analytics-error'])$(id).replaceChildren();
  for(const id of ['commerce-analytics-start','commerce-analytics-end'])$(id).value='';$('commerce-analytics').hidden=true;
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
  botMenuRequest++;botMenuVersion=0;botMenuButtons=[];
  $('bot-menu-rows').replaceChildren();$('bot-menu-preview').replaceChildren();$('bot-menu-status').textContent='';$('bot-menu-panel').hidden=true;
  authEpoch++; token = ''; usersRequest++; usersPage = 1; usersQuery = '';
  ordersRequest++; ordersPage = 1; ordersFilters = {q:'', status:'', method:'', protocol:''};
  serverCache = []; editingServer = null; $('server-list').replaceChildren(); $('servers-notice').textContent = '';
  if ($('server-dialog').open) $('server-dialog').close();
  if ($('server-credentials-dialog').open) $('server-credentials-dialog').close();
  $('server-form').reset();
  $('orders-search').reset(); $('orders-state').textContent = ''; $('orders-page').textContent = '';
  $('orders-prev').disabled = true; $('orders-next').disabled = true;
  $('users-query').value = ''; $('users-list').replaceChildren(); $('users-page').textContent = ''; $('users-state').textContent = '';
  if ($('user-dialog').open) $('user-dialog').close();
  clearUser();
  $('referral-policy-editor').replaceChildren();$('referral-policy-count').textContent='';$('referral-policy-error').textContent='';
  $('coupons-list').replaceChildren();$('coupons-error').textContent='';$('coupon-create').reset();
  $('trial-policy-editor').replaceChildren();$('trial-policy-count').textContent='';$('trial-policy-error').textContent='';
  $('flow-auto-section').hidden=true;$('flow-auto-editor').replaceChildren();$('flow-auto-count').textContent='';$('flow-auto-error').textContent='';
  $('list').replaceChildren(); $('stats').replaceChildren(); $('plan-editors').replaceChildren();
  $('admin-notice').textContent = ''; $('error').textContent = ''; $('dashboard').hidden = true; $('admin-login').hidden = false;
};
$('plan-editors').onclick = async event => {
  const button=event.target.closest('[data-plan-delete]');
  if(!button || role!=='owner')return;
  const id=button.dataset.planDelete;
  if(!id || !confirm('Удалить неиспользуемый тариф '+id+'?'))return;
  if(prompt('Для удаления тарифа введите УДАЛИТЬ','')!=='УДАЛИТЬ')return;
  button.disabled=true;
  const epoch=authEpoch;
  try{
    await api('plans/'+encodeURIComponent(id),{method:'DELETE'});
    if(epoch===authEpoch){
      button.closest('[data-plan-id]')?.remove();
      $('admin-notice').textContent='Тариф '+id+' удалён. Оплаченные заказы не затронуты.';
    }
  }catch(error){
    if(epoch===authEpoch)$('admin-notice').textContent=error.message;
    button.disabled=false;
  }
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
      stars: Number(values.get('stars')), device_limit:Number(values.get('device_limit') ?? 4), free_activation_limit:Number(values.get('free_activation_limit') ?? 0), free_traffic_gb:Number(values.get('free_traffic_gb') ?? 0), free_auto_renew:values.has('free_auto_renew'), free_max_renewals:Number(values.get('free_max_renewals') ?? 0), enabled: values.has('enabled'), version: Number(form.dataset.version),
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
      price: Number(values.get('price')), stars: Number(values.get('stars')), device_limit:Number(values.get('device_limit') ?? 4), free_activation_limit:Number(values.get('free_activation_limit') ?? 0), free_traffic_gb:Number(values.get('free_traffic_gb') ?? 0), free_auto_renew:values.has('free_auto_renew'), free_max_renewals:Number(values.get('free_max_renewals') ?? 0), enabled: values.has('enabled'),
    }});
    $('plan-editors').insertAdjacentHTML('beforeend', planForm(saved));
    $('create-plan-dialog').close();
    $('error').textContent = '';
    $('admin-notice').textContent = saved.enabled ? 'Тариф создан и доступен покупателям.' : 'Тариф создан и пока скрыт. Включите его после проверки условий.';
  } catch (error) { $('create-plan-error').textContent = error.message; }
  finally { button.disabled = false; }
};

const orderStatus = {pending:'Ожидает оплаты', checkout:'Оплата не завершена', paid:'Готовим доступ', active:'Доступ выдан', canceled:'Отменён'};
const protocolLabel = value => ({wg:'WireGuard',xui:'VLESS',vmess:'VMess AEAD',hysteria2:'Hysteria2',tuic:'TUIC v5'})[value] || value;
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
    $('user-details').innerHTML = `<p>Email: ${esc(user.email || '—')}</p><p>Telegram ID: ${esc(user.telegram_id ?? '—')}</p><p>Регистрация: ${esc(dateTime(user.created))}</p>` + (role==='owner' ? '<div class="record-actions"><button type="button" class="secondary" data-flow-account-check="1">Проверить аккаунт Flow</button></div><div class="record-note" id="flow-binding-status" role="status" aria-live="polite"></div>' : '');
    $('user-subscriptions').innerHTML = data.subscriptions.length ? data.subscriptions.map(sub => {
      const status = sub.access_active ? 'Активна' : sub.state === 'pending' ? 'Готовится'
        : sub.state === 'revoked' ? 'Доступ отозван' : 'Неактивна';
      const tools = role==='owner'
        ? `<div class="record-actions">
          ${sub.protocol==='wg' ? '<button type="button" class="secondary" data-peer-check="'+esc(sub.id)+'">Проверить пир</button>' : ''}
          ${sub.protocol==='wg' && ['active','expired'].includes(sub.state) ? '<button type="button" class="secondary" data-wg-extend="'+esc(sub.id)+'">Продлить WireGuard</button>' : ''}
          ${sub.state!=='revoked' ? '<button type="button" class="danger-action" data-subscription-revoke="'+esc(sub.id)+'">Отозвать доступ</button>' : ''}
          ${sub.state!=='revoked' ? '<button type="button" class="danger-action" data-subscription-remove="'+esc(sub.id)+'">Удалить подписку</button>' : ''}
          ${sub.state==='revoked' && sub.can_delete ? '<button type="button" class="danger-action" data-subscription-delete="'+esc(sub.id)+'">Удалить запись</button>' : ''}
          </div>
          ${sub.protocol==='wg' ? '<div class="record-note" role="status" aria-live="polite" data-peer-status="'+esc(sub.id)+'"></div>' : ''}
          ${sub.state==='revoked' && !sub.can_delete ? '<p class="record-note">'+esc(sub.delete_blocker||'Запись хранится для истории')+'</p>' : ''}
          <p class="record-note">Сначала отзываются пиры на сервере. Оплаченные заказы и финансовая история сохраняются.</p>`
        : '';
      return `<article class="subscription"><b>${esc(protocolLabel(sub.protocol))}</b>
        <p>${esc(status)} · Срок: ${esc(dateTime(sub.expires))}</p>
        ${sub.protocol==='wg' ? '<p>Доступ закреплён за аккаунтом Pushok — без привязки к телефону.</p>'
          : '<p>Дополнительных профилей: '+(sub.device_limit ?? 4)+'</p>'}
        ${tools}</article>`;
    }).join('') : '<p>Подписок пока нет.</p>';
    $('user-orders').innerHTML = data.orders.length ? data.orders.map(order => `<article class="admin-order"><b>${esc(order.plan_name || order.plan)} · ${order.days} дней · ${order.method==='referral'?'Бонус · '+order.days+' дней':order.amount+' '+(order.currency === 'XTR' ? '★' : '₽')}</b><p>${esc(protocolLabel(order.protocol))} · ${esc(orderStatus[order.status] || order.status)}</p><p>Заказ: ${esc(order.id)}</p>${order.promo_code?`<p>Код ${esc(order.promo_code)} · скидка ${order.discount} ${order.currency==='XTR'?'★':'₽'} от ${order.base_amount}</p>`:''}<p>${esc(dateTime(order.created))} · ${esc(order.method==='referral'?'Бонусное продление':order.method==='trial'?'Пробный доступ':order.method)}</p>${order.error ? `<p>Ошибка выдачи: ${esc(order.error)} · Попыток: ${order.attempts}</p>` : ''}</article>`).join('') : '<p>Заказов пока нет.</p>';
    const pages = Math.max(1, Math.ceil(data.orders_total / data.orders_page_size));
    $('user-orders-page').textContent = 'Заказов: ' + data.orders_total + ' · Страница ' + profilePage + ' из ' + pages;
    $('user-orders-prev').disabled = profilePage <= 1;
    $('user-orders-next').disabled = profilePage * data.orders_page_size >= data.orders_total;
    $('user-error').textContent = '';
  } catch (error) { if (request === profileRequest && selectedUser === userId) $('user-error').textContent = error.message; }
}
// Account identity is checked from the signed MTProxy proof, not a phone.
// This read-only admin view deliberately exposes no device hashes or keys.
$('user-details').onclick = async event => {
  const check=event.target.closest('[data-flow-account-check]');
  if (!check || role!=='owner' || selectedUser===null) return;
  const panel=$('flow-binding-status');
  if (!panel) return;
  const uid=selectedUser, epoch=authEpoch;
  panel.textContent='Проверяем доступ аккаунта Pushok…';
  try {
    const state=await api('users/'+encodeURIComponent(uid)+'/flow/account');
    if(uid!==selectedUser || epoch!==authEpoch || !$('user-dialog').open)return;
    const names={active:'активен',pending:'создаётся',expired:'срок истёк',revoked:'отозван',none:'не выдан'};
    panel.textContent=(state.account_linked?'Аккаунт подтверждён через Telegram.':'Связь с Telegram ещё не подтверждена.')
      +' Доступ WireGuard: '+(names[state.access]||'неизвестен')+'.'
      +' Привязка к телефону не применяется.';
  }catch(error){
    if(uid===selectedUser && epoch===authEpoch && $('user-dialog').open)panel.textContent=error.message;
  }
};
// Destructive actions require the developer role and explicit confirmation.
// Server failure never removes the card or pretends the peer was revoked.
const PEER_STATUS_LABELS = {
  no_subscription:'У пользователя нет подписки WireGuard.',
  server_unbound:'Подписка не закреплена за сервером. Автоматически переносить её нельзя.',
  server_missing:'Закреплённый сервер отсутствует или протокол изменился.',
  server_configuration_error:'Ошибка сохранённой конфигурации сервера.',
  other_backend:'Подписка закреплена за другим адаптером, не WGDashboard.',
  key_not_created:'Shop ещё не сохранил ключ пира для этого сервера.',
  key_unreadable:'Ключ Shop повреждён или недоступен для расшифровки.',
  peer_missing:'Пир с ключом Shop отсутствует на закреплённом WGDashboard.',
  peer_restricted:'Пир существует, но ограничен на стороне WGDashboard.',
  peer_found:'Пир существует на закреплённом WGDashboard.',
  panel_unavailable:'WGDashboard ответил ошибкой. Проверьте настройки API и журнал Shop.',
  panel_connection_failed:'Shop не может подключиться к адресу WGDashboard. Проверьте HTTPS и доступность узла.',
  panel_auth_failed:'WGDashboard отклонил API-ключ Shop (401/403). Обновите ключ в карточке сервера.',
  panel_api_missing:'API WGDashboard не найден (404/405). Проверьте адрес панели без /api.',
  panel_timeout:'WGDashboard не ответил за время проверки. Проверьте соединение Shop → панель.'
};
$('user-subscriptions').onclick = async event => {
  const check=event.target.closest('[data-peer-check]');
  const retry=event.target.closest('[data-peer-retry]');
  const restore=event.target.closest('[data-peer-restore]');
  if ((check || retry || restore) && role==='owner' && selectedUser!==null) {
    const button=check||retry||restore;
    const sid=check?check.dataset.peerCheck:retry?retry.dataset.peerRetry:restore.dataset.peerRestore;
    const card=button.closest('.subscription');
    const panel=card?.querySelector('[data-peer-status]');
    if(!panel || panel.dataset.peerStatus!==sid)return;
    const uid=selectedUser,epoch=authEpoch;
    if(retry && !confirm('Повторить создание пира из существующей очереди? Узел и ключ не изменятся.'))return;
    if(restore && !confirm('Проверить и синхронизировать пир на исходном сервере с прежним ключом? Срок подписки не изменится.'))return;
    button.disabled=true;
    panel.textContent=retry?'Повторная выдача ставится в очередь…':restore?'Восстанавливаем пир с прежним ключом…':'Проверяем ключ и пир на закреплённом сервере…';
    try {
      const url='users/'+encodeURIComponent(uid)+'/wireguard/'+(retry?'retry':restore?'restore':'peer');
      const result=await api(url,{method:retry||restore?'POST':'GET'});
      if(uid!==selectedUser || epoch!==authEpoch || panel.isConnected===false)return;
      panel.replaceChildren();
      if(retry){
        panel.textContent='Заявка поставлена в очередь. Повторная попытка выполняется фоновым обработчиком.';
      } else if(restore){
        panel.textContent=result.status==='already_present'
          ? 'Пир уже существует на закреплённом сервере. Данные не менялись.'
          : result.status==='refreshed'
            ? 'Пир уже работал. Конфигурация обновлена с WGDashboard; обновите подписку в Flow.'
            : 'Пир восстановлен с прежним ключом. Теперь обновите подписку в Pushok Flow.';
      } else {
        const state=PEER_STATUS_LABELS[result.status]||'Статус не определён.';
        const extra=result.server_name?' Закреплённый узел: '+result.server_name+'.':'';
        const attempts=result.grant?.state==='pending'
          ? ' В очереди, попыток: '+result.grant.attempts+'.':'';
        panel.textContent=state+extra+attempts;
        if(result.can_retry || result.can_restore){
          const action=document.createElement('button');
          action.type='button';
          action.className='small secondary';
          if(result.can_retry){
            action.dataset.peerRetry=sid;
            action.textContent='Повторить выдачу';
          } else {
            action.dataset.peerRestore=sid;
            action.textContent=result.status==='peer_found'
              ? 'Обновить конфигурацию' : 'Восстановить отсутствующий пир';
          }
          panel.appendChild(action);
        }
      }
    }catch(error){
      if(uid===selectedUser && epoch===authEpoch && panel.isConnected!==false)panel.textContent=error.message;
    }finally{button.disabled=false}
    return;
  }
  const extend=event.target.closest('[data-wg-extend]');
  if(extend && role==='owner' && selectedUser!==null) {
    const uid=selectedUser, epoch=authEpoch, sid=extend.dataset.wgExtend;
    if(!sid)return;
    const key=uid+':'+sid;
    let request=flowExtensionAttempts.get(key);
    extend.disabled=true;
    try {
      if(request) {
        if(!confirm('Предыдущее продление могло быть принято WGDashboard. '
          +'Повторить ТОТ ЖЕ запрос без начисления дополнительных дней?'))return;
      } else {
        const daysInput=prompt('Продлить WireGuard на сколько дней? (1–365)', '30');
        if(daysInput===null)return;
        const days=Number(daysInput);
        if(!Number.isInteger(days)||days<1||days>365){
          $('user-error').textContent='Укажите целое количество дней от 1 до 365.';
          return;
        }
        const reason=prompt('Причина продления (сохраняется в журнале)', 'Ручное продление разработчиком');
        if(reason===null)return;
        if(reason.trim().length<5 || reason.length>240){
          $('user-error').textContent='Причина должна содержать от 5 до 240 символов.';
          return;
        }
        const plan=await api('users/'+encodeURIComponent(uid)+'/wireguard/extend/plan?days='+days);
        if(uid!==selectedUser || epoch!==authEpoch || !$('user-dialog').open)return;
        if(plan.subscription_id!==sid)throw new Error('Подписка изменилась. Обновите карточку.');
        if(!confirm('Продлить доступ аккаунта до '+dateTime(plan.target_expires)+
          '? Shop должен подтвердить новый срок на прежнем WGDashboard.'))return;
        request={
          request_id:crypto.randomUUID(),subscription_id:sid,
          before_expires:plan.before_expires,target_expires:plan.target_expires,
          days:plan.days,reason:reason.trim(),confirm:'EXTEND_ACCOUNT_WIREGUARD'
        };
        flowExtensionAttempts.set(key,request);
      }
      const result=await api('users/'+encodeURIComponent(uid)+'/wireguard/extend',{
        method:'POST',data:request
      });
      flowExtensionAttempts.delete(key);
      if(uid===selectedUser && epoch===authEpoch && $('user-dialog').open){
        await loadUser(profilePage);
        $('user-error').textContent='Срок WireGuard продлён до '+dateTime(result.expires_at)+'.';
      }
    }catch(error){
      if(uid===selectedUser && epoch===authEpoch){
        $('user-error').textContent=error.message+
          ' При повторе кнопки будет использован тот же идентификатор, чтобы не начислить дни дважды.';
      }
    }finally{extend.disabled=false}
    return;
  }
  const revoke=event.target.closest('[data-subscription-revoke]');
  const erase=event.target.closest('[data-subscription-delete]');
  const remove=event.target.closest('[data-subscription-remove]');
  const button=revoke || erase || remove;
  if(!button || role!=='owner' || selectedUser===null)return;
  const sid=revoke?revoke.dataset.subscriptionRevoke:erase?erase.dataset.subscriptionDelete:remove.dataset.subscriptionRemove;
  if(typeof sid!=='string' || !sid)return;
  const question=revoke
    ? 'Отозвать подключение пользователя на сервере? История оплат сохранится.'
    : remove ? 'Удалить подписку? Shop сначала отключит доступ на сервере. Если запись связана с историей, она останется отозванной.'
    : 'Удалить запись подписки после подтверждённого отзыва пиров?';
  if(!confirm(question))return;
  if((erase || remove) && prompt('Для удаления записи введите УДАЛИТЬ','')!=='УДАЛИТЬ')return;
  button.disabled=true;
  const uid=selectedUser, epoch=authEpoch;
  try{
    const result=await api('subscriptions/'+encodeURIComponent(sid)+(revoke?'/revoke':remove?'/remove':''),
      {method:revoke||remove?'POST':'DELETE'});
    if(uid===selectedUser && epoch===authEpoch){
      await loadUser(profilePage);
      $('user-error').textContent=revoke?'Подключение отозвано.':result.status==='revoked_retained'?'Доступ отозван, запись сохранена в истории. '+(result.reason||''):'Запись подписки удалена.';
    }
  }catch(error){
    if(uid===selectedUser && epoch===authEpoch)$('user-error').textContent=error.message;
  }finally{button.disabled=false}
};
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
    $('list').innerHTML = data.orders.map(order => `<article class="admin-order"><b>${esc(order.plan_name || order.plan)} · ${esc(protocolLabel(order.protocol))} · ${order.method==='referral'?'Бонус · '+order.days+' дней':order.amount+' '+(order.currency === 'XTR' ? '★' : '₽')}</b><p><button class="text-link" data-order-user="${order.user_id}">${esc(order.email || 'Telegram ' + order.telegram_id)} · № ${order.user_id}</button></p><p>Заказ: ${esc(order.id)}</p>${order.promo_code?`<p>Код ${esc(order.promo_code)} · скидка ${order.discount} ${order.currency==='XTR'?'★':'₽'} от ${order.base_amount}</p>`:''}<p>${esc(orderStatus[order.status] || order.status)} · ${esc(order.method==='referral'?'Бонусное продление':order.method==='trial'?'Пробный доступ':order.method)} · ${esc(dateTime(order.created))}</p>${order.error ? `<p>Ошибка выдачи: ${esc(order.error)} · Попыток: ${order.attempts}</p>` : ''}<div class="actions">${order.can_confirm && (!delegatedRole() || canGroup('payments')) ? `<button data-action="confirm" data-id="${esc(order.id)}">Подтвердить поступление оплаты</button>` : ''}${order.can_reject && (!delegatedRole() || canGroup('payments')) ? `<button class="secondary" data-action="reject" data-id="${esc(order.id)}">Отменить заказ</button>` : ''}${order.can_retry ? `<button data-action="retry" data-id="${esc(order.id)}">Повторить выдачу</button>` : ''}</div></article>`).join('');
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

const serverProfileLabel = {legacy_vless:'Legacy API', clients_vless:'Clients JSON API', clients_vmess:'3x-ui Clients JSON · VMess', sui_hysteria2:'S-UI apiv2 · Hysteria2', sui_tuic:'S-UI apiv2 · TUIC v5', remnawave_vless:'Remnawave v3 API', wireguard_agent:'Pushok WG-агент', wgdashboard:'WGDashboard'};
const serverState = {active:'Принимать новых', draining:'Только продления', maintenance:'Обслуживание', disabled:'Выдача выключена'};
function serverCard(server) {
  const health = server.health === 'ok' ? 'API доступен' : server.health === 'error' ? 'Ошибка API' : 'Ещё не проверен';
  const panelExpiry = server.profile === 'wgdashboard' ? `<label><input name="schedule_expiry" type="checkbox" ${server.schedule_expiry?'checked':''}> Дублировать срок подписки заданием WGDashboard</label><label>Часовой пояс ОС панели<input name="panel_timezone" value="${esc(server.panel_timezone || 'UTC')}" maxlength="100" placeholder="Europe/Moscow" required></label><p class="muted">Панель удалит peer по расписанию, даже если Shop временно выключен. Укажите часовой пояс самого сервера WGDashboard.</p>` : '';
  return `<form class="plan-editor" data-server="${esc(server.id)}" data-version="${server.version}"><h3>${esc(server.name)}</h3><p>${esc(protocolLabel(server.protocol))} · ${esc(serverProfileLabel[server.profile] || server.profile)} · ${health}</p><p>Проверка: ${esc(dateTime(server.checked_at))}</p><p class="muted">🔒 Профиль подключения сохранён в Shop. API-адрес и секреты намеренно не отображаются; это не подтверждение доступности сервера.</p><p>Занято: ${server.occupied} · Всего привязано: ${server.bound_subscriptions}</p><label>Название<input name="name" value="${esc(server.name)}" maxlength="80" required></label><label>Локация<input name="location" value="${esc(server.location)}" maxlength="80"></label><label>Лимит занятых подписок (0 — без лимита)<input name="capacity" type="number" min="0" max="1000000" value="${server.capacity}" required></label><label>Режим<select name="state">${Object.entries(serverState).map(([id,label])=>`<option value="${id}" ${server.state===id?'selected':''}>${label}</option>`).join('')}</select></label>${panelExpiry}<button type="submit" class="small">Сохранить настройки сервера</button><button type="button" class="small secondary" data-server-check="${esc(server.id)}">Проверить API</button><button type="button" class="text-link" data-server-edit="${esc(server.id)}">Профиль подключения</button><button type="button" class="text-link" data-server-credentials="${esc(server.id)}">Обновить доступ</button>
    ${role==='owner' && server.state==='disabled' && !server.bound_subscriptions
      ? '<button type="button" class="danger-action" data-server-delete="'+esc(server.id)+'">Удалить пустой сервер</button>' : ''}
    </form>`;
}
function showServerNotice(message) {
  const previous = String($('servers-notice').textContent || '').trim();
  $('servers-notice').textContent = message + (previous ? ' ' + previous : '');
}
async function refreshServers() {
  const data = await api('servers');
  serverCache = data.servers;
  $('server-list').innerHTML = data.servers.map(s=>delegatedRole()?'<article class="admin-order"><h3>'+esc(s.name)+'</h3><p>'+esc(protocolLabel(s.protocol))+' · '+esc(serverState[s.state]||s.state)+'</p><p>API: '+esc(s.health)+' · Занято: '+esc(s.occupied)+' · Лимит: '+esc(s.capacity||'без лимита')+'</p><p>Проверено: '+esc(dateTime(s.checked_at))+'</p></article>':serverCard(s)).join('');
  $('servers-notice').textContent = data.unassigned_subscriptions ? 'Есть подписки без привязки: '+data.unassigned_subscriptions+'. Автоматический перенос запрещён; проверьте настройки миграции.' : data.servers.length ? '' : 'Добавьте первый сервер для продажи VPN.';
}
function setServerStep(step) {
  serverWizardStep = step;
  for (const n of [1,2,3]) {
    $('server-step-'+n).hidden = n !== step;
    $('server-step-label-'+n).setAttribute?.('aria-current', n === step ? 'step' : 'false');
  }
  $('server-back').hidden = step === 1;
  $('server-next').hidden = step === 3;
  $('server-save').hidden = step !== 3;
  const fields = $('server-form').elements;
  if (step === 3 && fields) {
    const name = String(fields.name.value || '').trim(), location = String(fields.location.value || '').trim();
    const replacing = Boolean(String(fields.url?.value || '').trim());
    const stored = editingServer && !replacing;
    $('server-review').innerHTML = '<h3>'+esc(name)+'</h3><p>'+esc(protocolLabel(fields.protocol.value))+
      (location ? ' · '+esc(location) : '')+'</p><p>Лимит подписок: '+esc(Number(fields.capacity.value) || 'без лимита')+'</p>'+
      '<p>'+(stored ? 'Сохранённый профиль останется без изменений (секреты скрыты).' :
      'Новый профиль будет зашифрован и сохранён только после подтверждения Shop.')+'</p>'+
      '<p>Доступ к панели будет проверен отдельной кнопкой после сохранения.</p>';
  }
}
function updateServerRequirements() {
  const form = $('server-form'), p = $('server-protocol').value, mode = $('server-api').value;
  const names = p === 'tuic' ? ['tuic_token','tuic_inbound_id','tuic_address','tuic_port','tuic_sni']
    : p === 'hysteria2' ? ['hy_token','hy_inbound_id','hy_address','hy_port','hy_sni']
    : p === 'vmess' ? ['vmess_token','vmess_inbound_id','vmess_address','vmess_port','vmess_sni']
    : p === 'wg' ? ['token',...($('server-wg-api').value === 'wgdashboard' ? ['configuration'] : [])]
    : mode === 'remnawave' ? ['remnawave_token']
    : [...(mode === 'clients' ? ['api_token'] : ['username','password']),'inbound_id','link_template'];
  const replacement = !editingServer || Boolean(form.elements?.url?.value);
  for (const field of form.querySelectorAll?.('input,select,textarea') || []) {
    field.required = ['name','capacity'].includes(field.name) ||
      (replacement && ['url',...names].includes(field.name));
  }
}
function validateServerStep(step) {
  setServerStep(step); updateServerRequirements();
  for (const field of $('server-step-'+step).querySelectorAll?.('input,select,textarea') || []) {
    if (!field.disabled && !field.closest('[hidden]') && !field.reportValidity()) return false;
  }
  return true;
}
function nextServerStep() {
  if (!serverSubmitting && validateServerStep(serverWizardStep)) {
    $('server-form-error').textContent = '';
    setServerStep(Math.min(3, serverWizardStep + 1));
    $('server-step-'+serverWizardStep).querySelector?.('input,select,textarea')?.focus();
  }
}
$('server-next').onclick = nextServerStep;
$('server-back').onclick = () => { if (!serverSubmitting) setServerStep(Math.max(1,serverWizardStep-1)); };

function clearServerCredentials() {
  for (const id of ['username','password','token']) $('server-credentials-'+id).value = '';
  $('server-credentials-target').textContent = '';
  $('server-credentials-error').textContent = '';
}
function openServerCredentials(server) {
  if (!server || !serverProfileLabel[server.profile]) return;
  credentialsEpoch++; credentialsServer = server; credentialsSubmitting = false;
  clearServerCredentials(); $('server-credentials-save').disabled = false;
  const legacy = server.profile === 'legacy_vless';
  $('server-credentials-target').textContent = server.name+' · '+serverProfileLabel[server.profile];
  $('server-credentials-legacy').hidden = !legacy;
  $('server-credentials-token-label').hidden = legacy;
  $('server-credentials-token').disabled = legacy;
  $('server-credentials-token').required = !legacy;
  $('server-credentials-username').disabled = !legacy;
  $('server-credentials-password').disabled = !legacy;
  $('server-credentials-dialog').showModal();
}
$('server-credentials-close').onclick = () => $('server-credentials-dialog').close();
$('server-credentials-dialog').addEventListener('close', () => {
  credentialsEpoch++; credentialsServer = null; credentialsSubmitting = false; clearServerCredentials();
});
$('server-credentials-form').onsubmit = async event => {
  event.preventDefault();
  if (!credentialsServer || credentialsSubmitting) return;
  const server = credentialsServer, epoch = credentialsEpoch;
  const data = {version:server.version};
  if (server.profile === 'legacy_vless') {
    const username = $('server-credentials-username').value;
    const password = $('server-credentials-password').value;
    if (username) data.username = username;
    if (password) data.password = password;
    if (!username && !password) { $('server-credentials-error').textContent = 'Введите новый логин или пароль.'; return; }
  } else {
    data.token = $('server-credentials-token').value;
    if (!data.token) { $('server-credentials-error').textContent = 'Введите новый токен API.'; return; }
  }
  credentialsSubmitting = true; $('server-credentials-save').disabled = true;
  $('server-credentials-error').textContent = '';
  try {
    await api('servers/'+encodeURIComponent(server.id)+'/credentials', {method:'PATCH',data});
    if (epoch !== credentialsEpoch) return;
    $('server-credentials-dialog').close();
    const message = '✅ Новый API-ключ сохранён в зашифрованном виде. Проверьте API и включите нужный режим сервера.';
    try {
      await refreshServers();
      showServerNotice(message);
    } catch (refreshError) {
      $('servers-notice').textContent = message+' Список не обновился: '+refreshError.message+'. Нажмите «Обновить».';
    }
  } catch (error) {
    if (epoch === credentialsEpoch) $('server-credentials-error').textContent = error.message;
  } finally {
    if (epoch === credentialsEpoch) { credentialsSubmitting = false; $('server-credentials-save').disabled = false; }
  }
};

function showServerFields() {
  const xui = $('server-protocol').value === 'xui';
  const wg = $('server-protocol').value === 'wg';
  const vmess = $('server-protocol').value === 'vmess';
  const mode = $('server-api').value;
  $('server-xui-fields').hidden = !xui; $('server-wg-fields').hidden = !wg;
  $('server-vmess-fields').hidden = !vmess;
  $('server-hysteria2-fields').hidden = $('server-protocol').value !== 'hysteria2';
  $('server-tuic-fields').hidden = $('server-protocol').value !== 'tuic';
  $('server-vmess-ws-fields').hidden = !vmess || $('server-vmess-network').value !== 'ws';
  $('server-legacy-auth').hidden = mode !== 'legacy';
  $('server-clients-auth').hidden = mode !== 'clients';
  $('server-remnawave-auth').hidden = mode !== 'remnawave';
  $('server-vless-link-fields').hidden = mode === 'remnawave';
  $('server-remnawave-help').hidden = !xui || mode !== 'remnawave';
  const dashboard = wg && $('server-wg-api').value === 'wgdashboard';
  $('server-wg-configuration').hidden = !dashboard;
  $('server-wg-interface').disabled = !dashboard;
  $('server-wgdashboard-help').hidden = !dashboard;
  updateServerRequirements();
}
$('server-protocol').onchange = showServerFields;
$('server-api').onchange = showServerFields;
$('server-wg-api').onchange = showServerFields;
$('server-vmess-network').onchange = showServerFields;
function openServer(server = null) {
  editingServer = server; serverDialogEpoch++; serverSubmitting = false;
  $('server-save').disabled = false;
  const form = $('server-form'); form.reset();
  form.elements.name.value = server?.name || ''; form.elements.location.value = server?.location || '';
  form.elements.capacity.value = server?.capacity || 0; form.elements.protocol.value = server?.protocol || 'xui';
  form.elements.api.value = server?.profile === 'clients_vless' ? 'clients' : server?.profile === 'remnawave_vless' ? 'remnawave' : 'legacy';
  form.elements.wg_api.value = server?.profile === 'wgdashboard' ? 'wgdashboard' : 'agent';
  form.elements.protocol.disabled = !!server; form.elements.url.required = !server;
  $('server-title').textContent = server ? 'Профиль сервера' : 'Новый сервер';
  $('server-profile-state').textContent = server
    ? '✅ Профиль подключения сохранён в Shop в зашифрованном виде. Поля API и токена пустые намеренно. Оставьте их пустыми, чтобы сохранить прежние настройки, или заполните весь профиль для замены.'
    : 'Профиль пока не сохранён. После успешного создания он появится в списке серверов; API отдельно проверяется кнопкой «Проверить API».';
  $('server-form-error').textContent = ''; showServerFields(); setServerStep(1); $('server-dialog').showModal();
}
$('new-server').onclick = () => openServer();
$('server-close').onclick = () => $('server-dialog').close();
$('server-dialog').addEventListener('close', () => { serverDialogEpoch++; serverSubmitting = false; $('server-form').reset(); editingServer = null; });
$('server-form').onsubmit = async event => {
  event.preventDefault();
  if (serverSubmitting) return;
  if (serverWizardStep < 3) { nextServerStep(); return; }
  if ($('server-dialog').open && (!validateServerStep(1) || !validateServerStep(2))) return;
  setServerStep(3);
  const epoch = serverDialogEpoch; serverSubmitting = true;
  const form = event.target, values = new FormData(form), button = form.querySelector('button[type="submit"]');
  button.disabled = true; $('server-form-error').textContent = '';
  const original = editingServer, protocol = original?.protocol || String(values.get('protocol'));
  try {
    const url = String(values.get('url') || '').trim();
    const mode = String(values.get('api') || 'legacy');
    const wgMode = String(values.get('wg_api') || 'agent');
    if (protocol === 'wg' && !['agent','wgdashboard'].includes(wgMode)) throw new Error('Выберите панель WireGuard.');
    const originalWGMode = original?.profile === 'wgdashboard' ? 'wgdashboard' : 'agent';
    const originalMode = original?.profile === 'clients_vless' ? 'clients' : original?.profile === 'remnawave_vless' ? 'remnawave' : 'legacy';
    const fields = protocol === 'tuic' ? ['tuic_token','tuic_inbound_id','tuic_address','tuic_port','tuic_sni'] : protocol === 'hysteria2' ? ['hy_token','hy_inbound_id','hy_address','hy_port','hy_sni','hy_obfs_password'] : protocol === 'vmess' ? ['vmess_token','vmess_inbound_id','vmess_address','vmess_port','vmess_sni','vmess_path','vmess_host'] : protocol !== 'xui' ? ['token','configuration'] : mode === 'remnawave' ? ['remnawave_token','squad_uuids'] : mode === 'clients' ? ['api_token','inbound_id','link_template','flow'] : ['username','password','inbound_id','link_template','flow'];
    if (original?.protocol === 'xui' && !url && mode !== originalMode) throw new Error('Для смены режима API введите полный профиль.');
    if (original?.protocol === 'wg' && !url && wgMode !== originalWGMode) throw new Error('Для смены панели введите полный профиль.');
    if (!url && fields.some(key=>values.get(key))) throw new Error('Для замены профиля укажите адрес и все данные подключения.');
    if (!url && protocol === 'tuic' && ((values.get('tuic_congestion') || 'cubic') !== 'cubic' || (values.get('tuic_udp') || 'native') !== 'native')) throw new Error('Для изменения режимов TUIC введите полный профиль.');
    if (!url && protocol === 'vmess' && values.get('vmess_network') === 'ws') throw new Error('Для изменения транспорта VMess введите полный профиль.');
    if (url && protocol === 'wg' && wgMode === 'wgdashboard' && !/^[A-Za-z0-9_=+.\-]{1,64}$/.test(String(values.get('configuration') || '').trim())) throw new Error('Укажите интерфейс WireGuard, например wg0.');
    const network = String(values.get('vmess_network') || 'tcp');
    const profile = url ? protocol === 'tuic'
      ? {url,api:'sui',token:String(values.get('tuic_token') || ''),inbound_id:Number(values.get('tuic_inbound_id')),address:String(values.get('tuic_address') || '').trim(),port:Number(values.get('tuic_port')),sni:String(values.get('tuic_sni') || '').trim(),congestion_control:String(values.get('tuic_congestion') || 'cubic'),udp_relay_mode:String(values.get('tuic_udp') || 'native')}
      : protocol === 'hysteria2'
      ? {url,api:'sui',token:String(values.get('hy_token') || ''),inbound_id:Number(values.get('hy_inbound_id')),address:String(values.get('hy_address') || '').trim(),port:Number(values.get('hy_port')),sni:String(values.get('hy_sni') || '').trim(),obfs_password:String(values.get('hy_obfs_password') || '')}
      : protocol === 'vmess'
      ? {url,api:'clients',token:String(values.get('vmess_token') || ''),inbound_id:Number(values.get('vmess_inbound_id')),address:String(values.get('vmess_address') || '').trim(),port:Number(values.get('vmess_port')),network,sni:String(values.get('vmess_sni') || '').trim(),path:network==='ws'?String(values.get('vmess_path') || ''):'',ws_host:network==='ws'?String(values.get('vmess_host') || '').trim():''}
      : protocol === 'xui' ? mode === 'remnawave'
      ? {url,api:mode,token:String(values.get('remnawave_token') || ''),squad_uuids:String(values.get('squad_uuids') || '').split(/[\n,]+/).map(x=>x.trim()).filter(Boolean)}
      : {url,api:mode,...(mode === 'clients' ? {token:String(values.get('api_token') || '')} : {username:String(values.get('username') || ''),password:String(values.get('password') || '')}),inbound_id:Number(values.get('inbound_id')),link_template:String(values.get('link_template') || ''),flow:String(values.get('flow') || '')}
      : {url,token:String(values.get('token') || ''),...(wgMode === 'wgdashboard' ? {api:'wgdashboard',configuration:String(values.get('configuration') || '').trim()} : {})} : null;
    const data = {name:String(values.get('name')),location:String(values.get('location') || ''),capacity:Number(values.get('capacity'))};
    if (original) { data.version=original.version; data.state=profile ? 'disabled' : original.state; if (profile) data.settings=profile; }
    else { data.protocol=protocol; data.settings=profile; }
    await api(original ? 'servers/'+encodeURIComponent(original.id) : 'servers', {method:original?'PUT':'POST',data});
    if (epoch !== serverDialogEpoch) return;
    $('server-dialog').close();
    const message = profile
      ? '✅ Профиль подключения сохранён в Shop (зашифрован). Адрес и ключ скрыты намеренно. Проверьте API перед включением продаж.'
      : '✅ Настройки сервера сохранены. Профиль подключения остался без изменений.';
    // Server status belongs next to the server list, not in the tariff section.
    try {
      await refreshServers();
      showServerNotice(message);
    } catch (refreshError) {
      $('servers-notice').textContent = message+' Список серверов не обновился: '+refreshError.message+'. Нажмите «Обновить».';
    }
  } catch (error) {
    if (epoch === serverDialogEpoch) {
      $('server-form-error').textContent = '❌ Не сохранено: '+error.message+'. Исправьте данные — введённые поля остались в форме.';
      $('server-profile-state').textContent = 'Данные не записаны. Исправьте ошибку и повторите сохранение.';
    }
  }
  finally { if (epoch === serverDialogEpoch) { button.disabled=false; serverSubmitting=false; } }
};
$('server-list').onsubmit = async event => {
  event.preventDefault(); const form=event.target.closest('[data-server]'); if (!form) return;
  const values=new FormData(form), button=form.querySelector('button[type="submit"]'); button.disabled=true;
  try {
    await api('servers/'+encodeURIComponent(form.dataset.server), {method:'PUT',data:{name:String(values.get('name')),location:String(values.get('location') || ''),capacity:Number(values.get('capacity')),state:String(values.get('state')),version:Number(form.dataset.version),...(form.elements?.schedule_expiry ? {schedule_expiry:form.elements.schedule_expiry.checked,panel_timezone:String(values.get('panel_timezone') || 'UTC')} : {})}});
    await refreshServers();
    showServerNotice('✅ Настройки сервера сохранены.');
    $('error').textContent='';
  } catch (error) {
    $('servers-notice').textContent='❌ Не удалось сохранить настройки сервера: '+error.message;
    button.disabled=false;
  }
};
$('server-list').onclick = async event => {
  const remove=event.target.closest('[data-server-delete]');
  if(remove && role==='owner'){
    const id=remove.dataset.serverDelete;
    if(!confirm('Удалить выключенный сервер без подписок и ключей?'))return;
    if(prompt('Для удаления сервера введите УДАЛИТЬ','')!=='УДАЛИТЬ')return;
    remove.disabled=true;
    try{
      await api('servers/'+encodeURIComponent(id),{method:'DELETE'});
      await refreshServers();
      showServerNotice('Пустой сервер удалён.');
    }catch(error){
      $('servers-notice').textContent=error.message;
      remove.disabled=false;
    }
    return;
  }
  const credentials=event.target.closest('[data-server-credentials]');
  if (credentials) { openServerCredentials(serverCache.find(s=>s.id===credentials.dataset.serverCredentials)); return; }
  const edit=event.target.closest('[data-server-edit]');
  if (edit) { openServer(serverCache.find(s=>s.id===edit.dataset.serverEdit)); return; }
  const check=event.target.closest('[data-server-check]'); if (!check) return;
  check.disabled=true;
  try {
    await api('servers/'+encodeURIComponent(check.dataset.serverCheck)+'/check',{method:'POST'});
    await refreshServers();
    showServerNotice('✅ API сервера проверен. Для запуска подключения отдельно проверьте выдачу WireGuard peer.');
  } catch (error) {
    $('servers-notice').textContent='❌ Проверка API не пройдена: '+error.message;
    check.disabled=false;
  }
};

async function refreshFlowAutoPolicy() {
  const epoch=authEpoch;
  const p=await api('flow/wireguard/auto');
  if(epoch!==authEpoch || !canGroup('starter'))return;
  const diagnostic=p.diagnostics||{};
  const statusText={
    policy_disabled:'Автовыдача выключена.',
    wgdashboard_not_ready:'Нет активного WGDashboard с настроенным удалением пиров по сроку.',
    issuance_retrying:'Есть повторяющиеся ошибки создания пиров — проверьте API и очередь.',
    issuance_pending:'Пиры ожидают создания в фоновой очереди.',
    ready:'Автовыдача готова для пользователей, подходящих под условия.'
  };
  $('flow-auto-count').textContent='Зарезервировано: '+p.used+' из '+p.max_users+
    '. Осталось: '+p.remaining+'. '+(statusText[diagnostic.state]||'')+
    ' Ожидают: '+(diagnostic.pending??'—')+
    ', с повторными попытками: '+(diagnostic.retrying??'—')+
    ', выдано: '+(diagnostic.active??'—')+
    ', существующих WG-подписок: '+(diagnostic.existing_wg_subscriptions??'—')+'.'+
    (diagnostic.mtproxy_only?' Выдача ограничена подтверждёнными пользователями MTProxy.':'');

  $('flow-auto-editor').innerHTML=
    '<form class="plan-editor" data-flow-auto-version="'+p.version+'">'+
    '<label class="plan-enabled"><input name="enabled" type="checkbox" '+(p.enabled?'checked':'')+
    '> Выдавать WireGuard при входе в Shop</label>'+
    '<label>Стартовый срок, дней<input name="days" type="number" min="1" max="30" value="'+p.days+'" required></label>'+
    '<label>Общий лимит пользователей<input name="max_users" type="number" min="1" max="100000" value="'+p.max_users+'" required></label>'+
    '<p>Лимит учитывает ожидающие выдачи и не сбрасывается при изменении. Отключение останавливает новые выдачи, но ранее выданные профили действуют до своего серверного срока.</p>'+
    '<button type="submit" class="small">Сохранить автовыдачу</button></form>';
}
$('flow-auto-editor').onsubmit=async event=>{
  event.preventDefault();
  const form=event.target.closest('[data-flow-auto-version]');
  if(!form || !canGroup('starter'))return;
  const values=new FormData(form),button=form.querySelector('button');
  button.disabled=true;
  try {
    await api('flow/wireguard/auto',{method:'PUT',data:{
      enabled:values.has('enabled'),days:Number(values.get('days')),
      max_users:Number(values.get('max_users')),version:Number(form.dataset.flowAutoVersion)}});
    await refreshFlowAutoPolicy();
    $('flow-auto-error').textContent='';
  }catch(error){
    $('flow-auto-error').textContent=error.message;button.disabled=false;
  }
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
  return `<form class="plan-editor" data-coupon="${esc(p.id)}" data-version="${p.version}"><h3>${esc(p.code)}</h3><p>Оформлено: ${p.used} из ${p.max_uses}. Применения не сбрасываются.</p><label>Скидка, %<input name="percent" type="number" min="1" max="90" required value="${p.percent}"></label><label>Лимит заказов<input name="max_uses" type="number" min="1" max="100000" required value="${p.max_uses}"></label><label>Окончание (часовой пояс браузера)<input name="expires_at" type="datetime-local" value="${couponDate(p.expires_at)}"></label><label>Код тарифа (пусто — все)<input name="plan_id" maxlength="32" value="${esc(p.plan_id || '')}"></label><label>Протокол<select name="protocol">${[['all','Все'],['xui','VLESS'],['wg','WireGuard'],['vmess','VMess AEAD'],['hysteria2','Hysteria2'],['tuic','TUIC v5']].map(([id,label])=>`<option value="${id}" ${p.protocol===id?'selected':''}>${label}</option>`).join('')}</select></label><label class="plan-enabled"><input name="enabled" type="checkbox" ${p.enabled?'checked':''}> Включён</label><button type="submit" class="small">Сохранить</button></form>`;
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

const diagnosticWarnings={worker_disabled:'Фоновые задачи выключены настройками',encryption_key_invalid:'Неверный формат ключа шифрования',worker_failed:'Последний цикл worker завершился ошибкой',worker_not_observed:'Цикл worker ещё не наблюдался в этом процессе',worker_slow:'Цикл worker выполняется больше 3 минут',worker_stale:'Нет свежего успешного цикла worker',queue_errors:'В очереди есть задания с ошибками',queue_wait_over_10m:'Задания выдачи или устройств ожидают больше 10 минут',expiry_wait_over_10m:'Есть доступы с просроченным отзывом больше 10 минут',queues_paused:'Фоновые задачи выключены при непустой очереди',server_probe_not_recent_ok:'Не все включённые серверы имеют успешную API-проверку за сутки',no_new_assignments_xui:'Нет сервера VLESS для новых назначений',no_new_assignments_wg:'Нет сервера WireGuard для новых назначений',no_new_assignments_vmess:'Нет сервера VMess для новых назначений',no_new_assignments_hysteria2:'Нет сервера Hysteria2 для новых назначений',no_new_assignments_tuic:'Нет сервера TUIC для новых назначений',no_active_plans:'Нет опубликованных тарифов',no_assignable_servers:'Нет серверов для новых назначений',payment_settings_incomplete:'Не заполнены настройки оплаты',telegram_sales_settings_incomplete:'Не заполнены настройки продаж в Telegram'};
const diagnosticWorker={disabled:'Выключен настройками',not_observed:'Ещё не наблюдался',failed:'Ошибка последнего цикла',busy:'Выполняет цикл',slow:'Длительный цикл',stale:'Нет свежего успешного цикла',recent_success:'Недавно завершил цикл'};
function diagnosticsMarkup(r) {
  const labels={issuance:'Выдача и продление',payment_poll:'Сверка ЮKassa',devices:'Создание/отзыв устройств',device_rotations:'Смена ключей устройств',primary_rotations:'Смена основного ключа'};
  return `<p><b>${r.status==='local_checks_ok'?'Локальные проверки без замечаний':'Требуется внимание'}</b> · ${esc(new Date(r.generated_at*1000).toLocaleString('ru-RU'))}</p><p>Worker: ${esc(diagnosticWorker[r.worker.state] || r.worker.state)}. Последний успешный цикл: ${r.worker.last_success?esc(new Date(r.worker.last_success*1000).toLocaleString('ru-RU')):'не наблюдался'}.</p>${r.warnings.length?'<ul>'+r.warnings.map(w=>'<li>'+esc(diagnosticWarnings[w] || w)+'</li>').join('')+'</ul>':''}<div class="table-scroll"><table><thead><tr><th>Очередь</th><th>Ожидает</th><th>Срок повтора наступил</th><th>С ошибкой</th><th>Возраст старейшего, сек.</th></tr></thead><tbody>${Object.entries(r.queues).map(([key,q])=>`<tr><td>${esc(labels[key] || key)}</td><td>${esc(q.pending)}</td><td>${esc(q.due)}</td><td>${esc(q.with_error)}</td><td>${esc(q.oldest_age_seconds ?? '—')}</td></tr>`).join('')}</tbody></table></div><p>Подписок с истёкшим сроком в состоянии active: ${esc(r.expiry.pending)}. Это очередь проверки отзыва, а не доказательство текущего доступа на узле.</p><p>Серверы для новых назначений / недавние успешные API-проверки: ${Object.entries(r.servers).map(([id,s])=>esc(protocolLabel(id))+": "+esc(s.assignable)+" / "+esc(s.cached_api_ok_24h)).join(" · ")}.</p><p>Статус относится только к локальным наблюдениям. Проверка трафика, платежа и восстановления копии выполняется отдельно.</p>`;
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

// The server renders only a presence hint, never a cookie value or trusted role.
if(document.querySelector('meta[name="shop-staff-session"]')?.content==='1') {
  void restoreStaffSession();
}
