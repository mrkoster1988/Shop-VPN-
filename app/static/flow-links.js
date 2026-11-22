'use strict';
(function(scope) {
  const fallbackName = 'Подписка Pushok';
  function normalizeSubscription(raw) {
    if (typeof raw !== 'string') return null;
    const text = raw.trim();
    if (!text || text.length > 4096 || !/^https:\/\/[^/?#]+/i.test(text) || text.includes('#') ||
        /^https:\/\/[^/?#]*@/i.test(text) || /[\s\u0000-\u001f\u007f-\u009f\\]/u.test(text) || /%(?![0-9a-f]{2})/i.test(text)) return null;
    let value;
    try { value = new URL(text); } catch { return null; }
    if (value.protocol !== 'https:' || value.username || value.password || value.hash || !value.hostname || value.port === '0') return null;
    const host = value.hostname.toLowerCase();
    if (host === 'localhost' || /\.(localhost|local|lan|home|internal)$/.test(host) || host.endsWith('.')) return null;
    if (host.startsWith('[')) {
      // Public global-unicast IPv6 only; never loopback, local or IPv4-mapped aliases.
      if (!/^\[[23][0-9a-f]{3}:/.test(host)) return null;
    } else if (/^[0-9.]+$/.test(host)) {
      const parts = host.split('.').map(Number);
      if (parts.length !== 4 || parts.some(part => !Number.isInteger(part) || part < 0 || part > 255)) return null;
      const [a,b] = parts;
      if (a === 0 || a === 10 || a === 127 || a >= 224 || (a === 100 && b >= 64 && b <= 127) ||
          (a === 169 && b === 254) || (a === 172 && b >= 16 && b <= 31) ||
          (a === 192 && (b === 168 || b === 0)) || (a === 198 && (b === 18 || b === 19))) return null;
    } else if (host.length > 253 || !host.includes('.') || !host.split('.').every(label =>
      /^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/.test(label))) return null;
    const result = value.href;
    return result.length <= 4096 ? result : null;
  }
  function safeName(raw) {
    if (typeof raw !== 'string') return fallbackName;
    const name = raw.trim();
    return name && Array.from(name).length <= 64 && !/[\u0000-\u001f\u007f-\u009f\u2028\u2029\u202a-\u202e\u2066-\u2069]/u.test(name)
      ? name : fallbackName;
  }
  function appUrl(raw, name) {
    const url = normalizeSubscription(raw);
    return url ? 'pushokflow://subscription?url=' + encodeURIComponent(url) + '&name=' + encodeURIComponent(safeName(name)) : null;
  }
  function landingUrl(raw, name, origin, basePath) {
    const url = normalizeSubscription(raw);
    if (!url || typeof origin !== 'string' || !/^https:\/\//.test(origin) ||
        typeof basePath !== 'string' || !/^(?:\/[A-Za-z0-9_-]+)*$/.test(basePath)) return null;
    try {
      const target = new URL(basePath + '/static/flow-import.html', origin);
      if (target.origin !== origin || target.username || target.password || target.search) return null;
      target.hash = 'url=' + encodeURIComponent(url) + '&name=' + encodeURIComponent(safeName(name));
      return target.href;
    } catch { return null; }
  }
  function parseFragment(raw) {
    if (typeof raw !== 'string' || raw.length > 16384 || !raw.startsWith('#')) return null;
    const params = new URLSearchParams(raw.slice(1));
    if (Array.from(params.keys()).some(key => !['url','name'].includes(key)) ||
        params.getAll('url').length !== 1 || params.getAll('name').length > 1) return null;
    const url = normalizeSubscription(params.get('url'));
    return url ? Object.freeze({url, name:safeName(params.get('name')), host:new URL(url).hostname}) : null;
  }
  const api = Object.freeze({normalizeSubscription, safeName, appUrl, landingUrl, parseFragment});
  if (typeof module === 'object' && module.exports) module.exports = api;
  else scope.PushokFlowLinks = api;
})(globalThis);
