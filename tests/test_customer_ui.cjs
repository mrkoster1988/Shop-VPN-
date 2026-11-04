const test=require('node:test');
const assert=require('node:assert/strict');
const vm=require('node:vm');
const fs=require('node:fs');
const tick=()=>new Promise(setImmediate);

async function customerPage() {
  const clock={now:Date.now()}, timers=new Map(), elements=new Map(), calls=[], overrides=new Map();
  let timerId=0;
  const catalog={protocols:[{id:'xui',name:'VLESS',available:true},{id:'wg',name:'WireGuard',available:true}],plans:[{id:'month',name:'Месяц',days:30,price:199,version:1}],currency:'RUB',support:'Поддержка: @support',bot_username:'',terms:'Условия',payment_mode:'manual'};
  const subscription={id:'sub-a',protocol:'xui',state:'active',expires:Math.floor(clock.now/1000)+3600};
  const state={customer:{id:1,email:'buyer@example.com',subscriptions:[subscription],orders:[]},devices:[],catalog,config:{protocol:'xui',config:'vless://private-secret@vpn.example'},copied:[]};
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
  const context=vm.createContext({document:{getElementById:element,querySelector:()=>({content:'/shop'}),querySelectorAll:()=>[],createElement:tag=>element('created-'+tag)},
    fetch,URL,URLSearchParams,FormData:class {constructor(form){this.fields=form.fields;}get(name){return this.fields[name];}},crypto:require('node:crypto').webcrypto,Date:class extends Date { static now(){return clock.now;} },
    navigator:{platform:'Linux x86_64',userAgent:'Android',clipboard:{writeText:async value=>state.copied.push(value)}},
    window:{confirm:()=>true},location:{search:''},history:{replaceState(){}},
    setTimeout:(fn,delay)=>{const id=++timerId;timers.set(id,{fn,delay});return id;},clearTimeout:id=>timers.delete(id),setInterval:()=>0});
  vm.runInContext(fs.readFileSync('app/static/app.js','utf8'),context);
  await tick();
  return {state,element,calls,timers,clock,run:code=>vm.runInContext(code,context),
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

test('all five platforms have three instructions for both protocols',async()=>{
  const p=await customerPage();
  for(const protocol of ['xui','wg']) {
    p.state.customer.subscriptions[0].protocol=protocol;p.state.config={protocol,config:'private-config'};
    await p.run('refreshMe()');await p.run("openConnection('sub-a')");
    for(const platform of ['android','ios','windows','macos','linux']) {
      p.element('config-platform').value=platform;p.element('config-platform').onchange();
      assert.equal((p.element('connection-steps').innerHTML.match(/<li>/g)||[]).length,3);
      assert.ok(p.element('connection-steps').innerHTML.includes(protocol==='wg'?'.conf':'VLESS'));
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
