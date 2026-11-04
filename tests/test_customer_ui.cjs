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
  const state={customer:{id:1,email:'buyer@example.com',subscriptions:[subscription],orders:[]},catalog,config:{protocol:'xui',config:'vless://private-secret@vpn.example'},copied:[]};
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
    else if(url.endsWith('/config'))body=state.config;
    else throw Error('Unexpected '+url);
    return {ok,status,json:async()=>body};
  };
  const context=vm.createContext({document:{getElementById:element,querySelector:()=>({content:'/shop'}),querySelectorAll:()=>[],createElement:tag=>element('created-'+tag)},
    fetch,URL,URLSearchParams,FormData,Date:class extends Date { static now(){return clock.now;} },
    navigator:{platform:'Linux x86_64',userAgent:'Android',clipboard:{writeText:async value=>state.copied.push(value)}},
    window:{},location:{search:''},history:{replaceState(){}},
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
  p.element('subscriptions').onclick({target:{closest(){return {dataset:{renew:'xui'}};}}});
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
