"""Muestra las operaciones del bot de los últimos días y POR QUÉ se cerró cada una.
Uso:  python historial.py        (últimos 2 días)
      python historial.py 7      (últimos 7 días)
"""
import sys
from datetime import datetime, timedelta

import MetaTrader5 as mt5

import config

dias = int(sys.argv[1]) if len(sys.argv) > 1 else 2
path = config.MT5_PATH.strip('"') if config.MT5_PATH else None
if not (mt5.initialize(path) if path else mt5.initialize()):
    sys.exit(f"No conecta con MT5: {mt5.last_error()}")

MOTIVO = {
    getattr(mt5, "DEAL_REASON_CLIENT", 0): "manual (PC)",
    getattr(mt5, "DEAL_REASON_MOBILE", 1): "manual (móvil)",
    getattr(mt5, "DEAL_REASON_WEB", 2): "manual (web)",
    getattr(mt5, "DEAL_REASON_EXPERT", 3): "el BOT",
    getattr(mt5, "DEAL_REASON_SL", 4): "STOP LOSS",
    getattr(mt5, "DEAL_REASON_TP", 5): "TAKE PROFIT",
    getattr(mt5, "DEAL_REASON_SO", 6): "stop out (margen)",
}

deals = mt5.history_deals_get(datetime.now() - timedelta(days=dias), datetime.now() + timedelta(days=1)) or []
del_bot = {d.position_id for d in deals if d.entry == 0 and (d.magic == config.MAGIC or d.comment.startswith("tg"))}
deals = [d for d in deals if d.position_id in del_bot]  # incluye los cierres que haces tú a mano
if not deals:
    print("No hay operaciones del bot en ese periodo.")
for d in sorted(deals, key=lambda d: d.time):
    hora = datetime.fromtimestamp(d.time).strftime("%d/%m %H:%M:%S")
    entrada = {0: "ABRE", 1: "CIERRA", 2: "INVIERTE", 3: "CIERRA (por)"}.get(d.entry, d.entry)
    lado = "BUY" if d.type == 0 else "SELL" if d.type == 1 else d.type
    print(f"{hora} | {entrada:6} {lado:4} {d.symbol} {d.volume} lotes @ {d.price} | "
          f"beneficio {d.profit:+.2f} | posición #{d.position_id} | motivo: {MOTIVO.get(d.reason, d.reason)} "
          f"| {d.comment}")
print("\n(La hora es la del servidor del bróker, puede no coincidir con la de España)")
mt5.shutdown()
