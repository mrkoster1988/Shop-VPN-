// Real Chromium smoke for compact Shop; private data never enters the fixture.
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const {chromium}=require('playwright');
const root=path.resolve(__dirname,'..');
const output=process.env.STOREFRONT_UI_OUTPUT||'/tmp/pushok-storefront-ui';
fs.mkdirSync(output,{recursive:true});
const catalog={
 guest_access:true, protocols:[{id:'xui',available:true},{id:'wg',available:true},{id:'vmess',available:false}],
 currency:'RUB',payment_available:true,payment_mode:'manual',payment_policy_version:1,
 support:'Поддержка Pushok',terms:'Тестовые условия',
 plans:[
  {id:'month',name:'Месяц',price:199,days:30,version:1,device_limit:4},
  {id:'quarter',name:'Три месяца',price:499,days:90,version:1,device_limit:4},
  {id:'year',name:'Год',price:1499,days:365,version:1,device_limit:4}
 ]
};
const customer={id:'fixture',telegram_id:123,subscriptions:[],orders:[],can_change_password:false};

(async()=>{
 const browser=await chromium.launch({headless:true,args:['--no-sandbox']});
 const results=[];
 try{
  for(const {width,telegram,authenticated} of [
   {width:320,telegram:false,authenticated:false},
   {width:390,telegram:false,authenticated:false},
   {width:390,telegram:false,authenticated:true},
   {width:390,telegram:true,authenticated:true},
   {width:768,telegram:false,authenticated:true},
   {width:1100,telegram:false,authenticated:true}
  ]){
   const page=await browser.newPage({viewport:{width,height:844}});
   const errors=[],unexpected=[];
   page.on('pageerror',err=>errors.push(err.message));
   if(telegram)await page.addInitScript(()=>{
    window.Telegram={WebApp:{initData:'synthetic',colorScheme:'dark',
      ready(){},expand(){},onEvent(){},
      BackButton:{onClick(){},show(){},hide(){}}}};
   });
   await page.route('**/*',async route=>{
    const request=route.request(),url=new URL(request.url()),p=url.pathname;
    if(url.pathname==='/shop/static/vendor/telegram-web-app.baa788a6f686.js' && url.hostname==='pushok.test')
      return route.fulfill({contentType:'text/javascript',body:telegram?'':fs.readFileSync(root+'/app/static/vendor/telegram-web-app.baa788a6f686.js')});
    if(url.hostname==='fonts.googleapis.com')return route.fulfill({contentType:'text/css',body:''});
    if(url.hostname!=='pushok.test'){unexpected.push(url.href);return route.abort();}
    if(p==='/shop/')return route.fulfill({contentType:'text/html',body:fs.readFileSync(root+'/app/static/index.html','utf8').replaceAll('__BASE_PATH__','/shop')});
    if(p.startsWith('/shop/static/')){
     const file=p.split('/').at(-1);
     if(['app.js','flow-links.js','style.css','storefront.css','pushok-cat.webp','pushok-icons.svg','flow-map.webp','flow-stars.webp'].includes(file)){
      const contentType=file.endsWith('.js')?'text/javascript':file.endsWith('.webp')?'image/webp':file.endsWith('.svg')?'image/svg+xml':'text/css';
      return route.fulfill({contentType,body:fs.readFileSync(root+'/app/static/'+file)});
     }
    }
    if(p==='/shop/api/auth/telegram')return route.fulfill({json:{ok:true}});
    if(p==='/shop/api/catalog')return route.fulfill({json:catalog});
    if(p==='/shop/api/auth/telegram')return route.fulfill({json:{ok:true}});
    if(p==='/shop/api/guest/browser'){
     const {activate}=request.postDataJSON();
     return route.fulfill({json:activate?{status:'pending',expires_at:1900000000}:{status:'available'}});
    }
    if(p==='/shop/api/me')return route.fulfill(authenticated?{json:customer}:{status:401,json:{detail:'Войдите'}});
    if(p==='/shop/api/support/tickets')return route.fulfill({json:{cases:[]}});
    unexpected.push(p);return route.fulfill({status:404,json:{detail:'Unexpected fixture'}});
   });
   try{
    await page.goto('http://pushok.test/shop/');
    await page.waitForFunction(()=>document.querySelectorAll('#plan-list [data-plan]').length===3);
    const metrics=await page.evaluate(()=>{
     const box=id=>{const r=document.getElementById(id).getBoundingClientRect();return {top:r.top,height:r.height};};
     return {
      overflow:document.documentElement.scrollWidth>innerWidth,
      docWidth:document.documentElement.scrollWidth,
      bodyWidth:document.body.scrollWidth,
      offenders:[...document.querySelectorAll('body *')].map(node=>{
        const r=node.getBoundingClientRect();
        return {tag:node.tagName,id:node.id,className:typeof node.className==='string'?node.className.slice(0,50):'',left:Math.round(r.left),right:Math.round(r.right),width:Math.round(r.width)};
      }).filter(x=>x.width&& (x.left < -5 || x.right > innerWidth+5)).slice(0,15),
      plans:box('plans'),hero:box('home'),
      visual:getComputedStyle(document.querySelector('.hero .visual')).display,
      mobileNav:getComputedStyle(document.querySelector('.shop-mobile-nav')).display
     };
    });
    assert.equal(metrics.overflow,false,'No horizontal scroll: '+JSON.stringify(metrics));
    assert.equal(metrics.visual==='none',width<=680);
    if(width<=680){
     assert.ok(metrics.plans.top<520,'Plans must start near the top on mobile');
     assert.equal(metrics.mobileNav,telegram?'none':'grid');
    }else assert.equal(metrics.mobileNav,'none');
    await page.locator('#connection-options summary').click();
    await page.locator('#protocols [data-protocol="wg"]').click();
    assert.equal(await page.locator('#protocols [data-protocol="wg"]').getAttribute('aria-pressed'),'true');
    await page.locator('#protocols [data-protocol="xui"]').click();
    await page.locator('#how summary').click();
    assert.equal(await page.locator('#how').evaluate(el=>el.querySelector('details').open),true);
    assert.equal(await page.locator('#how .steps article').count(),3);
    if(telegram)await page.locator('#miniapp-account').click();
    else if(width<=680)await page.locator('#shop-mobile-account').click();
    else await page.locator('#hero-account-btn').click();
    await page.locator(authenticated?'#account-dialog':'#auth-dialog').waitFor({state:'visible',timeout:10000});
    if(authenticated){
     const nav=page.locator('#account-dialog .account-shortcuts');
     assert.equal(await page.locator('#subscriptions-pane').isVisible(),true);
     assert.equal(await page.locator('#orders-pane').isVisible(),false);
     await nav.locator('[data-account-section="orders"]').click();
     assert.equal(await page.locator('#orders-pane').isVisible(),true);
     assert.equal(await page.locator('#subscriptions-pane').isVisible(),false);
     assert.equal(await nav.locator('[data-account-section="orders"]').getAttribute('aria-pressed'),'true');
     await nav.locator('[data-account-section="support-panel"]').click();
     assert.equal(await page.locator('#support-panel').isVisible(),true);
     assert.equal(await page.locator('#orders-pane').isVisible(),false);
     await page.locator('#account-dialog [data-close="account-dialog"]').click();
    }else{
     await page.locator('#auth-guest').click();
     await page.getByText(/Готовим ваше подключение/).waitFor();
     assert.equal(await page.locator('#guest-home').isVisible(),true);
     await page.locator('#plan-list [data-plan="month"]').click();
     await page.locator('#auth-dialog').waitFor({state:'visible',timeout:10000});
     await page.locator('#auth-dialog [data-close="auth-dialog"]').click();
    }
    await page.screenshot({path:path.join(output,'shop-'+width+'-'+(telegram?'telegram':'browser')+'-'+(authenticated?'customer':'guest')+'.png'),fullPage:true});
    assert.deepEqual(errors,[]);
    assert.deepEqual(unexpected,[]);
    results.push({width,telegram,authenticated,plansTop:Math.round(metrics.plans.top)});
   }finally{await page.close();}
  }
 }finally{await browser.close();}
 console.log(JSON.stringify({status:'passed',scenarios:results,checks:['short hero','compact tariffs','browser-only bottom tabs','Mini App','protocol switches','purchase auth','customer tabs','no overflow','no browser errors']}));
})().catch(err=>{console.error(err);process.exit(1)});
