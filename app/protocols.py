"""Реестр протоколов: справочник возможностей, отдельно от доступности серверов."""
from dataclasses import asdict, dataclass
from typing import Literal

SalesProtocol = Literal['xui', 'wg', 'vmess', 'hysteria2', 'tuic']


@dataclass(frozen=True)
class Protocol:
    id: str
    name: str
    description: str
    server_options: str
    client_requirements: str
    documentation_url: str
    driver_id: str | None = None
    adapters: tuple[str, ...] = ()


# driver_id — существующее значение в заказах/подписках. Не переименовывать
# xui в vless или wg в wireguard без отдельной миграции данных и контрактов.
# Отсутствие driver_id означает: в справочнике есть, в продажах и выдаче нет.
PROTOCOLS = (
    Protocol('vless', 'VLESS', 'Прокси-доступ с UUID и настройками защиты и транспорта.',
             '3x-ui / Remnawave',
             'Клиент должен поддерживать выбранные TLS/REALITY и транспорт сервера.',
             'https://xtls.github.io/en/config/inbounds/vless.html', 'xui',
             ('3x-ui Legacy', '3x-ui Clients JSON', 'Remnawave v3')),
    Protocol('wireguard', 'WireGuard', 'VPN-туннель с отдельными ключами устройств.',
             'WGDashboard / агент puShok',
             'WireGuard: импорт файла .conf или QR; Android, iOS, Windows, macOS, Linux.',
             'https://www.wireguard.com/install/', 'wg', ('Агент WireGuard', 'WGDashboard')),
    Protocol('vmess', 'VMess', 'Шифрованный прокси-протокол с UUID.',
             '3x-ui Clients JSON API',
             'VMess AEAD: TCP+TLS или WebSocket+TLS; время клиента и сервера синхронизировано.',
             'https://www.v2fly.org/en_US/developer/protocols/vmess.html', 'vmess',
             ('3x-ui Clients JSON · TCP/WS + TLS',)),
    Protocol('trojan', 'Trojan', 'Прокси поверх TLS с аутентификацией по паролю.',
             'Кандидаты: 3x-ui / S-UI',
             'Клиент Trojan; совпадающие пароль, TLS и имя сервера.',
             'https://sing-box.sagernet.org/manual/proxy-protocol/trojan/'),
    Protocol('shadowsocks', 'Shadowsocks / Shadowsocks 2022', 'Семейство шифрованных прокси.',
             'Кандидаты: 3x-ui / S-UI; метод проверяется отдельно',
             'Нужна поддержка конкретного метода шифрования; 2022 не равнозначен старым методам.',
             'https://sing-box.sagernet.org/manual/proxy-protocol/shadowsocks/'),
    Protocol('hysteria2', 'Hysteria2', 'Прокси на базе QUIC для сетей с потерями.',
             'S-UI apiv2',
             'Hysteria2: UDP/QUIC + TLS, при необходимости Salamander; доступный UDP-порт.',
             'https://v2.hysteria.network/', 'hysteria2', ('S-UI apiv2 · QUIC/TLS',)),
    Protocol('tuic', 'TUIC', 'Прокси на базе QUIC для передачи TCP и UDP.',
             'S-UI apiv2',
             'TUIC v5, UUID + пароль, UDP/QUIC + TLS, ALPN h3; без 0-RTT.',
             'https://github.com/tuic-protocol/tuic', 'tuic', ('S-UI apiv2 · TUIC v5',)),
    Protocol('anytls', 'AnyTLS', 'Прокси поверх TLS с дополнением пакетов.',
             'Реализация sing-box; управляющий адаптер предстоит выбрать',
             'Явная поддержка AnyTLS в версии клиента; обычной поддержки TLS недостаточно.',
             'https://sing-box.sagernet.org/configuration/inbound/anytls/'),
    Protocol('amneziawg', 'AmneziaWG', 'VPN на основе WireGuard с маскировкой трафика.',
             'Сервер AmneziaWG; отдельный адаптер предстоит реализовать',
             'Совместимый клиент AmneziaWG и совпадающие параметры версии; обычный WG не заменяет его.',
             'https://docs.amnezia.org/documentation/amnezia-wg/'),
    Protocol('openvpn', 'OpenVPN', 'VPN с выдачей профилей подключения.',
             'Сервер OpenVPN; управляющий API предстоит выбрать',
             'Клиент OpenVPN, согласованные сертификаты и параметры профиля.',
             'https://openvpn.net/connect-docs/'),
    Protocol('ikev2-ipsec', 'IKEv2 / IPsec', 'Согласование и защита VPN-туннеля.',
             'Сервер strongSwan; управляющий адаптер предстоит реализовать',
             'Клиент IKEv2/IPsec с выбранным способом аутентификации и доверенными сертификатами.',
             'https://docs.strongswan.org/docs/latest/howtos/introduction.html'),
)


def supported_drivers():
    return tuple(item.driver_id for item in PROTOCOLS if item.driver_id is not None)


def sales_options():
    """Только реализованные пути. Наличие свободного сервера проверяет вызывающий код."""
    return [{'id': item.driver_id, 'name': item.name}
            for item in PROTOCOLS if item.driver_id is not None]


def catalog():
    """Без БД, расшифровки профилей, сетевых вызовов и утверждений о живом трафике."""
    return {
        'schema_version': 1,
        'external_checks_performed': False,
        'protocols': [dict(asdict(item),
                           implementation='in_code' if item.driver_id is not None else 'planned')
                      for item in PROTOCOLS],
    }
