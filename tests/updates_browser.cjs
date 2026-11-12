const fs=require('node:fs'),assert=require('node:assert/strict');
const {chromium}=require('playwright');
const path=require('node:path');
const root=path.resolve(__dirname,'..'),output=process.env.PANEL_UI_OUTPUT||'/tmp';
fs.mkdirSync(output,{recursive:true});
(async()=>{
 const browser=await chromium.launch({headless:true,args:['--no-sandbox']});
 const page=await browser.newPage({viewport:{width:1100,height:900}});
 const errors=[],writes=[];let networkFailed=false;
 let status={current_version:'v0.055',state:'available',checking:false,
  settings:{enabled:true,check_time:'21:00',timezone:'Europe/Moscow',version:1},scheduler_running:true,
  last_attempt:1794502800,last_success:1794502800,next_check:1794506400,error_code:null,stale:false,
  release:{tag:'v0.056',notes:'Исправления и улучшения\n<img src=x onerror="window.injected=true">',notes_truncated:false},
  can_open_guide:true,update_available:true};
 page.on('pageerror',e=>errors.push(e.message));
 await page.route('**/*',async route=>{
  const request=route.request(),url=new URL(request.url()),p=url.pathname;
  if(url.hostname!=='pushok.test')return route.abort();
  if(p==='/shop/admin')return route.fulfill({contentType:'text/html',body:fs.readFileSync(root+'/app/static/admin.html','utf8').replaceAll('__BASE_PATH__','/shop')});
  if(p.startsWith('/shop/static/'))return route.fulfill({contentType:p.endsWith('.js')?'text/javascript':'text/css',body:fs.readFileSync(root+'/app/static/'+p.split('/').pop(),'utf8')});
  if(p==='/shop/api/admin/updates')return route.fulfill({json:status});
  if(p==='/shop/api/admin/updates/schedule'){
   const body=request.postDataJSON();writes.push(body);status={...status,settings:{...body,version:body.version+1}};
   return route.fulfill({json:status});
  }
  if(p==='/shop/api/admin/updates/check'){
   if(networkFailed)status={...status,state:'check_failed',error_code:'network_error',stale:true,can_open_guide:false};
   return route.fulfill({json:status});
  }
  return route.fulfill({status:401,json:{detail:'Fixture only'}});
 });
 await page.goto('http://pushok.test/shop/admin');
 await page.evaluate(async()=>{role='owner';document.getElementById('dashboard').hidden=false;document.getElementById('admin-login').hidden=true;await refreshUpdates();});
 assert.equal(await page.locator('#updates-time').inputValue(),'21:00');
 assert.equal(await page.locator('#updates-timezone').inputValue(),'Europe/Moscow');
 await page.locator('#updates-release summary').click();
 assert.equal(await page.locator('#updates-notes img').count(),0);
 assert.ok(!(await page.evaluate(()=>window.injected)));
 await page.locator('#updates-panel').screenshot({path:path.join(output,'updates-desktop.png')});
 await page.setViewportSize({width:390,height:844});
 assert.ok(await page.locator('#updates-open').isVisible());
 assert.ok(await page.evaluate(()=>{const e=document.getElementById('updates-panel');return e.scrollWidth<=e.clientWidth;}));
 await page.locator('#updates-panel').screenshot({path:path.join(output,'updates-mobile.png')});
 await page.locator('#updates-time').fill('22:30');await page.locator('#updates-save').click();
 await page.waitForFunction(()=>document.getElementById('updates-error').textContent==='Расписание сохранено.');
 assert.deepEqual(writes,[{enabled:true,check_time:'22:30',timezone:'Europe/Moscow',version:1}]);
 await page.locator('#updates-open').click();assert.ok(await page.locator('#updates-dialog').isVisible());
 assert.equal(await page.locator('#updates-guide-link').getAttribute('href'),'https://github.com/mrkoster1988/Shop-VPN-/blob/v0.056/docs/UPGRADE.md');
 assert.ok(await page.evaluate(()=>{const e=document.getElementById('updates-dialog');return e.scrollWidth<=e.clientWidth;}));
 await page.locator('#updates-dialog').screenshot({path:path.join(output,'updates-guide-mobile.png')});
 await page.locator('#updates-dialog-close').click();networkFailed=true;await page.locator('#updates-check').click();
 await page.waitForFunction(()=>document.getElementById('updates-status').textContent.includes('Нет связи'));
 assert.equal(await page.locator('#updates-open').isVisible(),false);
 assert.equal(await page.locator('#updates-latest').textContent(),'v0.056');
 assert.deepEqual(errors,[]);
 console.log(JSON.stringify({status:'passed',viewports:[1100,390],checks:['default schedule','safe notes','schedule save','pinned manual guide','mobile layout','failure keeps previous release without update action','no browser errors']}));
 await browser.close();
})().catch(e=>{console.error(e);process.exit(1)});
