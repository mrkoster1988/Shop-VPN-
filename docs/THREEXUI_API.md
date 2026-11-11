# Подключение 3x-ui по API

Shop уже создаёт клиентов VLESS, продлевает их и отключает по сроку через API. Установка новой панели для этого не требуется.

## Настройка

1. Создайте VLESS inbound в панели и проверьте ручное подключение клиента.
2. В админке Shop откройте «Серверы», добавьте VLESS-сервер.
3. Укажите HTTPS-адрес панели с её секретным путём, например `https://pushok.shop/your-path`, без `/panel/api`. Адрес магазина `/shop` сюда не подходит.
4. Выберите API явно: Legacy — логин/пароль и cookie-сессия; Clients JSON — Bearer-токен и новая схема клиентов. Наличие API Token само по себе не гарантирует поддержку Clients JSON вашей версией.
5. Для Legacy укажите логин/пароль; для Clients JSON — API Token из Settings → Security. В обоих случаях нужны inbound ID и шаблон ссылки с `{uuid}`, совпадающий с транспортом/TLS/REALITY inbound.
6. Нажмите проверку API, затем включите сервер. Подтвердите покупку, продление и отзыв на тестовой подписке.

При двухфакторном входе Legacy может требовать OTP, который текущий Shop не вводит. Используйте совместимый токеновый режим; не отключайте 2FA автоматически. Секреты хранятся в зашифрованном профиле сервера.

## Используемые операции

| Действие | Legacy | Clients JSON |
|---|---|---|
| Вход | POST /login | Authorization: Bearer |
| Inbound | GET /panel/api/inbounds/get/:id | То же |
| Создание | POST /panel/api/inbounds/addClient | POST /panel/api/clients/add |
| Продление/отключение | POST /panel/api/inbounds/updateClient/:uuid | POST /panel/api/clients/update/:email |

В Legacy settings передаются JSON-строкой внутри формы; expiryTime — миллисекунды. Отключение устанавливает enable=false. Существующие параметры клиента сохраняются; после записи Shop повторно читает состояние и проверяет результат. Повтор выдачи использует прежний UUID. При CSRF-заголовке в ответе чтения Legacy передаёт его в последующих запросах. Автоматического переключения API после ошибки записи нет.

## Приёмка

Проверить секретный подпуть, сертификат, отсутствие второго клиента при повторе, сохранение UUID при продлении, отключение и фактический VPN-трафик. Контрактные тесты не заменяют проверку установленной версии панели. Совместимость со всеми релизами не заявляется.

Официальная документация:
- https://github.com/MHSanaei/3x-ui/wiki/Configuration
- https://github.com/MHSanaei/3x-ui/blob/main/docs/content/docs/en/reference/api/authentication.mdx
- https://github.com/MHSanaei/3x-ui/blob/main/docs/content/docs/en/reference/api/clients.mdx
