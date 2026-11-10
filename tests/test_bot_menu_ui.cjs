const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');

function page(){
  const elements=new Map(),calls=[],replies=[];
  const get=id=>{
    if(!elements.has(id))elements.set(id,{innerHTML:'',textContent:'',value:'',
      checked:false,disabled:false,hidden:false,open:false,replaceChildren(){this.innerHTML='';this.textContent='';},
      reset(){},addEventListener(){},close(){this.open=false}});
    return elements.get(id);
  };
  const context=vm.createContext({
    document:{getElementById:get,querySelector:()=>({content:'/shop'})},
    crypto:require('node:crypto').webcrypto,FormData,URLSearchParams,
    fetch:(url,opts={})=>new Promise(resolve=>{
      calls.push({url,opts});replies.push((data,status=200)=>resolve({ok:status<400,json:async()=>data}));
    }),
  });
  vm.runInContext(fs.readFileSync('app/static/admin.js','utf8'),context);
  return {get,calls,replies,run:code=>vm.runInContext(code,context)};
}
const buttons=[
  {id:'buy',text:'Купить VPN',enabled:true,row:1,url:''},
  {id:'account',text:'Мой кабинет',enabled:true,row:2,url:''},
  {id:'support',text:'<img src=x onerror=alert(1)>',enabled:true,row:3,url:''},
  {id:'terms',text:'Условия',enabled:true,row:3,url:''},
  {id:'link1',text:'Наш канал',enabled:false,row:4,url:''},
  {id:'link2',text:'Новости',enabled:false,row:5,url:''},
];

test('owner preview escapes labels and never receives a private login URL',async()=>{
  const ui=page();
  const promise=ui.run("role='owner';refreshBotMenu()");
  assert.match(ui.calls[0].url,/\/shop\/api\/admin\/bot-menu$/);
  ui.replies[0]({buttons,version:1,updated:0});await promise;
  const preview=ui.get('bot-menu-preview').innerHTML;
  assert.match(preview,/&lt;img src=x onerror=alert\(1\)&gt;/);
  assert.ok(!preview.includes('<img'));
  assert.ok(!preview.includes('/login?ticket='));
  assert.equal(ui.get('bot-menu-panel').hidden,false);
});

test('logout clears menu and blocks late API refresh',async()=>{
  const ui=page();
  const load=ui.run("role='owner';refreshBotMenu()");
  ui.run("$('exit').onclick()");
  ui.replies[0]({buttons,version:1,updated:0});
  await load;
  assert.equal(ui.get('bot-menu-rows').innerHTML,'');
  assert.equal(ui.get('bot-menu-panel').hidden,true);
});
