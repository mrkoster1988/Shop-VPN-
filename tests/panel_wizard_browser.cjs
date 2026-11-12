const fs=require('node:fs'),assert=require('node:assert/strict');
const {chromium}=require('playwright');
const path=require('node:path');
const root=path.resolve(__dirname,'..');
const output=process.env.PANEL_UI_OUTPUT || '/tmp';
fs.mkdirSync(output,{recursive:true});
(async()=>{
 const browser=await chromium.launch({headless:true,args:['--no-sandbox']});
 const page=await browser.newPage({viewport:{width:1100,height:900}});
 const errors=[],writes=[];let row=null;
 page.on('pageerror',e=>errors.push(e.message));
 await page.route('**/*',async route=>{
  const request=route.request(),url=new URL(request.url()),p=url.pathname;
  if(url.hostname!=='pushok.test')return route.abort();
  if(p==='/shop/admin')return route.fulfill({contentType:'text/html',body:fs.readFileSync(root+'/app/static/admin.html','utf8').replaceAll('__BASE_PATH__','/shop')});
  if(p.startsWith('/shop/static/'))return route.fulfill({contentType:p.endsWith('.js')?'text/javascript':'text/css',body:fs.readFileSync(root+'/app/static/'+p.split('/').pop(),'utf8')});
  if(p==='/shop/api/admin/servers'&&request.method()==='POST'){
   const body=request.postDataJSON();writes.push({path:p,body});row={id:'fixture',name:body.name,location:body.location,capacity:body.capacity,protocol:body.protocol,profile:'clients_vless',state:'disabled',health:'unknown',version:1,occupied:0,bound_subscriptions:0};
   return route.fulfill({status:201,json:row});
  }
  if(p==='/shop/api/admin/servers/fixture/credentials'){
   const body=request.postDataJSON();writes.push({path:p,body});row={...row,version:2};return route.fulfill({json:row});
  }
  if(p==='/shop/api/admin/servers')return route.fulfill({json:{servers:row?[row]:[],unassigned_subscriptions:0}});
  return route.fulfill({status:401,json:{detail:'Fixture only'}});
 });
 await page.goto('http://pushok.test/shop/admin');
 await page.evaluate(()=>{role='owner';document.getElementById('dashboard').hidden=false;document.getElementById('admin-login').hidden=true;});
 await page.locator('#new-server').click();await page.locator('#server-next').click();
 assert.equal(await page.locator('#server-step-1').isVisible(),true);
 await page.locator('#server-form [name=name]').fill('Германия · VLESS');
 await page.locator('#server-form [name=location]').fill('Германия');
 await page.locator('#server-next').click();
 await page.locator('#server-api').selectOption('clients');
 await page.locator('#server-form [name=url]').fill('https://panel.example/private-path');
 await page.locator('#server-form [name=api_token]').fill('fixture-token-'+'x'.repeat(32));
 await page.locator('#server-form [name=inbound_id]').fill('7');
 await page.locator('#server-form [name=link_template]').fill('vless://{uuid}@vpn.example:443?security=tls');
 await page.setViewportSize({width:390,height:844});
 assert.ok(await page.locator('#server-step-2').isVisible());
 assert.ok(await page.evaluate(()=>document.getElementById('server-dialog').scrollWidth<=document.getElementById('server-dialog').clientWidth));
 await page.screenshot({path:path.join(output,'panel-wizard-mobile.png')});
 await page.locator('#server-next').click();assert.ok(await page.locator('#server-step-3').isVisible());
 const summary=await page.locator('#server-review').textContent();assert.ok(!summary.includes('fixture-token')&&!summary.includes('private-path'));
 await page.setViewportSize({width:1100,height:900});
 await page.screenshot({path:path.join(output,'panel-wizard-desktop.png')});
 await page.locator('#server-save').click();await page.waitForFunction(()=>!document.getElementById('server-dialog').open);
 await page.locator('[data-server-credentials=fixture]').click();
 await page.locator('#server-credentials-token').fill('new-fixture-token-'+'z'.repeat(32));
 await page.setViewportSize({width:390,height:844});
 assert.ok(await page.evaluate(()=>document.getElementById('server-credentials-dialog').scrollWidth<=document.getElementById('server-credentials-dialog').clientWidth));
 await page.screenshot({path:path.join(output,'panel-credentials-mobile.png')});
 await page.locator('#server-credentials-save').click();await page.waitForFunction(()=>!document.getElementById('server-credentials-dialog').open);
 assert.equal(await page.locator('#server-credentials-token').inputValue(),'');
 assert.equal(writes.length,2);assert.deepEqual(Object.keys(writes[1].body).sort(),['token','version']);
 assert.deepEqual(errors,[]);
 console.log(JSON.stringify({status:'passed',viewports:[1100,390],writes:2,checks:['required fields','wizard navigation','secret-free review','profile input flow','credential-only payload','mobile layout','no browser errors']}));
 await browser.close();
})().catch(e=>{console.error(e);process.exit(1)});
