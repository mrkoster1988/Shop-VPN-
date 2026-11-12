// Настоящий DOM и SVG в двух движках. Все ответы и снимки — локальные фикстуры.
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const {chromium,webkit}=require('playwright');
const root=path.resolve(__dirname,'..'),output=process.env.ICON_UI_OUTPUT || '/tmp/pushok-icons-ui';
fs.mkdirSync(output,{recursive:true});
const catalog={protocols:[{id:'xui',available:true},{id:'wg',available:true}],currency:'RUB',support:'Поддержка Pushok',terms:'Тестовые условия',plans:[{id:'month',name:'Месяц',price:300,days:30,version:1,device_limit:4}]};
const customer={id:'fixture',telegram_id:123,subscriptions:[],orders:[],can_change_password:false};
(async()=>{
 let cases=0;
 for(const [engine,type] of Object.entries({chromium,webkit})){
  const browser=await type.launch({headless:true});
  try{
   const scenarios=[];
   for(const base of ['', '/shop'])for(const theme of ['browser','light','dark'])for(const width of [1100,390])scenarios.push({base,theme,width,missing:false});
   scenarios.push({base:'/shop',theme:'light',width:390,missing:true});
   for(const {base,theme,width,missing} of scenarios){
    const page=await browser.newPage({viewport:{width,height:844}}),errors=[],unexpected=[],sprites=[];
    let signedIn=theme!=='browser';
    page.on('pageerror',error=>errors.push(error.message));
    if(theme!=='browser')await page.addInitScript(colorScheme=>{
     window.Telegram={WebApp:{initData:'fixture',colorScheme,ready(){},expand(){},onEvent(){},BackButton:{onClick(){},show(){},hide(){}}}};
    },theme);
    await page.route('**/*',async route=>{
     const request=route.request(),url=new URL(request.url()),p=url.pathname;
     if(url.href==='https://telegram.org/js/telegram-web-app.js')return route.fulfill({contentType:'text/javascript',body:''});
     // Проверяем и системный шрифт: внешний CDN не нужен для работы значков.
     if(url.hostname==='fonts.googleapis.com')return route.fulfill({contentType:'text/css',body:''});
     if(url.hostname!=='pushok.test'){unexpected.push(url.href);return route.abort();}
     if(p===base+'/')return route.fulfill({contentType:'text/html',body:fs.readFileSync(root+'/app/static/index.html','utf8').replaceAll('__BASE_PATH__',base)});
     if(p.startsWith(base+'/static/')){
      const file=p.slice((base+'/static/').length);
      if(['app.js','style.css','pushok-icons.svg'].includes(file)){
       if(file==='pushok-icons.svg'){sprites.push(p);if(missing)return route.fulfill({status:404,body:''});}
       return route.fulfill({contentType:file.endsWith('.js')?'text/javascript':file.endsWith('.svg')?'image/svg+xml':'text/css',body:fs.readFileSync(root+'/app/static/'+file)});
      }
     }
     if(p===base+'/api/catalog')return route.fulfill({json:catalog});
     if(p===base+'/api/me')return route.fulfill(signedIn?{json:customer}:{status:401,json:{detail:'Войдите'}});
     if(p===base+'/api/auth/telegram')return route.fulfill({json:{ok:true}});
     if(p===base+'/api/auth/logout'){signedIn=false;return route.fulfill({json:{ok:true}});}
     if(p===base+'/api/support/tickets')return route.fulfill({json:{cases:[]}});
     unexpected.push(p);return route.fulfill({status:404,json:{detail:'Unexpected fixture request'}});
    });
    try{
     await page.goto('http://pushok.test'+base+'/');
     await page.waitForFunction(()=>typeof me!=='undefined' && catalog && document.querySelector('#plan-list button'));
     assert.equal(await page.locator('.pushok-icon').count(),6,'Все значки сохранены после загрузки аккаунта');
     assert.ok(sprites.length>0,'Браузер запросил внешний SVG');
     assert.ok(sprites.every(p=>p===base+'/static/pushok-icons.svg'));
     const visible=page.locator('.pushok-icon:visible');
     // В Mini App верхняя кнопка кабинета штатно скрыта: её заменяет нижняя.
     assert.equal(await visible.count(),theme==='browser'?2:5);
     assert.equal(await page.locator('#account-btn').isVisible(),theme==='browser');
     for(const icon of await visible.all()){
      assert.equal(await icon.getAttribute('aria-hidden'),'true');
      assert.equal(await icon.getAttribute('focusable'),'false');
      if(!missing)await page.waitForFunction(el=>{const b=el.querySelector('use').getBBox();return b.width>0 && b.height>0;},await icon.elementHandle());
     }
     assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),'Нет горизонтального скролла');
     const name=engine+'-'+(base?'shop':'root')+'-'+theme+'-'+width+(missing?'-no-sprite':'');
     await page.screenshot({path:path.join(output,name+'.png')});
     // Опциональные компактные превью только фикстур для приёмки через CI-логи.
     if(process.env.ICON_UI_LOG_PREVIEWS==='1' && engine==='chromium' && base==='/shop' && !missing && (theme==='browser'&&width===1100 || theme!=='browser'&&width===390)){
      const shot=await page.screenshot({type:'jpeg',quality:45});
      console.log('ICON_PREVIEW '+name+' '+shot.toString('base64'));
     }
     await page.locator('.hero a.button').click();assert.equal(new URL(page.url()).hash,'#plans');
     if(theme==='browser'){
      await page.locator('#account-btn').click();await page.locator('#auth-dialog').waitFor({state:'visible'});
     }else{
      await page.locator('#miniapp-account').click();await page.locator('#account-dialog').waitFor({state:'visible'});
      assert.equal(await page.locator('#miniapp-account').getAttribute('aria-current'),'page');
      await page.locator('[data-close="account-dialog"]').click();
      await page.locator('[data-miniapp-route="help"]').click();assert.equal(new URL(page.url()).hash,'#help');
      await page.locator('#miniapp-account').click();await page.locator('#logout').click();
      await page.waitForFunction(()=>document.getElementById('account-label').textContent==='Личный кабинет ↗' && !document.getElementById('account-dialog').open);
      assert.equal(await page.locator('#account-btn .pushok-icon').count(),1,'Выход сохраняет значок');
     }
     assert.deepEqual(errors,[]);assert.deepEqual(unexpected,[]);cases++;
     console.log('PASS '+name);
    }finally{await page.close();}
   }
  }finally{await browser.close();}
 }
 console.log(JSON.stringify({status:'passed',cases,engines:['chromium','webkit'],checks:['root and /shop','external SVG rendering','account refresh and logout','light/dark','desktop/mobile','missing SVG fallback','navigation','decorative accessibility','no overflow']}));
})().catch(error=>{console.error(error);process.exit(1);});
