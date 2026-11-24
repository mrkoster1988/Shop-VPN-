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
платежей, Caddy, Docker и Pushok ID не меняются.

На этом этапе **сервисы ещё не перезапущены**, а `Flow`-автовыдача по-прежнему
`enabled=false`.

## 3. Применение на действующих API (короткое обслуживание)

Команды по очереди пересоздают **только** два API-контейнера из существующих
образов. MTProxy API может на несколько секунд перестать отвечать,
в том числе для рекламы и авторизации. Выберите спокойное время для этого шага.

```bash
cd /opt/pushok-shop
docker compose -f compose.mtproxy-edge.yaml -p pushok-shop config --quiet

cd /opt/mtproxy-pushok
docker compose config --quiet

# Только Shop; сохраняет именованный том pushok_shop_data:
cd /opt/pushok-shop
docker compose -f compose.mtproxy-edge.yaml -p pushok-shop up -d --no-deps --no-build --force-recreate shop

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

## 5. Реальный WGDashboard — отдельная приёмка

В административной панели Shop выбрать активный WGDashboard, настроить
действительный API-профиль, `schedule_expiry=true` и временную зону панели.
Проверить право панели выключать peer при окончании оплаченного/бесплатного
срока. Только затем включить ограниченную политику `days=30,max_users=1`
из панели разработчика и проверить вход **одного** MTProxy-пользователя:
`pending` → `active` → личный `pushok-flow-subscription` → handshake
на Android. Первая выдача не может повторяться при следующем входе.

До проверки серверного истечения: **никакой массовой выдачи**.
