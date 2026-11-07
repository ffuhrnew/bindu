"""Simulierter microReticulum-Node (nur das Provisioning-Protokoll) für Tests."""

from RNS.vendor import umsgpack


class FakeDevice:
    # Schema: [id, name, parent, [Felder]]; Feld-Keys: 1 id, 2 name, 3 type, 4 flags, 5/6 min/max int, 10/11 enum
    SCHEMA = [
        [1, "General", 0, [
            {1: 1, 2: "nomadnet_name", 3: 4, 4: 1, 9: 32},
            {1: 2, 2: "nomadnet_enabled", 3: 1, 4: 1},
            {1: 3, 2: "mgmt_allowed", 3: 7, 4: 0, 13: 16, 14: 8},
        ]],
        [2, "Radio", 0, [
            {1: 1, 2: "frequency", 3: 2, 4: 2, 5: 433000000, 6: 928000000},
            {1: 2, 2: "mode", 3: 6, 4: 1, 10: [0, 1], 11: ["full", "gateway"]},
            {1: 3, 2: "rssi", 3: 2, 4: 4},
        ]],
    ]

    def __init__(self):
        self.working = {1: {1: "NODE-A", 2: True, 3: [b"\x01" * 16]}, 2: {1: 869525000, 2: 0, 3: -80}}
        self.drafts = {}
        self.needs_reboot = False
        self.rebooted = False
        self.log = []
        self.legacy_caps = False

    def handle(self, data):
        env = list(data) if isinstance(data, (list, tuple)) else umsgpack.unpackb(data)
        op, seq = env[0], env[1]
        body = env[2] if len(env) > 2 else None
        self.log.append(op)
        try:
            return umsgpack.packb(self._op(op, seq, body))
        except KeyError as exc:
            return umsgpack.packb([101, seq, {1: 4, 2: "UnknownField %s" % exc}])

    def _op(self, op, seq, body):
        if op == 2:
            return [2, seq, {1: "1.86", 2: 2, 3: self.needs_reboot, 4: 1234}]
        if op == 3 and self.legacy_caps:
            return [3, seq, [n[0] for n in self.SCHEMA]]
        if op == 3:
            return [3, seq, [{1: n[0], 2: n[1], 4: n[2], 5: len(n[3]), 6: 1000 + n[0]} for n in self.SCHEMA]]
        if op == 1:
            ids = (body or {}).get(1)
            return [1, seq, [n for n in self.SCHEMA if not ids or n[0] in ids]]
        if op == 4:
            ids = (body or {}).get(1)
            vals = {n: dict(f) for n, f in self.working.items() if not ids or n in ids}
            out = {1: vals, 3: 99}
            if (body or {}).get(2) and self.drafts:
                out[2] = {n: dict(f) for n, f in self.drafts.items()}
            return [4, seq, out]
        if op == 5:
            applied, errs = 0, []
            for ns, fields in body[3].items():
                for fid, val in fields.items():
                    if (ns, fid) == (2, 3):
                        errs.append([ns, fid, 7])
                        continue
                    self.drafts.setdefault(ns, {})[fid] = val
                    applied += 1
            return [5, seq, {1: applied, 2: any((n, f) == (2, 1) for n, fs in self.drafts.items() for f in fs), 3: errs}]
        if op == 6:
            ids = (body or {}).get(1)
            n = 0
            for ns in list(self.drafts):
                if ids and ns not in ids:
                    continue
                self.working[ns].update(self.drafts.pop(ns))
                n += 1
            self.needs_reboot = self.needs_reboot or n > 0 and 2 not in ids if ids else self.needs_reboot
            return [6, seq, {1: n, 2: self.needs_reboot}]
        if op == 7:
            n = len(self.drafts)
            self.drafts.clear()
            return [7, seq, {1: n}]
        if op == 8:
            self.needs_reboot = True
            return [8, seq, {2: True}]
        if op == 9:
            self.rebooted = True
            return [100, seq]
        return [101, seq, {1: 2, 2: "UnknownOp"}]
