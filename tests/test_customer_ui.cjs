const test=require('node:test');
const assert=require('node:assert/strict');
const vm=require('node:vm');
const fs=require('node:fs');
const tick=()=>new Promise(setImmediate);

async function customerPage(options={}) {
  const clock={now:Date.now()}, timers=new Map(), elements=new Map(), calls=[], overrides=new Map(), intervals=[];
  let timerId=0,elementId=0;
  const catalog={protocols:[{id:'xui',name:'VLESS',available:true},{id:'wg',name:'WireGuard',available:true}],plans:[{id:'month',name:'Месяц',days:30,price:199,version:1}],currency:'RUB',support:'Поддержка: @support',bot_username:'',terms:'Условия',payment_mode:'manual'};
  const subscription={id:'sub-a',protocol:'xui',state:'active',expires:Math.floor(clock.now/1000)+3600};
  const state={customer:{id:1,email:'buyer@example.com',subscriptions:[subscription],orders:[]},devices:[],catalog,config:{protocol:'xui',config:'vless://private-secret@vpn.example'},copied:[]};
  if(options.anonymous)state.customer=null;
  function element(id) {
    if (!elements.has(id)) elements.set(id,{value:'',textContent:'',innerHTML:'',hidden:false,open:false,disabled:false,attrs:{},listeners:{},children:[],
      reset(){},select(){this.selected=true;},showModal(){this.open=true;},close(){if(this.open){this.open=false;this.listeners.close?.();}},
      addEventListener(name,handler){this.listeners[name]=handler;},replaceChildren(){this.innerHTML='';this.textContent='';this.children=[];},
      removeAttribute(name){delete this.attrs[name];delete this[name];},setAttribute(name,value){this.attrs[name]=value;},append(...items){this.children.push(...items);},
      querySelector(){return element(id+'-input');},scrollIntoView(){this.scrolled=true;}});
    return elements.get(id);
  }
  const fetch=async(url,opts)=>{
    calls.push({url,opts});
    if (overrides.has(url)) return overrides.get(url)(url,opts);
    let body,ok=true,status=200;
    if(url==='/shop/api/catalog')body=state.catalog;
    else if(url==='/shop/api/me'){body=state.customer||{detail:'Войдите'};ok=!!state.customer;status=ok?200:401;}
    else if(url==='/shop/api/auth/logout'){state.customer=null;body={ok:true};}
    else if(url.endsWith('/devices'))body={devices:state.devices,max_extra:4};
    else if(url.endsWith('/config'))body=state.config;
    else throw Error('Unexpected '+url);
    return {ok,status,json:async()=>body};
  };
  const context=vm.createContext({document:{getElementById:element,querySelector:()=>({content:'/shop'}),querySelectorAll:()=>[],createElement:tag=>element('created-'+tag+'-'+(++elementId))},
    fetch,URL,URLSearchParams,FormData:class {constructor(form){this.fields=form.fields;}get(name){return this.fields[name];}},crypto:require('node:crypto').webcrypto,Date:class extends Date { static now(){return clock.now;} },
    navigator:{platform:'Linux x86_64',userAgent:'Android',clipboard:{writeText:async value=>state.copied.push(value)}},
    window:{confirm:()=>true},location:{search:options.search || ''},history:{replaceState(){}},
    setTimeout:(fn,delay)=>{const id=++timerId;timers.set(id,{fn,delay});return id;},clearTimeout:id=>timers.delete(id),setInterval:fn=>{intervals.push(fn);return intervals.length;}});
  vm.runInContext(fs.readFileSync('app/static/app.js','utf8'),context);
  await tick();
  return {state,element,calls,timers,clock,intervals,run:code=>vm.runInContext(code,context),
    defer(path){let resolve;overrides.set(path,()=>new Promise(r=>resolve=r));return (body,status=200)=>resolve({ok:status<400,status,json:async()=>body});},
    respond(path,body,status=200){overrides.set(path,async()=>({ok:status<400,status,json:async()=>body}));}};
}

test('guided VLESS connection loads private settings with platform steps and lazy QR',async()=>{
  const p=await customerPage();await p.run("openConnection('sub-a')");
  assert.equal(p.element('config-dialog').open,true);
  assert.ok(p.element('connection-steps').innerHTML.includes('буфера обмена'));
  assert.equal(p.element('config-platform').value,'android');
  assert.equal(p.element('config-qr').src,undefined);
  assert.equal(p.element('download-config').href,'/shop/api/subscriptions/sub-a/download');
  p.element('toggle-config-qr').onclick();
  assert.equal(p.element('config-qr').src,'/shop/api/subscriptions/sub-a/qr');
  assert.equal(p.element('config-qr-panel').hidden,false);
  p.element('config-qr').onerror();
  assert.ok(p.element('config-status').textContent.includes('копирование'));
  await p.element('copy-config').onclick();
  assert.deepEqual(p.state.copied,['vless://private-secret@vpn.example']);
});

test('WireGuard Linux instructions use a conf file and platform can change',async()=>{
  const p=await customerPage();p.state.customer.subscriptions[0].protocol='wg';p.state.config={protocol:'wg',config:'[Interface]\nPrivateKey = secret'};
  await p.run('refreshMe()');await p.run("openConnection('sub-a')");
  p.element('config-platform').value='linux';p.element('config-platform').onchange();
  assert.ok(p.element('connection-steps').innerHTML.includes('.conf'));
  assert.ok(p.element('connection-steps').innerHTML.includes('администратору'));
  assert.ok(!p.element('connection-steps').innerHTML.includes('буфера обмена'));
});

test('closing the connection clears secrets and ignores a late response',async()=>{
  const p=await customerPage();const reply=p.defer('/shop/api/subscriptions/sub-a/config');
  const opening=p.run("openConnection('sub-a')");p.element('config-dialog').close();
  reply(p.state.config);await opening;
  assert.equal(p.element('config-dialog').open,false);
  assert.equal(p.element('config-value').value,'');assert.equal(p.element('download-config').href,undefined);
});

test('a late response for an earlier subscription cannot replace a newer connection',async()=>{
  const p=await customerPage();p.state.customer.subscriptions.push({...p.state.customer.subscriptions[0],id:'sub-b'});
  const reply=p.defer('/shop/api/subscriptions/sub-a/config');const first=p.run("openConnection('sub-a')");
  await p.run("openConnection('sub-b')");reply({protocol:'xui',config:'old-secret'});await first;
  assert.equal(p.element('config-value').value,p.state.config.config);
  assert.equal(p.element('download-config').href,'/shop/api/subscriptions/sub-b/download');
});

test('logout clears loaded secrets, QR, payment and account data',async()=>{
  const p=await customerPage();await p.run("openConnection('sub-a')");p.element('toggle-config-qr').onclick();
  p.element('payment-result').innerHTML='private-payment';await p.element('logout').onclick();
  for(const id of ['identity','subscriptions','orders','payment-result'])assert.equal(p.element(id).innerHTML,'');
  assert.equal(p.element('config-value').value,'');assert.equal(p.element('config-qr').src,undefined);
  assert.equal(p.element('download-config').href,undefined);assert.equal(p.element('config-dialog').open,false);
});

test('logout rejects pending configuration and account responses',async()=>{
  const p=await customerPage();const configReply=p.defer('/shop/api/subscriptions/sub-a/config');const meReply=p.defer('/shop/api/me');
  const opening=p.run("openConnection('sub-a')");const refresh=p.run('refreshMe()');
  const rejected=assert.rejects(refresh,/Сессия изменилась/);
  const previous=p.state.customer;await p.element('logout').onclick();
  configReply(p.state.config);meReply(previous);await opening;await rejected;
  assert.equal(p.element('config-value').value,'');assert.equal(p.element('identity').textContent,'');
});

test('expired access cannot expose, copy or download cached settings',async()=>{
  const p=await customerPage();await p.run("openConnection('sub-a')");p.clock.now+=7200000;
  await p.element('copy-config').onclick();let prevented=false;p.element('download-config').onclick({preventDefault(){prevented=true;}});
  assert.equal(p.state.copied.length,0);assert.ok(prevented);assert.equal(p.element('config-value').value,'');
});

test('expired subscription is rejected before requesting a configuration',async()=>{
  const p=await customerPage();p.clock.now+=7200000;
  await assert.rejects(p.run("openConnection('sub-a')"),/срок подписки/);
  assert.equal(p.calls.filter(x=>x.url.endsWith('/config')).length,0);
});

test('clipboard denial offers manual selection and malformed config stays hidden',async()=>{
  const p=await customerPage();await p.run("openConnection('sub-a')");
  p.run("navigator.clipboard.writeText=async()=>{throw Error('denied')}");await p.element('copy-config').onclick();
  assert.equal(p.element('config-details').open,true);assert.equal(p.element('config-value').selected,true);
  p.state.config={protocol:'wg',config:'wrong-secret'};await p.run("openConnection('sub-a')");
  assert.equal(p.element('config-value').value,'');assert.equal(p.element('copy-config').disabled,true);
});

test('renewal selects the original protocol and unavailable renewal stays closed',async()=>{
  const p=await customerPage();
  const event={target:{closest(selector){return selector==='[data-renew]'?{dataset:{renew:'wg'}}:null;}}};
  p.element('subscriptions').onclick(event);
  assert.equal(p.run('protocol'),'wg');assert.equal(p.element('plans').scrolled,true);
  assert.ok(p.element('toast').textContent.includes('Оставшиеся дни'));
  p.state.catalog.protocols[0].available=false;
  p.element('subscriptions').onclick({target:{closest(selector){return selector==='[data-renew]'?{dataset:{renew:'xui'}}:null;}}});
  assert.equal(p.run('protocol'),'wg');assert.ok(p.element('toast').textContent.includes('поддержку'));
});

test('lost authorization clears an already displayed configuration',async()=>{
  const p=await customerPage();await p.run("openConnection('sub-a')");p.state.customer=null;
  await p.run('refreshMe()');assert.equal(p.element('config-dialog').open,false);assert.equal(p.element('config-value').value,'');
});

test('switching accounts invalidates a pending response from the previous customer',async()=>{
  const p=await customerPage();const reply=p.defer('/shop/api/subscriptions/sub-a/config');
  const opening=p.run("openConnection('sub-a')");p.state.customer={id:2,email:'other@example.com',subscriptions:[],orders:[]};
  await p.run('refreshMe()');reply(p.state.config);await opening;
  assert.equal(p.element('identity').textContent,'other@example.com');assert.equal(p.element('config-value').value,'');
  assert.equal(p.element('config-dialog').open,false);
});

test('the expiry timer removes the configuration without waiting for another click',async()=>{
  const p=await customerPage();await p.run("openConnection('sub-a')");
  const timeout=[...p.timers.values()].find(t=>t.delay>6000);assert.ok(timeout);p.clock.now+=7200000;timeout.fn();
  assert.equal(p.element('config-value').value,'');assert.equal(p.element('download-config').href,undefined);
  assert.equal(p.element('copy-config').disabled,true);
});

test('all five platforms have three instructions for all supported protocols',async()=>{
  const p=await customerPage();
  for(const protocol of ['xui','wg','vmess']) {
    p.state.customer.subscriptions[0].protocol=protocol;p.state.config={protocol,config:'private-config'};
    await p.run('refreshMe()');await p.run("openConnection('sub-a')");
    for(const platform of ['android','ios','windows','macos','linux']) {
      p.element('config-platform').value=platform;p.element('config-platform').onchange();
      assert.equal((p.element('connection-steps').innerHTML.match(/<li>/g)||[]).length,3);
      assert.ok(p.element('connection-steps').innerHTML.includes(protocol==='wg'?'.conf':protocol==='vmess'?'VMess':'VLESS'));
    }
  }
});

test('renewal refresh extends an open connection and a long timer never expires it early',async()=>{
  const p=await customerPage();await p.run("openConnection('sub-a')");
  p.state.customer.subscriptions[0].expires+=90*86400;await p.run('refreshMe()');
  const timeout=[...p.timers.values()].find(t=>t.delay===2147483647);assert.ok(timeout);
  p.clock.now+=2147483647;timeout.fn();
  assert.equal(p.element('config-value').value,p.state.config.config);
  assert.equal(p.element('copy-config').disabled,false);
});

test('device manager escapes names and connects to the separate device endpoint',async()=>{
  const p=await customerPage();p.state.devices=[{id:'device-a',name:'<img src=x>',protocol:'xui',state:'active',ready:true,expires:p.state.customer.subscriptions[0].expires}];
  await p.run("openDevices('sub-a')");assert.ok(p.element('devices-list').innerHTML.includes('&lt;img src=x&gt;'));
  const device=JSON.stringify(p.state.devices[0]);await p.run("openConnection('sub-a',"+device+")");
  assert.equal(p.element('download-config').href,'/shop/api/devices/device-a/download');
  p.element('toggle-config-qr').onclick();assert.equal(p.element('config-qr').src,'/shop/api/devices/device-a/qr');
  p.state.devices[0].ready=false;p.state.devices[0].state='revoking';await p.run('refreshDevices()');
  assert.equal(p.element('config-dialog').open,false);assert.equal(p.element('config-value').value,'');
});

test('closing device management rejects an older list response',async()=>{
  const p=await customerPage();const reply=p.defer('/shop/api/subscriptions/sub-a/devices');
  const opening=p.run("openDevices('sub-a')");p.element('devices-dialog').close();reply({devices:[{name:'private-name',id:'a'}],max_extra:4});await opening;
  assert.equal(p.element('devices-list').innerHTML,'');
});

test('failed device creation retries the same request identifier',async()=>{
  const p=await customerPage();await p.run("openDevices('sub-a')");
  p.respond('/shop/api/subscriptions/sub-a/devices',{detail:'Try again'},503);
  const event={preventDefault(){},target:{fields:{name:'Phone'},querySelector(){return {disabled:false};}}};
  await p.element('device-form').onsubmit(event);await p.element('device-form').onsubmit(event);
  const posts=p.calls.filter(c=>c.url.endsWith('/devices')&&c.opts.method==='POST');
  assert.equal(posts.length,2);assert.deepEqual(JSON.parse(posts[0].opts.body),JSON.parse(posts[1].opts.body));
});

test('device revocation requires confirmation and uses DELETE',async()=>{
  const p=await customerPage();await p.run("openDevices('sub-a')");
  const event={target:{closest(selector){return selector==='[data-device-revoke]'?{dataset:{deviceRevoke:'device-a'}}:null;}}};
  p.run('window.confirm=()=>false');await p.element('devices-list').onclick(event);assert.ok(!p.calls.some(c=>c.opts.method==='DELETE'));
  p.run('window.confirm=()=>true');p.respond('/shop/api/devices/device-a',{ok:true});await p.element('devices-list').onclick(event);
  assert.ok(p.calls.some(c=>c.url==='/shop/api/devices/device-a'&&c.opts.method==='DELETE'));
});

test('catalog shows the paid profile allowance including primary-only plans',async()=>{
  const p=await customerPage();p.state.catalog.plans[0].device_limit=0;p.run('renderPlans()');
  assert.ok(p.element('plan-list').innerHTML.includes('Только основной профиль'));
  p.state.catalog.plans[0].device_limit=8;p.run('renderPlans()');
  assert.ok(p.element('plan-list').innerHTML.includes('до 8 дополнительных профилей'));
});

test('device manager hides creation at quota and keeps grandfathered profiles visible',async()=>{
  const p=await customerPage();p.state.devices=[{id:'old-device',name:'Phone',protocol:'xui',state:'active',ready:true,expires:p.state.customer.subscriptions[0].expires}];
  p.respond('/shop/api/subscriptions/sub-a/devices',{devices:p.state.devices,max_extra:0});
  await p.run("openDevices('sub-a')");
  assert.equal(p.element('device-form').hidden,true);
  assert.ok(p.element('devices-list').innerHTML.includes('data-device-config="old-device"'));
  assert.ok(p.element('devices-status').textContent.includes('Уже выданные профили работают'));
});

test('rotation confirmation can cancel without sending a request',async()=>{
  const p=await customerPage();await p.run("openDevices('sub-a')");p.run('window.confirm=()=>false');
  const event={target:{closest(selector){return selector==='[data-device-rotate]'?{dataset:{deviceRotate:'device-a'}}:null;}}};
  await p.element('devices-list').onclick(event);
  assert.ok(!p.calls.some(c=>c.url.endsWith('/rotate')));
});

test('rotation retry keeps its request ID after an ambiguous response',async()=>{
  const p=await customerPage();await p.run("openDevices('sub-a')");p.respond('/shop/api/devices/device-a/rotate',{detail:'Retry'},503);
  const event={target:{closest(selector){return selector==='[data-device-rotate]'?{dataset:{deviceRotate:'device-a'}}:null;}}};
  await p.element('devices-list').onclick(event);await p.element('devices-list').onclick(event);
  const requests=p.calls.filter(c=>c.url.endsWith('/rotate'));
  assert.equal(requests.length,2);assert.deepEqual(JSON.parse(requests[0].opts.body),JSON.parse(requests[1].opts.body));
});

test('rotating profile hides connection and rotation controls but can be revoked',async()=>{
  const p=await customerPage();p.state.devices=[{id:'device-a',name:'Phone',protocol:'xui',state:'rotating',ready:false,expires:p.state.customer.subscriptions[0].expires}];
  await p.run("openDevices('sub-a')");const markup=p.element('devices-list').innerHTML;
  assert.ok(markup.includes('Меняем ключ')&&markup.includes('data-device-revoke'));
  assert.ok(!markup.includes('data-device-config')&&!markup.includes('data-device-rotate'));
});

test('primary rotation hides main connection while extra device remains accessible',async()=>{
  const p=await customerPage();await p.run("openConnection('sub-a')");p.state.customer.subscriptions[0].primary_key_state='rotating';
  await p.run('refreshMe()');assert.equal(p.element('config-dialog').open,false);
  const markup=p.element('subscriptions').innerHTML;
  assert.ok(markup.includes('Меняем основной ключ')&&markup.includes('data-devices'));
  assert.ok(!markup.includes('data-config=')&&!markup.includes('data-primary-rotate='));
  const device={id:'device-a',name:'Phone',expires:p.state.customer.subscriptions[0].expires};
  await p.run("openConnection('sub-a',"+JSON.stringify(device)+")");
  assert.equal(p.element('download-config').href,'/shop/api/devices/device-a/download');
});

test('primary rotation requires confirmation and reuses request after failure',async()=>{
  const p=await customerPage();p.respond('/shop/api/subscriptions/sub-a/rotate',{detail:'Retry'},503);
  const event={target:{closest(selector){return selector==='[data-primary-rotate]'?{dataset:{primaryRotate:'sub-a'}}:null;}}};
  p.run('window.confirm=()=>false');await p.element('subscriptions').onclick(event);
  assert.ok(!p.calls.some(c=>c.url.endsWith('/rotate')));
  p.run('window.confirm=()=>true');await p.element('subscriptions').onclick(event);await p.element('subscriptions').onclick(event);
  const posts=p.calls.filter(c=>c.url.endsWith('/rotate'));assert.equal(posts.length,2);
  assert.deepEqual(JSON.parse(posts[0].opts.body),JSON.parse(posts[1].opts.body));
});

test('main connection cannot reopen from stale markup during rotation',async()=>{
  const p=await customerPage();p.state.customer.subscriptions[0].primary_key_state='rotating';
  await assert.rejects(p.run("openConnection('sub-a')"));
  assert.equal(p.calls.filter(c=>c.url.endsWith('/config')).length,0);
});

test('link management copies the private URL and clears it when closed',async()=>{
  const p=await customerPage();const state={enabled:true,follow_rotation:false,version:1,url:'https://example.com/shop/sub/private-token'};
  p.respond('/shop/api/subscriptions/sub-a/link',state);await p.run("openLink('sub-a')");
  await p.element('copy-link').onclick();assert.equal(p.state.copied[0],state.url);
  p.element('link-dialog').close();assert.equal(p.element('link-value').value,'');assert.equal(p.element('copy-link').disabled,true);
});

test('closing or logging out cannot restore an in-flight private link',async()=>{
  const p=await customerPage();const reply=p.defer('/shop/api/subscriptions/sub-a/link');
  const opening=p.run("openLink('sub-a')");p.element('link-dialog').close();reply({enabled:true,version:1,url:'private-url'});await opening;
  assert.equal(p.element('link-value').value,'');
  p.respond('/shop/api/subscriptions/sub-a/link',{enabled:true,follow_rotation:false,version:1,url:'private-url'});
  await p.run("openLink('sub-a')");await p.element('logout').onclick();assert.equal(p.element('link-value').value,'');
});

test('link setting writes explicit rotation consent and current version',async()=>{
  const p=await customerPage();p.respond('/shop/api/subscriptions/sub-a/link',{enabled:false,follow_rotation:false,version:3,url:null});
  await p.run("openLink('sub-a')");p.element('link-follow').checked=true;
  p.respond('/shop/api/subscriptions/sub-a/link',{enabled:true,follow_rotation:true,version:4,url:'private-new-url'});
  await p.run("changeLink('enable')");
  const put=p.calls.find(c=>c.opts.method==='PUT');assert.deepEqual(JSON.parse(put.opts.body),{action:'enable',version:3,follow_rotation:true});
  assert.equal(p.element('link-value').value,'private-new-url');
});

test('failed link refresh removes the previously displayed URL',async()=>{
  const p=await customerPage();p.respond('/shop/api/subscriptions/sub-a/link',{enabled:true,follow_rotation:false,version:1,url:'private-url'});
  await p.run("openLink('sub-a')");p.respond('/shop/api/subscriptions/sub-a/link',{detail:'Login'},401);
  await assert.rejects(p.run('refreshLink()'));assert.equal(p.element('link-value').value,'');assert.equal(p.element('copy-link').disabled,true);
});

test('trial offer requires Telegram and accepted claim links to the account',async()=>{
  const p=await customerPage();p.state.catalog.trial={enabled:true,days:1,eligible:false,reason:'telegram_required',claim:null};p.run('renderPlans()');
  assert.equal(p.element('trial-offer').hidden,false);assert.equal(p.element('trial-start').disabled,false);
  assert.ok(p.element('trial-info').textContent.includes('Telegram'));
  await p.run("selectedPlan={id:'month'};beginTrial()");assert.equal(p.run('selectedPlan'),null);assert.equal(p.element('auth-dialog').open,true);
  p.state.catalog.trial.claim={id:'trial',protocol:'wg'};p.run('renderPlans()');
  assert.equal(p.element('trial-start').textContent,'Открыть кабинет');
});

test('trial checkout uses policy snapshot and requires terms without receipt',async()=>{
  const p=await customerPage();p.state.customer.telegram_id=123;p.state.catalog.trial={enabled:true,days:2,version:7,eligible:true,reason:'eligible',claim:null};
  await p.run('beginTrial()');
  assert.ok(p.element('checkout-info').textContent.includes('без оплаты'));
  assert.equal(p.element('accept-terms').required,true);assert.equal(p.element('receipt-label').hidden,true);
  assert.equal(p.run('checkoutTrial.version'),7);
});

test('trial account shows no payment claim or paid profile features',async()=>{
  const p=await customerPage();p.state.customer.subscriptions[0].access_source='trial';p.state.customer.subscriptions[0].device_limit=0;
  p.state.customer.orders=[{id:'trial-order',protocol:'xui',amount:0,currency:'XTR',kind:'trial',status:'paid',created:100}];
  await p.run('refreshMe()');
  const subscriptions=p.element('subscriptions').innerHTML,orders=p.element('orders').innerHTML;
  assert.ok(subscriptions.includes('Пробный доступ')&&!subscriptions.includes('data-primary-rotate')&&!subscriptions.includes('data-link='));
  assert.ok(orders.includes('Без оплаты')&&orders.includes('Готовим пробный VPN')&&!orders.includes('Оплачено'));
});

test('promo quote shows exact discount and sends the approved amount to checkout',async()=>{
  const p=await customerPage();await p.run("beginCheckout('month')");p.element('promo-code').value='SAVE10';
  p.respond('/shop/api/quote',{plan:'month',protocol:'xui',plan_version:1,base_amount:199,amount:180,discount:19,currency:'RUB',promo_code:'SAVE10',promo_version:2,promo_percent:10});
  await p.run('applyPromo()');assert.ok(p.element('checkout-info').textContent.includes('180')&&p.element('checkout-info').textContent.includes('19'));
  p.respond('/shop/api/orders',{id:'discount-order',plan_name:'Month',protocol:'xui',days:30,amount:180,currency:'RUB',method:'manual',status:'pending',base_amount:199,discount:19,promo_code:'SAVE10',promo_percent:10});
  await p.element('checkout-form').onsubmit({preventDefault(){},target:{fields:{receipt_email:''}}});
  const order=p.calls.find(c=>c.url==='/shop/api/orders');assert.ok(order);
  const body=JSON.parse(order.opts.body);assert.equal(body.quoted_amount,180);assert.equal(body.promo_version,2);assert.equal(body.quote_currency,'RUB');
});

test('typing a code without applying it cannot silently buy at full price',async()=>{
  const p=await customerPage();await p.run("beginCheckout('month')");p.element('promo-code').value='SAVE10';
  await p.element('checkout-form').onsubmit({preventDefault(){},target:{fields:{receipt_email:''}}});
  assert.ok(!p.calls.some(c=>c.url==='/shop/api/orders'));
  assert.ok(p.element('toast').textContent.includes('Применить'));
});

test('a closed checkout cannot receive a late discount quote',async()=>{
  const p=await customerPage();await p.run("beginCheckout('month')");p.element('promo-code').value='SAVE10';const reply=p.defer('/shop/api/quote');
  const pending=p.run('applyPromo()');p.element('checkout-dialog').close();reply({amount:1,currency:'RUB',base_amount:199,discount:198,promo_code:'OLD',promo_version:1,promo_percent:90});await pending;
  assert.equal(p.run('checkoutQuote'),null);assert.equal(p.element('promo-code').value,'');assert.equal(p.element('promo-status').textContent,'');
});

function referralState(overrides={}) {
  return {policy:{enabled:true,days:7,min_rub:199,min_stars:150,version:2},balance_days:7,earned_days:7,spent_days:0,invited:2,rewarded:1,can_bind:false,can_share:true,url:'/shop/?ref=abc',code:'abc',bound:null,history:[{days:7,created:100}],...overrides};
}

test('referral terms balance and bonus action are shown without enabling trial redemption',async()=>{
  const p=await customerPage();p.state.customer.referrals=referralState();await p.run('refreshMe()');
  assert.ok(p.element('referral-info').textContent.includes('Бонусных дней: 7'));
  assert.ok(p.element('referral-info').textContent.includes('199 ₽'));
  assert.ok(p.element('subscriptions').innerHTML.includes('data-referral="sub-a"'));
  p.state.customer.subscriptions[0].access_source='trial';await p.run('refreshMe()');
  assert.ok(!p.element('subscriptions').innerHTML.includes('data-referral='));
  await p.element('logout').onclick();
  assert.equal(p.element('referral-url').value,'');assert.equal(p.element('referral-history').innerHTML,'');assert.equal(p.element('referrals-panel').hidden,true);
});

test('referral acceptance requires explicit action and sends policy snapshot',async()=>{
  const p=await customerPage();p.state.customer.referrals=referralState({can_bind:true,can_share:false,url:null});
  p.run("location.search='?ref=aaaaaaaaaaaaaaaaaaaaaaaa'");await p.run('refreshMe()');
  assert.equal(p.element('referral-code').value,'aaaaaaaaaaaaaaaaaaaaaaaa');
  assert.equal(p.calls.filter(c=>c.url.endsWith('/bind')).length,0);
  p.element('referral-accept').checked=true;p.respond('/shop/api/referrals/bind',{});
  await p.element('referral-bind').onsubmit({preventDefault(){}});
  const call=p.calls.find(c=>c.url.endsWith('/bind'));
  assert.deepEqual(JSON.parse(call.opts.body),{code:'aaaaaaaaaaaaaaaaaaaaaaaa',policy_version:2,accept_terms:true});
});

test('failed bonus redemption retry preserves request identity',async()=>{
  const p=await customerPage();p.state.customer.referrals=referralState();await p.run('refreshMe()');p.element('referral-days').value='3';
  p.respond('/shop/api/referrals/redeem',{detail:'Temporary error'},502);
  await assert.rejects(p.run("redeemReferral('sub-a')"),/Temporary error/);
  p.respond('/shop/api/referrals/redeem',{id:'bonus-order'});await p.run("redeemReferral('sub-a')");
  const bodies=p.calls.filter(c=>c.url.endsWith('/redeem')).map(c=>JSON.parse(c.opts.body));
  assert.equal(bodies.length,2);assert.equal(bodies[0].request_id,bodies[1].request_id);assert.equal(bodies[0].days,3);
});

test('logout during bonus refresh prevents late private status',async()=>{
  const p=await customerPage();p.state.customer.referrals=referralState();await p.run('refreshMe()');p.element('referral-days').value='3';
  p.respond('/shop/api/referrals/redeem',{id:'private-bonus-order'});const release=p.defer('/shop/api/me');
  const request=p.run("redeemReferral('sub-a')");await tick();await p.element('logout').onclick();release(p.state.customer,401);await assert.rejects(request,/Сессия изменилась/);
  assert.equal(p.element('referral-status').textContent,'');
});

test('password form is email-only and rejects a mismatching repeat',async()=>{
  const p=await customerPage();assert.equal(p.element('password-settings').hidden,true);
  p.state.customer.can_change_password=true;await p.run('refreshMe()');
  assert.equal(p.element('password-settings').hidden,false);
  p.element('password-new').value='new-password-123';p.element('password-repeat').value='different';
  await p.element('password-form').onsubmit({preventDefault(){}});
  assert.match(p.element('password-status').textContent,/не совпадают/);
  assert.equal(p.calls.filter(c=>c.url.endsWith('/auth/password')).length,0);
});

test('password success clears inputs and private connection data',async()=>{
  const p=await customerPage();p.state.customer.can_change_password=true;await p.run('refreshMe()');
  await p.run("openConnection('sub-a')");p.element('account-dialog').showModal();
  p.respond('/shop/api/auth/password',{ok:true});
  for(const id of ['password-new','password-repeat'])p.element(id).value='new-password-123';
  p.element('password-current').value='old-password-123';
  await p.element('password-form').onsubmit({preventDefault(){}});
  assert.equal(p.element('account-dialog').open,true);
  assert.equal(p.element('config-value').value,'');
  for(const id of ['password-current','password-new','password-repeat'])assert.equal(p.element(id).value,'');
  assert.match(p.element('toast').textContent,/Пароль изменён/);
  const body=JSON.parse(p.calls.find(c=>c.url.endsWith('/auth/password')).opts.body);
  assert.deepEqual(body,{current_password:'old-password-123',new_password:'new-password-123'});
});

test('logout clears secrets immediately and waits for password response before deleting cookie',async()=>{
  const p=await customerPage();p.state.customer.can_change_password=true;await p.run('refreshMe()');
  const reply=p.defer('/shop/api/auth/password');
  for(const id of ['password-new','password-repeat'])p.element(id).value='new-password-123';
  p.element('password-current').value='old-password-123';
  const changing=p.element('password-form').onsubmit({preventDefault(){}});
  const leaving=p.element('logout').onclick();
  assert.equal(p.element('password-current').value,'');
  assert.equal(p.calls.some(c=>c.url.endsWith('/auth/logout')),false);
  reply({ok:true});await changing;await leaving;
  assert.equal(p.calls.filter(c=>c.url.endsWith('/auth/logout')).length,1);
  assert.equal(p.element('account-dialog').open,false);
  assert.equal(p.element('identity').textContent,'');
});

test('closing account erases password fields and a failed change shows a safe error',async()=>{
  const p=await customerPage();p.state.customer.can_change_password=true;await p.run('refreshMe()');
  p.element('account-dialog').showModal();
  for(const id of ['password-current','password-new','password-repeat'])p.element(id).value='same-password-123';
  p.element('account-dialog').close();
  for(const id of ['password-current','password-new','password-repeat'])assert.equal(p.element(id).value,'');
  p.respond('/shop/api/auth/password',{detail:'Текущий пароль неверен'},403);
  for(const id of ['password-current','password-new','password-repeat'])p.element(id).value='same-password-123';
  await p.element('password-form').onsubmit({preventDefault(){}});
  assert.match(p.element('password-status').textContent,/Текущий пароль неверен/);
  assert.equal(p.element('password-submit').disabled,false);
  for(const id of ['password-current','password-new','password-repeat'])assert.equal(p.element(id).value,'');
});

test('order history shows delayed fulfillment and opens help for paid orders',async()=>{
  const p=await customerPage();
  const order={id:'paid-order',protocol:'wg',status:'paid',amount:199,currency:'RUB',created:1,days:30,kind:'purchase',progress:{stage:'help',title:'Выдача задерживается · нужна помощь',message:'Повторно оплачивать не нужно.',needs_support:true}};
  p.state.customer.orders=[order];p.respond('/shop/api/orders/paid-order',order);await p.run('refreshMe()');
  assert.match(p.element('orders').innerHTML,/Статус и помощь/);assert.match(p.element('orders').innerHTML,/Повторно оплачивать не нужно/);
  await p.run("openOrder('paid-order')");
  assert.equal(p.element('checkout-dialog').open,true);assert.equal(p.element('checkout-form').hidden,true);
  const children=p.element('payment-result').children;
  assert.ok(children.some(c=>c.textContent.includes('нужна помощь')));
  assert.ok(!children.some(c=>c.textContent==='Перейти к оплате ↗'));
  await children.find(c=>c.textContent==='Скопировать номер заказа').onclick();
  assert.deepEqual(p.state.copied,['Заказ: paid-order']);
});

test('closing order details prevents late response from restoring private data',async()=>{
  const p=await customerPage();const reply=p.defer('/shop/api/orders/old-order');
  const pending=p.run("openOrder('old-order')");p.element('checkout-dialog').close();
  reply({id:'old-order',protocol:'wg',days:30,status:'paid',amount:199,currency:'RUB'});await pending;
  assert.equal(p.element('checkout-dialog').open,false);assert.equal(p.element('payment-result').children.length,0);
  assert.equal(p.run('paymentOrder'),null);
});

test('late order detail cannot replace a more recently selected order',async()=>{
  const p=await customerPage();const reply=p.defer('/shop/api/orders/old-order');
  p.respond('/shop/api/orders/new-order',{id:'new-order',protocol:'wg',days:30,status:'paid',amount:199,currency:'RUB'});
  const pending=p.run("openOrder('old-order')");await p.run("openOrder('new-order')");
  reply({id:'old-order',protocol:'wg',days:30,status:'paid',amount:199,currency:'RUB'});await pending;
  assert.equal(p.run('paymentOrder.id'),'new-order');
});

test('status refresh reaches ready and offers account navigation without another payment',async()=>{
  const p=await customerPage();
  p.respond('/shop/api/orders/order-a',{id:'order-a',protocol:'wg',days:30,status:'paid',amount:199,currency:'RUB'});
  await p.run("openOrder('order-a')");
  p.respond('/shop/api/orders/order-a',{id:'order-a',protocol:'wg',days:30,status:'active',amount:199,currency:'RUB',progress:{title:'Доступ выдан',message:'Настройки в кабинете.'}});
  await p.run('refreshPaymentOrder()');
  const button=p.element('payment-result').children.find(c=>c.textContent==='Перейти к подключениям');assert.ok(button);
  await button.onclick();assert.equal(p.element('checkout-dialog').open,false);assert.equal(p.element('account-dialog').open,true);
});

test('logout during order status refresh clears the order and ignores response',async()=>{
  const p=await customerPage();p.respond('/shop/api/orders/order-a',{id:'order-a',protocol:'wg',days:30,status:'paid',amount:199,currency:'RUB'});
  await p.run("openOrder('order-a')");const reply=p.defer('/shop/api/orders/order-a');const pending=p.run('refreshPaymentOrder()');
  await p.element('logout').onclick();reply({id:'order-a',protocol:'wg',days:30,status:'active',amount:199,currency:'RUB'});await pending;
  assert.equal(p.run('paymentOrder'),null);assert.equal(p.element('payment-result').children.length,0);assert.equal(p.element('checkout-dialog').open,false);
});

test('closing checkout during purchase leaves the saved order in history without late dialog data',async()=>{
  const p=await customerPage();await p.run("beginCheckout('month')");const reply=p.defer('/shop/api/orders');
  const pending=p.element('checkout-form').onsubmit({preventDefault(){},target:{fields:{receipt_email:''}}});
  p.element('checkout-dialog').close();reply({id:'saved-order',protocol:'wg',days:30,status:'pending',amount:199,currency:'RUB'});await pending;
  assert.equal(p.run('paymentOrder'),null);assert.equal(p.element('payment-result').children.length,0);assert.equal(p.element('checkout-dialog').open,false);
  assert.equal(p.element('checkout-submit').disabled,false);
});

test('order progress and support text cannot insert HTML',async()=>{
  const p=await customerPage();const order={id:'order-a',protocol:'wg',created:1,days:30,status:'paid',amount:199,currency:'RUB',progress:{title:'<img src=x>',message:'<script>unsafe</script>'}};
  p.state.customer.orders=[order];p.state.catalog.support='<img src=support>';p.respond('/shop/api/orders/order-a',order);await p.run('refreshMe()');
  assert.ok(p.element('orders').innerHTML.includes('&lt;img'));assert.ok(!p.element('orders').innerHTML.includes('<script>'));
  await p.run("openOrder('order-a')");
  const support=p.element('payment-result').children.find(c=>c.textContent==='<img src=support>');assert.ok(support);assert.equal(support.innerHTML,'');
});

test('automatic order polling updates an open pending order and stops when closed',async()=>{
  const p=await customerPage();p.respond('/shop/api/orders/order-a',{id:'order-a',protocol:'wg',days:30,status:'paid',amount:199,currency:'RUB'});
  await p.run("openOrder('order-a')");p.respond('/shop/api/orders/order-a',{id:'order-a',protocol:'wg',days:30,status:'active',amount:199,currency:'RUB'});
  await p.intervals[0]();assert.equal(p.run('paymentOrder.status'),'active');
  p.element('checkout-dialog').close();const count=p.calls.filter(c=>c.url.endsWith('/orders/order-a')).length;
  await p.intervals[0]();assert.equal(p.calls.filter(c=>c.url.endsWith('/orders/order-a')).length,count);
});

test('invalid payment URL still leaves status and support reference available',async()=>{
  const p=await customerPage();p.respond('/shop/api/orders/order-a',{id:'order-a',protocol:'wg',days:30,status:'pending',amount:199,currency:'RUB',payment_url:'not a URL'});
  await p.run("openOrder('order-a')");
  assert.ok(p.element('payment-result').children.some(c=>c.textContent==='Скопировать номер заказа'));
  assert.ok(!p.element('payment-result').children.some(c=>c.textContent==='Перейти к оплате ↗'));
});

test('код из партнёрской ссылки подставляется без автоматического запроса скидки или покупки',async()=>{
  const p=await customerPage({search:'?promo=partner15&discount=99'});await p.run("beginCheckout('month')");
  assert.equal(p.element('promo-code').value,'PARTNER15');
  assert.match(p.element('promo-status').textContent,/Применить/);
  assert.ok(!p.calls.some(c=>c.url.endsWith('/api/quote')||c.url.endsWith('/api/orders')));
  assert.equal(p.run('checkoutQuote'),null);
});

test('скидка по ссылке применяется только после серверного расчёта и явного действия',async()=>{
  const p=await customerPage({search:'?promo=PARTNER15&discount=99'});await p.run("beginCheckout('month')");
  p.respond('/shop/api/quote',{plan:'month',protocol:'xui',plan_version:1,base_amount:199,amount:170,discount:29,currency:'RUB',promo_code:'PARTNER15',promo_version:2,promo_percent:15});
  await p.run('applyPromo()');assert.match(p.element('checkout-info').textContent,/170/);
  p.respond('/shop/api/orders',{id:'partner-order',protocol:'xui',days:30,status:'pending',amount:170,currency:'RUB'});
  await p.element('checkout-form').onsubmit({preventDefault(){},target:{fields:{receipt_email:''}}});
  const body=JSON.parse(p.calls.find(c=>c.url.endsWith('/api/orders')).opts.body);
  assert.equal(body.quoted_amount,170);assert.equal(body.promo_code,'PARTNER15');assert.equal(body.promo_version,2);assert.equal(body.discount,undefined);
});

test('недействующий партнёрский код не позволяет незаметно оформить заказ по полной цене',async()=>{
  const p=await customerPage({search:'?promo=EXPIRED15'});await p.run("beginCheckout('month')");
  p.respond('/shop/api/quote',{detail:'Промокод недействителен'},409);
  await p.run('applyPromo()');
  await p.element('checkout-form').onsubmit({preventDefault(){},target:{fields:{receipt_email:''}}});
  assert.ok(!p.calls.some(c=>c.url.endsWith('/api/orders')));
});

test('небезопасные и неоднозначные параметры промокода игнорируются',async()=>{
  for(const search of ['?promo=%3Cscript%3E','?promo=A','?promo='+('A'.repeat(33)),'?promo=PARTNER15&promo=OTHER15']){
    const p=await customerPage({search});await p.run("beginCheckout('month')");assert.equal(p.element('promo-code').value,'');
  }
});

test('пользователь может убрать промокод из ссылки без повторной подстановки',async()=>{
  const p=await customerPage({search:'?promo=PARTNER15'});await p.run("beginCheckout('month')");
  p.element('promo-clear').onclick();p.element('checkout-dialog').close();await p.run("beginCheckout('month')");
  assert.equal(p.element('promo-code').value,'');assert.equal(p.run('promoIntent'),'');
});

test('выход очищает намерение применить партнёрский промокод',async()=>{
  const p=await customerPage({search:'?promo=PARTNER15'});await p.run("beginCheckout('month')");await p.element('logout').onclick();
  assert.equal(p.run('promoIntent'),'');assert.equal(p.element('promo-code').value,'');
});

test('публичный партнёрский код сохраняется до входа покупателя',async()=>{
  const p=await customerPage({search:'?promo=PARTNER15',anonymous:true});await p.run("beginCheckout('month')");
  assert.equal(p.element('auth-dialog').open,true);assert.equal(p.run('promoIntent'),'PARTNER15');
  p.state.customer={id:2,email:'new@example.com',subscriptions:[],orders:[]};await p.run('refreshMe()');await p.run("beginCheckout('month')");
  assert.equal(p.element('promo-code').value,'PARTNER15');
});

function adsReplies(p,changes={}) {
  p.respond('/shop/api/ads/policy',{accepting:true,amount:300,hours:24,version:1,instructions:'На рассмотрение',...changes});
  p.respond('/shop/api/ads/requests?page=1',{requests:[],total:0,page:1,page_size:20});
}

test('кабинет рекламодателя показывает закрытый приём без формы и оплаты',async()=>{
  const p=await customerPage();adsReplies(p,{accepting:false});await p.element('ads-open').onclick();
  assert.equal(p.element('ads-form').hidden,true);assert.match(p.element('ads-offer').textContent,/закрыт/);
  assert.ok(!p.calls.some(c=>c.url.endsWith('/api/orders')));
});

test('после потери ответа рекламная заявка повторяется с тем же идентификатором',async()=>{
  const p=await customerPage();adsReplies(p);await p.element('ads-open').onclick();
  p.element('ads-channel').value='@channel_name';p.element('ads-comment').value='Предложение';
  p.respond('/shop/api/ads/requests',{detail:'Соединение потеряно'},502);
  await p.element('ads-form').onsubmit({preventDefault(){}});await p.element('ads-form').onsubmit({preventDefault(){}});
  const calls=p.calls.filter(c=>c.url==='/shop/api/ads/requests');assert.equal(calls.length,2);
  assert.equal(JSON.parse(calls[0].opts.body).request_id,JSON.parse(calls[1].opts.body).request_id);
  assert.equal(JSON.parse(calls[0].opts.body).amount,undefined);
});

test('закрытый рекламный кабинет не получает запоздалые данные',async()=>{
  const p=await customerPage();adsReplies(p);const reply=p.defer('/shop/api/ads/requests?page=1');const pending=p.element('ads-open').onclick();
  p.element('ads-dialog').close();reply({requests:[{id:'private-id',channel:'private_channel'}],total:1,page:1,page_size:20});await pending;
  assert.equal(p.element('ads-list').innerHTML,'');assert.equal(p.run('adsPolicy'),null);
});

test('комментарии и причины модерации экранируются, одобрение не выдаётся за размещение',async()=>{
  const p=await customerPage();adsReplies(p);
  p.respond('/shop/api/ads/requests?page=1',{requests:[{id:'ad-a',channel:'channel_name',comment:'<img src=x>',reason:'<script>unsafe</script>',state:'approved',version:2,amount:300,hours:24}],total:1,page:1,page_size:20});
  await p.element('ads-open').onclick();const html=p.element('ads-list').innerHTML;
  assert.ok(html.includes('&lt;img')&&!html.includes('<script>'));assert.match(html,/Размещение не запущено/);
});

test('выход во время отправки рекламы очищает форму и не возвращает заявку',async()=>{
  const p=await customerPage();adsReplies(p);await p.element('ads-open').onclick();
  p.element('ads-channel').value='@channel_name';p.element('ads-comment').value='Личный комментарий';const reply=p.defer('/shop/api/ads/requests');
  const pending=p.element('ads-form').onsubmit({preventDefault(){}});await p.element('logout').onclick();reply({id:'ad-a'});await pending;
  assert.equal(p.element('ads-dialog').open,false);assert.equal(p.element('ads-comment').value,'');assert.equal(p.element('ads-list').innerHTML,'');assert.equal(p.run('adsIntent.size'),0);
});

test('учтённое поступление не даёт отменить заявку и не означает показ канала',async()=>{
  const p=await customerPage();adsReplies(p);
  p.respond('/shop/api/ads/requests?page=1',{requests:[{id:'ad-a',channel:'channel_name',comment:'',reason:'',state:'approved',version:2,amount:300,hours:24,payment:{status:'confirmed',version:1}}],total:1,page:1,page_size:20});
  await p.element('ads-open').onclick();const html=p.element('ads-list').innerHTML;
  assert.match(html,/владельцем вручную/);assert.match(html,/Размещение не запущено/);assert.ok(!html.includes('data-ads-cancel'));
  assert.ok(!html.includes('href='));
});

test('учтённый полный возврат разрешает отмену заявки без повторного платежа',async()=>{
  const p=await customerPage();adsReplies(p);
  p.respond('/shop/api/ads/requests?page=1',{requests:[{id:'ad-a',channel:'channel_name',comment:'',reason:'',state:'approved',version:2,amount:300,hours:24,payment:{status:'refunded',version:2}}],total:1,page:1,page_size:20});
  await p.element('ads-open').onclick();const html=p.element('ads-list').innerHTML;
  assert.match(html,/Полный возврат/);assert.match(html,/data-ads-cancel/);assert.ok(!p.calls.some(x=>x.url.includes('/payments/')));
});

test('рекламодатель видит ручной запуск и остаток времени без обещания автоматической остановки',async()=>{
  const p=await customerPage();adsReplies(p);
  p.respond('/shop/api/ads/requests?page=1',{requests:[{id:'ad-a',channel:'channel_name',state:'approved',version:2,amount:300,hours:24,payment:{status:'confirmed'},placement:{state:'active',site_name:'<Площадка>',remaining_seconds:0,attention_due:true}}],total:1,page:1,page_size:20});
  await p.element('ads-open').onclick();const html=p.element('ads-list').innerHTML;
  assert.match(html,/Запуск подтверждён владельцем/);assert.match(html,/ожидается подтверждение остановки/);assert.match(html,/&lt;Площадка&gt;/);assert.ok(!html.includes('Размещение не запущено'));assert.ok(!html.includes('data-ads-cancel'));
});

test('пауза и раннее завершение показывают неоказанный остаток',async()=>{
  for(const state of ['paused','finished']){
    const p=await customerPage();adsReplies(p);
    p.respond('/shop/api/ads/requests?page=1',{requests:[{id:'ad-a',channel:'channel_name',state:'approved',version:2,amount:300,hours:24,payment:{status:'confirmed'},placement:{state,site_name:'Площадка',remaining_seconds:1200}}],total:1,page:1,page_size:20});
    await p.element('ads-open').onclick();assert.match(p.element('ads-list').innerHTML,/осталось 1200 сек/);
  }
});


test('рекламодатель видит период своей брони как план, а не запущенный показ',async()=>{
  const p=await customerPage();adsReplies(p);
  p.respond('/shop/api/ads/requests?page=1',{requests:[{id:'ad-a',channel:'channel_name',state:'approved',version:2,amount:300,hours:1,payment:{status:'confirmed'},booking:{state:'reserved',site_name:'<Площадка>',starts_at:10000,ends_at:13600,expired:false}}],total:1,page:1,page_size:20});
  await p.element('ads-open').onclick();const html=p.element('ads-list').innerHTML;
  assert.match(html,/Забронировано владельцем/);assert.match(html,/Размещение не запущено/);assert.match(html,/&lt;Площадка&gt;/);assert.ok(!html.includes('data-ads-booking'));
});

test('ссылка уведомления открывает кабинет только после определения аккаунта',async()=>{
 const signed=await customerPage({search:'?view=account'});
 assert.equal(signed.element('account-dialog').open,true);
 const anonymous=await customerPage({search:'?view=account',anonymous:true});
 assert.equal(anonymous.element('account-dialog').open,false);
 assert.equal(anonymous.element('auth-dialog').open,true);
 const unknown=await customerPage({search:'?view=untrusted'});
 assert.equal(unknown.element('account-dialog').open,false);
});
