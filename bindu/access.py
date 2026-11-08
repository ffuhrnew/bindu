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


def parse_networks(items):
    """Liste von IPs oder Netzen (``10.0.0.5``, ``10.0.0.0/24``) -> ip_network-Liste; ValueError bei Unsinn."""
    return [ipaddress.ip_network(str(x).strip(), strict=False) for x in items]


def _in(ip, networks):
    return any(ip.version == net.version and ip in net for net in networks)


def client_ip(peer, forwarded_for, trusted):
    """Die Adresse, die die Zugriffsprüfung beurteilen soll.

    Kommt die Verbindung von einem vertrauenswürdigen Reverse-Proxy (``trusted``), steht der echte Besucher in
    ``X-Forwarded-For``; gilt die letzte Adresse der Kette, die nicht selbst ein vertrauenswürdiger Proxy ist.
    Alles andere (fremde Verbindung, fehlender Header) bleibt bei der Adresse der Verbindung: ein Besucher kann sich
    den Header also nicht selbst setzen, weil er nur von den genannten Proxys beachtet wird."""
    ip = _parse_ip(peer)
    if not trusted or ip is None or not _in(ip, trusted):
        return peer
    chain = [part.strip() for line in forwarded_for or () for part in str(line).split(",") if part.strip()]
    if not chain:
        return peer
    for text in reversed(chain):
        hop = _parse_ip(text)
        if hop is None:
            return text  # unlesbar: die Zugriffsprüfung lehnt es ab
        if not _in(hop, trusted):
            return text
    return chain[0]
