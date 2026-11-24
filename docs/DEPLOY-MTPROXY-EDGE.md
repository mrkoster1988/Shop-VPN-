# Shop рядом с MTProxy: отдельный Docker-контейнер

Проверенная на 24.11.2026 топология VPS: `/opt/mtproxy-pushok/Caddyfile`,
контейнер `mtproxy-pushok-caddy-1`, внешняя сеть `mtproxy-pushok_edge`.
Caddy уже занимает 80/443, MTProxy занимает хостовый 127.0.0.1:8000,
Pushok ID — 127.0.0.1:18088. Ни один существующий сервис останавливать нельзя.

## Этап А — установка без изменения Caddy

В рабочей копии актуального `main`:

```bash
cd /opt/pushok-shop
git pull --ff-only
python3 scripts/prepare_mtproxy_edge.py
docker compose -f compose.mtproxy-edge.yaml -p pushok-shop config --quiet
docker compose -f compose.mtproxy-edge.yaml -p pushok-shop up -d --build
docker compose -f compose.mtproxy-edge.yaml -p pushok-shop ps
docker exec pushok-shop python -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8000/shop/health',timeout=8).read().decode())"
```

Первый запуск создаёт `.env` с режимом 0600 и случайными секретами. Повторный
не перезаписывает существующий `.env`. Не копируйте `.env`, логи со ссылками
подписки, ADMIN_TOKEN или приватные конфиги в переписку. `compose.mtproxy-edge.yaml`
намеренно **не содержит** `ports`, `network_mode: host` и вторую Caddy.
Команда `up` не меняет MTProxy/ID/Caddy. Shop отвечает только через Docker DNS
`pushok-shop:8000` в сети edge. Исходный `compose.yaml` здесь НЕ ИСПОЛЬЗОВАТЬ.

Если первый запуск не удался, для осмотра безопасны:
`docker compose -f compose.mtproxy-edge.yaml -p pushok-shop ps` и
`docker inspect --format '{{.State.Health.Status}}' pushok-shop`.
Не удаляйте том `pushok_shop_data`: там база, подписки и шифрованные ключи.

## Этап Б — подключение существующего Caddy (отдельно, после успешного этапа А)

Добавить в ТОЛЬКО `pushok.shop` обработчик `/shop` и `/shop/*` перед
общим маршрутом MTProxy, с сохранением исходного URL без `strip_prefix`:

```caddyfile
pushok.shop {
    @shop path /shop /shop/*
    handle @shop {
        reverse_proxy pushok-shop:8000
    }
    handle {
        reverse_proxy mtproxy-pushok-api-1:8000
    }
}
```

Блоки `ads.pushok.shop` и `auth.pushok.shop` НЕ МЕНЯТЬ. Перед правкой
создать резервную копию Caddyfile. После правки выполнить:
`docker exec mtproxy-pushok-caddy-1 caddy validate --config /etc/caddy/Caddyfile`;
при ошибке немедленно восстановить исходный файл, **не перезапуская Caddy**.
Только после успешной проверки — `caddy reload` и HTTP-тесты
`/api/v1/flow/capabilities`, `/shop/health`, `auth.pushok.shop`.

Не публиковать access logs на пути `/shop/flow/wireguard/*`: там
bearer-token личного профиля. Не включать массовую выдачу до теста реального
WGDashboard и серверного истечения 30 дней.

## Этап В — выдача

Настроить `FLOW_SHOP_BRIDGE_SECRET` на сервере MTProxy и на сервере Shop
одинаковым 32-байтным случайным значением, не размещая его в APK/репозитории.
Владелец добавляет рабочий узел WGDashboard с подтверждёнными
`schedule_expiry=true` и `panel_timezone`. После подключения рабочей панели
и настройки условий доступа можно временно включить trial-политику
`enabled=true`, `days=30`, `max_users=1` для тестового пользователя.
До подтверждения удаления peer при истечении срока массовую выдачу не включать.
