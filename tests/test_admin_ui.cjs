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
    URLSearchParams, FormData:options.FormData || FormData,
    fetch:(url,opts)=>new Promise(resolve=>{ requests.push(url); bodies.push(opts.body ? JSON.parse(opts.body) : null); replies.push(body=>resolve({ok:true,json:async()=>body})); }),
  });
  vm.runInContext(fs.readFileSync('app/static/admin.js','utf8'), context);
  vm.runInContext("token='admin-test-token'", context);
  return {element, run:code=>vm.runInContext(code,context), reply:body=>replies.at(-1)(body), replyAt:(index,body)=>replies[index](body), requests,bodies};
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
