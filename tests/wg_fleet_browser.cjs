// Full admin-page acceptance against synthetic responses only. Never use a live API.
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const {chromium}=require('playwright');
const root=path.resolve(__dirname,'..'),output=process.env.PANEL_UI_OUTPUT||'/tmp';
const origin='https://pushok.test',base='/shop',clone=value=>JSON.parse(JSON.stringify(value));
const panelToken='fixture-panel-key-not-a-real-secret-12345';
const probeToken='fixture-probe-key-not-a-real-secret-67890';
const assets=new Set(['admin.js','style.css','wg-fleet.js','wg-fleet.css']);
fs.mkdirSync(output,{recursive:true});

function fixture(){
 const node={id:'server-1',name:'Амстердам',location:'Нидерланды',domain:'wg1.pushok.shop',endpoint_ip:'8.8.8.8',
  dns_state:'ready',state:'draining',health:'ok',traffic_verified:false,probe_status:'unhealthy',archived:false,
  load_score:.25,capacity:500,occupied:10,primary_count:7,standby_count:3,version:1,
  api_checked_at:1799996400,checked_at:1800000000,error_code:null,
  settings:{api:'wgdashboard',url:'https://panel.example.invalid',configuration:'wg0',
   panel_timezone:'Europe/Moscow',schedule_expiry:true,token_configured:true}};
 return {nodes:[node,{...clone(node),id:'server-2',name:'Хельсинки',location:'Финляндия',domain:'wg2.pushok.shop',
  state:'disabled',health:'unknown',probe_status:'unregistered',capacity:0,load_score:null,
  dns_state:'pending',checked_at:null,api_checked_at:null}],
  operations:[{id:'move-1',source_server_id:'server-1',state:'revoking',total:7,completed:7,failed:0,
   error:'RemoteRevokeUnconfirmed',pending_revocations:3,deadline:1800086400,lost:false}],
  policy:{enabled:true,standby_count:1,version:1}};
}

async function count(page,selector,expected){
 await page.waitForFunction(({selector,expected})=>document.querySelectorAll(selector).length===expected,{selector,expected});
}
async function contains(page,selector,text){
 await page.waitForFunction(({selector,text})=>document.querySelector(selector)?.textContent.includes(text),{selector,text});
}
async function layout(page){
 const widths=await page.evaluate(()=>{
  const section=document.getElementById('wg-fleet-section');
  return {viewport:innerWidth,page:document.documentElement.scrollWidth,section:section.scrollWidth,client:section.clientWidth};
 });
 assert.ok(widths.page<=widths.viewport+1&&widths.section<=widths.client+1,'Fleet must fit the viewport: '+JSON.stringify(widths));
}

async function run(browser,width){
 const context=await browser.newContext({viewport:{width,height:900},locale:'ru-RU',timezoneId:'Europe/Moscow',reducedMotion:'reduce'});
 const page=await context.newPage();page.setDefaultTimeout(10000);
 const errors=[],unexpected=[],writes=[],consoleText=[];
 const state=fixture(),creates=new Map();let actorRole='owner',snapshots=0,firstCreate=true,enrollments=0;
 page.on('pageerror',error=>errors.push(error.message));
 page.on('console',message=>consoleText.push(message.text()));
 await page.route('**/*',async route=>{
  const request=route.request(),url=new URL(request.url()),p=url.pathname,method=request.method();
  if(url.hostname==='fonts.googleapis.com')return route.fulfill({contentType:'text/css',body:''});
  if(url.origin!==origin){unexpected.push(url.origin+p);return route.abort();}
  if(p===base+'/admin')return route.fulfill({contentType:'text/html',body:fs.readFileSync(root+'/app/static/admin.html','utf8')
   .replaceAll('__BASE_PATH__',base).replaceAll('__STAFF_SESSION__','0')});
  if(p.startsWith(base+'/static/')&&assets.has(p.split('/').at(-1))){
   const file=p.split('/').at(-1);
   return route.fulfill({contentType:file.endsWith('.js')?'text/javascript':'text/css',body:fs.readFileSync(root+'/app/static/'+file)});
  }
  if(p==='/favicon.ico')return route.fulfill({status:204,body:''});
  if(p===base+'/api/admin/auth/logout'&&method==='POST')return route.fulfill({json:{ok:true}});
  if(p===base+'/api/admin/wg-fleet'&&method==='GET'){
   snapshots++;const data=clone(state);
   if(actorRole!=='owner')data.nodes.forEach(node=>delete node.settings);
   return route.fulfill({json:data});
  }
  if(p===base+'/api/admin/wg-fleet/probes/enroll'&&method==='POST'){
   const body=request.postDataJSON();writes.push({action:'enroll',payload:body});
   if(actorRole!=='owner')return route.fulfill({status:403,json:{detail:'Управление узлами доступно разработчику.'}});
   const node=state.nodes.find(row=>row.id===body.id);node.version++;enrollments++;
   return route.fulfill({json:{server_id:node.id,server_version:node.version,probe_id:'probe-1',generation:enrollments,token:probeToken}});
  }
  if(p===base+'/api/admin/wg-fleet/commands'&&method==='POST'){
   const body=request.postDataJSON();writes.push(body);const {action,payload}=body;
   if(actorRole!=='owner')return route.fulfill({status:403,json:{detail:'Управление узлами доступно разработчику.'}});
   if(action==='create'){
    // The first request is persisted, then its reply is lost. Only the same UUID deduplicates the retry.
    if(!creates.has(body.request_id)){
     const node={...clone(state.nodes[0]),id:'server-'+(state.nodes.length+1),name:payload.name,location:payload.location,
      endpoint_ip:payload.endpoint_ip,capacity:payload.capacity,state:'disabled',health:'unknown',traffic_verified:false,
      primary_count:0,standby_count:0,occupied:0,domain:'wg'+(state.nodes.length+1)+'.pushok.shop',version:1,
      settings:{...payload.settings,token_configured:true}};
     delete node.settings.token;state.nodes.push(node);creates.set(body.request_id,node);
    }
    if(firstCreate){firstCreate=false;return route.abort('timedout');}
    return route.fulfill({status:201,json:{node:clone(creates.get(body.request_id))}});
   }
   if(action==='update'){
    const node=state.nodes.find(row=>row.id===payload.id);
    for(const key of ['name','location','capacity'])if(key in payload)node[key]=payload[key];node.version++;
    return route.fulfill({json:{node:clone(node)}});
   }
   if(action==='check'){
    const node=state.nodes.find(row=>row.id===payload.id);node.health='ok';node.api_checked_at++;
    return route.fulfill({json:{node:clone(node)}});
   }
   if(action==='retry')return route.fulfill({json:{operation:clone(state.operations.find(row=>row.id===payload.id))}});
   if(action==='drain'){
    const operation={id:'move-2',source_server_id:payload.id,state:'preparing',total:3,completed:0,failed:0,
     error:null,pending_revocations:3,deadline:1800086400,lost:payload.lost};
    state.operations.unshift(operation);return route.fulfill({status:202,json:{operation}});
   }
  }
  unexpected.push(method+' '+p);return route.fulfill({status:404,json:{detail:'Unexpected fixture request'}});
 });

 async function role(name,permissions=[]){
  actorRole=name;
  await page.evaluate(({name,permissions})=>{
   authEpoch++;acceptStaffRole({id:1,role:name,permissions});currentManagementSection='management-overview';
   document.getElementById('dashboard').hidden=false;document.getElementById('admin-login').hidden=true;renderManagementNav();
  },{name,permissions});
 }
 async function open(){
  await page.locator('#management-quick [data-admin-target="wg-fleet-section"]').click();
  await page.locator('#wg-fleet-section').waitFor({state:'visible'});
  await count(page,'.wg-fleet-card',state.nodes.length);
 }
 async function home(){
  // Mobile hides the header button; its visible Home action is in the bottom navigation.
  const mobile=page.locator('#management-mobile-nav [data-admin-target="management-overview"]');
  const control=await mobile.isVisible()?mobile:page.locator('#management-home');
  await control.click();
  await page.locator('#management-overview').waitFor({state:'visible'});
  assert.equal(await page.locator('#wg-fleet-section').isVisible(),false);
 }
 async function enrollment(){
  await page.locator('[data-node-id="server-1"]').getByRole('button',{name:'Подключить проверку трафика',exact:true}).click();
  const secret=page.getByLabel('Секрет проверки',{exact:true});await secret.waitFor({state:'visible'});
  assert.equal(await secret.getAttribute('type'),'password');
  assert.ok(await secret.evaluate(input=>input.readOnly));
  assert.equal((await secret.inputValue())===probeToken,true,'Enrollment must show the one-time fixture value');
  assert.equal(await page.evaluate(value=>document.body.textContent.includes(value)||
   JSON.stringify({...localStorage,...sessionStorage}).includes(value),probeToken),false,'Probe credentials must not be stored or rendered as text');
  return secret.elementHandle();
 }

 try{
  await page.goto(origin+base+'/admin');
  // Real role/navigation helpers deny a crafted destination without servers_read.
  for(const name of ['support','administrator']){
   await role(name);const before=snapshots;
   assert.equal(await page.locator('#management-quick [data-admin-target="wg-fleet-section"]').count(),0);
   await page.evaluate(()=>showManagementSection('wg-fleet-section'));
   assert.equal(await page.locator('#wg-fleet-section').isVisible(),false);assert.equal(snapshots,before);
  }
  await role('owner');await open();await layout(page);
  assert.equal(await page.locator('body').evaluate(element=>getComputedStyle(element).backgroundColor),'rgb(255, 255, 255)');
  const first=page.locator('[data-node-id="server-1"]'),operation=page.locator('[data-operation-id="move-1"]');
  assert.match(await first.locator('.wg-fleet-health').textContent(),/Панель отвечает/);
  assert.match(await first.locator('.wg-fleet-health-detail').textContent(),/Проверка трафика не прошла/);
  assert.match(await first.locator('.wg-fleet-facts').textContent(),/25%/);
  assert.match(await operation.textContent(),/Отключаются прежние подключения/);
  assert.match(await operation.textContent(),/Перенесено: 7 из 7/);
  assert.match(await operation.textContent(),/На прежнем узле ждут отключения: 3/);
  assert.doesNotMatch(await operation.textContent(),/Перенос завершён/);
  await page.screenshot({path:path.join(output,'wg-fleet-'+width+'-overview.png'),fullPage:true});
  await first.getByRole('button',{name:'Проверить панель',exact:true}).click();
  await contains(page,'[data-node-id="server-1"] .wg-fleet-feedback','Работа интернета оценивается отдельно');
  assert.match(await first.locator('.wg-fleet-health-detail').textContent(),/Проверка трафика не прошла/);

  await page.locator('.wg-fleet-add > summary').click();const form=page.locator('.wg-fleet-add form');
  await form.getByLabel('Название сервера',{exact:true}).fill('Варшава');
  await form.getByLabel('Расположение',{exact:true}).fill('Польша');
  await form.getByLabel('IP подключения WireGuard',{exact:true}).fill('8.8.4.4');
  await form.getByLabel('Адрес панели WGDashboard',{exact:true}).fill('https://new-panel.example.invalid');
  await layout(page);await page.screenshot({path:path.join(output,'wg-fleet-'+width+'-create.png'),fullPage:true});
  await form.getByLabel('API-ключ панели',{exact:true}).fill(panelToken);
  await form.getByRole('button',{name:'Добавить сервер',exact:true}).click();
  await contains(page,'.wg-fleet-add .wg-fleet-feedback','не подтвердил');
  assert.equal(await page.locator('#wg-fleet-status').textContent(),'','A timed-out write must not report success');
  await form.getByRole('button',{name:'Добавить сервер',exact:true}).click();
  await contains(page,'#wg-fleet-status','Сервер добавлен выключенным');await count(page,'.wg-fleet-card',3);
  const created=writes.filter(row=>row.action==='create');assert.equal(created.length,2);
  assert.match(created[0].request_id,/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i);
  assert.equal(created[0].request_id,created[1].request_id);assert.equal(creates.size,1);
  assert.equal(created[1].payload.endpoint_ip,'8.8.4.4');assert.equal(created[1].payload.settings.url,'https://new-panel.example.invalid');
  assert.equal(created[1].payload.settings.token===panelToken,true);
  assert.equal(await page.locator('.wg-fleet-add input[type=password]').inputValue(),'');

  const second=page.locator('[data-node-id="server-2"]');await second.getByRole('button',{name:'Изменить',exact:true}).click();
  const editor=second.locator('form');assert.equal(await editor.getByLabel('Новый API-ключ панели',{exact:true}).inputValue(),'');
  await editor.getByLabel('Название сервера',{exact:true}).fill('Хельсинки 2');
  await editor.getByLabel('IP подключения WireGuard',{exact:true}).fill('');await layout(page);
  await editor.getByRole('button',{name:'Сохранить параметры',exact:true}).click();
  await contains(page,'[data-node-id="server-2"] h3','Хельсинки 2');
  const update=writes.find(row=>row.action==='update').payload;
  assert.equal('settings' in update,false,'Metadata edits must preserve credentials');
  assert.equal('endpoint_ip' in update,false,'A blank endpoint must retain the saved IP');assert.equal(update.capacity,0);
  assert.deepEqual(Object.keys(update).sort(),['capacity','id','location','name','version']);

  await operation.getByRole('button',{name:'Повторить перенос',exact:true}).click();
  await contains(page,'#wg-fleet-status','Отключаются прежние подключения');
  assert.doesNotMatch(await operation.textContent(),/Перенос завершён/);
  await second.getByRole('button',{name:'Перенести пользователей',exact:true}).click();
  await second.getByRole('button',{name:'Начать перенос',exact:true}).click();
  await contains(page,'[data-operation-id="move-2"]','Готовятся новые подключения');
  assert.match(await page.locator('[data-operation-id="move-2"]').textContent(),/Перенесено: 0 из 3/);
  assert.doesNotMatch(await page.locator('[data-operation-id="move-2"]').textContent(),/Перенос завершён/);

  let secret=await enrollment();
  await page.getByRole('button',{name:'Закрыть реквизиты',exact:true}).click();
  assert.equal(await secret.evaluate(input=>input.value),'');await secret.dispose();
  assert.equal(await page.getByLabel('Секрет проверки',{exact:true}).count(),0);
  secret=await enrollment();await home();
  assert.equal(await secret.evaluate(input=>input.value),'');await secret.dispose();
  await count(page,'.wg-fleet-card',0);

  await role('administrator',['servers_read']);await open();await layout(page);
  const writesBefore=writes.length;
  assert.equal(await page.locator('.wg-fleet-add').count(),0);
  assert.equal(await page.locator('#wg-fleet-section .wg-fleet-actions button').count(),0);
  assert.equal(await page.locator('#wg-fleet-section input[type=password]').count(),0);
  assert.match(await page.locator('#wg-fleet-section').textContent(),/Просмотр серверов/);
  assert.equal(writes.length,writesBefore);
  await page.screenshot({path:path.join(output,'wg-fleet-'+width+'-readonly.png'),fullPage:true});
  await home();await count(page,'.wg-fleet-card',0);

  await role('owner');await open();secret=await enrollment();await page.locator('#exit').click();
  assert.equal(await secret.evaluate(input=>input.value),'');await secret.dispose();
  await count(page,'.wg-fleet-card',0);assert.equal(await page.locator('#dashboard').isVisible(),false);
  assert.equal(consoleText.some(text=>text.includes(panelToken)||text.includes(probeToken)),false,'Credentials must not be logged');
  assert.deepEqual(errors,[]);assert.deepEqual(unexpected,[]);
  return {width,checks:['role-scoped navigation','API versus traffic','separate endpoint IP','metadata preserves credentials',
   'same UUID after timed-out write','revoking and preparing are pending','one-time secret close/navigation/logout cleanup','no horizontal overflow']};
 }finally{await context.close();}
}

(async()=>{
 const browser=await chromium.launch({headless:true,args:['--no-sandbox']});
 try{
  const scenarios=[];for(const width of [390,1100])scenarios.push(await run(browser,width));
  console.log(JSON.stringify({status:'passed',scenarios,scope:'synthetic admin UI; no live API, DNS or VPN traffic'}));
 }finally{await browser.close();}
})().catch(error=>{console.error(error.message);process.exitCode=1;});
