"""Ejecuta las acciones en MetaTrader 5."""
import itertools
import logging
import math
import time
from datetime import date

import config

try:
    import MetaTrader5 as mt5
except ImportError:  # el paquete solo existe en Windows
    mt5 = None

log = logging.getLogger("mt5")
_fake_ticket = itertools.count(-1, -1)  # tickets negativos = simulados (DRY_RUN)


def _decimals(step):
    return max(0, -int(math.floor(math.log10(step) + 1e-9)))


class Executor:
    # ------------------------------------------------------------------ conexión
    def connect(self):
        if mt5 is None:
            raise RuntimeError("Falta el paquete MetaTrader5 (solo Windows): pip install MetaTrader5")
        kw = {}
        if config.MT5_LOGIN:
            kw = dict(login=int(config.MT5_LOGIN), password=config.MT5_PASSWORD, server=config.MT5_SERVER)
        path = config.MT5_PATH.strip('"') if config.MT5_PATH else None
        ok = False
        for intento in range(1, 4):
            ok = mt5.initialize(path, timeout=60000, **kw) if path else mt5.initialize(timeout=60000, **kw)
            if ok:
                break
            log.warning("MT5 no responde (intento %d/3): %s", intento, mt5.last_error())
            mt5.shutdown()
            time.sleep(5)
        if not ok:
            raise RuntimeError(
                f"No se pudo conectar a MT5: {mt5.last_error()}\n"
                "  1) Abre MT5, inicia sesión en tu cuenta y espera a que cargue del todo.\n"
                "  2) Pon en .env MT5_PATH=ruta completa de terminal64.exe de TU MetaTrader\n"
                "     (clic derecho en el acceso directo de MT5 > Propiedades > Destino).\n"
                "  3) No ejecutes la consola como administrador si MT5 no lo está (o al revés).")
        acc = mt5.account_info()
        log.info("Conectado a MT5: cuenta %s (%s) balance %.2f %s",
                 acc.login, acc.server, acc.balance, acc.currency)
        term = mt5.terminal_info()
        if term and not term.trade_allowed:
            log.warning("¡'Algo Trading' está desactivado en MT5! Actívalo o no se abrirán operaciones.")
        return acc

    # ------------------------------------------------------------------ símbolos
    def resolve_symbol(self, raw):
        if not raw:
            return None
        s = raw.upper().replace("/", "").replace(" ", "").replace("#", "")
        s = config.SYMBOL_MAP.get(s, s)
        for cand in (s + config.SYMBOL_SUFFIX, s):
            info = mt5.symbol_info(cand)
            if info:
                if not info.visible:
                    mt5.symbol_select(cand, True)
                return cand
        for sym in mt5.symbols_get() or []:
            if sym.name.upper().startswith(s):
                mt5.symbol_select(sym.name, True)
                return sym.name
        return None

    def symbol_allowed(self, name):
        return not config.ALLOWED_SYMBOLS or any(name.upper().startswith(a) for a in config.ALLOWED_SYMBOLS)

    # ------------------------------------------------------------------ riesgo
    def normalize_volume(self, symbol, vol):
        info = mt5.symbol_info(symbol)
        step = info.volume_step
        v = math.floor(vol / step + 1e-9) * step
        v = min(v, info.volume_max, config.MAX_LOT or info.volume_max)
        v = round(v, _decimals(step))
        return v if v >= info.volume_min else 0.0

    def loss_per_lot(self, symbol, side, entry, sl):
        otype = mt5.ORDER_TYPE_BUY if side == "buy" else mt5.ORDER_TYPE_SELL
        p = mt5.order_calc_profit(otype, symbol, 1.0, entry, sl)
        if p is None or p >= 0:
            return None
        return abs(p)

    def daily_guard_ok(self, guard):
        if config.DAILY_LOSS_LIMIT_PERCENT <= 0:
            return True
        acc = mt5.account_info()
        today = date.today().isoformat()
        if guard.get("day") != today:
            guard["day"] = today
            guard["start_balance"] = acc.balance
        return acc.equity >= guard["start_balance"] * (1 - config.DAILY_LOSS_LIMIT_PERCENT / 100)

    # ------------------------------------------------------------------ envío
    def _fillings(self, symbol, pending=False):
        fm = mt5.symbol_info(symbol).filling_mode
        modes = []
        if fm & 1:
            modes.append(mt5.ORDER_FILLING_FOK)
        if fm & 2:
            modes.append(mt5.ORDER_FILLING_IOC)
        modes.append(mt5.ORDER_FILLING_RETURN)
        if pending:
            modes.remove(mt5.ORDER_FILLING_RETURN)
            modes.insert(0, mt5.ORDER_FILLING_RETURN)
        return modes

    def _send(self, req, fill=True):
        if config.DRY_RUN:
            t = next(_fake_ticket)
            log.info("[SIMULADO] %s", {k: v for k, v in req.items() if k != "deviation"})
            return t, None
        modes = self._fillings(req["symbol"], req.get("action") == mt5.TRADE_ACTION_PENDING) if fill else [None]
        err = "sin respuesta"
        for m in modes:
            if m is not None:
                req["type_filling"] = m
            r = mt5.order_send(req)
            if r is None:
                err = str(mt5.last_error())
                continue
            if r.retcode in (mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_PLACED, mt5.TRADE_RETCODE_DONE_PARTIAL):
                return (r.order or True), None  # SL/TP no devuelve número de orden
            if r.retcode == mt5.TRADE_RETCODE_NO_CHANGES:  # 10025: ya estaba así
                return req.get("position") or req.get("order") or r.order, None
            err = f"{r.retcode} {r.comment}"
            if r.retcode != mt5.TRADE_RETCODE_INVALID_FILL:
                break
        log.error("Orden rechazada: %s | %s", err, req)
        return None, err

    # ------------------------------------------------------------------ abrir
    def open_signal(self, key, a):
        """Devuelve (dict_con_datos | None, mensaje).
        Las señales a mercado entran SIEMPRE al instante al precio actual (sin margen) y se saltan los TP
        que el precio ya pasó. Solo las señales que piden expresamente limit/stop quedan como pendientes."""
        symbol = self.resolve_symbol(a.get("simbolo"))
        if not symbol:
            return None, f"Símbolo no encontrado en MT5: {a.get('simbolo')}"
        if not self.symbol_allowed(symbol):
            return None, f"Símbolo {symbol} no permitido (ALLOWED_SYMBOLS)"
        side = a.get("direccion")
        if side not in ("buy", "sell"):
            return None, "La señal no indica buy/sell"
        buy = side == "buy"

        info = mt5.symbol_info(symbol)
        tick = mt5.symbol_info_tick(symbol)
        pt, dg = info.point, info.digits
        now = tick.ask if buy else tick.bid

        if config.MAX_SPREAD_POINTS and (tick.ask - tick.bid) / pt > config.MAX_SPREAD_POINTS:
            return None, f"Spread demasiado alto en {symbol}"

        # --- tipo de orden y precio de entrada
        e = [x for x in (a.get("entrada_min"), a.get("entrada_max")) if x]
        emin, emax = (min(e), max(e)) if e else (None, None)
        tol = config.ENTRY_TOLERANCE_POINTS * pt
        ot = a.get("tipo_orden") or "market"
        kind, entry = "market", now
        if emin is not None and ot != "market":
            edge = emax if buy else emin  # primer precio de la zona que toca el mercado
            if ot == "limit":
                better = now <= edge if buy else now >= edge
                if not better:
                    kind, entry = "limit", edge
            elif ot == "stop":
                triggered = now >= edge if buy else now <= edge
                if not triggered:
                    kind, entry = "stop", edge
                elif abs(now - edge) > tol:
                    return None, "El precio ya pasó el nivel del stop; señal ignorada"

        # --- stop loss
        pip = config.PIP_SIZES.get(symbol.upper(), config.PIP_SIZES.get(a.get("simbolo", "").upper(), pt * 10))
        sl = a.get("sl")
        if not sl and a.get("sl_pips"):
            sl = entry - a["sl_pips"] * pip if buy else entry + a["sl_pips"] * pip
        sl_prov = False
        if not sl and config.DEFAULT_SL_POINTS:
            # El grupo manda el SL después: mientras tanto, SL de seguridad (se cambia al llegar el suyo)
            d = config.DEFAULT_SL_POINTS * pt
            sl = entry - d if buy else entry + d
            sl_prov = True
        if not sl:
            return None, "Señal sin SL (configura DEFAULT_SL_POINTS si quieres operarlas)"
        if (buy and sl >= entry) or (not buy and sl <= entry):
            return None, f"SL {sl} en el lado incorrecto de la entrada {entry}"

        # --- take profits
        tps = list(a.get("tps") or [])
        if not tps and a.get("tps_pips"):
            tps = [entry + p * pip if buy else entry - p * pip for p in a["tps_pips"]]
        raw_tps = list(tps)
        tps = [t for t in tps if (t > entry if buy else t < entry)][: config.MAX_TPS]
        pasados = [t for t in raw_tps if not (t > entry if buy else t < entry)]
        instant = kind == "market"
        if instant and pasados and not tps:
            return None, f"El precio ({round(entry, dg)}) ya ha pasado todos los TP. No se abre."
        if pasados and not instant:
            # El precio ya ha pasado algún TP de la señal: entramos tarde y la operación ya no tiene sentido
            return None, (f"Señal tardía: el precio de entrada ({round(entry, dg)}) ya ha pasado el TP "
                          f"{pasados[0]}. No se abre.")
        mode = config.TP_MODE
        if mode == "primero" and tps:
            tps = [tps[0]]

        # --- lotaje por riesgo
        lpl = self.loss_per_lot(symbol, side, entry, sl)
        if not lpl:
            return None, "No se pudo calcular el riesgo por lote"
        acc = mt5.account_info()
        if config.LOT_PER_100:
            # Lote fijo según el capital (no depende de la distancia al SL)
            total = self.normalize_volume(symbol, acc.balance / 100 * config.LOT_PER_100)
            if total <= 0:
                return None, f"Balance demasiado pequeño para el lote mínimo ({info.volume_min})"
        else:
            risk_money = acc.balance * config.RISK_PERCENT / 100
            total = self.normalize_volume(symbol, risk_money / lpl)
            if total <= 0:
                return None, (f"Riesgo {risk_money:.2f} demasiado pequeño para el lote mínimo "
                              f"({info.volume_min}) con ese SL")
        if mode == "dividir":
            # Una posición por TP: repartir el volumen (en múltiplos de volume_step, sin bajar del mínimo)
            step, dec = info.volume_step, _decimals(info.volume_step)
            steps_total = round(total / step)
            min_steps = max(1, round(info.volume_min / step))
            n = max(1, min(len(tps), steps_total // min_steps))
            base, extra = divmod(steps_total, n)
            vols = [round((base + (1 if i < extra else 0)) * step, dec) for i in range(n)]
            tps_use = tps[:n] if tps else [0.0]
            tps_store = tps_use
        else:
            # Una sola operación. En modo "parciales" el TP del servidor es el último;
            # los anteriores los gestiona el bot cerrando una parte en cada uno.
            vols = [total]
            tps_use = [tps[-1]] if tps else [0.0]
            tps_store = tps

        # --- envío
        if kind == "market":
            action = mt5.TRADE_ACTION_DEAL
            otype = mt5.ORDER_TYPE_BUY if buy else mt5.ORDER_TYPE_SELL
        else:
            action = mt5.TRADE_ACTION_PENDING
            otype = {("buy", "limit"): mt5.ORDER_TYPE_BUY_LIMIT, ("sell", "limit"): mt5.ORDER_TYPE_SELL_LIMIT,
                     ("buy", "stop"): mt5.ORDER_TYPE_BUY_STOP, ("sell", "stop"): mt5.ORDER_TYPE_SELL_STOP}[(side, kind)]

        tickets, errors, sent = [], [], []
        for tp, vol in zip(tps_use, vols):
            req = {
                "action": action, "symbol": symbol, "volume": vol, "type": otype,
                "price": round(entry, dg), "sl": round(sl, dg), "tp": round(tp, dg),
                "deviation": config.DEVIATION_POINTS, "magic": config.MAGIC,
                "comment": f"tg{key.split(':')[-1]}"[:31], "type_time": mt5.ORDER_TIME_GTC,
            }
            if kind == "market":  # precio actual para mercado
                req["price"] = round(mt5.symbol_info_tick(symbol).ask if buy else mt5.symbol_info_tick(symbol).bid, dg)
            t, err = self._send(req)
            if t:
                tickets.append(t)
                sent.append(vol)
            else:
                errors.append(err)

        if not tickets:
            return None, "Órdenes rechazadas: " + "; ".join(errors)
        real_risk = sum(sent) * lpl
        msg = (f"{'🟢 BUY' if buy else '🔴 SELL'} {symbol} ({kind}) lotes {' + '.join(map(str, sent))} @ {round(entry, dg)}\n"
               f"SL {round(sl, dg)} | TP {', '.join(str(round(t, dg)) for t in tps_store if t) or '—'}"
               + (" (cierres parciales)" if mode == "parciales" and len(tps_store) > 1 else "") + "\n"
               f"Riesgo ≈ {real_risk:.2f} {acc.currency}")
        if sl_prov:
            msg += "\n🛡️ La señal no traía SL: puesto uno provisional. Se cambiará cuando el grupo envíe el suyo."
        if instant and pasados:
            msg += f"\nTP ya superados por el precio (no se usan): {', '.join(map(str, pasados))}"
        if errors:
            msg += "\n⚠️ Algunas órdenes fallaron: " + "; ".join(errors)
        return {"symbol": symbol, "side": side, "entry": round(entry, dg), "sl": round(sl, dg),
                "tickets": tickets, "tps": tps_store, "mode": mode, "volume": sum(sent)}, msg

    # ------------------------------------------------------------------ gestión
    def live(self, tickets):
        pos, ords = [], []
        for t in tickets:
            if t < 0:
                continue
            p = mt5.positions_get(ticket=t)
            if p:
                pos.append(p[0])
                continue
            o = mt5.orders_get(ticket=t)
            if o:
                ords.append(o[0])
        return pos, ords

    def _sltp(self, p, sl=None, tp=None):
        req = {"action": mt5.TRADE_ACTION_SLTP, "position": p.ticket, "symbol": p.symbol,
               "sl": p.sl if sl is None else sl, "tp": p.tp if tp is None else tp, "magic": config.MAGIC}
        return self._send(req, fill=False)

    def _modify_order(self, o, sl=None, tp=None):
        req = {"action": mt5.TRADE_ACTION_MODIFY, "order": o.ticket, "symbol": o.symbol,
               "price": o.price_open, "sl": o.sl if sl is None else sl, "tp": o.tp if tp is None else tp,
               "type_time": o.type_time, "expiration": o.time_expiration}
        return self._send(req, fill=False)

    def _close(self, p, volume=None):
        tick = mt5.symbol_info_tick(p.symbol)
        is_buy = p.type == mt5.POSITION_TYPE_BUY
        req = {"action": mt5.TRADE_ACTION_DEAL, "position": p.ticket, "symbol": p.symbol,
               "volume": volume or p.volume, "type": mt5.ORDER_TYPE_SELL if is_buy else mt5.ORDER_TYPE_BUY,
               "price": tick.bid if is_buy else tick.ask, "deviation": config.DEVIATION_POINTS,
               "magic": config.MAGIC, "comment": "tg close"}
        return self._send(req)

    def _cancel(self, o):
        return self._send({"action": mt5.TRADE_ACTION_REMOVE, "order": o.ticket, "symbol": o.symbol}, fill=False)

    def _check_dry(self, sig, what):
        if all(t < 0 for t in sig["tickets"]):
            return f"[SIMULADO] {what} en {sig['symbol']}"
        return None

    def move_sl(self, sig, new_sl):
        if (m := self._check_dry(sig, f"SL → {new_sl}")):
            return m
        pos, ords = self.live(sig["tickets"])
        dg = mt5.symbol_info(sig["symbol"]).digits
        for p in pos:
            self._sltp(p, sl=round(new_sl, dg))
        for o in ords:
            self._modify_order(o, sl=round(new_sl, dg))
        return f"SL movido a {new_sl} en {sig['symbol']} ({len(pos) + len(ords)} órdenes)"

    def move_tp(self, sig, new_tps):
        if (m := self._check_dry(sig, f"TP → {new_tps}")):
            return m
        pos, ords = self.live(sig["tickets"])
        if sig.get("mode", "dividir") != "dividir":
            new_tps = [new_tps[-1]]  # una sola operación: el TP del servidor es el último
        items = sorted(pos + ords, key=lambda x: x.ticket)
        dg = mt5.symbol_info(sig["symbol"]).digits
        for i, it in enumerate(items):
            tp = round(new_tps[min(i, len(new_tps) - 1)], dg)
            (self._sltp(it, tp=tp) if it in pos else self._modify_order(it, tp=tp))
        return f"TP actualizado en {sig['symbol']}: {new_tps}"

    def breakeven(self, sig):
        if (m := self._check_dry(sig, "Breakeven")):
            return m
        pos, _ = self.live(sig["tickets"])
        info = mt5.symbol_info(sig["symbol"])
        tick = mt5.symbol_info_tick(sig["symbol"])
        done = ya = 0
        for p in pos:
            buy = p.type == mt5.POSITION_TYPE_BUY
            off = config.BE_OFFSET_POINTS * info.point
            be = round(p.price_open + off if buy else p.price_open - off, info.digits)
            # ¿El SL ya está en BE o mejor? Entonces no hay nada que hacer
            if p.sl and ((buy and p.sl >= be - info.point / 2) or (not buy and p.sl <= be + info.point / 2)):
                ya += 1
                continue
            in_profit = tick.bid > be if buy else tick.ask < be
            if in_profit:
                t, _ = self._sltp(p, sl=be)
                done += bool(t)
        if not pos:
            return f"Breakeven: no quedan posiciones abiertas de {sig['symbol']}"
        if ya == len(pos):
            return f"Breakeven: {sig['symbol']} ya estaba protegida en BE"
        return f"Breakeven aplicado a {done + ya}/{len(pos)} posiciones de {sig['symbol']}"

    def close_all(self, sig):
        if (m := self._check_dry(sig, "Cerrar todo")):
            return m
        pos, ords = self.live(sig["tickets"])
        for p in pos:
            self._close(p)
        for o in ords:
            self._cancel(o)
        return f"Cerradas {len(pos)} posiciones y canceladas {len(ords)} pendientes de {sig['symbol']}"

    def close_at_be(self, sig):
        """'Cerrar a BE': si está en la entrada o ganando se cierra ya; si va perdiendo, TP en la entrada."""
        if (m := self._check_dry(sig, "Cerrar a BE")):
            return m
        pos, ords = self.live(sig["tickets"])
        for o in ords:
            self._cancel(o)
        if not pos:
            return f"Cerrar a BE: no quedan posiciones abiertas de {sig['symbol']}"
        info = mt5.symbol_info(sig["symbol"])
        tick = mt5.symbol_info_tick(sig["symbol"])
        cerradas = esperando = 0
        for p in pos:
            buy = p.type == mt5.POSITION_TYPE_BUY
            if (tick.bid >= p.price_open) if buy else (tick.ask <= p.price_open):
                t, _ = self._close(p)
                cerradas += bool(t)
            else:
                t, _ = self._sltp(p, tp=round(p.price_open, info.digits))
                esperando += bool(t)
        partes = []
        if cerradas:
            partes.append(f"{cerradas} cerrada(s) ya en BE o ganando")
        if esperando:
            partes.append(f"{esperando} con el TP puesto en la entrada (se cerrará sola al volver a ese precio)")
        return f"🔒 Cerrar a BE en {sig['symbol']}: " + ("; ".join(partes) or "no se pudo modificar")

    def close_partial(self, sig, pct):
        if (m := self._check_dry(sig, f"Cierre parcial {pct}%")):
            return m
        pos, _ = self.live(sig["tickets"])
        if not pos:
            return "No hay posiciones abiertas para cerrar parcialmente"
        # Primero las posiciones con el TP más cercano (normalmente TP1)
        tick = mt5.symbol_info_tick(sig["symbol"])
        pos.sort(key=lambda p: abs((p.tp or 1e12) - (tick.bid + tick.ask) / 2))
        target = sum(p.volume for p in pos) * pct / 100
        closed = 0.0
        for p in pos:
            remaining = target - closed
            if remaining <= 1e-9:
                break
            if p.volume <= remaining + 1e-9:
                self._close(p)
                closed += p.volume
            else:
                v = self.normalize_volume(p.symbol, remaining)
                if v > 0:
                    self._close(p, v)
                    closed += v
                break
        return f"Cerrado parcial {closed} lotes ({pct}%) de {sig['symbol']}"

    def only_pending(self, sig):
        """True si la señal solo tiene órdenes pendientes (aún no se ha abierto ninguna posición)."""
        real = [t for t in sig["tickets"] if t > 0]
        if not real:
            return False
        pos, ords = self.live(real)
        return bool(ords) and not pos

    def cancel_pending(self, sig):
        if (m := self._check_dry(sig, "Cancelar pendientes")):
            return m
        _, ords = self.live(sig["tickets"])
        for o in ords:
            self._cancel(o)
        return f"Canceladas {len(ords)} órdenes pendientes de {sig['symbol']}"

    # ------------------------------------------------------------------ cierres parciales (modo una sola operación)
    def _partial_pcts(self, n):
        """% del volumen inicial a cerrar en cada TP intermedio (n = número total de TPs)."""
        custom = config.PARTIAL_PERCENTS[: n - 1]
        if len(custom) == n - 1:
            return custom
        return [100 / n] * (n - 1)  # reparto igual: con 3 TPs cierra 1/3 en TP1, 1/3 en TP2 y el resto va al TP3

    def take_partials(self, sig, store, upto=None):
        """Cierra la parte correspondiente de cada TP intermedio alcanzado. Devuelve avisos."""
        tps = sig.get("tps") or []
        if sig.get("mode") != "parciales" or len(tps) < 2:
            return []
        pos, _ = self.live([t for t in sig["tickets"] if t > 0])
        if not pos:
            return []
        p = pos[0]
        info = mt5.symbol_info(p.symbol)
        tick = mt5.symbol_info_tick(p.symbol)
        buy = p.type == mt5.POSITION_TYPE_BUY
        price = tick.bid if buy else tick.ask
        hits = sig.get("tp_hits", 0)
        pcts = self._partial_pcts(len(tps))
        init_vol = sig.get("volume") or p.volume
        notes = []
        while hits < len(tps) - 1:
            reached = (price >= tps[hits]) if buy else (price <= tps[hits])
            if not reached and not (upto and hits < upto):
                break
            vol = self.normalize_volume(p.symbol, init_vol * pcts[hits] / 100)
            vol = min(vol, round(p.volume - info.volume_min, _decimals(info.volume_step)))
            if vol >= info.volume_min:
                t, err = self._close(p, vol)
                notes.append(f"💰 TP{hits + 1} {p.symbol}: cerrados {vol} lotes" + (f" (error: {err})" if err else ""))
                pos, _ = self.live([p.ticket])
                if not pos:
                    break
                p = pos[0]
            else:
                notes.append(f"💰 TP{hits + 1} {p.symbol}: lote demasiado pequeño para cerrar parcial")
            hits += 1
            if hits == 1 and config.MOVE_BE_ON_TP1 and not sig.get("be_done"):
                notes.append("🔒 " + self.breakeven(sig))
                sig["be_done"] = True
        store.update(sig["id"], tp_hits=hits, be_done=sig.get("be_done", False))
        return notes

    # ------------------------------------------------------------------ mantenimiento periódico
    def maintenance(self, store):
        """BE automático al tocar TP1, caducidad de pendientes y limpieza. Devuelve avisos."""
        notes = []
        for sig in store.active():
            real = [t for t in sig["tickets"] if t > 0]
            if not real:
                continue
            pos, ords = self.live(real)
            if not pos and not ords:
                store.update(sig["id"], closed=True)
                notes.append(f"✅ Señal {sig['symbol']} {sig['side']} finalizada")
                continue
            if ords and config.PENDING_EXPIRY_MINUTES and not pos:
                if time.time() - sig["created"] > config.PENDING_EXPIRY_MINUTES * 60:
                    for o in ords:
                        self._cancel(o)
                    notes.append(f"⌛ Pendientes de {sig['symbol']} canceladas por caducidad")
                    continue
            if ords and not pos and sig.get("tps"):
                # El precio llegó al TP1 sin pasar por nuestra entrada: la operación ya se ha ido sin nosotros
                tick = mt5.symbol_info_tick(sig["symbol"])
                tp1 = sig["tps"][0]
                if tick and ((sig["side"] == "buy" and tick.bid >= tp1) or (sig["side"] == "sell" and tick.ask <= tp1)):
                    for o in ords:
                        self._cancel(o)
                    notes.append(f"⌛ Pendiente de {sig['symbol']} cancelada: el precio llegó al TP1 ({tp1}) sin entrar")
                    continue
            if sig.get("mode") == "parciales":
                notes += self.take_partials(sig, store)
                continue
            if config.MOVE_BE_ON_TP1 and not sig["be_done"] and pos:
                live_ids = {p.ticket for p in pos} | {o.ticket for o in ords}
                gone = [t for t in real if t not in live_ids]
                won = any(sum(d.profit for d in (mt5.history_deals_get(position=t) or [])) > 0 for t in gone)
                if won:
                    notes.append("🔒 TP1 tocado → " + self.breakeven(sig))
                    store.update(sig["id"], be_done=True)
        return notes
