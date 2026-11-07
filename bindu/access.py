"""Zugriffsschutz: nur Geräte aus dem Tailnet (oder ausdrücklich erlaubten Netzen) dürfen die Oberfläche nutzen.

Geprüft wird die Quell-IP jeder Anfrage. Loopback ist immer erlaubt (lokale Nutzung und
Reverse-Proxys wie `tailscale serve`, die auf demselben Rechner laufen). Das ist keine Anmeldung,
sondern ein Netzwerkfilter: er ersetzt keine Firewall, schließt aber das LAN aus.
"""

import ipaddress

TAILNET_NETWORKS = ("100.64.0.0/10", "fd7a:115c:a1e0::/48")


def _parse_ip(text):
    """IP-Text -> ip_address; IPv4-in-IPv6 ('::ffff:1.2.3.4') wird als IPv4 behandelt. None bei Unsinn."""
    try:
        ip = ipaddress.ip_address(str(text).split("%", 1)[0])  # Zonen-Suffix (fe80::1%en0) abschneiden
    except ValueError:
        return None
    mapped = getattr(ip, "ipv4_mapped", None)
    return mapped if mapped is not None else ip


class AccessPolicy:
    def __init__(self, networks=TAILNET_NETWORKS, extra=(), enabled=True):
        self.enabled = enabled
        self.networks = [ipaddress.ip_network(n, strict=False) for n in list(networks) + list(extra)]

    @classmethod
    def default(cls, extra=()):
        return cls(TAILNET_NETWORKS, extra)

    @classmethod
    def disabled(cls):
        return cls(enabled=False)

    def allows(self, client_ip):
        if not self.enabled:
            return True
        ip = _parse_ip(client_ip)
        if ip is None:
            return False
        if ip.is_loopback:
            return True
        return any(ip.version == net.version and ip in net for net in self.networks)

    def describe(self):
        if not self.enabled:
            return "Zugriffsprüfung AUS"
        return "erlaubt: Loopback, " + ", ".join(str(n) for n in self.networks)
