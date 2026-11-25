# Автоматические обновления Shop рядом с MTProxy

Начиная с v0.064, GitHub Release создаётся **автоматически после успешного CI**
при появлении новой версии в CHANGELOG на main. Это только публикация; VPS
использует отдельный безопасный системный таймер.

## Один раз на действующем сервере

Установка рассчитана **исключительно** на существующий `/opt/pushok-shop`,
контейнер `pushok-shop`, файлы `compose.mtproxy-edge.yaml` и локальный
`compose.wg-ip.yaml`, постоянный Docker-том `pushok_shop_data`. Не запускайте
этот инструмент для `/opt/pushok-vpn` с другим установщиком.

После обновления исходников (только fast-forward) запустите под root:

~~~bash
cd /opt/pushok-shop
git pull --ff-only origin main
python3 scripts/edge_auto_update.py plan
python3 scripts/edge_auto_update.py install --run-now
~~~

Для перехода с v0.068 на v2.000 один раз обновите эти скрипты на VPS приведёнными выше командами. Ранее установленный таймер распознаёт только последовательные номера внутри одной основной версии.

Первая команда загрузит опубликованный код с GitHub. `plan` ничего не меняет.
`install --run-now` **однократно** создаёт пароль резервных копий в закрытом
файле `/etc/pushok/shop-edge-autoupdate/backup-password` с правами 0600,
устанавливает systemd timer и запускает первую проверку/установку.

**Сохраните защищённую копию пароля вне VPS.** Потеря пароля делает созданные
зашифрованные бэкапы непригодными для восстановления. Не отправляйте пароль,
`.env`, ссылки конфигураций или закрытые ключи в чат/скриншоты/GitHub.

## После активации

~~~bash
systemctl status pushok-shop-edge-autoupdate.timer --no-pager
systemctl list-timers --all 'pushok-shop-edge-autoupdate*'
python3 /opt/pushok-shop/scripts/edge_auto_update.py status
curl -fsS https://pushok.shop/shop/api/version
~~~

После установки таймера из v0.066 он проверяет релизы каждые пять минут с небольшой случайной задержкой и после
восстановления VPS. Использует только опубликованные **нечерновые** GitHub
Releases официального репозитория. Если сервер пропустил несколько версий,
каждый запуск применяет только следующую по порядку; миграция через пробел
останавливается для ручной проверки. Все сборки привязаны к SHA
официального коммита.

Для обновления вызывается `scripts/upgrade_mtproxy_edge.py`, который
проверяет Compose hash, контейнер, image, том, `.env`, создаёт и проверяет
зашифрованный SQLite backup и пересоздаёт **только** Shop. Не применяется
`docker compose down`, не удаляется том и не перезапускаются MTProxy, Caddy,
Pushok ID и WGDashboard.

При сбое повторная установка блокируется; изучите:

~~~bash
journalctl -u pushok-shop-edge-autoupdate.service -n 80 --no-pager
python3 /opt/pushok-shop/scripts/upgrade_mtproxy_edge.py status
~~~

Остановить автоматические установки (без удаления настроек и архивов):

~~~bash
systemctl disable --now pushok-shop-edge-autoupdate.timer
~~~

Для включения после устранения проблемы:

~~~bash
systemctl enable --now pushok-shop-edge-autoupdate.timer
~~~

## Что остаётся внешней зависимостью

GitHub Actions должен иметь доступ к опубликованному релизу. Стабильный
релиз Shop — не доказательство успешного подключения к WGDashboard или выдачи
пира пользователю; для запуска трафика требуется отдельный тест, а просроченную
подписку нельзя оживлять искусственным созданием клиента. Имеющаяся
инсталляция на VPS не меняется от одного только commit/merge GitHub.
