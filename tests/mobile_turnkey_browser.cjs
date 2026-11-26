// Full mobile layout and navigation acceptance with synthetic data.
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const {chromium,webkit}=require('playwright');
const root=path.resolve(__dirname,'..'),output=process.env.STOREFRONT_UI_OUTPUT||'/tmp/pushok-storefront-ui';
fs.mkdirSync(output,{recursive:true});
const origin='https://pushok.test',base='/shop',expiry=Math.floor(Date.now()/1000)+2592000;
const catalog={protocols:[{id:'xui',available:true},{id:'wg',available:true}],plans:[
 {id:'month',name:'Месяц',days:30,price:199,version:1,device_limit:4},
 {id:'quarter',name:'Три месяца',days:90,price:499,version:1,device_limit:4},
 {id:'free',name:'Бесплатное подключение',days:1,price:0,version:1,device_limit:0}],
 currency:'RUB',payment_available:true,payment_mode:'manual',payment_policy_version:1,support:'Тестовая поддержка',terms:'Тестовые условия',receipts:false};
const subscription={id:'fixture-wg',protocol:'wg',state:'active',expires:expiry,device_limit:4,primary_key_state:'active',access_source:'purchase'};
const order={id:'fixture-order',protocol:'wg',plan:'month',plan_name:'Месяц',days:30,device_limit:4,amount:179,base_amount:199,discount:20,promo_code:'MOBILE',currency:'RUB',status:'pending',method:'manual',kind:'purchase',created:1,instructions:'Только тестовый платёж.',progress:{title:'Ожидает оплаты',message:'Тестовый счёт'}};
const customer={id:1,email:'fixture@example.invalid',telegram_id:123,subscriptions:[subscription,{...subscription,id:'fixture-pending',state:'pending'}],orders:[],can_change_password:true,pushok_id:{linked:false},referrals:{policy:{enabled:true,days:1,min_rub:1,min_stars:1},balance_days:2,earned_days:2,spent_days:0,invited:1,rewarded:1,history:[],can_bind:false,can_share:false}};
const assets=new Set(['app.js','admin.js','flow-links.js','style.css','storefront.css','pushok-cat.webp','pushok-icons.svg']);
async function staticResponse(route,url){
 const p=url.pathname;
 if(p===base+'/'||p===base+'/admin'){
  await route.fulfill({contentType:'text/html',body:fs.readFileSync(root+'/app/static/'+(p.endsWith('/admin')?'admin.html':'index.html'),'utf8').replaceAll('__BASE_PATH__',base)});return true;
 }
 if(p.startsWith(base+'/static/')&&assets.has(p.split('/').at(-1))){
  const file=p.split('/').at(-1),types={js:'text/javascript',css:'text/css',svg:'image/svg+xml',webp:'image/webp'};
  await route.fulfill({contentType:types[file.split('.').at(-1)],body:fs.readFileSync(root+'/app/static/'+file)});return true;
 }
 return false;
}
async function layout(page,id){
 const data=await page.evaluate(id=>{
  const el=id?document.getElementById(id):document.documentElement,r=el.getBoundingClientRect();
  return {width:r.width,height:r.height,client:el.clientWidth,scroll:el.scrollWidth,viewport:innerWidth,vh:innerHeight,open:id?el.open:true};
 },id);
 assert.ok(data.open&&data.scroll<=data.client+2,'Horizontal overflow '+(id||'page')+': '+JSON.stringify(data));
 if(id){assert.ok(data.width>=data.viewport-2,'Not full-width '+JSON.stringify(data));assert.ok(data.height<=data.vh+2,'Dialog exceeds viewport '+JSON.stringify(data));}
}
async function close(page,id){
 const selector=id==='user-dialog'?'#user-close':id==='server-dialog'?'#server-close':'#'+id+' > [data-close="'+id+'"]';
 const button=page.locator(selector);
 await button.waitFor({state:'visible'});
 assert.ok((await button.boundingBox()).height>=43,'Close control too small');
 await button.click();await page.locator('#'+id).waitFor({state:'hidden'});
}
async function runShop(browser,engine,width,height,telegram,guest){
 const context=await browser.newContext({viewport:{width,height},isMobile:true,hasTouch:true,deviceScaleFactor:2,
 userAgent:engine==='webkit'?'Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Version/18.0 Mobile/15E148 Safari/604.1':'Mozilla/5.0 (Linux; Android 15) AppleWebKit/537.36 Chrome/140.0.0.0 Mobile Safari/537.36'});
 const page=await context.newPage();let authenticated=!guest,created=false;const errors=[],unexpected=[],writes=[];
 page.on('pageerror',e=>errors.push(e.message));
 if(telegram)await page.addInitScript(()=>{window.Telegram={WebApp:{initData:'fixture',colorScheme:'dark',ready(){},expand(){},onEvent(){},BackButton:{onClick(){},show(){},hide(){}}}};});
 await page.route('**/*',async route=>{
  const request=route.request(),url=new URL(request.url()),p=url.pathname;
  if(url.pathname===base+'/static/vendor/telegram-web-app.baa788a6f686.js' && url.hostname==='pushok.test')
      return route.fulfill({contentType:'text/javascript',body:telegram?'':fs.readFileSync(root+'/app/static/vendor/telegram-web-app.baa788a6f686.js')});
  if(url.hostname==='fonts.googleapis.com')return route.fulfill({contentType:'text/css',body:''});
  if(url.origin!==origin){unexpected.push(url.origin+p);return route.abort();}
  if(await staticResponse(route,url))return;
  const respond=(json,status=200)=>route.fulfill({json,status});
  if(p===base+'/api/catalog')return respond(catalog);
  if(p===base+'/api/me')return authenticated?respond({...customer,orders:created?[order]:[]}):respond({detail:'Войдите'},401);
  if([base+'/api/auth/telegram',base+'/api/auth/login',base+'/api/auth/register'].includes(p)){authenticated=true;return respond({ok:true});}
  if(p===base+'/api/auth/logout'){authenticated=false;return respond({ok:true});}
  if(p===base+'/api/support/tickets')return respond({cases:[]});
  if(p===base+'/api/quote')return respond({promo_code:'MOBILE',promo_version:1,promo_percent:10,amount:179,base_amount:199,discount:20,currency:'RUB'});
  if(p===base+'/api/orders'&&request.method()==='POST'){created=true;writes.push(request.postDataJSON());return respond(order);}
  if(p===base+'/api/orders/fixture-order')return respond(order);
  if(p===base+'/api/subscriptions/fixture-wg/config')return respond({protocol:'wg',config:'[Interface]\\n# synthetic fixture only\\n[Peer]'});
  if(p===base+'/api/subscriptions/fixture-wg/qr')return route.fulfill({contentType:'image/svg+xml',body:'<svg xmlns="http://www.w3.org/2000/svg" width="220" height="220"><rect width="220" height="220" fill="white"/></svg>'});
  if(p===base+'/api/flow/wireguard/subscriptions/fixture-wg/link')return respond({enabled:true,version:1,url:origin+base+'/flow/wireguard/'+('a'.repeat(43)),follow_rotation:false});
  if(p===base+'/api/subscriptions/fixture-wg/devices')return respond({max_extra:4,devices:[{id:'fixture-device',name:'Тестовое устройство',protocol:'wg',state:'active',ready:true,expires:expiry}]});
  unexpected.push(p);return respond({detail:'Unexpected fixture'},404);
 });
 try{
  await page.goto(origin+base+'/');await page.locator('#plan-list [data-plan="month"]').waitFor();
  await layout(page);
  const nav=page.locator(telegram?'#miniapp-account':'#shop-mobile-account');
  await nav.waitFor({state:'visible'});
  assert.equal(await page.locator('#shop-mobile-nav').isVisible(),!telegram);
  assert.ok((await nav.boundingBox()).height>=44);
  await page.locator('#connection-options summary').click();
  await page.locator('#protocols [data-protocol="wg"]').click();
  await page.locator('#plan-list [data-plan="month"]').click();
  if(guest){
   await page.locator('#auth-dialog').waitFor({state:'visible'});await layout(page,'auth-dialog');
   await page.locator('#auth-form [name=email]').fill('fixture@example.invalid');
   await page.locator('#auth-form [name=password]').fill('fixture-password-123');
   await page.locator('#auth-submit').click();
  }
  await page.locator('#checkout-dialog').waitFor({state:'visible'});await layout(page,'checkout-dialog');
  await page.locator('#promo-controls summary').click();
  await page.locator('#promo-code').fill('MOBILE');await page.locator('#promo-apply').click();
  await page.waitForFunction(()=>document.getElementById('checkout-info').textContent.includes('179'));
  await page.locator('#accept-terms').check();await page.locator('#checkout-submit').click();
  await page.waitForFunction(()=>document.getElementById('payment-result').textContent.includes('fixture-order'));
  assert.equal(writes.length,1);assert.equal(writes[0].protocol,'wg');
  await page.screenshot({path:path.join(output,'mobile-'+engine+'-'+width+'-checkout.png')});await close(page,'checkout-dialog');
  await nav.click();await page.locator('#account-dialog').waitFor({state:'visible'});await layout(page,'account-dialog');
  assert.ok(await page.locator('#subscriptions-pane').isVisible());
  assert.ok(!(await page.locator('#account-settings-pane').isVisible()));
  assert.equal(await page.locator('[data-config="fixture-pending"]').count(),0);
  await page.screenshot({path:path.join(output,'mobile-'+engine+'-'+width+'-account.png')});
  await page.locator('[data-link="fixture-wg"]').click();
  await page.waitForFunction(()=>document.getElementById('link-dialog').open&&!document.getElementById('flow-open').disabled);
  await layout(page,'link-dialog');await close(page,'link-dialog');
  await page.waitForFunction(()=>document.getElementById('link-value').value==='');
  await page.locator('[data-config="fixture-wg"]').click();
  await page.waitForFunction(()=>document.getElementById('config-dialog').open&&!document.getElementById('copy-config').disabled);
  await layout(page,'config-dialog');
  await page.locator('#toggle-config-qr').click();await page.locator('#config-qr-panel').waitFor({state:'visible'});
  await close(page,'config-dialog');await page.waitForFunction(()=>document.getElementById('config-value').value==='');
  assert.equal(await page.locator('#config-qr').getAttribute('src'),null);
  await page.locator('details[data-subscription-options="fixture-wg"] summary').click();
  await page.locator('[data-devices="fixture-wg"]').click();
  await page.locator('[data-device-config="fixture-device"]').waitFor({state:'visible'});
  await layout(page,'devices-dialog');await close(page,'devices-dialog');
  for(const [key,pane] of [['orders','orders-pane'],['support-panel','support-panel'],['referrals-panel','referrals-panel'],['settings','account-settings-pane']]){
   await page.locator('[data-account-section="'+key+'"]').click();
   assert.ok(await page.locator('#'+pane).isVisible());assert.ok(!(await page.locator('#subscriptions-pane').isVisible()));
   await layout(page,'account-dialog');
  }
  await page.screenshot({path:path.join(output,'mobile-'+engine+'-'+width+'-settings.png')});
  await page.locator('#logout').click();await page.locator('#account-dialog').waitFor({state:'hidden'});
  assert.equal(await page.locator('#account-summary').textContent(),'');
  await layout(page);assert.deepEqual(errors,[]);assert.deepEqual(unexpected,[]);
 }finally{await context.close();}
}
async function runAdmin(browser,engine,width,height){
 const context=await browser.newContext({viewport:{width,height},isMobile:true,hasTouch:true,deviceScaleFactor:2});
 const page=await context.newPage();const errors=[];
 page.on('pageerror',e=>errors.push(e.message));
 await page.route('**/*',async route=>{
  const url=new URL(route.request().url()),p=url.pathname;
  if(url.hostname==='fonts.googleapis.com')return route.fulfill({contentType:'text/css',body:''});
  if(url.origin!==origin)return route.abort();
  if(await staticResponse(route,url))return;
  if(p===base+'/api/admin/users')return route.fulfill({json:{users:[{id:1,email:'fixture@example.invalid',telegram_id:123,active_subscriptions:1,orders_count:1,created:1}],total:1,page:1,page_size:25}});
  if(p===base+'/api/admin/users/1')return route.fulfill({json:{user:{id:1,email:'fixture@example.invalid',telegram_id:123,created:1},subscriptions:[{id:'fixture-wg',protocol:'wg',state:'active',expires:expiry,device_limit:4,access_active:true}],orders:[],orders_page:1,orders_page_size:20,orders_total:0}});
  return route.fulfill({status:401,json:{detail:'Fixture only'}});
 });
 try{
  await page.goto(origin+base+'/admin');
  await page.evaluate(()=>{role='owner';document.getElementById('dashboard').hidden=false;document.getElementById('admin-login').hidden=true;renderManagementNav();});
  await layout(page);
  assert.equal(await page.locator('body').evaluate(el=>getComputedStyle(el).backgroundColor),'rgb(255, 255, 255)');
  assert.ok(await page.locator('#management-mobile-nav').isVisible());
  assert.ok(!(await page.locator('#management-menu-content').isVisible()));
  await page.locator('#management-mobile-nav [data-admin-target="users-section"]').click();
  await page.evaluate(()=>refreshUsers());
  assert.ok(await page.locator('#users-section').isVisible());assert.ok(!(await page.locator('#management-overview').isVisible()));
  await layout(page);await page.locator('[data-user="1"]').click();
  await page.locator('[data-peer-check="fixture-wg"]').waitFor({state:'visible'});
  await layout(page,'user-dialog');await page.screenshot({path:path.join(output,'mobile-'+engine+'-'+width+'-admin-user.png')});
  await close(page,'user-dialog');
  await page.locator('#management-mobile-nav [data-admin-menu]').click();
  assert.ok(await page.locator('#management-menu-content').isVisible());
  await page.locator('#management-search').fill('WireGuard');
  await page.locator('#management-nav [data-admin-target="servers-section"]').click();
  assert.ok(await page.locator('#servers-section').isVisible());assert.ok(!(await page.locator('#management-menu-content').isVisible()));
  await page.locator('#new-server').click();await page.locator('#server-dialog').waitFor({state:'visible'});
  await layout(page,'server-dialog');await close(page,'server-dialog');
  await page.evaluate(()=>{acceptStaffRole({role:'moderator',permissions:['moderation']});renderManagementNav();});
  assert.equal(await page.locator('#management-mobile-nav [data-admin-target="users-section"]').count(),0);
  assert.equal(await page.locator('#management-mobile-nav [data-admin-target="moderation-section"]').count(),1);
  assert.ok(await page.locator('#management-overview').isVisible());
  await page.screenshot({path:path.join(output,'mobile-'+engine+'-'+width+'-admin-moderator.png')});await layout(page);assert.deepEqual(errors,[]);
 }finally{await context.close();}
}
(async()=>{
 const scenarios=[];
 for(const [name,engine,width,height,telegram,guest] of [['chromium',chromium,320,568,false,true],['chromium',chromium,390,844,true,false],['webkit',webkit,390,844,false,true],['webkit',webkit,430,932,true,false]]){
  const browser=await engine.launch({headless:true,...(name==='chromium'?{args:['--no-sandbox']}:{})});
  try{await runShop(browser,name,width,height,telegram,guest);await runAdmin(browser,name,width,height);scenarios.push({engine:name,width,height,telegram,guest});}
  finally{await browser.close();}
 }
 console.log(JSON.stringify({status:'passed',scenarios,scope:'synthetic mobile UI; no real payments, VPN traffic or native installation'}));
})().catch(e=>{console.error(e);process.exit(1);});
