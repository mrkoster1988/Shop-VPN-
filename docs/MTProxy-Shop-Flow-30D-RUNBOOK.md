# Связка MTProxy → Shop → Flow: только серверная авторизация

Этот сценарий намеренно не подключает Pushok ID и не меняет существующий
Telegram-бот, Caddy, базу MTProxy или права разработчика.

## Предварительные условия

- Работают MTProxy API `https://pushok.shop/api/v1/flow/capabilities` и
  Shop `https://pushok.shop/shop/health` (ожидается HTTP 200).
- MTProxy находится в `/opt/mtproxy-pushok`, Shop в `/opt/pushok-shop`.
- В Shop установлен контейнер `pushok-shop` с
  `compose.mtproxy-edge.yaml`, доступом только через существующий Caddy.
- В Android Flow подтверждается запрос в *существующем* MTProxy-аккаунте,
  затем приложение вызывает `/api/v1/flow/shop-ticket` и серверный
  `/shop/api/flow/bridge/exchange`.

## 1. Безопасная подготовка (ничего не изменяет)

```bash
cd /opt/pushok-shop
git pull --ff-only
python3 scripts/prepare_flow_bridge.py
```

Скрипт выдаёт `DRY RUN: needs_setup` или `ready`, **не печатает секреты**.
Не запускать, если локальный `git pull` конфликтует с незакоммиченными правками.

## 2. Однократная подготовка серверного секрета

```bash
cd /opt/pushok-shop
python3 scripts/prepare_flow_bridge.py --apply
```

Скрипт атомарно сохраняет `FLOW_SHOP_BRIDGE_SECRET` одновременно в
`/opt/mtproxy-pushok/.env` и `/opt/pushok-shop/.env`. Если в одном файле уже
задан валидный ключ, он используется повторно; если ключи различаются —
скрипт останавливается без изменений. Резервные копии обеих `.env`
с правами 0600 создаются в закрытом каталоге `/root/pushok-flow-bridge-backups/`.
При ошибке записи делается попытка отката. Существующие токены, настройки
платежей, Caddy, Docker и Pushok ID не меняются. На Shop по умолчанию
добавляется `FLOW_TRIAL_MTPROXY_ONLY=true`: публичная регистрация Shop не
резервирует пробный WireGuard, пока сервер MTProxy не подтвердил личность.
Существующее явное значение этого переключателя сохраняется.

На этом этапе **сервисы ещё не перезапущены**, а `Flow`-автовыдача по-прежнему
`enabled=false`.

## 2а. Применение исправления PostgreSQL MTProxy

Перед перезапуском MTProxy API требуется миграция категории лимитера `shop`
(поправка MTProxy PR #204). Миграция уже включена в `main`, но **не должна запускаться с другой ветки**,
пока `database/076_flow_shop_rate_window.sql` не появился в текущем checkout
`/opt/mtproxy-pushok`. Для сервера с автообновлением `release/stable`
выпуск должен быть опубликован в стабильную ветку отдельно. Перед командами
проверьте `git branch --show-current` и отсутствие незакоммиченных изменений.
Выполнить резервное копирование и миграцию можно только после получения
исправленной стабильной версии:

```bash
cd /opt/mtproxy-pushok
git status --short
git branch --show-current
git pull --ff-only
test -s database/076_flow_shop_rate_window.sql || { echo "BLOCKED: 076 migration missing. Stop before DB changes."; false; }
python3 scripts/installer_runtime.py backup
mapfile -t migrations < <(find database -maxdepth 1 -type f -name '[0-9][0-9][0-9]_*.sql' ! -name '001_*' | sort)
python3 scripts/installer_runtime.py migrate "${migrations[@]}"
```

Штатный мигратор сверяет контрольные суммы, использует транзакцию PostgreSQL
и не меняет бизнес-данные. Если резервная копия либо миграция завершилась
ошибкой, **не перезапускайте API**. Миграция не относится к Pushok ID.

## 3. Применение на действующих API (короткое обслуживание)

Команды по очереди обновляют **только** два API-контейнера. Shop
пересобирается из текущего проверенного кода (для MTProxy-only проверки),
MTProxy API использует прежний образ — там меняется только схема БД и секрет. MTProxy API может на несколько секунд перестать отвечать,
в том числе для рекламы и авторизации. Выберите спокойное время для этого шага.

```bash
cd /opt/pushok-shop
# Обязательно оставляем локальное закрепление wg.pushok.shop -> 45.90.56.23.
# compose.wg-ip.yaml — приватный локальный override, не заменяем и не теряем.
docker compose -f compose.mtproxy-edge.yaml -f compose.wg-ip.yaml -p pushok-shop config --quiet

cd /opt/mtproxy-pushok
docker compose config --quiet

# Только Shop; сохраняет именованный том pushok_shop_data:
cd /opt/pushok-shop
docker compose -f compose.mtproxy-edge.yaml -f compose.wg-ip.yaml -p pushok-shop up -d --no-deps --build --force-recreate shop

# Только API MTProxy; не затрагивает его bot, worker, Caddy и БД:
cd /opt/mtproxy-pushok
docker compose up -d --no-deps --no-build --force-recreate api
```

## 4. Проверка без выдачи доступа

```bash
docker exec pushok-shop python -c 'import os; print("Shop bridge:", "ready" if os.getenv("FLOW_SHOP_BRIDGE_SECRET") else "missing")'
docker exec mtproxy-pushok-api-1 python -c 'import os; print("MTProxy bridge:", "ready" if os.getenv("FLOW_SHOP_BRIDGE_SECRET") else "missing")'
python3 /opt/pushok-shop/scripts/flow_public_preflight.py
```

Не публикуйте полное содержимое `.env` и вывод `docker inspect` с
`Config.Env`; там секреты. `DRY RUN` и булевы статусы безопасны.

## 4а. Проверка текущего VPS без изменения данных

В Shop репозитории есть безопасный анализатор текущей инфраструктуры:

```bash
cd /opt/pushok-shop
python3 scripts/flow_pilot_runtime_check.py
```

Он проверяет совпадение закрытых ключей MTProxy/Shop (без их печати),
`FLOW_TRIAL_MTPROXY_ONLY=true`, локальный Compose-override с IP
`45.90.56.23`, отсутствие host-портов, существующий том
`pushok_shop_data`, HTTPS/TLS WGDashboard из контейнера Shop,
`database/076_flow_shop_rate_window.sql` и присутствие секрета в
действующем MTProxy API. Если какой-либо пункт показывает `BLOCK`, автоматическая
выдача пока не допускается. До обновления MTProxy API последний пункт может
показывать `BLOCK` — это ожидаемо.

**Сохранить HTTPS:** если `wg.pushok.shop` не разрешается
публичным DNS из-за рассинхронизации авторитетных NS, Shop использует
локальный `compose.wg-ip.yaml` со строкой
`wg.pushok.shop=45.90.56.23`. В профиле панели сохраняется
`https://wg.pushok.shop`, а не голый IP — TLS продолжает
проверять сертификат по имени. Для публичных Android-клиентов UDP endpoint
должен быть `45.90.56.23:51820` до восстановления DNS.
Если VPS WGDashboard сменит IP, override и endpoint нужно осознанно
обновить. Отдельно решить проблему DNS у провайдера.

**Важно:** `PASS` этого скрипта и кнопки «Проверить API» означают
только готовность чтения и связи. Они не доказывают право создания peer,
удаление по расписанию или работу Android-клиента. API WGDashboard 4.3.4
нужно отдельно проверить на реальном тестовом peer, включая `PeerScheduleJob`
и прекращение доступа по сроку.

## 5. Реальный WGDashboard — отдельная приёмка

В административной панели Shop выбрать активный WGDashboard, настроить
действительный API-профиль, `schedule_expiry=true` и временную зону панели.
Проверить право панели выключать peer при окончании оплаченного/бесплатного
срока. Только затем включить ограниченную политику `days=30,max_users=1`
из панели разработчика и проверить вход **одного** MTProxy-пользователя:
`pending` → `active` → личный `pushok-flow-subscription` → handshake
на Android. Первая выдача не может повторяться при следующем входе.

До проверки серверного истечения: **никакой массовой выдачи**.
