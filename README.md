<div align="center">

# puShok shop VPN

**Ваш магазин VPN. Ваш бренд. Ваши правила.**

Сайт · Telegram-бот · Mini App · Управление подключениями через API

[Установка](docs/INSTALL_QUICKSTART.md) · [Возможности](#возможности-магазина) · [Технологии](#технологии-подключения) · [План развития](docs/ROADMAP.md) · [Обновления](docs/UPDATE_CHECKER.md)

[![CI](https://github.com/mrkoster1988/Shop-VPN-/actions/workflows/ci.yml/badge.svg)](https://github.com/mrkoster1988/Shop-VPN-/actions/workflows/ci.yml)
[![Релиз](https://img.shields.io/github/v/release/mrkoster1988/Shop-VPN-?label=release&color=00a89d)](https://github.com/mrkoster1988/Shop-VPN-/releases)

</div>

puShok shop VPN — самостоятельный магазин подключений VLESS, VMess, Hysteria2, TUIC и WireGuard для владельцев своих серверов и доменов. Покупатель выбирает тариф, оплачивает заказ и получает настройки подключения; владелец управляет продажами, клиентами и панелями из своего кабинета.

У puShok shop VPN собственные код, база, интерфейс и логика продаж. Панели подключаются через отдельные API-адаптеры; другой магазин для работы не требуется. Фирменный стиль puShok — рыжий кот, бирюзовый, тёмно-синий и оранжевый, иллюстрации цветными карандашами.

> **Статус: подготовка к запуску. Версия кода и установщика — v0.055.** Релизный тег публикуется GitHub Actions после полного CI. Реальные платежи, целевые панели и чистая установка на VPS требуют приёмки. [Отчёт о готовности](docs/LAUNCH_READINESS.md) содержит результаты проверки и открытые блокеры; актуальный CI показан выше.

## Возможности магазина

| Покупателю | Владельцу |
|---|---|
| Личный кабинет на сайте и в Telegram Mini App | Управление тарифами, сроками и лимитами устройств |
| Покупка, пробный доступ и продление подписки | Подключение серверов и проверка их API |
| Ссылка, QR-код, скачивание конфигурации и инструкции | Заказы, подтверждения оплаты и повтор выдачи при сбое |
| Профили устройств и перевыпуск ключей | Промокоды, реферальные бонусные дни и настройки магазина |
| История заказов, статусы выдачи и поддержка | Роли сотрудников, аудит, поддержка и аналитика |
| Stars в Telegram; СБП/ЮKassa для email-аккаунтов сайта | Настраиваемое меню бота, диагностика, резервные копии и обновление |

Это возможности текущего кода, а не подтверждение их проверки на вашем VPS. RUB и XTR учитываются раздельно. Общий денежный кошелёк, расширенные акции и автоматические возвраты остаются отдельными этапами.

В кабинете владельца и оператора панелей доступен [справочник 11 протоколов](docs/PROTOCOLS.md): карточки, фильтр по статусу, требования к клиентам и официальные ссылки. Планируемые интеграции не попадают в выбор при покупке.

## Технологии подключения

Начните с установки магазина, затем выберите панель и протокол. У каждой карточки одинаковые ссылки: **установка → подключение к Shop → документация**. Иллюстрация также открывает инструкцию установки.

<p align="center">
  <a href="docs/INSTALL_QUICKSTART.md"><strong>Установить puShok shop VPN</strong></a> ·
  <a href="docs/PANELS_SETUP.md">Мастер установки панелей</a> ·
  <a href="docs/PANEL_CHECK.md">Проверить API</a>
</p>

### Панели управления

<h4 align="center">3x-ui</h4>

<p align="center">
  <a href="https://docs.sanaei.dev/docs/guide/installation/"><img src="docs/assets/technologies/3x-ui.webp" width="440" alt="3x-ui — фирменная иллюстрация puShok"></a>
</p>
<p align="center">
  Панель для управления пользователями VLESS и VMess через API.<br>
  <strong>🟢 Реализовано в коде · требуется приёмка</strong>
</p>
<p align="center">
  <a href="https://docs.sanaei.dev/docs/guide/installation/"><strong>Установка</strong></a> ·
  <a href="docs/THREEXUI_API.md">Подключение к Shop</a> ·
  <a href="https://github.com/MHSanaei/3x-ui">Документация</a>
</p>

<h4 align="center">WGDashboard</h4>

<p align="center">
  <a href="https://docs.wgdashboard.dev/install/"><img src="docs/assets/technologies/wgdashboard.webp" width="440" alt="WGDashboard — фирменная иллюстрация puShok"></a>
</p>
<p align="center">
  Управление WireGuard peers, сроками доступа и конфигурациями через API.<br>
  <strong>🟢 Реализовано в коде · требуется приёмка</strong>
</p>
<p align="center">
  <a href="https://docs.wgdashboard.dev/install/"><strong>Установка</strong></a> ·
  <a href="docs/WGDASHBOARD.md">Подключение к Shop</a> ·
  <a href="https://docs.wgdashboard.dev/">Документация</a>
</p>

<h4 align="center">Remnawave</h4>

<p align="center">
  <a href="https://docs.rw/overview/quick-start/"><img src="docs/assets/technologies/remnawave.webp" width="440" alt="Remnawave — фирменная иллюстрация puShok"></a>
</p>
<p align="center">
  Управление пользователями, узлами и подписками через REST API.<br>
  <strong>🟢 Реализовано в коде · требуется приёмка</strong>
</p>
<p align="center">
  <a href="https://docs.rw/overview/quick-start/"><strong>Установка</strong></a> ·
  <a href="docs/REMNAWAVE.md">Подключение к Shop</a> ·
  <a href="https://docs.rw/">Документация</a>
</p>

**S-UI** используется для Hysteria2 и TUIC: [установка](https://github.com/alireza0/s-ui#install--upgrade-to-latest-version) · [Hysteria2 в Shop](docs/HYSTERIA2.md) · [TUIC в Shop](docs/TUIC.md). Панель устанавливается отдельно.

**NX Panel · 🟡 Диагностика API** — проверка токена и шаблона; выдача подписок в разработке: [установка](https://docs.nx-panel.org/start/install-panel) · [проверка и план интеграции](docs/NX_PANEL.md) · [документация](https://docs.nx-panel.org/).

### Протоколы, реализованные в Shop

Ссылка «Установка» ведёт к серверной реализации, которую использует адаптер Shop. Сам протокол отдельно не устанавливается. Для VLESS также доступна Remnawave, для WireGuard — собственный агент магазина.

<h4 align="center">VLESS</h4>

<p align="center">
  <a href="https://docs.sanaei.dev/docs/guide/installation/"><img src="docs/assets/technologies/vless.webp" width="440" alt="VLESS — фирменная иллюстрация puShok"></a>
</p>
<p align="center">
  Подключения с UUID через 3x-ui или Remnawave. Транспорт и защита задаются на сервере.<br>
  <strong>🟢 Реализовано в коде · требуется приёмка</strong>
</p>
<p align="center">
  <a href="https://docs.sanaei.dev/docs/guide/installation/"><strong>Установка</strong></a> ·
  <a href="docs/THREEXUI_API.md">Подключение к Shop</a> ·
  <a href="https://xtls.github.io/en/config/inbounds/vless.html">Документация</a>
</p>

<h4 align="center">WireGuard</h4>

<p align="center">
  <a href="https://www.wireguard.com/install/"><img src="docs/assets/technologies/wireguard.webp" width="440" alt="WireGuard — фирменная иллюстрация puShok"></a>
</p>
<p align="center">
  VPN-туннель с ключами устройств, конфигурацией и QR-кодом.<br>
  <strong>🟢 Реализовано в коде · требуется приёмка</strong>
</p>
<p align="center">
  <a href="https://www.wireguard.com/install/"><strong>Установка</strong></a> ·
  <a href="docs/WGDASHBOARD.md">Подключение к Shop</a> ·
  <a href="https://www.wireguard.com/quickstart/">Документация</a>
</p>

<h4 align="center">VMess AEAD</h4>

<p align="center">
  <a href="https://docs.sanaei.dev/docs/guide/installation/"><img src="docs/assets/technologies/vmess.webp" width="440" alt="VMess AEAD — фирменная иллюстрация puShok"></a>
</p>
<p align="center">
  Выдача, продление и отключение через 3x-ui: TCP или WebSocket с TLS.<br>
  <strong>🟢 Реализовано в коде · требуется приёмка</strong>
</p>
<p align="center">
  <a href="https://docs.sanaei.dev/docs/guide/installation/"><strong>Установка</strong></a> ·
  <a href="docs/VMESS.md">Подключение к Shop</a> ·
  <a href="https://www.v2fly.org/en_US/developer/protocols/vmess.html">Документация</a>
</p>

<h4 align="center">Hysteria2</h4>

<p align="center">
  <a href="https://github.com/alireza0/s-ui#install--upgrade-to-latest-version"><img src="docs/assets/technologies/hysteria2.webp" width="440" alt="Hysteria2 — фирменная иллюстрация puShok"></a>
</p>
<p align="center">
  Выдача и отзыв через S-UI: UDP/QUIC, TLS и необязательный Salamander.<br>
  <strong>🟢 Реализовано в коде · требуется приёмка</strong>
</p>
<p align="center">
  <a href="https://github.com/alireza0/s-ui#install--upgrade-to-latest-version"><strong>Установка</strong></a> ·
  <a href="docs/HYSTERIA2.md">Подключение к Shop</a> ·
  <a href="https://v2.hysteria.network/">Документация</a>
</p>

<h4 align="center">TUIC v5</h4>

<p align="center">
  <a href="https://github.com/alireza0/s-ui#install--upgrade-to-latest-version"><img src="docs/assets/technologies/tuic.webp" width="440" alt="TUIC v5 — фирменная иллюстрация puShok"></a>
</p>
<p align="center">
  Выдача и отзыв через S-UI: UUID и пароль, QUIC/TLS с ALPN h3.<br>
  <strong>🟢 Реализовано в коде · требуется приёмка</strong>
</p>
<p align="center">
  <a href="https://github.com/alireza0/s-ui#install--upgrade-to-latest-version"><strong>Установка</strong></a> ·
  <a href="docs/TUIC.md">Подключение к Shop</a> ·
  <a href="https://sing-box.sagernet.org/configuration/inbound/tuic/">Документация</a>
</p>

### Планируемые интеграции

Официальные инструкции доступны уже сейчас. Продажа, выдача и отзыв этих подключений в Shop пока не реализованы; ссылка «План интеграции» показывает текущий статус.

<h4 align="center">Trojan</h4>

<p align="center">
  <a href="https://sing-box.sagernet.org/installation/package-manager/"><img src="docs/assets/technologies/trojan.webp" width="440" alt="Trojan — фирменная иллюстрация puShok"></a>
</p>
<p align="center">
  Прокси с защитой TLS и аутентификацией по паролю.<br>
  <strong>🟠 Интеграция запланирована</strong>
</p>
<p align="center">
  <a href="https://sing-box.sagernet.org/installation/package-manager/"><strong>Установка</strong></a> ·
  <a href="docs/PROTOCOLS.md">План интеграции</a> ·
  <a href="https://sing-box.sagernet.org/configuration/inbound/trojan/">Документация</a>
</p>

<h4 align="center">Shadowsocks / Shadowsocks 2022</h4>

<p align="center">
  <a href="https://sing-box.sagernet.org/installation/package-manager/"><img src="docs/assets/technologies/shadowsocks.webp" width="440" alt="Shadowsocks / Shadowsocks 2022 — фирменная иллюстрация puShok"></a>
</p>
<p align="center">
  Шифрованные прокси с согласованным методом шифрования и форматом ключа.<br>
  <strong>🟠 Интеграция запланирована</strong>
</p>
<p align="center">
  <a href="https://sing-box.sagernet.org/installation/package-manager/"><strong>Установка</strong></a> ·
  <a href="docs/PROTOCOLS.md">План интеграции</a> ·
  <a href="https://sing-box.sagernet.org/configuration/inbound/shadowsocks/">Документация</a>
</p>

<h4 align="center">AnyTLS</h4>

<p align="center">
  <a href="https://sing-box.sagernet.org/installation/package-manager/"><img src="docs/assets/technologies/anytls.webp" width="440" alt="AnyTLS — фирменная иллюстрация puShok"></a>
</p>
<p align="center">
  Прокси поверх TLS с настраиваемым дополнением пакетов.<br>
  <strong>🟠 Интеграция запланирована</strong>
</p>
<p align="center">
  <a href="https://sing-box.sagernet.org/installation/package-manager/"><strong>Установка</strong></a> ·
  <a href="docs/PROTOCOLS.md">План интеграции</a> ·
  <a href="https://sing-box.sagernet.org/configuration/inbound/anytls/">Документация</a>
</p>

<h4 align="center">AmneziaWG</h4>

<p align="center">
  <a href="https://docs.amnezia.org/documentation/instructions/install-vpn-on-server/"><img src="docs/assets/technologies/amneziawg.webp" width="440" alt="AmneziaWG — фирменная иллюстрация puShok"></a>
</p>
<p align="center">
  Основа WireGuard с дополнительной маскировкой признаков трафика.<br>
  <strong>🟠 Интеграция запланирована</strong>
</p>
<p align="center">
  <a href="https://docs.amnezia.org/documentation/instructions/install-vpn-on-server/"><strong>Установка</strong></a> ·
  <a href="docs/PROTOCOLS.md">План интеграции</a> ·
  <a href="https://docs.amnezia.org/documentation/amnezia-wg/">Документация</a>
</p>

<h4 align="center">OpenVPN</h4>

<p align="center">
  <a href="https://openvpn.net/community-docs/how-to/"><img src="docs/assets/technologies/openvpn.webp" width="440" alt="OpenVPN — фирменная иллюстрация puShok"></a>
</p>
<p align="center">
  VPN для устройств, удалённого доступа и соединения сетей.<br>
  <strong>🟠 Интеграция запланирована</strong>
</p>
<p align="center">
  <a href="https://openvpn.net/community-docs/how-to/"><strong>Установка</strong></a> ·
  <a href="docs/PROTOCOLS.md">План интеграции</a> ·
  <a href="https://openvpn.net/community/">Документация</a>
</p>

<h4 align="center">IKEv2 / IPsec</h4>

<p align="center">
  <a href="https://docs.strongswan.org/docs/latest/install/install.html"><img src="docs/assets/technologies/ikev2-ipsec.webp" width="440" alt="IKEv2 / IPsec — фирменная иллюстрация puShok"></a>
</p>
<p align="center">
  Защищённый доступ к сети: IKEv2 согласует параметры, IPsec защищает трафик.<br>
  <strong>🟠 Интеграция запланирована</strong>
</p>
<p align="center">
  <a href="https://docs.strongswan.org/docs/latest/install/install.html"><strong>Установка</strong></a> ·
  <a href="docs/PROTOCOLS.md">План интеграции</a> ·
  <a href="https://docs.strongswan.org/docs/latest/config/quickstart.html">Документация</a>
</p>

<details>
<summary><strong>Как связаны протокол, защита соединения и панель?</strong></summary>

Панель управляет пользователями и серверами. Протокол задаёт способ соединения. TLS и REALITY относятся к защите транспорта; WebSocket, gRPC и XHTTP — к транспортным вариантам. Они выбираются в совместимой комбинации, например VLESS + REALITY, и не считаются отдельными VPN-панелями.

Проверка по официальной документации: **12 ноября 2026 года**. Поддерживаемая реализация протокола не гарантирует работу в каждой сети. Для каждой интеграции проверяются версии сервера и клиента, создание доступа, продление, трафик и отключение.

</details>

Иллюстрации созданы для puShok shop VPN в фирменном карандашном стиле. Это оригинальные изображения, а не официальные логотипы; партнёрство и спонсорство не заявляются.

[Роль технологий, источники и границы совместимости →](docs/TECHNOLOGIES.md)

[Подключить панель и обновить ключи в админке →](docs/PANEL_CONNECTIONS.md)

## Установка на ваш VPS

Целевая система — Ubuntu 24.04 LTS. Домен указывает владелец; магазин работает по пути `/shop`, например `https://example.com/shop`. Уже установленную панель можно подключить по API.

**Начните с [короткой инструкции](docs/INSTALL_QUICKSTART.md):** в ней показано, как подключиться к VPS, обновить систему, скачать установщик и что отвечать на каждый вопрос мастера.

| Ситуация | Инструкция |
|---|---|
| Первый запуск и ответы в диалоге | [Быстрый старт](docs/INSTALL_QUICKSTART.md) |
| Подробная установка и HTTPS | [Установщик](docs/INSTALL.md) |
| Дополнительно установить 3x-ui или WGDashboard | [Установка и настройка панелей](docs/PANELS_SETUP.md) |
| Настроить СБП и Stars | [Платежи](docs/SBP_PAYMENTS.md) |
| Проверить, что магазин действительно готов | [Стендовая приёмка](docs/STAGING.md) |
| Автоматическая проверка первой установки | [Ubuntu 24.04 в CI](docs/INSTALLER_STAGING.md) |

Загрузчик v0.055 закреплён на одноимённом релизе и включает новые протоколы, подсказки и выбор дополнительных панелей. Установка S-UI остаётся отдельной. Приёмка целевой Ubuntu и VPN-клиентов ещё открыта.

## Документация и развитие

- [Техническое руководство](docs/TECHNICAL_GUIDE.md) — подробности функций, настройки и API.
- [План развития](docs/ROADMAP.md) · [Принципы продукта и выбранные улучшения](docs/PRODUCT_DESIGN.md).
- [Состояние заказа](docs/ORDER_STATUS.md) · [Меню Telegram](docs/TELEGRAM_MENU.md).
- [Резервное копирование](docs/BACKUP.md) · [Обновление и откат](docs/UPGRADE.md) · [Диагностика](docs/DIAGNOSTICS.md).
- [История изменений](CHANGELOG.md) · [Задачи](https://github.com/mrkoster1988/Shop-VPN-/issues) · [Релизы](https://github.com/mrkoster1988/Shop-VPN-/releases).

Ближайший приоритет — подтвердить установку на целевой инфраструктуре и пройти полный цикл «оплата → выдача → VPN-трафик → продление → отключение». Установочный релиз v0.055 опубликован; автоматические проверки дополняются отдельным стендом Ubuntu 24.04.

---

**puShok shop VPN** — собственный магазин с понятными настройками и проверяемыми результатами. Разработка ведётся в этом GitHub-репозитории.
