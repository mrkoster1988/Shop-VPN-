# Технологии puShok shop VPN

[← Главная](../README.md) · [Установка магазина](INSTALL_QUICKSTART.md) · [Справочник протоколов](PROTOCOLS.md)

Панели управляют пользователями и серверами; протоколы определяют способ подключения. TLS и REALITY относятся к защите транспорта. Возможности самого протокола не означают, что они поддержаны адаптером Shop.

В коде магазина реализованы VLESS, WireGuard, VMess AEAD, Hysteria2 и TUIC v5. Перед продажами нужна приёмка выбранной версии панели и клиента, выдачи, продления, трафика и отключения. Остальные шесть интеграций находятся в плане.

Начните с установки магазина, затем выберите панель и протокол. У каждой карточки одинаковые ссылки: **установка → подключение к Shop → документация**. Иллюстрация также открывает инструкцию установки.

<p align="center">
  <a href="INSTALL_QUICKSTART.md"><strong>Установить puShok shop VPN</strong></a> ·
  <a href="PANELS_SETUP.md">Мастер установки панелей</a> ·
  <a href="PANEL_CHECK.md">Проверить API</a>
</p>

### Панели управления

<h4 align="center">3x-ui</h4>

<p align="center">
  <a href="https://docs.sanaei.dev/docs/guide/installation/"><img src="assets/technologies/3x-ui.webp" width="440" alt="3x-ui — фирменная иллюстрация puShok"></a>
</p>
<p align="center">
  Панель для управления пользователями VLESS и VMess через API.<br>
  <strong>🟢 Реализовано в коде · требуется приёмка</strong>
</p>
<p align="center">
  <a href="https://docs.sanaei.dev/docs/guide/installation/"><strong>Установка</strong></a> ·
  <a href="THREEXUI_API.md">Подключение к Shop</a> ·
  <a href="https://github.com/MHSanaei/3x-ui">Документация</a>
</p>

<h4 align="center">WGDashboard</h4>

<p align="center">
  <a href="https://docs.wgdashboard.dev/install/"><img src="assets/technologies/wgdashboard.webp" width="440" alt="WGDashboard — фирменная иллюстрация puShok"></a>
</p>
<p align="center">
  Управление WireGuard peers, сроками доступа и конфигурациями через API.<br>
  <strong>🟢 Реализовано в коде · требуется приёмка</strong>
</p>
<p align="center">
  <a href="https://docs.wgdashboard.dev/install/"><strong>Установка</strong></a> ·
  <a href="WGDASHBOARD.md">Подключение к Shop</a> ·
  <a href="https://docs.wgdashboard.dev/">Документация</a>
</p>

<h4 align="center">Remnawave</h4>

<p align="center">
  <a href="https://docs.rw/overview/quick-start/"><img src="assets/technologies/remnawave.webp" width="440" alt="Remnawave — фирменная иллюстрация puShok"></a>
</p>
<p align="center">
  Управление пользователями, узлами и подписками через REST API.<br>
  <strong>🟢 Реализовано в коде · требуется приёмка</strong>
</p>
<p align="center">
  <a href="https://docs.rw/overview/quick-start/"><strong>Установка</strong></a> ·
  <a href="REMNAWAVE.md">Подключение к Shop</a> ·
  <a href="https://docs.rw/">Документация</a>
</p>

**S-UI** используется для Hysteria2 и TUIC: [установка](https://github.com/alireza0/s-ui#install--upgrade-to-latest-version) · [Hysteria2 в Shop](HYSTERIA2.md) · [TUIC в Shop](TUIC.md). Панель устанавливается отдельно.

**NX Panel · 🟡 Диагностика API** — проверка токена и шаблона; выдача подписок в разработке: [установка](https://docs.nx-panel.org/start/install-panel) · [проверка и план интеграции](NX_PANEL.md) · [документация](https://docs.nx-panel.org/).

### Протоколы, реализованные в Shop

Ссылка «Установка» ведёт к серверной реализации, которую использует адаптер Shop. Сам протокол отдельно не устанавливается. Для VLESS также доступна Remnawave, для WireGuard — собственный агент магазина.

<h4 align="center">VLESS</h4>

<p align="center">
  <a href="https://docs.sanaei.dev/docs/guide/installation/"><img src="assets/technologies/vless.webp" width="440" alt="VLESS — фирменная иллюстрация puShok"></a>
</p>
<p align="center">
  Подключения с UUID через 3x-ui или Remnawave. Транспорт и защита задаются на сервере.<br>
  <strong>🟢 Реализовано в коде · требуется приёмка</strong>
</p>
<p align="center">
  <a href="https://docs.sanaei.dev/docs/guide/installation/"><strong>Установка</strong></a> ·
  <a href="THREEXUI_API.md">Подключение к Shop</a> ·
  <a href="https://xtls.github.io/en/config/inbounds/vless.html">Документация</a>
</p>

<h4 align="center">WireGuard</h4>

<p align="center">
  <a href="https://www.wireguard.com/install/"><img src="assets/technologies/wireguard.webp" width="440" alt="WireGuard — фирменная иллюстрация puShok"></a>
</p>
<p align="center">
  VPN-туннель с ключами устройств, конфигурацией и QR-кодом.<br>
  <strong>🟢 Реализовано в коде · требуется приёмка</strong>
</p>
<p align="center">
  <a href="https://www.wireguard.com/install/"><strong>Установка</strong></a> ·
  <a href="WGDASHBOARD.md">Подключение к Shop</a> ·
  <a href="https://www.wireguard.com/quickstart/">Документация</a>
</p>

<h4 align="center">VMess AEAD</h4>

<p align="center">
  <a href="https://docs.sanaei.dev/docs/guide/installation/"><img src="assets/technologies/vmess.webp" width="440" alt="VMess AEAD — фирменная иллюстрация puShok"></a>
</p>
<p align="center">
  Выдача, продление и отключение через 3x-ui: TCP или WebSocket с TLS.<br>
  <strong>🟢 Реализовано в коде · требуется приёмка</strong>
</p>
<p align="center">
  <a href="https://docs.sanaei.dev/docs/guide/installation/"><strong>Установка</strong></a> ·
  <a href="VMESS.md">Подключение к Shop</a> ·
  <a href="https://www.v2fly.org/en_US/developer/protocols/vmess.html">Документация</a>
</p>

<h4 align="center">Hysteria2</h4>

<p align="center">
  <a href="https://github.com/alireza0/s-ui#install--upgrade-to-latest-version"><img src="assets/technologies/hysteria2.webp" width="440" alt="Hysteria2 — фирменная иллюстрация puShok"></a>
</p>
<p align="center">
  Выдача и отзыв через S-UI: UDP/QUIC, TLS и необязательный Salamander.<br>
  <strong>🟢 Реализовано в коде · требуется приёмка</strong>
</p>
<p align="center">
  <a href="https://github.com/alireza0/s-ui#install--upgrade-to-latest-version"><strong>Установка</strong></a> ·
  <a href="HYSTERIA2.md">Подключение к Shop</a> ·
  <a href="https://v2.hysteria.network/">Документация</a>
</p>

<h4 align="center">TUIC v5</h4>

<p align="center">
  <a href="https://github.com/alireza0/s-ui#install--upgrade-to-latest-version"><img src="assets/technologies/tuic.webp" width="440" alt="TUIC v5 — фирменная иллюстрация puShok"></a>
</p>
<p align="center">
  Выдача и отзыв через S-UI: UUID и пароль, QUIC/TLS с ALPN h3.<br>
  <strong>🟢 Реализовано в коде · требуется приёмка</strong>
</p>
<p align="center">
  <a href="https://github.com/alireza0/s-ui#install--upgrade-to-latest-version"><strong>Установка</strong></a> ·
  <a href="TUIC.md">Подключение к Shop</a> ·
  <a href="https://sing-box.sagernet.org/configuration/inbound/tuic/">Документация</a>
</p>

### Планируемые интеграции

Официальные инструкции доступны уже сейчас. Продажа, выдача и отзыв этих подключений в Shop пока не реализованы; ссылка «План интеграции» показывает текущий статус.

<h4 align="center">Trojan</h4>

<p align="center">
  <a href="https://sing-box.sagernet.org/installation/package-manager/"><img src="assets/technologies/trojan.webp" width="440" alt="Trojan — фирменная иллюстрация puShok"></a>
</p>
<p align="center">
  Прокси с защитой TLS и аутентификацией по паролю.<br>
  <strong>🟠 Интеграция запланирована</strong>
</p>
<p align="center">
  <a href="https://sing-box.sagernet.org/installation/package-manager/"><strong>Установка</strong></a> ·
  <a href="PROTOCOLS.md">План интеграции</a> ·
  <a href="https://sing-box.sagernet.org/configuration/inbound/trojan/">Документация</a>
</p>

<h4 align="center">Shadowsocks / Shadowsocks 2022</h4>

<p align="center">
  <a href="https://sing-box.sagernet.org/installation/package-manager/"><img src="assets/technologies/shadowsocks.webp" width="440" alt="Shadowsocks / Shadowsocks 2022 — фирменная иллюстрация puShok"></a>
</p>
<p align="center">
  Шифрованные прокси с согласованным методом шифрования и форматом ключа.<br>
  <strong>🟠 Интеграция запланирована</strong>
</p>
<p align="center">
  <a href="https://sing-box.sagernet.org/installation/package-manager/"><strong>Установка</strong></a> ·
  <a href="PROTOCOLS.md">План интеграции</a> ·
  <a href="https://sing-box.sagernet.org/configuration/inbound/shadowsocks/">Документация</a>
</p>

<h4 align="center">AnyTLS</h4>

<p align="center">
  <a href="https://sing-box.sagernet.org/installation/package-manager/"><img src="assets/technologies/anytls.webp" width="440" alt="AnyTLS — фирменная иллюстрация puShok"></a>
</p>
<p align="center">
  Прокси поверх TLS с настраиваемым дополнением пакетов.<br>
  <strong>🟠 Интеграция запланирована</strong>
</p>
<p align="center">
  <a href="https://sing-box.sagernet.org/installation/package-manager/"><strong>Установка</strong></a> ·
  <a href="PROTOCOLS.md">План интеграции</a> ·
  <a href="https://sing-box.sagernet.org/configuration/inbound/anytls/">Документация</a>
</p>

<h4 align="center">AmneziaWG</h4>

<p align="center">
  <a href="https://docs.amnezia.org/documentation/instructions/install-vpn-on-server/"><img src="assets/technologies/amneziawg.webp" width="440" alt="AmneziaWG — фирменная иллюстрация puShok"></a>
</p>
<p align="center">
  Основа WireGuard с дополнительной маскировкой признаков трафика.<br>
  <strong>🟠 Интеграция запланирована</strong>
</p>
<p align="center">
  <a href="https://docs.amnezia.org/documentation/instructions/install-vpn-on-server/"><strong>Установка</strong></a> ·
  <a href="PROTOCOLS.md">План интеграции</a> ·
  <a href="https://docs.amnezia.org/documentation/amnezia-wg/">Документация</a>
</p>

<h4 align="center">OpenVPN</h4>

<p align="center">
  <a href="https://openvpn.net/community-docs/how-to/"><img src="assets/technologies/openvpn.webp" width="440" alt="OpenVPN — фирменная иллюстрация puShok"></a>
</p>
<p align="center">
  VPN для устройств, удалённого доступа и соединения сетей.<br>
  <strong>🟠 Интеграция запланирована</strong>
</p>
<p align="center">
  <a href="https://openvpn.net/community-docs/how-to/"><strong>Установка</strong></a> ·
  <a href="PROTOCOLS.md">План интеграции</a> ·
  <a href="https://openvpn.net/community/">Документация</a>
</p>

<h4 align="center">IKEv2 / IPsec</h4>

<p align="center">
  <a href="https://docs.strongswan.org/docs/latest/install/install.html"><img src="assets/technologies/ikev2-ipsec.webp" width="440" alt="IKEv2 / IPsec — фирменная иллюстрация puShok"></a>
</p>
<p align="center">
  Защищённый доступ к сети: IKEv2 согласует параметры, IPsec защищает трафик.<br>
  <strong>🟠 Интеграция запланирована</strong>
</p>
<p align="center">
  <a href="https://docs.strongswan.org/docs/latest/install/install.html"><strong>Установка</strong></a> ·
  <a href="PROTOCOLS.md">План интеграции</a> ·
  <a href="https://docs.strongswan.org/docs/latest/config/quickstart.html">Документация</a>
</p>

## Как выбрать путь установки

1. Установите магазин по [краткой инструкции](INSTALL_QUICKSTART.md), начиная с `--plan`.
2. Если панель уже работает, подключите её по API. Новые 3x-ui и WGDashboard можно подготовить через [мастер панелей](PANELS_SETUP.md); S-UI и Remnawave устанавливаются отдельно по официальным руководствам.
3. Для VLESS выберите [3x-ui](THREEXUI_API.md) или [Remnawave](REMNAWAVE.md). Для WireGuard доступны [WGDashboard](WGDASHBOARD.md) и [агент магазина](TECHNICAL_GUIDE.md#wireguard-на-этом-же-vps).
4. Проверьте API и выполните [сценарий приёмки](STAGING.md). Совместимость зависит от конкретного адаптера, конфигурации и клиента.

Ссылки установки сверены с официальными ресурсами 12 ноября 2026 года. Команды сторонних проектов не копируются в эту галерею: актуальные шаги открываются на странице разработчика. Иллюстрации выполнены для puShok в фирменном карандашном стиле; это собственные изображения, без заявления о партнёрстве или спонсорстве.

## Границы поддержки

### VLESS

В магазине есть выдача VLESS через API 3x-ui и Remnawave. Параметры ссылки должны совпадать с inbound панели. В новых Xray также существует VLESS Encryption; поддержку конкретной конфигурации нужно проверять у клиента.

### WireGuard

В магазине есть путь через собственный WireGuard-агент и через API WGDashboard. Проверка API дополняется проверкой реального туннеля, продления и отзыва доступа.

### VMess

Поддерживается Xray и sing-box. Старый режим аутентификации MD5 устарел; для нового доступа нужен AEAD. Время клиента и сервера должно быть синхронизировано. В магазине реализован путь через 3x-ui Clients JSON: TCP/WS + TLS, выдача `vmess://`, продление и отзыв. Legacy API, другие транспорты и обновляемый URL подписки VMess пока не поддерживаются. [Настройка и приёмка](VMESS.md).

### Trojan

Нужны согласованные настройки TLS, серверного имени и аутентификации. Для магазина предстоит реализовать управление учётными данными, выдачу конфигурации и отзыв доступа.

### Shadowsocks / Shadowsocks 2022

Shadowsocks 2022 учитывается отдельно при выборе метода и ключей. Совместимость с обычным Shadowsocks нельзя предполагать автоматически. Интеграция должна учитывать возможности выбранного сервера и клиентских приложений.

### AnyTLS

Есть реализация в sing-box. Перед добавлением продаж нужно проверить выбранную панель или серверный API, клиентские версии, выдачу конфигураций и отключение пользователя.

### Hysteria2

Реализован отдельный адаптер S-UI apiv2: выдача, продление, отключение и закрытие сессий, с TLS и необязательным Salamander. [Настройка и приёмка](HYSTERIA2.md). Доступность UDP/QUIC и фактическую скорость нужно проверять в целевых сетях.

### TUIC

В магазине реализован TUIC v5 через S-UI: UUID + пароль, TLS с ALPN h3, выдача и проверяемый отзыв сессий. Выбранные режим UDP и алгоритм передаются в ссылке. [Настройка и приёмка](TUIC.md).

### AmneziaWG

Для механизмов маскировки требуется совместимый клиент и согласованные параметры версии. Нельзя считать существующий адаптер WireGuard готовым адаптером AmneziaWG без отдельной реализации и проверки.

### OpenVPN

Для интеграции нужно выбрать управляющий сервер или API, реализовать выдачу профилей, управление сертификатами/учётными данными и отзыв доступа. Наличие OpenVPN в системе само по себе не подключает его к магазину.

### IKEv2 / IPsec

Одна из серверных реализаций — strongSwan. Для магазина нужны управление пользователями или сертификатами, выдача профилей, учёт срока и отключение. Способ настройки зависит от клиентской платформы.

## Защита и транспорт

TLS и REALITY относятся к защите транспорта. WebSocket, gRPC и XHTTP — транспортные варианты. Их поддержка зависит от ядра, панели, клиента и выбранной конфигурации; это не отдельные панели управления. [Документация Xray](https://xtls.github.io/en/config/transport.html).

## Условия готовности новой интеграции

1. Определены поддерживаемые версии панели, серверного ядра и клиентских приложений.
2. Проверены создание пользователя, повтор запроса после сбоя и сохранение идентичности при продлении.
3. Клиент получает рабочую конфигурацию, а реальный трафик проходит через целевой сервер.
4. Истечение срока и отзыв доступа действительно прекращают подключение; лимиты соответствуют тарифу.
5. Проверены ошибки API, восстановление и отсутствие повторной выдачи после повторного события оплаты.

Наличие протокола в документации ядра или панели не означает готовность его адаптера в магазине. Исходные пути VLESS/WireGuard сохраняются; неподдерживаемые протоколы не должны незаметно попадать в другой путь выдачи.

## Об иллюстрациях

Это оригинальная серия puShok с рыжим котом в бирюзовом худи, выполненная в стиле цветного карандаша. Изображения не являются официальными логотипами или схемами протоколов. Партнёрство и спонсорство не заявляются. [Описание файлов и запросы генерации](assets/technologies/README.md).
