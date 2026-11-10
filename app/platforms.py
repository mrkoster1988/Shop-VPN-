"""Read-only platform inventory for Shop VPN.

This is not OAuth, proof of connectivity, or an identity linking mechanism.
"""
from . import config as c


def public_catalog():
    """Report only code/config facts, never credentials or external health."""
    configured = bool(c.BOT_TOKEN and c.BOT_USERNAME)
    return {
        'schema_version': 1,
        'account_linking': 'disabled',
        'external_checks_performed': False,
        'platforms': [
            {'id': 'web', 'name': 'Веб-кабинет', 'implementation': 'in_code',
             'configured': True, 'connection_verified': False,
             'enabled_for_sales': True},
            {'id': 'telegram', 'name': 'Telegram', 'implementation': 'in_code',
             'configured': configured, 'connection_verified': False,
             'enabled_for_sales': configured},
            *[
                {'id': channel, 'name': title, 'implementation': 'planned',
                 'configured': False, 'connection_verified': False,
                 'enabled_for_sales': False}
                for channel, title in (
                    ('vk', 'ВКонтакте'),
                    ('max', 'MAX'),
                    ('matrix', 'Matrix / Element'),
                )
            ],
        ],
    }
