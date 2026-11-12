const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');

function adminPage(options={}) {
  const elements = new Map();
  function element(id) {
    if (!elements.has(id)) elements.set(id, {
      innerHTML:'', textContent:'', value:'', open:false, disabled:false, checked:false,
      listeners:{}, reset() {},
      replaceChildren() { this.innerHTML = ''; this.textContent = ''; },
      addEventListener(name, handler) { this.listeners[name] = handler; },
      showModal() { this.open = true; },
      close() { this.open = false; this.listeners.close?.(); },
    });
    return elements.get(id);
  }
  const replies = [], requests = [], bodies = [];
  const context = vm.createContext({
    document:{getElementById:element, querySelector:()=>({content:'/shop'})},
    crypto:require('node:crypto').webcrypto, URLSearchParams, FormData:options.FormData || FormData,
    fetch:(url,opts)=>new Promise(resolve=>{ requests.push(url); bodies.push(opts.body ? JSON.parse(opts.body) : null); replies.push((body,status=200)=>resolve({ok:status<400,json:async()=>body})); }),
  });
  vm.runInContext(fs.readFileSync('app/static/admin.js','utf8'), context);
  vm.runInContext("token='admin-test-token'", context);
  return {element, run:code=>vm.runInContext(code,context), reply:body=>replies.at(-1)(body), replyAt:(index,body,status=200)=>replies[index](body,status), requests,bodies};
}

function protocolCatalog() {
  return {schema_version:1, protocols:[
    {id:'vless',name:'VLESS',implementation:'in_code',adapters:['3x-ui','Remnawave'],description:'UUID',server_options:'3x-ui / Remnawave',client_requirements:'TLS',documentation_url:'https://xtls.github.io/'},
    {id:'vmess',name:'VMess',implementation:'planned',adapters:[],description:'AEAD',server_options:'Кандидат: 3x-ui',client_requirements:'Часы',documentation_url:'https://www.v2fly.org/'}
  ]};
}

test('protocol catalog filters implemented and planned adapters without purchase actions', async () => {
  const page=adminPage(), request=page.run("role='owner'; refreshProtocols()");
  assert.equal(page.requests[0], '/shop/api/admin/protocols');
  page.reply(protocolCatalog()); await request;
  assert.match(page.element('protocols-list').innerHTML, /Адаптер в коде/);
  assert.match(page.element('protocols-list').innerHTML, /В плане/);
  assert.doesNotMatch(page.element('protocols-list').innerHTML, /<button|<form|В продаже/);
  page.element('protocols-filter').value='planned'; page.run('renderProtocols()');
  assert.match(page.element('protocols-list').innerHTML, /VMess/);
  assert.doesNotMatch(page.element('protocols-list').innerHTML, /VLESS/);
  assert.equal(page.element('protocols-status').textContent, 'Показано: 1 из 2.');
});

test('protocol catalog escapes text and rejects unsafe documentation links', async () => {
  const page=adminPage(), data=protocolCatalog();
  data.protocols[0].name='<img src=x onerror=alert(1)>';
  data.protocols[0].documentation_url='javascript:alert(1)';
  data.protocols[0].adapters=['<script>'];
  const request=page.run("role='panel_operator'; refreshProtocols()"); page.reply(data); await request;
  const markup=page.element('protocols-list').innerHTML;
  assert.match(markup, /&lt;img/); assert.match(markup, /&lt;script&gt;/);
  assert.doesNotMatch(markup, /<img|<script|javascript:/);
  assert.match(markup, /rel="noopener noreferrer"/);
});

test('protocol catalog ignores late requests and clears results on errors', async () => {
  const page=adminPage();
  const first=page.run("role='owner'; refreshProtocols()");
  const second=page.run('refreshProtocols()');
  page.replyAt(1,protocolCatalog()); await second;
  page.replyAt(0,{schema_version:1,protocols:[]}); await first;
  assert.match(page.element('protocols-list').innerHTML, /VMess/);
  const failed=page.run('refreshProtocols()'); page.replyAt(2,{detail:'Нет доступа'},403); await failed;
  assert.equal(page.element('protocols-list').innerHTML,'');
  assert.equal(page.element('protocols-status').textContent,'Нет доступа');
  assert.equal(page.element('protocols-refresh').disabled,false);
});

test('protocol catalog cannot reappear after logout or load for an unrelated role', async () => {
  const page=adminPage(), request=page.run("role='owner'; refreshProtocols()");
  page.run("$('exit').onclick()"); page.replyAt(0,protocolCatalog()); await request;
  assert.equal(page.element('protocols-list').innerHTML,'');
  assert.equal(page.element('protocols-panel').hidden,true);
  const count=page.requests.length;
  await page.run("role='finance'; refreshProtocols()");
  assert.equal(page.requests.length,count);
});

test('logout rejects an in-flight response with customer data', async () => {
  const page = adminPage();
  const request = page.run("api('users')");
  page.run("$('exit').onclick()");
  page.replyAt(0,{users:[{email:'customer@example.com'}]});
  await assert.rejects(request, /Сессия администратора завершена/);
  assert.equal(page.element('users-list').innerHTML, '');
});

test('a closed customer dialog cannot be filled by a late response', async () => {
  const page = adminPage();
  page.element('user-dialog').open = true;
  const request = page.run('selectedUser=1; loadUser(1)');
  page.run("$('user-close').onclick()");
  page.reply({user:{id:1,email:'private@example.com'},subscriptions:[],orders:[],orders_total:0,orders_page:1,orders_page_size:20});
  await request;
  assert.equal(page.element('user-details').innerHTML, '');
  assert.equal(page.element('user-dialog').open, false);
});

test('customer search escapes displayed email and enables correct pagination', async () => {
  const page = adminPage();
  const request = page.run('refreshUsers()');
  page.reply({users:[{id:1,email:'<img src=x>@example.com',telegram_id:null,created:1,active_subscriptions:0,orders_count:0}],total:26,page:1,page_size:25});
  await request;
  const markup = page.element('users-list').innerHTML;
  assert.ok(markup.includes('&lt;img src=x&gt;@example.com'));
  assert.ok(!markup.includes('<img'));
  assert.equal(page.element('users-prev').disabled, true);
  assert.equal(page.element('users-next').disabled, false);
});

function uiOrder(id, options={}) {
  return {id,user_id:1,email:'buyer@example.com',telegram_id:null,plan:'month',plan_name:'На месяц',protocol:'wg',amount:199,currency:'RUB',method:'manual',status:'pending',created:100,attempts:0,error:null,can_confirm:false,can_reject:false,can_retry:false,...options};
}

test('order actions follow server flags including externally paid methods', async () => {
  const page=adminPage(); const request=page.run('refreshOrders()');
  page.reply({orders:[uiOrder('manual',{can_confirm:true,can_reject:true}),uiOrder('stars',{method:'stars'}),uiOrder('paid',{status:'paid',can_retry:true})],total:3,page:1,page_size:25});
  await request;
  const markup=page.element('list').innerHTML;
  assert.equal((markup.match(/data-action="confirm"/g)||[]).length,1);
  assert.equal((markup.match(/data-action="reject"/g)||[]).length,1);
  assert.equal((markup.match(/data-action="retry"/g)||[]).length,1);
  assert.ok(!markup.includes('data-id="stars"'));
});

test('an older filter response cannot replace newer order search results', async () => {
  const page=adminPage();
  const first=page.run("ordersFilters.q='first'; refreshOrders()");
  const second=page.run("ordersFilters.q='second'; refreshOrders()");
  page.replyAt(1,{orders:[uiOrder('new-result')],total:1,page:1,page_size:25}); await second;
  page.replyAt(0,{orders:[uiOrder('old-result')],total:1,page:1,page_size:25}); await first;
  assert.ok(page.element('list').innerHTML.includes('new-result'));
  assert.ok(!page.element('list').innerHTML.includes('old-result'));
});

test('order pagination moves back when the last filtered row leaves a page', async () => {
  const page=adminPage();
  const request=page.run("ordersPage=2; ordersFilters.status='pending'; refreshOrders()");
  page.replyAt(0,{orders:[],total:25,page:2,page_size:25});
  await new Promise(setImmediate);
  assert.equal(page.requests.length,2);
  assert.ok(page.requests[1].includes('page=1'));
  assert.ok(page.requests[1].includes('status=pending'));
  page.replyAt(1,{orders:[uiOrder('remaining')],total:25,page:1,page_size:25});
  await request;
  assert.equal(page.element('orders-page').textContent,'Страница 1 из 1');
  assert.equal(page.element('orders-next').disabled,true);
});

function uiServer(options={}) {
  return {id:'node-a',name:'<img src=x>',location:'Финляндия',protocol:'xui',state:'active',capacity:0,version:1,health:'unknown',checked_at:null,occupied:1,bound_subscriptions:2,...options};
}

test('registry renders untested API honestly and escapes server names', async () => {
  const page=adminPage(); const request=page.run('refreshServers()');
  page.reply({servers:[uiServer()],unassigned_subscriptions:0}); await request;
  const markup=page.element('server-list').innerHTML;
  assert.ok(markup.includes('Ещё не проверен') && markup.includes('&lt;img src=x&gt;'));
  assert.ok(!markup.includes('<img') && !markup.includes('API доступен'));
});

test('editing server metadata preserves connection credentials without sending a profile', async () => {
  class Fields { constructor(form){this.fields=form.fields;} get(name){return this.fields[name];} }
  const page=adminPage({FormData:Fields});
  const request=page.run(`$('server-list').onsubmit({preventDefault(){},target:{dataset:{server:'node-a',version:'1'},fields:{name:'New name',location:'FI',capacity:'10',state:'draining'},closest(){return this;},querySelector(){return {disabled:false};}}})`);
  assert.deepEqual(page.bodies[0],{name:'New name',location:'FI',capacity:10,state:'draining',version:1});
  page.replyAt(0,uiServer({name:'New name',state:'draining',version:2}));
  await new Promise(setImmediate);
  page.replyAt(1,{servers:[uiServer({name:'New name',state:'draining',version:2})],unassigned_subscriptions:0});
  await request;
  assert.ok(page.element('server-list').innerHTML.includes('New name'));
});

test('new clients profile submits a token without legacy credentials', async () => {
  class Fields { constructor(form){this.fields=form.fields;} get(name){return this.fields[name];} }
  const page=adminPage({FormData:Fields});
  const request=page.run(`$('server-form').onsubmit({preventDefault(){},target:{fields:{name:'Modern',location:'FI',capacity:'2',protocol:'xui',api:'clients',url:'https://panel.example',api_token:'private-token',username:'stale-user',password:'stale-password',inbound_id:'7',link_template:'vless://{uuid}@vpn.example'},querySelector(){return {disabled:false};}}})`);
  assert.deepEqual(page.bodies[0],{name:'Modern',location:'FI',capacity:2,protocol:'xui',settings:{url:'https://panel.example',api:'clients',token:'private-token',inbound_id:7,link_template:'vless://{uuid}@vpn.example',flow:''}});
  page.replyAt(0,uiServer({profile:'clients_vless'}));
  await new Promise(setImmediate);
  page.replyAt(1,{servers:[uiServer({profile:'clients_vless'})],unassigned_subscriptions:0});
  await request;
  assert.ok(page.element('server-list').innerHTML.includes('Clients JSON API'));
});

test('changing API mode requires a complete replacement profile', async () => {
  class Fields { constructor(form){this.fields=form.fields;} get(name){return this.fields[name];} }
  const page=adminPage({FormData:Fields});
  await page.run(`editingServer={id:'node-a',protocol:'xui',profile:'legacy_vless'}; $('server-form').onsubmit({preventDefault(){},target:{fields:{api:'clients',url:''},querySelector(){return {disabled:false};}}})`);
  assert.equal(page.requests.length,0);
  assert.ok(page.element('server-form-error').textContent.includes('полный профиль'));
});

test('VMess form sends explicit Clients JSON settings without stale VLESS or WG secrets', async () => {
  class Fields { constructor(form){this.fields=form.fields;} get(name){return this.fields[name];} }
  const page=adminPage({FormData:Fields});
  const request=page.run(`$('server-form').onsubmit({preventDefault(){},target:{fields:{name:'VMess',capacity:'2',protocol:'vmess',url:'https://panel.example/private',vmess_token:'vmess-token',vmess_inbound_id:'7',vmess_address:'vpn.example',vmess_port:'443',vmess_network:'ws',vmess_path:'/vpn',vmess_host:'vpn.example',vmess_sni:'vpn.example',api:'legacy',username:'stale-user',password:'stale-password',token:'stale-wg',link_template:'vless://stale'},querySelector(){return {disabled:false};}}})`);
  assert.deepEqual(page.bodies[0],{name:'VMess',location:'',capacity:2,protocol:'vmess',settings:{url:'https://panel.example/private',api:'clients',token:'vmess-token',inbound_id:7,address:'vpn.example',port:443,network:'ws',path:'/vpn',ws_host:'vpn.example',sni:'vpn.example'}});
  page.replyAt(0,uiServer({protocol:'vmess',profile:'clients_vmess'})); await new Promise(setImmediate);
  page.replyAt(1,{servers:[uiServer({protocol:'vmess',profile:'clients_vmess'})],unassigned_subscriptions:0}); await request;
  assert.match(page.element('server-list').innerHTML,/VMess AEAD/);
});

test('VMess field visibility does not expose unrelated panel controls', () => {
  const page=adminPage();page.element('server-protocol').value='vmess';
  page.element('server-api').value='remnawave';page.element('server-wg-api').value='wgdashboard';
  page.element('server-vmess-network').value='ws';page.run('showServerFields()');
  assert.equal(page.element('server-vmess-fields').hidden,false);
  assert.equal(page.element('server-vmess-ws-fields').hidden,false);
  for(const id of ['server-xui-fields','server-wg-fields','server-remnawave-help','server-wgdashboard-help','server-wg-configuration']) assert.equal(page.element(id).hidden,true);
  page.element('server-vmess-network').value='tcp';page.run('showServerFields()');
  assert.equal(page.element('server-vmess-ws-fields').hidden,true);
});

test('editing VMess without a replacement profile preserves the encrypted settings', async () => {
  class Fields { constructor(form){this.fields=form.fields;} get(name){return this.fields[name];} }
  const page=adminPage({FormData:Fields});
  const request=page.run(`editingServer={id:'vmess-a',protocol:'vmess',profile:'clients_vmess',state:'active',version:3}; $('server-form').onsubmit({preventDefault(){},target:{fields:{name:'Renamed',capacity:'0',url:'',vmess_network:'tcp'},querySelector(){return {disabled:false};}}})`);
  assert.deepEqual(page.bodies[0],{name:'Renamed',location:'',capacity:0,version:3,state:'active'});
  page.replyAt(0,uiServer()); await new Promise(setImmediate);
  page.replyAt(1,{servers:[],unassigned_subscriptions:0}); await request;
});

test('tariff editor displays a zero profile limit without substituting the default',()=>{
  const page=adminPage();
  const markup=page.run("planForm({id:'single',name:'Один',days:30,price:100,stars:10,version:1,enabled:true,device_limit:0})");
  assert.ok(markup.includes('name="device_limit" type="number" value="0"'));
  assert.ok(markup.includes('min="0" max="20"'));
});

test('trial policy editor displays the lifetime activation budget and version',async()=>{
  const p=adminPage();const request=p.run('refreshTrialPolicy()');p.reply({enabled:false,days:1,max_claims:100,used:7,remaining:93,version:3});await request;
  assert.ok(p.element('trial-policy-count').textContent.includes('7 из 100'));
  assert.ok(p.element('trial-policy-editor').innerHTML.includes('data-trial-version="3"'));
});

test('coupon form preserves its immutable code and shows consumed reservations',()=>{
  const p=adminPage();const markup=p.run("couponForm({id:'c1',code:'SAVE10',percent:10,enabled:false,expires_at:0,max_uses:5,used:3,plan_id:null,protocol:'all',version:2})");
  assert.ok(markup.includes('SAVE10')&&markup.includes('3 из 5')&&markup.includes('data-version="2"'));
  assert.ok(!markup.includes('name="code"'));
});

test('referral policy editor shows bounded minimums and immutable invitation budget',()=>{
  const p=adminPage();const html=p.run("referralPolicyForm({enabled:false,days:7,max_invites:100,min_rub:199,min_stars:150,version:4})");
  assert.ok(html.includes('data-referral-version="4"'));assert.ok(html.includes('name="min_rub"'));assert.ok(html.includes('name="min_stars"'));assert.ok(html.includes('max="30"'));
  p.run("$('exit').onclick()");assert.equal(p.element('referral-policy-editor').innerHTML,'');
});

function diagnosticReport(overrides={}) {
  return {format:1,generated_at:100,status:'needs_attention',warnings:['worker_not_observed'],worker:{state:'not_observed',last_success:null},queues:{issuance:{pending:2,due:1,with_error:1,oldest_age_seconds:200}},expiry:{pending:0},servers:{xui:{assignable:1,cached_api_ok_24h:0},wg:{assignable:0,cached_api_ok_24h:0}},...overrides};
}

test('diagnostics escapes unknown codes and shows cached rather than live evidence',async()=>{
  const p=adminPage();const request=p.run('refreshDiagnostics()');
  p.reply(diagnosticReport({warnings:['<img src=x>']}));await request;
  const html=p.element('diagnostics-report').innerHTML;
  assert.ok(html.includes('&lt;img src=x&gt;'));assert.ok(!html.includes('<img'));
  assert.ok(html.includes('недавние успешные API-проверки'));
  assert.equal(JSON.parse(p.element('diagnostics-json').value).format,1);
});

test('logout rejects and clears a pending diagnostics report',async()=>{
  const p=adminPage();const request=p.run('refreshDiagnostics()');p.run("$('exit').onclick()");
  p.replyAt(0,diagnosticReport());await request;
  assert.equal(p.element('diagnostics-report').innerHTML,'');assert.equal(p.element('diagnostics-json').value,'');assert.equal(p.element('diagnostics-error').textContent,'');
});

test('newer diagnostics cannot be replaced by an older response',async()=>{
  const p=adminPage();const first=p.run('refreshDiagnostics()'),second=p.run('refreshDiagnostics()');
  p.replyAt(1,diagnosticReport({generated_at:200}));await second;p.replyAt(0,diagnosticReport({generated_at:100}));await first;
  assert.equal(JSON.parse(p.element('diagnostics-json').value).generated_at,200);
});

const adsPolicyFixture={accepting:false,amount:300,hours:24,version:0,instructions:'Только модерация'};
test('модерация рекламы отображается безопасно и отдельно от оплаты',async()=>{
  const p=adminPage();const pending=p.run('refreshAdsAdmin()');p.replyAt(0,adsPolicyFixture);
  p.replyAt(1,{requests:[{id:'ad-a',user_id:1,channel:'channel_name',state:'pending',comment:'<img src=x>',reason:'',version:1,amount:300,hours:24}],total:1,page:1,page_size:20});await pending;
  assert.ok(p.element('ads-admin-list').innerHTML.includes('&lt;img'));assert.ok(p.element('ads-admin-list').innerHTML.includes('data-ads-id="ad-a"'));
  assert.equal(p.element('ads-accepting').checked,false);assert.equal(p.element('ads-price').value,300);
});

test('выход администратора отсекает поздний ответ списка рекламодателей',async()=>{
  const p=adminPage();const pending=p.run('refreshAdsAdmin()');p.run("$('exit').onclick()");
  p.replyAt(0,adsPolicyFixture);p.replyAt(1,{requests:[{comment:'private-text'}],total:1,page:1,page_size:20});
  await assert.rejects(pending,/Сессия/);assert.equal(p.element('ads-admin-list').innerHTML,'');assert.equal(p.element('ads-instructions').value,'');
});

test('новая страница модерации не заменяется старым ответом',async()=>{
  const p=adminPage();const first=p.run('refreshAdsAdmin()');p.run('adsPage=2');const second=p.run('refreshAdsAdmin()');
  p.replyAt(2,adsPolicyFixture);p.replyAt(3,{requests:[],total:30,page:2,page_size:20});await second;
  p.replyAt(0,adsPolicyFixture);p.replyAt(1,{requests:[],total:30,page:1,page_size:20});await first;
  assert.match(p.element('ads-admin-page').textContent,/Страница 2/);
});

class AdsPaymentFields {
  constructor(form){this.fields=form.fields;}
  get(name){return this.fields[name] ?? null;}
  has(name){return name in this.fields;}
}
const paymentFormSource=`({dataset:{adsPayment:'ad-a',kind:'receipt',version:'2',paymentVersion:'0'},fields:{amount:'300',reference:'bank/receipt',note:'Проверено',checked:'on'},querySelector(){return paymentButton;}})`;

test('форма ручного учёта показывает поступление или возврат и экранирует основания',()=>{
  const p=adminPage();
  const unpaid=p.run("adsPaymentForm({id:'ad-a',state:'approved',version:2,amount:300})");
  assert.match(unpaid,/data-kind="receipt"/);assert.match(unpaid,/name="checked"/);
  const confirmed=p.run("adsPaymentForm({id:'ad-a',state:'approved',version:2,amount:300,payment:{status:'confirmed',version:1,entries:[{kind:'receipt',amount:300,reference:'<img>',note:'<script>'}]}})");
  assert.match(confirmed,/data-kind="refund"/);assert.ok(!confirmed.includes('<script>'));assert.match(confirmed,/&lt;img&gt;/);
  const refunded=p.run("adsPaymentForm({id:'ad-a',state:'approved',version:2,amount:300,payment:{status:'refunded',version:2}})");
  assert.ok(!refunded.includes('<form'));assert.match(refunded,/Полный возврат учтён вручную/);
});

test('повтор ручного учёта после ошибки сохраняет UUID операции и версии',async()=>{
  const p=adminPage({FormData:AdsPaymentFields});p.run('var paymentButton={disabled:false};');
  const first=p.run('saveAdsPayment('+paymentFormSource+')');p.replyAt(0,{detail:'Ответ потерян'},502);await first;
  const second=p.run('saveAdsPayment('+paymentFormSource+')');p.replyAt(1,{detail:'Ответ потерян'},502);await second;
  assert.equal(p.bodies[0].operation_id,p.bodies[1].operation_id);
  assert.equal(p.bodies[0].payment_version,0);assert.equal(p.bodies[0].request_version,2);assert.equal(p.bodies[0].checked,true);
  assert.equal(p.run('adsPaymentIntents.size'),1);assert.equal(p.run('paymentButton.disabled'),false);
});

test('выход администратора очищает платёжное намерение и поздний ответ',async()=>{
  const p=adminPage({FormData:AdsPaymentFields});p.run('var paymentButton={disabled:false};');
  const request=p.run('saveAdsPayment('+paymentFormSource+')');p.run("$('exit').onclick()");
  p.replyAt(0,{status:'confirmed'});await request;
  assert.equal(p.run('adsPaymentIntents.size'),0);assert.equal(p.element('ads-admin-list').innerHTML,'');
  assert.equal(p.element('ads-admin-error').textContent,'');assert.equal(p.requests.length,2);assert.equal(p.requests[1],'/shop/api/admin/auth/logout');
});

test('успешный ручной учёт обновляет очередь без повторного сохранения',async()=>{
  const p=adminPage({FormData:AdsPaymentFields});p.run('var paymentButton={disabled:false};');
  const request=p.run('saveAdsPayment('+paymentFormSource+')');
  await p.run('saveAdsPayment('+paymentFormSource+')');assert.equal(p.requests.length,1);
  p.replyAt(0,{status:'confirmed'});await new Promise(setImmediate);
  p.replyAt(1,adsPolicyFixture);p.replyAt(2,{requests:[],total:0,page:1,page_size:20});await request;
  assert.equal(p.run('adsPaymentIntents.size'),0);assert.equal(p.requests.filter(x=>x.includes('/payments/')).length,1);
});

test('размещение предлагает только свободную проверенную площадку и фиксирует ручную проверку',()=>{
  const p=adminPage();p.run("adsSitesCache=[{id:'free',name:'<Свободная>',version:3,enabled:true,verified:true,occupied:false},{id:'busy',name:'Занята',enabled:true,verified:true,occupied:true}]");
  const html=p.run("adsPlacementForm({id:'ad-a',state:'approved',version:2,payment:{status:'confirmed',version:1}})");
  assert.match(html,/value="free"/);assert.ok(!html.includes('value="busy"'));assert.match(html,/&lt;Свободная&gt;/);assert.match(html,/name="checked"/);
  const unpaid=p.run("adsPlacementForm({id:'ad-a',state:'approved',payment:{status:'unpaid'}})");assert.ok(!unpaid.includes('<form'));
});

test('просроченное активное размещение требует остановки и скрывает возврат',()=>{
  const p=adminPage();const row="{id:'ad-a',state:'approved',version:2,payment:{status:'confirmed',version:1},placement:{state:'active',version:1,site_name:'Площадка',remaining_seconds:0,attention_due:true,actions:[{action:'start',note:'<script>'}]}}";
  const html=p.run('adsPlacementForm('+row+')');assert.match(html,/требуется подтвердить фактическую остановку/);assert.match(html,/value="finish"/);assert.ok(!html.includes('<script>'));
  const money=p.run('adsPaymentForm('+row+')');assert.ok(!money.includes('<form'));
});

const placementFormSource=`({dataset:{adsPlacement:'ad-a',requestVersion:'2',paymentVersion:'1',placementVersion:'1'},fields:{action:'pause',note:'Показ остановлен',checked:'on'},querySelector(){return placementButton;}})`;
test('потерянный ответ размещения повторяется с тем же UUID',async()=>{
  const p=adminPage({FormData:AdsPaymentFields});p.run('var placementButton={disabled:false};');
  const first=p.run('saveAdsPlacement('+placementFormSource+')');p.replyAt(0,{detail:'Ответ потерян'},502);await first;
  const second=p.run('saveAdsPlacement('+placementFormSource+')');p.replyAt(1,{detail:'Ответ потерян'},502);await second;
  assert.equal(p.bodies[0].operation_id,p.bodies[1].operation_id);assert.equal(p.bodies[0].placement_version,1);assert.equal(p.bodies[0].checked,true);
  assert.equal(p.run('adsPlacementIntents.size'),1);
});

test('выход очищает площадки и игнорирует позднее подтверждение размещения',async()=>{
  const p=adminPage({FormData:AdsPaymentFields});p.run("var placementButton={disabled:false};adsPrivateSites=[{note:'private-check'}];$('ads-site-note').value='private-check';");
  const pending=p.run('saveAdsPlacement('+placementFormSource+')');p.run("$('exit').onclick()");p.replyAt(0,{state:'paused'});await pending;
  assert.equal(p.run('adsPlacementIntents.size'),0);assert.equal(p.run('adsPrivateSites.length'),0);assert.equal(p.element('ads-site-note').value,'');assert.equal(p.element('ads-admin-error').textContent,'');
});

test('запоздалый список площадок не восстанавливает приватные основания после выхода',async()=>{
  const p=adminPage();const pending=p.run('refreshAdsSites()');p.run("$('exit').onclick()");p.replyAt(0,{sites:[{id:'site',name:'Сайт',note:'private-note'}]});await pending;
  assert.equal(p.element('ads-sites-list').innerHTML,'');assert.equal(p.run('adsPrivateSites.length'),0);
});

test('ошибка сохранения площадки сохраняет её UUID и не создаёт другой объект',async()=>{
  const p=adminPage();p.element('ads-site-name').value='Площадка';p.element('ads-site-note').value='Проверено';
  const first=p.run("$('ads-site-form').onsubmit({preventDefault(){}})");p.replyAt(0,{detail:'Ответ потерян'},502);await first;
  const second=p.run("$('ads-site-form').onsubmit({preventDefault(){}})");p.replyAt(1,{detail:'Ответ потерян'},502);await second;
  assert.equal(p.requests[0],p.requests[1]);assert.equal(p.bodies[0].version,0);assert.equal(p.bodies[0].verified,false);assert.equal(p.bodies[0].enabled,false);
});

test('список площадок экранирует основания и выбор редактора подставляет текущую версию',async()=>{
  const p=adminPage();const pending=p.run('refreshAdsSites()');p.replyAt(0,{sites:[{id:'s1',name:'<Имя>',note:'<script>',enabled:true,verified:false,occupied:true,version:4}]});await pending;
  assert.match(p.element('ads-sites-list').innerHTML,/&lt;script&gt;/);assert.ok(!p.element('ads-sites-list').innerHTML.includes('<script>'));
  p.run("$('ads-sites-list').onclick({target:{closest(){return {dataset:{adsSite:'s1'}}}}})");
  assert.equal(p.run('adsSiteEditor.version'),4);assert.equal(p.element('ads-site-name').value,'<Имя>');assert.equal(p.element('ads-site-verified').checked,false);
});


function stopQueueFixture(changes={}){
  return {requests:[],counts:{due:1,upcoming:2,paused:3},total:0,page:1,page_size:20,generated_at:10000,...changes};
}
test('очередь остановок экранирует канал и показывает просрочку с ручным подтверждением',async()=>{
  const p=adminPage();const pending=p.run('refreshStopQueue()');
  p.replyAt(0,stopQueueFixture({requests:[{id:'ad-a',user_id:1,channel:'<img>',state:'approved',version:2,queue_status:'due',overdue_seconds:12,payment:{status:'confirmed',version:1},placement:{state:'active',version:1,site_name:'<Площадка>',expected_stop_at:9900,remaining_seconds:0,attention_due:true}}],total:1}));
  await pending;const html=p.element('ads-stop-list').innerHTML;
  assert.match(html,/&lt;img&gt;/);assert.match(html,/Просрочка: 12 сек/);assert.match(html,/data-ads-queue="true"/);assert.match(html,/value="finish"/);
  assert.ok(!html.includes('<img>'));assert.match(p.element('ads-stop-counts').textContent,/Требуют остановки: 1/);
});

test('выход отсекает позднюю очередь остановок и очищает её данные',async()=>{
  const p=adminPage();const pending=p.run('refreshStopQueue()');p.run("$('exit').onclick()");
  p.replyAt(0,stopQueueFixture());await pending;
  assert.equal(p.element('ads-stop-list').innerHTML,'');assert.equal(p.element('ads-stop-counts').textContent,'');assert.equal(p.element('ads-stop-error').textContent,'');
});

test('новая страница очереди не заменяется старым ответом',async()=>{
  const p=adminPage();const first=p.run('refreshStopQueue()');p.run('adsStopPage=2');const second=p.run('refreshStopQueue()');
  p.replyAt(1,stopQueueFixture({total:30,page:2}));await second;
  p.replyAt(0,stopQueueFixture({total:30,page:1}));await first;
  assert.match(p.element('ads-stop-page').textContent,/Страница 2/);
});

test('ошибка обновления удаляет прежние команды и счётчики очереди',async()=>{
  const p=adminPage();p.element('ads-stop-list').innerHTML='stale-private-data';p.element('ads-stop-counts').textContent='old-counts';
  const pending=p.run('refreshStopQueue()');p.replyAt(0,{detail:'Нет связи'},502);await pending;
  assert.equal(p.element('ads-stop-list').innerHTML,'');assert.equal(p.element('ads-stop-counts').textContent,'');
  assert.equal(p.element('ads-stop-error').textContent,'Нет связи');assert.equal(p.element('ads-stop-next').disabled,true);
});

test('подтверждение из очереди использует прежний API и обновляет саму очередь',async()=>{
  const p=adminPage({FormData:AdsPaymentFields});p.run('var stopButton={disabled:false};');
  const pending=p.run("saveAdsPlacement({dataset:{adsQueue:'true',adsPlacement:'ad-a',requestVersion:'2',paymentVersion:'1',placementVersion:'3'},fields:{action:'finish',note:'Остановка проверена',checked:'on'},querySelector(){return stopButton;}})");
  assert.ok(p.requests[0].endsWith('/placement/finish'));assert.equal(p.bodies[0].placement_version,3);assert.equal(p.bodies[0].checked,true);
  p.replyAt(0,{state:'finished'});await new Promise(setImmediate);
  assert.match(p.requests[1],/stop-queue/);p.replyAt(1,stopQueueFixture({counts:{due:0,upcoming:0,paused:0}}));await pending;
  assert.equal(p.run('adsPlacementIntents.size'),0);assert.match(p.element('ads-stop-list').innerHTML,/размещений нет/);
});

test('ошибка подтверждения из очереди сохраняет UUID и выводится рядом с очередью',async()=>{
  const p=adminPage({FormData:AdsPaymentFields});p.run('var stopButton={disabled:false};');
  const source="saveAdsPlacement({dataset:{adsQueue:'true',adsPlacement:'ad-a',requestVersion:'2',paymentVersion:'1',placementVersion:'1'},fields:{action:'pause',note:'Пауза проверена',checked:'on'},querySelector(){return stopButton;}})";
  const first=p.run(source);p.replyAt(0,{detail:'Ответ потерян'},502);await first;
  const second=p.run(source);p.replyAt(1,{detail:'Ответ потерян'},502);await second;
  assert.equal(p.bodies[0].operation_id,p.bodies[1].operation_id);assert.equal(p.element('ads-stop-error').textContent,'Ответ потерян');
  assert.equal(p.element('ads-admin-error').textContent,'');
});


test('форма брони берёт версию и предлагает только свободные проверенные площадки',()=>{
  const p=adminPage();p.run("adsSitesCache=[{id:'free',name:'<Имя>',version:1,enabled:true,verified:true,occupied:false},{id:'busy',name:'Занята',enabled:true,verified:true,occupied:true}]");
  const html=p.run("adsBookingForm({id:'ad-a',state:'approved',version:2,payment:{status:'confirmed',version:1}})");
  assert.match(html,/datetime-local/);assert.match(html,/&lt;Имя&gt;/);assert.ok(!html.includes('value="busy"'));assert.match(html,/data-booking-version="0"/);
});

test('будущая бронь скрывает запуск и возврат до отмены или окна старта',()=>{
  const p=adminPage();const row="{id:'ad-a',state:'approved',version:2,payment:{status:'confirmed',version:1},booking:{state:'reserved',version:1,site_id:'s1',site_name:'Площадка',starts_at:10000,ends_at:13600,can_start:false}}";
  assert.ok(!p.run('adsPlacementForm('+row+')').includes('data-ads-placement'));
  assert.ok(!p.run('adsPaymentForm('+row+')').includes('data-ads-payment'));
  assert.match(p.run('adsBookingForm('+row+')'),/data-action="cancel"/);
});

test('потеря ответа бронирования сохраняет UUID и серверные версии',async()=>{
  const p=adminPage({FormData:AdsPaymentFields});p.run("var bookingButton={disabled:false};adsSitesCache=[{id:'s1',version:4}];");
  const source="saveAdsBooking({dataset:{adsBooking:'ad-a',action:'reserve',requestVersion:'2',paymentVersion:'1',bookingVersion:'0'},fields:{site_id:'s1',starts_at:'2027-01-01T12:00',note:'План'},querySelector(){return bookingButton;}})";
  const first=p.run(source);p.replyAt(0,{detail:'Нет ответа'},502);await first;
  const second=p.run(source);p.replyAt(1,{detail:'Нет ответа'},502);await second;
  assert.equal(p.bodies[0].operation_id,p.bodies[1].operation_id);assert.equal(p.bodies[0].site_version,4);assert.equal(p.bodies[0].booking_version,0);
  assert.ok(Number.isInteger(p.bodies[0].starts_at));assert.equal(p.bodies[0].hours,undefined);
});

test('выход очищает намерение брони и отсекает поздний календарь',async()=>{
  const p=adminPage();p.run("adsBookingIntents.set('private-note','id')");
  const pending=p.run('refreshBookingCalendar()');p.run("$('exit').onclick()");p.replyAt(0,{sites:[],requests:[],total:0,page_size:20});await pending;
  assert.equal(p.run('adsBookingIntents.size'),0);assert.equal(p.element('ads-bookings-list').innerHTML,'');assert.equal(p.element('ads-bookings-error').textContent,'');
});

test('новая страница календаря не заменяется старым ответом',async()=>{
  const p=adminPage();const first=p.run('refreshBookingCalendar()');p.run('adsBookingPage=2');const second=p.run('refreshBookingCalendar()');
  p.replyAt(1,{sites:[],requests:[],total:30,page_size:20});await second;
  p.replyAt(0,{sites:[],requests:[],total:30,page_size:20});await first;
  assert.match(p.element('ads-bookings-page').textContent,/Страница 2/);
});

test('ошибка чтения календаря убирает прежние формы',async()=>{
  const p=adminPage();p.element('ads-bookings-list').innerHTML='stale-private-note';const pending=p.run('refreshBookingCalendar()');
  p.replyAt(0,{detail:'Нет связи'},502);await pending;assert.equal(p.element('ads-bookings-list').innerHTML,'');assert.equal(p.element('ads-bookings-error').textContent,'Нет связи');
});

test('запуск по брони передаёт её версию и обновляет календарь',async()=>{
  const p=adminPage({FormData:AdsPaymentFields});p.run("var bookingStartButton={disabled:false};adsSitesCache=[{id:'s1',version:2}];");
  const pending=p.run("saveAdsPlacement({dataset:{adsCalendar:'true',adsPlacement:'ad-a',requestVersion:'2',paymentVersion:'1',placementVersion:'0',bookingVersion:'3'},fields:{action:'start',site_id:'s1',note:'Проверено',checked:'on'},querySelector(){return bookingStartButton;}})");
  assert.equal(p.bodies[0].booking_version,3);p.replyAt(0,{state:'active'});await new Promise(setImmediate);
  assert.match(p.requests[1],/ads\/bookings/);p.replyAt(1,{sites:[],requests:[],total:0,page_size:20});await pending;
});

test('календарь экранирует закрытое основание и имя площадки',async()=>{
  const p=adminPage();const pending=p.run('refreshBookingCalendar()');
  p.replyAt(0,{sites:[],requests:[{id:'ad-a',channel:'<img>',state:'approved',version:2,payment:{status:'confirmed',version:1},booking:{state:'reserved',version:1,site_name:'<Сайт>',starts_at:10000,ends_at:13600,can_start:false,actions:[{action:'reserve',note:'<script>'}]}}],total:1,page_size:20});
  await pending;const html=p.element('ads-bookings-list').innerHTML;
  assert.match(html,/&lt;script&gt;/);assert.match(html,/&lt;img&gt;/);assert.ok(!html.includes('<script>'));
});


test('отмена из календаря передаёт только версионные данные и обновляет список',async()=>{
  const p=adminPage({FormData:AdsPaymentFields});p.run('var cancelBookingButton={disabled:false};');
  const pending=p.run("saveAdsBooking({dataset:{adsCalendar:'true',adsBooking:'ad-a',action:'cancel',requestVersion:'2',paymentVersion:'1',bookingVersion:'1'},fields:{note:'Перенос'},querySelector(){return cancelBookingButton;}})");
  assert.ok(p.requests[0].endsWith('/booking/cancel'));assert.equal(p.bodies[0].site_id,undefined);assert.equal(p.bodies[0].booking_version,1);
  p.replyAt(0,{state:'canceled'});await new Promise(setImmediate);p.replyAt(1,{sites:[],requests:[],total:0,page_size:20});await pending;
  assert.equal(p.run('adsBookingIntents.size'),0);
});

test('действующая бронь предлагает перенос без предварительной отмены',()=>{
  const p=adminPage();p.run("adsSitesCache=[{id:'s1',name:'<Новая>',version:2,enabled:true,verified:true,occupied:false}]");
  const html=p.run("adsBookingForm({id:'ad-a',state:'approved',version:2,payment:{status:'confirmed',version:1},booking:{state:'reserved',version:3,site_name:'Старая',starts_at:10000,ends_at:13600}})");
  assert.match(html,/data-action="reschedule"/);assert.match(html,/data-action="cancel"/);assert.match(html,/data-booking-version="3"/);assert.match(html,/&lt;Новая&gt;/);
});

test('календарь помечает обе формы брони для обновления своего списка',async()=>{
  const p=adminPage();const pending=p.run('refreshBookingCalendar()');
  p.replyAt(0,{sites:[{id:'s1',name:'Свободная',enabled:true,verified:true,occupied:false}],requests:[{id:'ad-a',channel:'channel_name',state:'approved',version:2,payment:{status:'confirmed',version:1},booking:{state:'reserved',version:1,site_name:'Площадка',starts_at:10000,ends_at:13600,can_start:false}}],total:1,page_size:20});await pending;
  const forms=p.element('ads-bookings-list').innerHTML.match(/<form[^>]+>/g);
  assert.equal(forms.length,2);assert.ok(forms.every(f=>f.includes('data-ads-calendar="true"')));
});

const transferSource="saveAdsBooking({dataset:{adsCalendar:'true',adsBooking:'ad-a',action:'reschedule',requestVersion:'2',paymentVersion:'1',bookingVersion:'3'},fields:{site_id:'s1',starts_at:'2027-01-02T12:00',note:'Перенос'},querySelector(){return transferButton;}})";
test('повтор переноса сохраняет UUID и не вызывает отмену',async()=>{
  const p=adminPage({FormData:AdsPaymentFields});p.run("var transferButton={disabled:false};adsSitesCache=[{id:'s1',version:2}];");
  const first=p.run(transferSource);p.replyAt(0,{detail:'Нет ответа'},502);await first;
  const second=p.run(transferSource);p.replyAt(1,{detail:'Нет ответа'},502);await second;
  assert.equal(p.bodies[0].operation_id,p.bodies[1].operation_id);assert.ok(p.requests.every(x=>x.endsWith('/booking/reschedule')));assert.equal(p.bodies[0].booking_version,3);
});

test('успешный перенос обновляет календарь одним изменением',async()=>{
  const p=adminPage({FormData:AdsPaymentFields});p.run("var transferButton={disabled:false};adsSitesCache=[{id:'s1',version:2}];");
  const pending=p.run(transferSource);p.replyAt(0,{state:'reserved',version:4});await new Promise(setImmediate);
  assert.ok(p.requests[1].includes('/ads/bookings'));p.replyAt(1,{sites:[],requests:[],total:0,page_size:20});await pending;
  assert.equal(p.requests.filter(x=>x.includes('/booking/')).length,1);assert.equal(p.run('adsBookingIntents.size'),0);
});

function financeFixture(changes={}){
  return {period:{start:1000,end:2000},cash:{received:300,refunded:0,net_received:300},expenses:{amount:50,recorded_count:1,voided_count:0,entries:[],total:0,page:1,page_size:20},cash_after_recorded_expenses:250,is_net_profit:false,current_paid_requests:{not_started:{requests:1,contract_amount:300,remaining_seconds:3600},early_stopped:{requests:1,contract_amount:300,remaining_seconds:1200}},...changes};
}

test('финансовая сводка не называет остаток прибылью и экранирует частные записи',async()=>{
  const p=adminPage();const pending=p.run('refreshAdsFinance()');
  p.replyAt(0,financeFixture({expenses:{amount:0,entries:[{id:'e1',amount:50,category:'other',incurred_at:1000,reference:'<img>',note:'<script>',state:'void',void_reason:'<исправление>'}],total:1,page_size:20}}));await pending;
  assert.match(p.element('ads-finance-summary').textContent,/Чистая прибыль не рассчитывается/);
  assert.match(p.element('ads-finance-obligations').innerHTML,/неоказанным остатком/);
  const html=p.element('ads-expense-list').innerHTML;assert.match(html,/&lt;script&gt;/);assert.match(html,/&lt;исправление&gt;/);assert.ok(!html.includes('<script>'));
});

test('выход очищает финансовые поля и отсекает запоздалую сводку',async()=>{
  const p=adminPage();p.run("adsExpenseIntents.set('private-note','id');$('ads-expense-note').value='private-note'");
  const pending=p.run('refreshAdsFinance()');p.run("$('exit').onclick()");p.replyAt(0,financeFixture());await pending;
  assert.equal(p.element('ads-finance-summary').textContent,'');assert.equal(p.element('ads-expense-note').value,'');assert.equal(p.run('adsExpenseIntents.size'),0);
});

test('новая страница расходов не заменяется старым ответом',async()=>{
  const p=adminPage();const first=p.run('refreshAdsFinance()');p.run('adsFinancePage=2');const second=p.run('refreshAdsFinance()');
  p.replyAt(1,financeFixture({expenses:{amount:0,entries:[],total:30,page_size:20}}));await second;
  p.replyAt(0,financeFixture());await first;assert.match(p.element('ads-finance-page').textContent,/Страница 2/);
});

test('ошибка чтения убирает устаревшие расходы и денежные суммы',async()=>{
  const p=adminPage();p.element('ads-expense-list').innerHTML='private-old-note';p.element('ads-finance-summary').textContent='old-money';
  const pending=p.run('refreshAdsFinance()');p.replyAt(0,{detail:'Нет связи'},502);await pending;
  assert.equal(p.element('ads-expense-list').innerHTML,'');assert.equal(p.element('ads-finance-summary').textContent,'');assert.equal(p.element('ads-finance-error').textContent,'Нет связи');
});

function fillExpense(p){p.element('ads-expense-amount').value='50';p.element('ads-expense-category').value='server';p.element('ads-expense-date').value='2026-11-01T12:00';p.element('ads-expense-reference').value='cost/abc';p.element('ads-expense-note').value='Проверено';}

test('потеря ответа расхода сохраняет UUID без повторного списания',async()=>{
  const p=adminPage();fillExpense(p);
  const first=p.run("$('ads-expense-form').onsubmit({preventDefault(){}})");p.replyAt(0,{detail:'Ответ потерян'},502);await first;
  const second=p.run("$('ads-expense-form').onsubmit({preventDefault(){}})");p.replyAt(1,{detail:'Ответ потерян'},502);await second;
  assert.equal(p.bodies[0].operation_id,p.bodies[1].operation_id);assert.equal(p.bodies[0].amount,50);assert.ok(p.requests.every(x=>x.endsWith('/ads/expenses')));
});

test('исключение расхода передаёт версию и причину и обновляет сводку',async()=>{
  const p=adminPage({FormData:AdsPaymentFields});p.run('var expenseVoidButton={disabled:false};');
  const pending=p.run("$('ads-expense-list').onsubmit({preventDefault(){},target:{closest(){return {dataset:{adsExpense:'e1',version:'1'},fields:{note:'Ошибка ввода'},querySelector(){return expenseVoidButton;}};}}})");
  assert.ok(p.requests[0].endsWith('/expenses/e1/void'));assert.equal(p.bodies[0].version,1);assert.equal(p.bodies[0].amount,undefined);
  p.replyAt(0,{state:'void'});await new Promise(setImmediate);p.replyAt(1,financeFixture());await pending;
  assert.equal(p.run('adsExpenseIntents.size'),0);assert.match(p.element('ads-expense-status').textContent,/движения денег нет/);
});

test('одна граница периода не отправляет неоднозначный запрос',async()=>{
  const p=adminPage();p.element('ads-finance-start').value='2026-11-01T12:00';await p.run('refreshAdsFinance()');
  assert.equal(p.requests.length,0);assert.match(p.element('ads-finance-error').textContent,/обе границы/);
});

test('ответ сохранения не стирает новые изменения формы расхода',async()=>{
  const p=adminPage();fillExpense(p);const pending=p.run("$('ads-expense-form').onsubmit({preventDefault(){}})");
  p.element('ads-expense-note').value='Новая ещё не сохранённая запись';p.replyAt(0,{id:'e1'});await new Promise(setImmediate);
  p.replyAt(1,financeFixture());await pending;assert.equal(p.element('ads-expense-note').value,'Новая ещё не сохранённая запись');
});

function commerceFixture(events={source:'first_observed_transition',recording_started_at:100,paid:2,issued:1}){
 return {accounts_registered:0,orders:{created:0,paid:0,issued:0,renewals:0,payment_rate_percent:null,issuance_rate_percent:null,confirmed_amount_by_currency:{},by_status:{}},trials:{claimed:0,issued:0,converted_to_paid:0,conversion_rate_percent:null},period:{start:50,end:200},events};
}
test('аналитика показывает события отдельно и предупреждает о неполной истории',async()=>{
 const p=adminPage();const pending=p.run("role='finance';refreshCommerceAnalytics()");
 p.reply(commerceFixture());await pending;
 const html=p.element('commerce-analytics-details').innerHTML;
 assert.match(html,/Подтверждения оплаты<strong>2/);assert.match(html,/Выдачи доступа<strong>1/);
 assert.match(html,/только часть/);assert.match(html,/не является конверсией/);
 assert.match(p.element('commerce-analytics-cards').innerHTML,/Коммерческие заказы<strong>0/);
});
test('период до начала истории и отсутствие данных не выдаются за полную историю',()=>{
 const p=adminPage();
 assert.match(p.run("commerceEventsMarkup({period:{start:0,end:100},events:{source:'first_observed_transition',recording_started_at:100,paid:0,issued:0}})"),/целиком предшествует/);
 assert.match(p.run("commerceEventsMarkup({period:{start:100,end:200},events:{source:'first_observed_transition',recording_started_at:100,paid:0,issued:0}})"),/в пределах/);
 assert.match(p.run("commerceEventsMarkup({period:{start:0,end:200}})"),/недоступна/);
 assert.match(p.run("commerceEventsMarkup({period:{start:0,end:200},events:{source:'first_observed_transition',recording_started_at:100,paid:'<img>',issued:0}})"),/недоступна/);
});
test('устаревший ответ аналитики и ошибка не оставляют прежние события',async()=>{
 const p=adminPage();const first=p.run("role='owner';refreshCommerceAnalytics()");const second=p.run("refreshCommerceAnalytics()");
 const newer=commerceFixture();newer.events.paid=8;p.replyAt(1,newer);await second;
 p.replyAt(0,commerceFixture());await first;assert.match(p.element('commerce-analytics-details').innerHTML,/оплаты<strong>8/);
 const failed=p.run("refreshCommerceAnalytics()");p.replyAt(2,{detail:'Нет связи'},503);await failed;
 assert.equal(p.element('commerce-analytics-details').innerHTML,'');
});

for (const mode of ['wgdashboard','agent']) test(`WireGuard form submits explicit ${mode} profile`,async()=>{
  class Fields {constructor(form){this.fields=form.fields;}get(name){return this.fields[name];}}
  const page=adminPage({FormData:Fields});
  const request=page.run(`$('server-form').onsubmit({preventDefault(){},target:{fields:{name:'WireGuard',protocol:'wg',capacity:'0',wg_api:'${mode}',url:'https://wg.example/prefix',token:'test-key',configuration:'wg0',api:'remnawave',remnawave_token:'stale-secret'},querySelector(){return {disabled:false};}}})`);
  assert.deepEqual(page.bodies[0].settings,{url:'https://wg.example/prefix',token:'test-key',...(mode==='wgdashboard'?{api:'wgdashboard',configuration:'wg0'}:{})});
  page.replyAt(0,uiServer({protocol:'wg',profile:mode==='agent'?'wireguard_agent':mode}));
  await new Promise(setImmediate);
  page.replyAt(1,{servers:[uiServer({protocol:'wg',profile:mode==='agent'?'wireguard_agent':mode})]});
  await request;
  assert.ok(page.element('server-list').innerHTML.includes(mode==='agent'?'Pushok WG-агент':'WGDashboard'));
});

test('WireGuard mode changes and invalid interfaces cannot silently save',async()=>{
  class Fields {constructor(form){this.fields=form.fields;}get(name){return this.fields[name];}}
  for (const fields of [{wg_api:'agent',url:''},{wg_api:'wgdashboard',url:'https://wg.example',configuration:'../wg0'},{wg_api:'unknown',url:'https://wg.example'}]){
    const page=adminPage({FormData:Fields});
    await page.run(`editingServer={id:'a',protocol:'wg',profile:'wgdashboard'}; $('server-form').onsubmit({preventDefault(){},target:{fields:${JSON.stringify(fields)},querySelector(){return {disabled:false};}}})`);
    assert.equal(page.requests.length,0);
    assert.notEqual(page.element('server-form-error').textContent,'');
  }
});

test('editing WGDashboard keeps blank credentials and the selected adapter',async()=>{
  class Fields {constructor(form){this.fields=form.fields;}get(name){return this.fields[name];}}
  const page=adminPage({FormData:Fields});
  const form=page.element('server-form');form.elements={};
  for(const name of ['name','location','capacity','protocol','api','wg_api','url']) form.elements[name]={value:''};
  page.element('server-dialog').showModal=function(){this.open=true;};
  page.run("openServer({id:'a',name:'WG',protocol:'wg',profile:'wgdashboard',state:'active',version:3})");
  assert.equal(form.elements.wg_api.value,'wgdashboard');
  page.run('nextServerStep(); nextServerStep()');
  const request=page.run(`$('server-form').onsubmit({preventDefault(){},target:{fields:{name:'Renamed',capacity:'0',wg_api:'wgdashboard',url:'',token:'',configuration:''},querySelector(){return {disabled:false};}}})`);
  assert.deepEqual(page.bodies[0],{name:'Renamed',location:'',capacity:0,version:3,state:'active'});
  page.replyAt(0,{});await new Promise(setImmediate);page.replyAt(1,{servers:[]});await request;
});

test('panel-specific fields and limitations follow current protocol selection',()=>{
  const page=adminPage();
  page.element('server-protocol').value='wg';page.element('server-wg-api').value='wgdashboard';
  page.element('server-api').value='remnawave';page.run('showServerFields()');
  assert.equal(page.element('server-wg-configuration').hidden,false);
  assert.equal(page.element('server-wg-interface').disabled,false);
  assert.equal(page.element('server-wgdashboard-help').hidden,false);
  assert.equal(page.element('server-remnawave-help').hidden,true);
  page.element('server-protocol').value='xui';page.run('showServerFields()');
  assert.equal(page.element('server-wg-configuration').hidden,true);
  assert.equal(page.element('server-wg-interface').disabled,true);
  assert.equal(page.element('server-wgdashboard-help').hidden,true);
  assert.equal(page.element('server-remnawave-help').hidden,false);
});


test('Hysteria2 form sends only S-UI credentials and connection fields',async()=>{
  class Fields {constructor(form){this.fields=form.fields;}get(name){return this.fields[name];}}
  const page=adminPage({FormData:Fields});
  const request=page.run(`$('server-form').onsubmit({preventDefault(){},target:{fields:{name:'HY',protocol:'hysteria2',capacity:'0',url:'https://panel.example/app',hy_token:'hy-token',hy_inbound_id:'7',hy_address:'vpn.example',hy_port:'443',hy_sni:'vpn.example',hy_obfs_password:'salamander-secret',api:'clients',api_token:'stale-secret',vmess_token:'stale-vmess',token:'stale-wg'},querySelector(){return {disabled:false};}}})`);
  assert.equal(page.bodies[0].protocol,'hysteria2');
  assert.deepEqual(page.bodies[0].settings,{url:'https://panel.example/app',api:'sui',token:'hy-token',inbound_id:7,address:'vpn.example',port:443,sni:'vpn.example',obfs_password:'salamander-secret'});
  page.replyAt(0,{});await new Promise(setImmediate);page.replyAt(1,{servers:[]});await request;
});

test('Hysteria2 selection hides other adapters and keeps coupon scope',()=>{
  const page=adminPage();page.element('server-protocol').value='hysteria2';
  page.element('server-api').value='remnawave';page.element('server-wg-api').value='wgdashboard';
  page.run('showServerFields()');
  assert.equal(page.element('server-hysteria2-fields').hidden,false);
  for(const id of ['server-wg-fields','server-xui-fields','server-vmess-fields','server-remnawave-help','server-wgdashboard-help']) assert.equal(page.element(id).hidden,true);
  assert.match(page.run("couponForm({id:'c',protocol:'hysteria2',version:1,code:'HY10',percent:10,max_uses:100,used:0,enabled:true})"),/value="hysteria2" selected/);
});

test('blank Hysteria2 edit preserves encrypted profile, partial fields cannot overwrite it',async()=>{
  class Fields {constructor(form){this.fields=form.fields;}get(name){return this.fields[name];}}
  const page=adminPage({FormData:Fields});
  const request=page.run(`editingServer={id:'a',protocol:'hysteria2',profile:'sui_hysteria2',state:'active',version:3};$('server-form').onsubmit({preventDefault(){},target:{fields:{name:'Renamed',capacity:'0',url:''},querySelector(){return {disabled:false};}}})`);
  assert.deepEqual(page.bodies[0],{name:'Renamed',location:'',capacity:0,state:'active',version:3});
  page.replyAt(0,{});await new Promise(setImmediate);page.replyAt(1,{servers:[]});await request;
  const second=adminPage({FormData:Fields});
  await second.run(`editingServer={id:'a',protocol:'hysteria2',state:'active',version:3};$('server-form').onsubmit({preventDefault(){},target:{fields:{name:'HY',url:'',hy_obfs_password:'changed'},querySelector(){return {disabled:false};}}})`);
  assert.equal(second.requests.length,0);assert.notEqual(second.element('server-form-error').textContent,'');
});


test('TUIC form submits explicit settings without other adapter credentials',async()=>{
  class Fields {constructor(form){this.fields=form.fields;}get(name){return this.fields[name];}}
  const page=adminPage({FormData:Fields});
  const request=page.run(`$('server-form').onsubmit({preventDefault(){},target:{fields:{name:'TUIC',protocol:'tuic',capacity:'0',url:'https://panel.example/app',tuic_token:'tuic-token',tuic_inbound_id:'7',tuic_address:'vpn.example',tuic_port:'443',tuic_sni:'vpn.example',tuic_congestion:'bbr',tuic_udp:'quic',hy_token:'stale',hy_obfs_password:'stale',vmess_token:'stale'},querySelector(){return {disabled:false};}}})`);
  assert.equal(page.bodies[0].protocol,'tuic');
  assert.deepEqual(page.bodies[0].settings,{url:'https://panel.example/app',api:'sui',token:'tuic-token',inbound_id:7,address:'vpn.example',port:443,sni:'vpn.example',congestion_control:'bbr',udp_relay_mode:'quic'});
  page.replyAt(0,{});await new Promise(setImmediate);page.replyAt(1,{servers:[]});await request;
});

test('TUIC fields and coupon scope follow selection',()=>{
  const page=adminPage();page.element('server-protocol').value='tuic';page.run('showServerFields()');
  assert.equal(page.element('server-tuic-fields').hidden,false);
  for(const id of ['server-wg-fields','server-xui-fields','server-vmess-fields','server-hysteria2-fields']) assert.equal(page.element(id).hidden,true);
  assert.match(page.run("couponForm({id:'c',protocol:'tuic',version:1,code:'TUIC10',percent:10,max_uses:100,used:0,enabled:true})"),/value="tuic" selected/);
});

test('TUIC blank edit preserves profile and partial settings are refused',async()=>{
  class Fields {constructor(form){this.fields=form.fields;}get(name){return this.fields[name];}}
  const page=adminPage({FormData:Fields});
  const request=page.run(`editingServer={id:'a',protocol:'tuic',profile:'sui_tuic',state:'active',version:3};$('server-form').onsubmit({preventDefault(){},target:{fields:{name:'Renamed',capacity:'0',url:'',tuic_congestion:'cubic',tuic_udp:'native'},querySelector(){return {disabled:false};}}})`);
  assert.deepEqual(page.bodies[0],{name:'Renamed',location:'',capacity:0,state:'active',version:3});
  page.replyAt(0,{});await new Promise(setImmediate);page.replyAt(1,{servers:[]});await request;
  for(const fields of [{tuic_congestion:'bbr'},{tuic_udp:'quic'},{tuic_token:'changed'}]){
    const other=adminPage({FormData:Fields});
    await other.run(`editingServer={id:'a',protocol:'tuic',state:'active',version:3};$('server-form').onsubmit({preventDefault(){},target:{fields:${JSON.stringify({name:'TUIC',url:'',...fields})},querySelector(){return {disabled:false};}}})`);
    assert.equal(other.requests.length,0);assert.notEqual(other.element('server-form-error').textContent,'');
  }
});

function prepareServerWizard(page) {
  const form=page.element('server-form'); form.elements={};
  for (const key of ['name','location','capacity','protocol','api','wg_api','url']) form.elements[key]={value:''};
  return form;
}

test('server wizard starts with metadata, navigates back and never sends credentials in review',()=>{
  const page=adminPage(), form=prepareServerWizard(page);
  page.run('openServer()');
  assert.equal(page.run('serverWizardStep'),1);
  assert.equal(page.element('server-step-2').hidden,true);
  form.elements.name.value='<img src=x>';form.elements.location.value='FI';
  form.elements.token={value:'secret-token'};form.elements.url.value='https://panel.example/private';
  page.run('nextServerStep(); nextServerStep()');
  assert.equal(page.run('serverWizardStep'),3);
  assert.equal(page.element('server-save').hidden,false);
  assert.match(page.element('server-review').innerHTML,/&lt;img/);
  assert.doesNotMatch(page.element('server-review').innerHTML,/<img|secret-token|private|panel.example/);
  page.run("$('server-back').onclick()");
  assert.equal(page.run('serverWizardStep'),2);
  assert.equal(form.elements.token.value,'secret-token');
  assert.equal(page.requests.length,0);
});

test('invalid wizard field stops navigation and Enter advances without saving',async()=>{
  const page=adminPage();prepareServerWizard(page);page.run('openServer()');
  page.element('server-step-1').querySelectorAll=()=>[{disabled:false,closest:()=>null,reportValidity:()=>false}];
  page.run('nextServerStep()');assert.equal(page.run('serverWizardStep'),1);
  page.element('server-step-1').querySelectorAll=()=>[];
  await page.run("$('server-form').onsubmit({preventDefault(){}})");
  assert.equal(page.run('serverWizardStep'),2);assert.equal(page.requests.length,0);
});

test('credential dialog chooses token fields and sends no endpoint or stale password',async()=>{
  const page=adminPage();
  page.run("openServerCredentials({id:'node-a',name:'<name>',profile:'wgdashboard',version:4})");
  assert.equal(page.element('server-credentials-legacy').hidden,true);
  page.element('server-credentials-token').value='new-token';
  page.element('server-credentials-password').value='stale-password';
  const request=page.run("$('server-credentials-form').onsubmit({preventDefault(){}})");
  await page.run("$('server-credentials-form').onsubmit({preventDefault(){}})");
  assert.equal(page.requests.length,1);
  assert.equal(page.requests[0],'/shop/api/admin/servers/node-a/credentials');
  assert.deepEqual(page.bodies[0],{version:4,token:'new-token'});
  page.replyAt(0,{});await new Promise(setImmediate);
  assert.equal(page.element('server-credentials-token').value,'');
  assert.equal(page.element('server-credentials-password').value,'');
  page.replyAt(1,{servers:[]});await request;
  assert.match(page.element('admin-notice').textContent,/Проверьте API/);
});

test('legacy credential dialog preserves login when changing password only',async()=>{
  const page=adminPage();
  page.run("openServerCredentials({id:'node-a',name:'Legacy',profile:'legacy_vless',version:2})");
  assert.equal(page.element('server-credentials-token').disabled,true);
  await page.run("$('server-credentials-form').onsubmit({preventDefault(){}})");
  assert.equal(page.requests.length,0);
  page.element('server-credentials-password').value='new-password';
  const request=page.run("$('server-credentials-form').onsubmit({preventDefault(){}})");
  assert.deepEqual(page.bodies[0],{version:2,password:'new-password'});
  page.replyAt(0,{detail:'Версия изменилась'},409);await request;
  assert.equal(page.element('server-credentials-error').textContent,'Версия изменилась');
  assert.equal(page.element('server-credentials-save').disabled,false);
});

test('closed credential request cannot close or refill a newly opened dialog',async()=>{
  const page=adminPage();
  page.run("openServerCredentials({id:'a',name:'A',profile:'wgdashboard',version:1})");
  page.element('server-credentials-token').value='old-secret';
  const request=page.run("$('server-credentials-form').onsubmit({preventDefault(){}})");
  page.run("$('server-credentials-close').onclick(); openServerCredentials({id:'b',name:'B',profile:'wgdashboard',version:2})");
  page.element('server-credentials-token').value='new-draft';
  page.replyAt(0,{});await request;
  assert.equal(page.element('server-credentials-dialog').open,true);
  assert.equal(page.element('server-credentials-token').value,'new-draft');
  assert.equal(page.requests.length,1);
});

test('logout clears credential inputs and ignores late server errors',async()=>{
  const page=adminPage();
  page.run("openServerCredentials({id:'a',name:'A',profile:'wgdashboard',version:1})");
  page.element('server-credentials-token').value='secret-token';
  const request=page.run("$('server-credentials-form').onsubmit({preventDefault(){}})");
  page.run("$('exit').onclick()");page.replyAt(0,{detail:'late error'},403);await request;
  assert.equal(page.element('server-credentials-token').value,'');
  assert.equal(page.element('server-credentials-error').textContent,'');
  assert.equal(page.element('server-credentials-dialog').open,false);
});

function updatesStatus(extra={}) {
 return {current_version:'v0.055',state:'available',checking:false,
  settings:{enabled:true,check_time:'21:00',timezone:'Europe/Moscow',version:1},scheduler_running:true,
  last_attempt:1794502800,last_success:1794502800,next_check:1794506400,error_code:null,stale:false,
  release:{tag:'v0.056',notes:'Исправления',url:'javascript:alert(1)',guide_url:'https://evil.example'},
  can_open_guide:true,update_available:true,...extra};
}
test('updates show safe release notes and a pinned manual upgrade guide',async()=>{
 const page=adminPage(),req=page.run("role='owner';refreshUpdates()");
 const data=updatesStatus();data.release.notes='<img src=x onerror=alert(1)>';
 page.reply(data);await req;
 assert.equal(page.element('updates-notes').textContent,data.release.notes);
 assert.equal(page.element('updates-notes').innerHTML,'');
 assert.equal(page.element('updates-open').hidden,false);
 page.run("$('updates-open').onclick()");
 assert.equal(page.element('updates-dialog').open,true);
 assert.equal(page.element('updates-guide-link').href,'https://github.com/mrkoster1988/Shop-VPN-/blob/v0.056/docs/UPGRADE.md');
 assert.equal(page.requests.length,1);
});
test('updates never offer an unverified stale or invalid release',async()=>{
 const page=adminPage();page.run("role='owner'");
 for(const data of [updatesStatus({can_open_guide:false,stale:true,error_code:'network_error',state:'check_failed'}),
   updatesStatus({release:{tag:'../../main',notes:'bad'}})]){
  const req=page.run('refreshUpdates()');page.reply(data);await req;
  assert.equal(page.element('updates-open').hidden,true);
  page.run("$('updates-open').onclick()");
  assert.equal(page.element('updates-dialog').open,false);
 }
});
test('updates settings stay editable across polling and stale revision is preserved',async()=>{
 const page=adminPage(),req=page.run("role='owner';refreshUpdates()");page.reply(updatesStatus());await req;
 page.element('updates-time').value='22:30';page.run("$('updates-schedule').oninput()");
 const poll=page.run('refreshUpdates()');page.reply(updatesStatus({settings:{enabled:true,check_time:'19:00',timezone:'UTC',version:2}}));await poll;
 assert.equal(page.element('updates-time').value,'22:30');assert.equal(page.run('updatesVersion'),1);
 const save=page.run("$('updates-schedule').onsubmit({preventDefault(){}})");
 assert.deepEqual(page.bodies.at(-1),{enabled:true,check_time:'22:30',timezone:'Europe/Moscow',version:1});
 page.replyAt(page.requests.length-1,{detail:'Расписание уже изменено'},409);await save;
 assert.match(page.element('updates-error').textContent,/уже изменено/);
 assert.equal(page.element('updates-time').value,'22:30');
});
test('updates check serializes double click and reenables controls after failure',async()=>{
 const page=adminPage();page.run("role='owner'");
 const req=page.run("$('updates-check').onclick()");await page.run("$('updates-check').onclick()");
 assert.equal(page.requests.length,1);assert.equal(page.element('updates-check').disabled,true);
 page.replyAt(0,{detail:'Подождите минуту'},429);await req;
 assert.equal(page.element('updates-check').disabled,false);
 assert.equal(page.element('updates-open').hidden,true);
 assert.match(page.element('updates-error').textContent,/Подождите/);
});
test('updates ignore old reads overtaken by a manual check',async()=>{
 const page=adminPage(),old=page.run("role='owner';refreshUpdates()");
 const manual=page.run("$('updates-check').onclick()");
 page.replyAt(1,updatesStatus());await manual;
 page.replyAt(0,updatesStatus({state:'not_checked',release:null,can_open_guide:false}));await old;
 assert.equal(page.element('updates-latest').textContent,'v0.056');
});
test('updates hide data after logout and reject late responses and other roles',async()=>{
 const page=adminPage(),req=page.run("role='owner';refreshUpdates()");
 page.run("$('exit').onclick()");page.replyAt(0,updatesStatus());await req;
 assert.equal(page.element('updates-panel').hidden,true);
 assert.equal(page.element('updates-notes').textContent,'');
 const count=page.requests.length;
 await page.run("role='support';refreshUpdates()");assert.equal(page.requests.length,count);
 page.run("$('updates-open').onclick()");assert.equal(page.element('updates-dialog').open,false);
});
test('updates failed status read closes upgrade guide until refreshed',async()=>{
 const page=adminPage(),req=page.run("role='owner';refreshUpdates()");page.reply(updatesStatus());await req;
 page.run("$('updates-open').onclick()");
 const failed=page.run('refreshUpdates()');page.replyAt(page.requests.length-1,{detail:'Нет доступа'},403);await failed;
 assert.equal(page.element('updates-dialog').open,false);
 assert.equal(page.element('updates-open').hidden,true);assert.equal(page.run('updatesCache'),null);
});
test('updates schedule conflict can be recovered by loading saved settings',async()=>{
 const page=adminPage(),req=page.run("role='owner';refreshUpdates()");page.reply(updatesStatus());await req;
 page.element('updates-time').value='23:00';page.run("$('updates-schedule').oninput()");
 const reset=page.run("$('updates-reset').onclick()");
 page.reply(updatesStatus({settings:{enabled:false,check_time:'18:00',timezone:'UTC',version:2}}));await reset;
 assert.equal(page.element('updates-time').value,'18:00');assert.equal(page.run('updatesVersion'),2);
});
test('updates date falls back to explicitly labelled UTC for a newer server timezone',()=>{
 const page=adminPage();
 assert.match(page.run("updatesDate(1794502800,'Unknown/New-Timezone')"),/ UTC$/);
});
