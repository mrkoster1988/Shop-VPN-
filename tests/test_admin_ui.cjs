const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');

function adminPage() {
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
  const replies = [], requests = [];
  const context = vm.createContext({
    document:{getElementById:element, querySelector:()=>({content:'/shop'})},
    URLSearchParams, FormData,
    fetch:url=>new Promise(resolve=>{ requests.push(url); replies.push(body=>resolve({ok:true,json:async()=>body})); }),
  });
  vm.runInContext(fs.readFileSync('app/static/admin.js','utf8'), context);
  vm.runInContext("token='admin-test-token'", context);
  return {element, run:code=>vm.runInContext(code,context), reply:body=>replies.at(-1)(body), replyAt:(index,body)=>replies[index](body), requests};
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
