'use strict';
(function() {
  const button = document.getElementById('flow-import-open');
  const status = document.getElementById('flow-import-status');
  let subscription = PushokFlowLinks.parseFragment(location.hash);
  // The bearer token is never sent to the server and does not remain in browser history.
  try { history.replaceState(null, '', location.pathname); } catch { /* Private mode may deny history changes. */ }
  if (!subscription) {
    status.textContent = 'Не удалось прочитать подписку. Откройте её заново из личного кабинета.';
    return;
  }
  status.textContent = 'Добавьте подписку «' + subscription.name + '». Источник: ' + subscription.host + '.';
  button.disabled = false;
  button.addEventListener('click', () => {
    if (!subscription || button.disabled) return;
    const target = PushokFlowLinks.appUrl(subscription.url, subscription.name);
    if (!target) return;
    status.textContent = 'Открываем Pushok Flow. Если приложение не открылось, установите его по ссылке ниже.';
    window.location.assign(target);
  });
  // A page restored from bfcache must not keep a previously copied credential ready to launch.
  window.addEventListener('pagehide', () => {
    subscription = null;
    button.disabled = true;
    status.textContent = 'Вернитесь в личный кабинет и откройте подписку ещё раз.';
  });
})();
