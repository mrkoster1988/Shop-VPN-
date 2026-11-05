const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');

function adminPage(options={}) {
  const elements = new Map();
  function element(id) {
    if (!elements.has(id)) elements.set(id, {
      innerHTML:'', textContent:'', value:'', open:false, disabled:false,
      listeners:{}, reset() {},
      replaceChildren() { this.innerHTML = ''; this.textContent = ''; },
      addEventListener(name, handler) { this.listeners[name] = handler; },
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

test('logout rejects an in-flight response with customer data', async () => {
  const page = adminPage();
  const request = page.run("api('users')");
  page.run("$('exit').onclick()");
  page.reply({users:[{email:'customer@example.com'}]});
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
  assert.ok(html.includes('Недавние успешные API-проверки'));
  assert.equal(JSON.parse(p.element('diagnostics-json').value).format,1);
});

test('logout rejects and clears a pending diagnostics report',async()=>{
  const p=adminPage();const request=p.run('refreshDiagnostics()');p.run("$('exit').onclick()");
  p.reply(diagnosticReport());await request;
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
  assert.ok(!refunded.includes('<form'));assert.match(refunded,/Размещение не запущено/);
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
  assert.equal(p.element('ads-admin-error').textContent,'');assert.equal(p.requests.length,1);
});

test('успешный ручной учёт обновляет очередь без повторного сохранения',async()=>{
  const p=adminPage({FormData:AdsPaymentFields});p.run('var paymentButton={disabled:false};');
  const request=p.run('saveAdsPayment('+paymentFormSource+')');
  await p.run('saveAdsPayment('+paymentFormSource+')');assert.equal(p.requests.length,1);
  p.replyAt(0,{status:'confirmed'});await new Promise(setImmediate);
  p.replyAt(1,adsPolicyFixture);p.replyAt(2,{requests:[],total:0,page:1,page_size:20});await request;
  assert.equal(p.run('adsPaymentIntents.size'),0);assert.equal(p.requests.filter(x=>x.includes('/payments/')).length,1);
});
