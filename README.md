<div align="center">

# puShok shop VPN

**Ваш магазин VPN. Ваш бренд. Ваши правила.**

Сайт · Telegram-бот · Mini App · Управление подключениями через API

[Установка](docs/INSTALL_QUICKSTART.md) · [Возможности](#возможности-магазина) · [Технологии](#технологии-подключения) · [План развития](docs/ROADMAP.md)

[![CI](https://github.com/mrkoster1988/Shop-VPN-/actions/workflows/ci.yml/badge.svg)](https://github.com/mrkoster1988/Shop-VPN-/actions/workflows/ci.yml)
[![Релиз](https://img.shields.io/github/v/release/mrkoster1988/Shop-VPN-?label=release&color=00a89d)](https://github.com/mrkoster1988/Shop-VPN-/releases)

</div>

puShok shop VPN — самостоятельный магазин подключений VLESS, VMess и WireGuard для владельцев своих серверов и доменов. Покупатель выбирает тариф, оплачивает заказ и получает настройки подключения; владелец управляет продажами, клиентами и панелями из своего кабинета.

У puShok shop VPN собственные код, база, интерфейс и логика продаж. Панели подключаются через отдельные API-адаптеры; другой магазин для работы не требуется. Фирменный стиль puShok — рыжий кот, бирюзовый, тёмно-синий и оранжевый, иллюстрации цветными карандашами.

> **Статус: подготовка к запуску.** Последний опубликованный релиз и закреплённый установщик — **v0.054**. В `main` есть более новые изменения. Реальные платежи, целевые панели и чистая установка на VPS требуют приёмки. [Отчёт о готовности](docs/LAUNCH_READINESS.md) содержит результаты проверки и открытые блокеры; актуальный CI показан выше.

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

**Выберите технологию — нажмите на иллюстрацию, чтобы открыть официальный ресурс.**

Галерея действующих технологий и направлений развития puShok shop VPN. Карточки сгруппированы по назначению; статус протокола в магазине указан отдельно от возможностей самого протокола.

### Панели управления

<p align="center">
  <a href="https://github.com/MHSanaei/3x-ui"><img src="docs/assets/technologies/3x-ui.webp" width="240" alt="3x-ui — иллюстрация puShok shop VPN"></a>
  &nbsp; <a href="https://github.com/WGDashboard/WGDashboard"><img src="docs/assets/technologies/wgdashboard.webp" width="240" alt="WGDashboard — иллюстрация puShok shop VPN"></a>
  &nbsp; <a href="https://docs.rw/"><img src="docs/assets/technologies/remnawave.webp" width="240" alt="Remnawave — иллюстрация puShok shop VPN"></a>
</p>


- **3x-ui** — Выдача, продление и отключение VLESS и VMess AEAD через API. [VLESS](docs/THREEXUI_API.md) · [VMess](docs/VMESS.md).
- **WGDashboard** — Управление WireGuard peers и выдача конфигураций через API. [Настройка](docs/WGDASHBOARD.md).
- **Remnawave** — Пользователи, сроки и подписки через прямой REST API. [Настройка](docs/REMNAWAVE.md).

### Протоколы подключения

#### 🟢 Реализованы в коде · требуется приёмка на целевых серверах

<p align="center">
  <a href="https://xtls.github.io/en/config/inbounds/vless.html"><img src="docs/assets/technologies/vless.webp" width="360" alt="VLESS — иллюстрация puShok shop VPN"></a>
  &nbsp; <a href="https://www.wireguard.com/"><img src="docs/assets/technologies/wireguard.webp" width="360" alt="WireGuard — иллюстрация puShok shop VPN"></a>
</p>


<p align="center">
  <a href="https://www.v2fly.org/en_US/developer/protocols/vmess.html"><img src="docs/assets/technologies/vmess.webp" width="360" alt="VMess — иллюстрация puShok shop VPN"></a>
</p>

- **VLESS** — Лёгкий протокол с UUID. Защита и транспорт выбираются в конфигурации сервера: например, TLS или REALITY.
- **WireGuard** — VPN-туннель с ключами устройств. Конфигурация и QR-код для подключения.

- **VMess AEAD** — 3x-ui Clients JSON, TCP/WS + TLS. Выдача ссылки, продление и отзыв; [настройка и границы поддержки](docs/VMESS.md).

[Проверка API панелей](docs/PANEL_CHECK.md) · [Приёмка WireGuard](docs/WIREGUARD_STAGING.md)

#### 🟠 Расширение каталога · интеграция в магазин запланирована

Следующие протоколы имеют действующие реализации. Их автоматическая продажа, выдача, продление и отключение в puShok shop VPN пока не реализованы. Карточка в галерее не включает протокол в магазине.

**Шифрованные прокси**

<p align="center">
  <a href="https://sing-box.sagernet.org/manual/proxy-protocol/trojan/"><img src="docs/assets/technologies/trojan.webp" width="360" alt="Trojan — иллюстрация puShok shop VPN"></a>
</p>

<p align="center">
  <a href="https://sing-box.sagernet.org/manual/proxy-protocol/shadowsocks/"><img src="docs/assets/technologies/shadowsocks.webp" width="360" alt="Shadowsocks / Shadowsocks 2022 — иллюстрация puShok shop VPN"></a>
  &nbsp; <a href="https://sing-box.sagernet.org/configuration/inbound/anytls/"><img src="docs/assets/technologies/anytls.webp" width="360" alt="AnyTLS — иллюстрация puShok shop VPN"></a>
</p>


- **Trojan** — Прокси-соединение с защитой TLS и аутентификацией по паролю.
- **Shadowsocks / Shadowsocks 2022** — Семейство шифрованных прокси. Метод шифрования и формат ключа должны поддерживаться клиентом.
- **AnyTLS** — Прокси поверх TLS с настраиваемым дополнением пакетов. Требуется совместимое приложение.

**Соединения на базе QUIC**

<p align="center">
  <a href="https://v2.hysteria.network/"><img src="docs/assets/technologies/hysteria2.webp" width="360" alt="Hysteria2 — иллюстрация puShok shop VPN"></a>
  &nbsp; <a href="https://github.com/tuic-protocol/tuic"><img src="docs/assets/technologies/tuic.webp" width="360" alt="TUIC — иллюстрация puShok shop VPN"></a>
</p>


- **Hysteria2** — Прокси на базе QUIC, разработанный для соединений с потерями и нестабильным качеством сети.
- **TUIC** — Прокси на базе QUIC с передачей TCP/UDP и несколькими потоками в одном соединении.

**VPN-туннели и доступ к сетям**

<p align="center">
  <a href="https://docs.amnezia.org/documentation/amnezia-wg/"><img src="docs/assets/technologies/amneziawg.webp" width="360" alt="AmneziaWG — иллюстрация puShok shop VPN"></a>
  &nbsp; <a href="https://openvpn.net/community/"><img src="docs/assets/technologies/openvpn.webp" width="360" alt="OpenVPN — иллюстрация puShok shop VPN"></a>
</p>

<p align="center">
  <a href="https://docs.strongswan.org/docs/latest/howtos/introduction.html"><img src="docs/assets/technologies/ikev2-ipsec.webp" width="360" alt="IKEv2 / IPsec — иллюстрация puShok shop VPN"></a>
</p>


- **AmneziaWG** — Основа WireGuard с дополнительной маскировкой признаков трафика.
- **OpenVPN** — VPN для пользовательских устройств, удалённого доступа и соединения сетей.
- **IKEv2 / IPsec** — Защищённый доступ к сети: IKEv2 согласует параметры, IPsec защищает трафик.

<details>
<summary><strong>Как связаны протокол, защита соединения и панель?</strong></summary>

Панель управляет пользователями и серверами. Протокол задаёт способ соединения. TLS и REALITY относятся к защите транспорта; WebSocket, gRPC и XHTTP — к транспортным вариантам. Они выбираются в совместимой комбинации, например VLESS + REALITY, и не считаются отдельными VPN-панелями.

Проверка по официальной документации: **12 ноября 2026 года**. Поддерживаемая реализация протокола не гарантирует работу в каждой сети. Для каждой интеграции проверяются версии сервера и клиента, создание доступа, продление, трафик и отключение.

</details>

Иллюстрации созданы для puShok shop VPN в фирменном карандашном стиле. Это оригинальные изображения, а не официальные логотипы; партнёрство и спонсорство не заявляются.

[Роль технологий, источники и границы совместимости →](docs/TECHNOLOGIES.md)

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

Новые подсказки и выбор установки панелей находятся в `main`; загрузчик пока получает архив v0.054. Новый установочный релиз выпускается после обязательных проверок.

## Документация и развитие

- [Техническое руководство](docs/TECHNICAL_GUIDE.md) — подробности функций, настройки и API.
- [План развития](docs/ROADMAP.md) · [Принципы продукта и выбранные улучшения](docs/PRODUCT_DESIGN.md).
- [Состояние заказа](docs/ORDER_STATUS.md) · [Меню Telegram](docs/TELEGRAM_MENU.md).
- [Резервное копирование](docs/BACKUP.md) · [Обновление и откат](docs/UPGRADE.md) · [Диагностика](docs/DIAGNOSTICS.md).
- [История изменений](CHANGELOG.md) · [Задачи](https://github.com/mrkoster1988/Shop-VPN-/issues) · [Релизы](https://github.com/mrkoster1988/Shop-VPN-/releases).

Ближайший приоритет — закрыть блокеры CI и восстановление, выпустить проверенный установщик и пройти полный цикл «оплата → выдача → VPN-трафик → продление → отключение».

---

**puShok shop VPN** — собственный магазин с понятными настройками и проверяемыми результатами. Разработка ведётся в этом GitHub-репозитории.
