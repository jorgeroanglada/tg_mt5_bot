"""Guarda en un JSON qué señales de Telegram corresponden a qué tickets de MT5."""
import json
import os
import time

import config

KEEP_DAYS = 30  # las señales cerradas y referencias más antiguas se borran al arrancar


class Store:
    def __init__(self, path=None):
        self.path = path or config.STATE_FILE
        self.data = {}
        if os.path.exists(self.path):
            with open(self.path, encoding="utf-8") as f:
                self.data = json.load(f)
        self.data.setdefault("signals", {})
        self.data.setdefault("guard", {})
        self.data.setdefault("refs", {})
        self._prune()

    def _prune(self):
        limit = time.time() - KEEP_DAYS * 86400
        sigs = self.data["signals"]
        for k in [k for k, s in sigs.items() if s["closed"] and s.get("created", 0) < limit]:
            del sigs[k]
        refs = self.data["refs"]
        for k in [k for k, r in refs.items() if r.get("t", 0) < limit]:
            del refs[k]

    def save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, self.path)

    @property
    def guard(self):
        return self.data["guard"]

    def add(self, key, chat_id, symbol, side, entry, sl, tickets, tps, **extra):
        self.data["signals"][key] = {
            "id": key, "chat_id": str(chat_id), "symbol": symbol, "side": side,
            "entry": entry, "sl": sl, "tps": tps, "tickets": tickets,
            "created": time.time(), "be_done": False, "closed": False, "tp_hits": 0, **extra,
        }
        self.save()

    def get(self, key):
        return self.data["signals"].get(key)

    def active(self, chat_id=None):
        sigs = [s for s in self.data["signals"].values() if not s["closed"]]
        if chat_id is not None:
            sigs = [s for s in sigs if s["chat_id"] == str(chat_id)]
        return sorted(sigs, key=lambda s: s["created"])

    def update(self, key, **fields):
        self.data["signals"][key].update(fields)
        self.save()

    # Un mensaje de gestión ("Mover el SL a BE") se apunta con sus señales: si luego el grupo
    # responde a ESE mensaje ("Be"), se sabe a qué operación se refiere.
    def add_ref(self, key, signal_ids):
        self.data["refs"][key] = {"ids": list(signal_ids), "t": time.time()}
        self.save()

    def resolve_reply(self, key):
        """Ids de las señales a las que se refiere un mensaje, o None si el bot no lo conoce."""
        if key in self.data["signals"]:
            return [key]
        ref = self.data["refs"].get(key)
        return list(ref["ids"]) if ref else None
