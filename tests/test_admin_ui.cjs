const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');

function adminPage() {
  const elements = new Map();
  function element(id) {
    if (!elements.has(id)) elements.set(id, {
      innerHTML:'', textContent:'', value:'', open:false, disabled:false,
      listeners:{},
      replaceChildren() { this.innerHTML = ''; this.textContent = ''; },
      addEventListener(name, handler) { this.listeners[name] = handler; },
      close() { this.open = false; this.listeners.close?.(); },
    });
    return elements.get(id);
  }
  let reply;
  const context = vm.createContext({
    document:{getElementById:element, querySelector:()=>({content:'/shop'})},
    URLSearchParams, FormData,
    fetch:()=>new Promise(resolve=>{ reply = body => resolve({ok:true,json:async()=>body}); }),
  });
  vm.runInContext(fs.readFileSync('app/static/admin.js','utf8'), context);
  vm.runInContext("token='admin-test-token'", context);
  return {element, run:code=>vm.runInContext(code,context), reply:body=>reply(body)};
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
