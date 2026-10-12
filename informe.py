"""Informe de resultados del bot: operaciones, % de aciertos, factor de beneficio y drawdown.
El bot lo envía solo cada semana a tus "Mensajes guardados" (y cuando escribes /informe allí).
Uso a mano:  python informe.py        (últimos 7 días)
             python informe.py 30     (últimos 30 días)
"""
from collections import defaultdict
from datetime import datetime, timedelta

import config

try:
    import MetaTrader5 as mt5
except ImportError:  # el paquete solo existe en Windows
    mt5 = None

EMPATE_EUR = 1.0  # resultados más pequeños que esto cuentan como breakeven (ni gana ni pierde)


def _neto(d):
    return d.profit + d.commission + d.swap + getattr(d, "fee", 0.0)


def _senales(desde):
    """Señales del bot cerradas desde `desde`, ordenadas por hora de cierre.
    Una señal puede tener varias posiciones (modo dividir): se suman."""
    hasta = datetime.now() + timedelta(days=2)
    todos = mt5.history_deals_get(desde - timedelta(days=30), hasta) or []
    abiertas = {p.ticket for p in (mt5.positions_get() or [])}

    # Posiciones abiertas por el bot. Se cogen TODOS sus deals: si la cierras tú a mano en MT5,
    # ese cierre no lleva la marca (magic) del bot, pero también cuenta.
    del_bot = {d.position_id for d in todos if d.entry == 0 and d.magic == config.MAGIC}
    por_pos = defaultdict(list)
    for d in todos:
        if d.position_id in del_bot:
            por_pos[d.position_id].append(d)

    senales = {}
    for pid, deals in por_pos.items():
        if pid in abiertas:
            continue
        cierres = [d for d in deals if d.entry != 0]
        if not cierres:
            continue
        cierre = max(d.time for d in cierres)
        if datetime.fromtimestamp(cierre) < desde:
            continue
        apertura = next((d for d in deals if d.entry == 0), deals[0])
        clave = apertura.comment if apertura.comment.startswith("tg") else f"pos{pid}"
        s = senales.setdefault(clave, {"symbol": apertura.symbol,
                                       "lado": "BUY" if apertura.type == 0 else "SELL",
                                       "neto": 0.0, "cierre": 0, "posiciones": 0})
        s["neto"] += sum(_neto(d) for d in deals)
        s["cierre"] = max(s["cierre"], cierre)
        s["posiciones"] += 1
    return sorted(senales.values(), key=lambda s: s["cierre"])


def _balance_en(desde):
    """Balance de la cuenta en la fecha `desde` (balance actual menos lo ganado/perdido después).
    Los depósitos y retiradas no se restan: cuentan como si ya estuvieran al principio."""
    acc = mt5.account_info()
    despues = mt5.history_deals_get(desde, datetime.now() + timedelta(days=2)) or []
    return acc.balance - sum(_neto(d) for d in despues if d.type in (0, 1)), acc.currency


def _eur(x, moneda):
    return f"{x:+,.2f} {moneda}".replace(",", "X").replace(".", ",").replace("X", ".")


def generar(dias=7):
    """Devuelve el texto del informe. MT5 tiene que estar ya conectado."""
    desde = datetime.now() - timedelta(days=dias)
    senales = _senales(desde)
    balance0, moneda = _balance_en(desde)
    titulo = f"📊 Informe del bot · últimos {dias} días ({desde:%d/%m} – {datetime.now():%d/%m})"
    if config.DRY_RUN:
        titulo += "\n🧪 Modo simulación (DRY_RUN): las operaciones simuladas no salen aquí."
    if not senales:
        return titulo + "\n\nNo se cerró ninguna operación del bot en este periodo."

    netos = [s["neto"] for s in senales]
    ganadas = [x for x in netos if x >= EMPATE_EUR]
    perdidas = [x for x in netos if x <= -EMPATE_EUR]
    empates = len(netos) - len(ganadas) - len(perdidas)
    total = sum(netos)
    decididas = len(ganadas) + len(perdidas)
    aciertos = 100 * len(ganadas) / decididas if decididas else 0
    suma_g, suma_p = sum(ganadas), -sum(perdidas)
    pf = f"{suma_g / suma_p:.2f}".replace(".", ",") if suma_p else "∞ (sin pérdidas)"

    # Máximo drawdown: mayor caída desde un máximo, cerrando operación a operación
    saldo = pico = balance0
    dd = dd_pct = 0.0
    for x in netos:
        saldo += x
        pico = max(pico, saldo)
        if pico - saldo > dd:
            dd, dd_pct = pico - saldo, 100 * (pico - saldo) / pico

    simbolos = defaultdict(lambda: [0, 0.0])
    for s in senales:
        simbolos[s["symbol"]][0] += 1
        simbolos[s["symbol"]][1] += s["neto"]
    mejor, peor = max(senales, key=lambda s: s["neto"]), min(senales, key=lambda s: s["neto"])

    def pct(x):
        return f"{100 * x / balance0:+.2f}".replace(".", ",") + " %"

    lineas = [
        titulo, "",
        f"Señales cerradas: {len(senales)}  (✅ {len(ganadas)} · ❌ {len(perdidas)} · ➖ {empates} en BE)",
        f"% de aciertos: {aciertos:.0f} %".replace(".", ",") + "  (sin contar las de BE)",
        f"Resultado: {_eur(total, moneda)}  ({pct(total)})",
        f"Factor de beneficio: {pf}",
        f"Máximo drawdown: {_eur(-dd, moneda)}  (-" + f"{dd_pct:.2f}".replace(".", ",") + " %)",
    ]
    if ganadas:
        lineas.append(f"Ganancia media: {_eur(suma_g / len(ganadas), moneda)}")
    if perdidas:
        lineas.append(f"Pérdida media: {_eur(-suma_p / len(perdidas), moneda)}")
    lineas += ["", "Por activo:"]
    lineas += [f"  {sym}: {n} señales, {_eur(v, moneda)}" for sym, (n, v) in sorted(simbolos.items())]
    lineas += [
        "",
        f"Mejor: {mejor['lado']} {mejor['symbol']} {datetime.fromtimestamp(mejor['cierre']):%d/%m} → {_eur(mejor['neto'], moneda)}",
        f"Peor:  {peor['lado']} {peor['symbol']} {datetime.fromtimestamp(peor['cierre']):%d/%m} → {_eur(peor['neto'], moneda)}",
        "",
        "Guía: factor de beneficio > 1,3 y drawdown < 10 % durante 4–8 semanas = el grupo merece la pena.",
    ]
    return "\n".join(lineas)


if __name__ == "__main__":
    import sys

    from executor import Executor

    Executor().connect()
    print(generar(int(sys.argv[1]) if len(sys.argv) > 1 else 7))
    print("\n(Las horas son las del servidor del bróker; pueden variar 1 hora respecto a España)")
    mt5.shutdown()
