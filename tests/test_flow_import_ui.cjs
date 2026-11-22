const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const links = require('../app/static/flow-links.js');

const secret = 'https://vpn.example/sub/private-token?mode=a%2Bb&x=1';
const fragment = '#url=' + encodeURIComponent(secret) + '&name=' + encodeURIComponent('Моя подписка');

test('Flow URL helper preserves the subscription in a fragment and encodes native query values', () => {
  const page = new URL(links.landingUrl(secret, 'Моя подписка', 'https://shop.example', '/shop'));
  assert.equal(page.origin, 'https://shop.example');
  assert.equal(page.pathname, '/shop/static/flow-import.html');
  assert.equal(page.search, '');
  assert.equal(new URLSearchParams(page.hash.slice(1)).get('url'), secret);
  const native = new URL(links.appUrl(secret, 'Моя подписка'));
  assert.equal(native.protocol, 'pushokflow:');
  assert.equal(native.hostname, 'subscription');
  assert.equal(native.searchParams.get('url'), secret);
  assert.equal(native.searchParams.get('name'), 'Моя подписка');
  assert.equal(native.searchParams.size, 2);
});

test('Flow URLs reject local addresses, credentials, fragments and malformed input', () => {
  for (const value of [null, '', 'http://vpn.example/sub/x', 'javascript:alert(1)',
      'https:vpn.example/sub/x', 'https://localhost/sub/x', 'https://router.local/sub/x',
      'https://10.0.0.1/sub/x', 'https://127.0.0.1/sub/x', 'https://2130706433/sub/x',
      'https://0x7f000001/sub/x', 'https://192.168.0.1/sub/x', 'https://172.31.1.2/sub/x',
      'https://100.64.1.2/sub/x', 'https://169.254.1.1/sub/x', 'https://[::1]/sub/x',
      'https://[fd00::1]/sub/x', 'https://[::ffff:127.0.0.1]/sub/x',
      'https://u:p@vpn.example/sub/x', 'https://@vpn.example/sub/x',
      'https://vpn.example/sub/x#private', 'https://vpn.example/sub/x#',
      'https://vpn.example:0/sub/x', 'https://vpn.example/sub/\nx', 'https://vpn.example/sub/%xx',
      'https://vpn.example/sub/\\x', 'https://vpn.example/sub/' + 'x'.repeat(4096)]) {
    assert.equal(links.normalizeSubscription(value), null, String(value).slice(0, 80));
    assert.equal(links.appUrl(value, 'name'), null);
  }
  assert.equal(links.normalizeSubscription('https://8.8.8.8/sub/x'), 'https://8.8.8.8/sub/x');
  assert.equal(links.normalizeSubscription('https://[2606:4700:4700::1111]/sub/x'), 'https://[2606:4700:4700::1111]/sub/x');
});

test('Flow landing requires a same-origin HTTPS page and a safe mounted prefix', () => {
  for (const [origin, base] of [['http://shop.example','/shop'],['https://u:p@shop.example','/shop'],
      ['https://shop.example','//evil.example'],['https://shop.example','/shop/../admin'],
      ['https://shop.example','/shop?secret=x'],['https://shop.example/path','/shop']]) {
    assert.equal(links.landingUrl(secret, 'name', origin, base), null);
  }
  assert.equal(new URL(links.landingUrl(secret, 'name', 'https://shop.example', '')).pathname, '/static/flow-import.html');
});

test('Flow fragment is all-or-nothing and invalid names fall back safely', () => {
  assert.deepEqual(links.parseFragment(fragment), {url:secret, name:'Моя подписка', host:'vpn.example'});
  for (const value of ['', '?url=' + encodeURIComponent(secret), '#url=',
      fragment + '&url=' + encodeURIComponent(secret), fragment + '&name=other',
      fragment + '&unknown=secret', '#url=' + 'x'.repeat(16384)]) {
    assert.equal(links.parseFragment(value), null);
  }
  assert.equal(links.safeName('name\u202esecret'), 'Подписка Pushok');
  assert.equal(links.safeName('x'.repeat(65)), 'Подписка Pushok');
  assert.equal(links.safeName('  Мой доступ  '), 'Мой доступ');
});

function landing(hash, options={}) {
  const elements = new Map(), events = new Map(), navigations = [], replaced = [];
  function element(id) {
    if (!elements.has(id)) elements.set(id, {disabled:true,textContent:'',listeners:{},
      set innerHTML(_) { throw Error('Untrusted text must not be rendered as HTML'); },
      addEventListener(name, callback) { this.listeners[name] = callback; }});
    return elements.get(id);
  }
  const location = {hash,pathname:'/shop/static/flow-import.html',search:options.search || '',
    assign:target=>navigations.push(target)};
  const context = vm.createContext({URL, URLSearchParams,
    document:{getElementById:element}, location,
    history:{replaceState:(_,__,url)=>{replaced.push(url);location.hash='';location.search='';}},
    window:{location,addEventListener:(name,callback)=>events.set(name,callback)},
    fetch:()=>{throw Error('The landing must never fetch a subscription');}});
  vm.runInContext(fs.readFileSync('app/static/flow-links.js','utf8'), context);
  vm.runInContext(fs.readFileSync('app/static/flow-import.js','utf8'), context);
  return {element, events, navigations, replaced, location};
}

test('Flow landing consumes the fragment without requests or auto-launch and acts only on a click', () => {
  const page = landing(fragment);
  assert.deepEqual(page.replaced, ['/shop/static/flow-import.html']);
  assert.equal(page.location.hash, '');assert.equal(page.navigations.length, 0);
  assert.equal(page.element('flow-import-open').disabled, false);
  const status=page.element('flow-import-status').textContent;
  assert.ok(status.includes('vpn.example'));assert.ok(status.includes('Моя подписка'));
  assert.ok(!status.includes('private-token'));assert.ok(!status.includes(secret));
  page.element('flow-import-open').listeners.click();
  assert.equal(page.navigations.length, 1);
  assert.equal(new URL(page.navigations[0]).searchParams.get('url'), secret);
  page.events.get('pagehide')();
  assert.equal(page.element('flow-import-open').disabled, true);
  page.element('flow-import-open').listeners.click();
  assert.equal(page.navigations.length, 1);
});

test('Flow landing ignores query tokens and never enables malformed imports', () => {
  for (const hash of ['', '#url=javascript%3Aalert(1)', fragment + '&url=other']) {
    const page = landing(hash, {search:'?url=' + encodeURIComponent(secret)});
    assert.equal(page.element('flow-import-open').disabled, true);
    assert.equal(page.navigations.length, 0);assert.equal(page.location.search, '');
    assert.ok(!page.element('flow-import-status').textContent.includes('private-token'));
  }
});
