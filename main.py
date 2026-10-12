"""Bot: lee señales de un grupo de Telegram y las ejecuta en MetaTrader 5.

Uso:
  python main.py                  -> arranca el bot
  python main.py --listar-chats   -> muestra tus chats y sus IDs (para TG_CHATS)
  python main.py --probar "texto" -> solo prueba cómo interpreta la IA un mensaje
"""
import argparse
import asyncio
import json
import logging
import sys
import time
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler

import config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(message)s",
    handlers=[RotatingFileHandler("bot.log", maxBytes=5_000_000, backupCount=3, encoding="utf-8"),
              logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("bot")

# Acciones que tocan dinero: si no se sabe a qué señal van, se avisa en vez de callar
_IMPORTANTES = {"mover_sl", "mover_tp", "breakeven", "cerrar_todo", "cerrar_be", "cerrar_parcial"}


class Bot:
    def __init__(self, client):
        from executor import Executor
        from parser_ai import SignalParser
        from store import Store

        self.client = client
        self.ex = Executor()
        self.parser = SignalParser()
        self.store = Store()
        self.lock = asyncio.Lock()
        self.inflight = set()  # mensajes que se están procesando ahora mismo
        self.mt5_ok = True
        self.last_msg = None

    async def notify(self, text):
        log.info(text.replace("\n", " | "))
        if config.NOTIFY:
            try:
                await self.client.send_message("me", ("🧪 " if config.DRY_RUN else "") + text)
            except Exception as e:  # noqa: BLE001
                log.warning("No se pudo enviar aviso: %s", e)

    # ---------------------------------------------------------------- objetivos de gestión
    def targets(self, chat_id, a, reply_id):
        """Señales a las que se aplica una acción de gestión.
        Devuelve None si el mensaje responde a algo que el bot no conoce (no se toca nada)."""
        active = self.store.active(chat_id)
        if not active:
            return []
        obj = a.get("objetivo") or "ultima"
        if obj == "todas":
            return active
        if reply_id:
            # El mensaje al que responde manda sobre lo que diga la IA
            ids = self.store.resolve_reply(f"{chat_id}:{reply_id}")
            if ids is not None:
                sigs = [self.store.get(i) for i in ids]
                return [s for s in sigs if s and not s["closed"]]
            if obj == "respondido":
                return None
        named = [i for i in (a.get("senal_ids") or []) if self.store.get(i)]
        ids = [i for i in named if not self.store.get(i)["closed"]]
        if ids:
            return [self.store.get(i) for i in ids]
        if named:
            return []  # la IA nombró señales concretas y ya están cerradas: no tocar otra distinta
        if obj == "simbolo" and a.get("simbolo"):
            sym = self.ex.resolve_symbol(a["simbolo"])
            match = [s for s in active if s["symbol"] == sym]
            if match:
                return match
        return [active[-1]]

    # ---------------------------------------------------------------- procesar un mensaje
    async def handle(self, msg, chat_id, edited=False, text=None):
        text = (msg.message if text is None else text or "").strip()
        if not text:
            return
        age = (datetime.now(timezone.utc) - msg.date).total_seconds()
        if not edited and age > config.MAX_MESSAGE_AGE_SEC:
            log.info("Mensaje ignorado por antiguo (%ds)", age)
            return

        key = f"{chat_id}:{msg.id}"
        if edited:
            # Si el mensaje original aún se está procesando, esperar a que acabe
            for _ in range(450):
                if key not in self.inflight:
                    break
                await asyncio.sleep(0.2)
            # Una edición solo sirve para corregir el SL/TP de una señal que ya abrimos: sin señal, ni preguntar a la IA
            if not self.store.get(key):
                log.info("Mensaje %s editado: no es una señal abierta por el bot, se ignora", key)
                return
        self.inflight.add(key)
        try:
            await self._handle(msg, chat_id, key, text, edited)
        finally:
            self.inflight.discard(key)

    async def _handle(self, msg, chat_id, key, text, edited):
        self.last_msg = datetime.now()
        reply_text, reply_id = None, None
        if msg.is_reply:
            r = await msg.get_reply_message()
            if r:
                reply_text, reply_id = r.message, r.id
            else:
                reply_id = msg.reply_to_msg_id
        log.info("Mensaje %s%s%s: %s", key, " (editado)" if edited else "",
                 f" (responde a {reply_id})" if reply_id else "", text.replace("\n", " | ")[:500])

        # La IA trabaja fuera del bloqueo: mientras piensa, el bot sigue gestionando las operaciones
        try:
            res = await self.parser.parse(text, reply_text, self.store.active(chat_id))
        except Exception as e:  # noqa: BLE001
            log.exception("Error de la IA")
            await self.notify(f"⚠️ La IA no ha podido leer este mensaje y NO se ha hecho nada ({e}). "
                              f"Revísalo tú en MT5:\n{text[:300]}")
            return
        acciones = res.get("acciones", [])
        log.info("IA: %s", json.dumps(res, ensure_ascii=False))
        if not acciones:
            return
        if res.get("confianza", 0) < config.MIN_CONFIDENCE:
            await self.notify(f"🤔 Mensaje ambiguo, NO ejecutado ({res.get('confianza')}):\n{text[:300]}\n→ {res.get('motivo')}")
            return

        async with self.lock:
            for a in acciones:
                try:
                    await self.run_action(a, key, chat_id, reply_id, edited, text)
                except Exception as e:  # noqa: BLE001
                    log.exception("Error ejecutando acción")
                    await self.notify(f"⚠️ Error ejecutando {a.get('tipo')}: {e}")

    async def run_action(self, a, key, chat_id, reply_id, edited, text=""):
        tipo = a["tipo"]

        if tipo == "abrir":
            existing = self.store.get(key)
            if existing:  # mensaje editado: actualizar SL/TP solo si el grupo los ha cambiado de verdad
                if edited and not existing["closed"]:
                    if a.get("sl") and abs(a["sl"] - existing["sl"]) > 1e-6:
                        await self.notify(self.ex.move_sl(existing, a["sl"]))
                        self.store.update(key, sl=a["sl"])
                    buy = existing["side"] == "buy"
                    tps = [t for t in (a.get("tps") or [])
                           if (t > existing["entry"] if buy else t < existing["entry"])][: config.MAX_TPS]
                    if existing.get("mode") == "primero":
                        tps = tps[:1]
                    if tps and tps != existing["tps"] and existing.get("mode") != "dividir":
                        await self.notify(self.ex.move_tp(existing, tps))
                        self.store.update(key, tps=tps)
                return
            if edited:
                return  # no abrir operaciones nuevas por ediciones de mensajes viejos
            if len(self.store.active()) >= config.MAX_OPEN_SIGNALS:
                await self.notify(f"⛔ Señal ignorada: ya hay {config.MAX_OPEN_SIGNALS} señales abiertas")
                return
            if not self.ex.daily_guard_ok(self.store.guard):
                self.store.save()
                await self.notify("⛔ Límite de pérdida diaria alcanzado: no se abren más operaciones hoy")
                return
            self.store.save()
            data, msg = self.ex.open_signal(key, a)
            if data:
                self.store.add(key, chat_id, data["symbol"], data["side"], data["entry"],
                               data["sl"], data["tickets"], data["tps"],
                               mode=data.get("mode"), volume=data.get("volume"))
                await self.notify("✅ " + msg)
            else:
                await self.notify("⛔ Señal no ejecutada: " + msg)
            return

        if edited:
            # Un mensaje de gestión editado ya se ejecutó cuando llegó: repetirlo podría cerrar o mover otra operación
            log.info("Acción %s de un mensaje editado: ignorada", tipo)
            return
        sigs = self.targets(chat_id, a, reply_id)
        if sigs is None:
            log.info("Acción %s: responde a un mensaje que el bot no tiene registrado", tipo)
            if tipo in _IMPORTANTES:
                await self.notify(f"🤷 El grupo pide «{tipo}» respondiendo a una señal que el bot no abrió. "
                                  f"No he tocado nada; revisa MT5 por si acaso:\n{text[:200]}")
            return
        if not sigs:
            log.info("Acción %s sin señales abiertas a las que aplicar", tipo)
            return
        self.store.add_ref(key, [s["id"] for s in sigs])  # para entender respuestas a este mensaje
        for s in sigs:
            if tipo == "tp_alcanzado" and self.ex.only_pending(s):
                # El grupo ya cobra el TP y nuestra orden pendiente ni se abrió: entraríamos tarde
                out = "⌛ " + self.ex.cancel_pending(s) + " (el grupo ya ha llegado al TP sin que entrara)"
            elif tipo == "breakeven":
                out, pendientes = self.ex.breakeven(s)
                self.store.update(s["id"], be_done=True, be_pending=bool(pendientes))
            elif tipo == "tp_alcanzado" and s.get("mode") == "parciales":
                notas = self.ex.take_partials(s, self.store, upto=a.get("tp_numero") or 1)
                if not notas:
                    continue
                out = "\n".join(notas)
            elif tipo == "tp_alcanzado":
                if config.MOVE_BE_ON_TP1 and not s["be_done"]:
                    out, pendientes = self.ex.breakeven(s)
                    self.store.update(s["id"], be_done=True, be_pending=bool(pendientes))
                else:
                    continue
            elif tipo == "mover_sl" and a.get("nuevo_sl"):
                out = self.ex.move_sl(s, a["nuevo_sl"])
                self.store.update(s["id"], sl=a["nuevo_sl"], be_pending=False)
            elif tipo == "mover_tp" and a.get("nuevos_tps"):
                out = self.ex.move_tp(s, a["nuevos_tps"])
                self.store.update(s["id"], tps=a["nuevos_tps"])
            elif tipo == "cerrar_todo":
                out = self.ex.close_all(s)
                if all(t < 0 for t in s["tickets"]):
                    self.store.update(s["id"], closed=True)
            elif tipo == "cerrar_be":
                out = self.ex.close_at_be(s)
            elif tipo == "cerrar_parcial":
                out = self.ex.close_partial(s, a.get("porcentaje") or 50)
            elif tipo == "cancelar_pendiente":
                out = self.ex.cancel_pending(s)
            else:
                continue
            await self.notify(out)

    # ---------------------------------------------------------------- bucles de fondo
    async def check_mt5(self):
        """Comprueba que MT5 sigue conectado; si no, avisa y reintenta conectar."""
        estado = self.ex.health()
        if estado is None:
            if self.mt5_ok:
                await self.notify("⚠️ Se ha perdido la conexión con MT5. Reintentando cada 30 s…")
                self.mt5_ok = False
            self.ex.reconnect()
            return False
        if not self.mt5_ok:
            self.mt5_ok = True
            await self.notify("✅ Conexión con MT5 recuperada")
        if estado:  # aviso puntual (p.ej. Algo Trading desactivado)
            await self.notify(estado)
        return True

    async def maintenance_loop(self):
        last_check = 0.0
        while True:
            await asyncio.sleep(2)
            if time.time() - last_check > (15 if self.mt5_ok else 30):
                last_check = time.time()
                try:
                    if not await self.check_mt5():
                        continue
                except Exception:  # noqa: BLE001
                    log.exception("Error comprobando MT5")
            if not self.mt5_ok:
                continue
            async with self.lock:
                try:
                    for n in self.ex.maintenance(self.store):
                        await self.notify(n)
                except Exception:  # noqa: BLE001
                    log.exception("Error en mantenimiento")

    async def send_report(self, dias=None):
        import informe
        async with self.lock:
            text = informe.generar(dias or config.REPORT_DAYS)
        await self.notify(text)

    async def send_status(self):
        async with self.lock:
            text = self.ex.status_text(self.store)
        ultimo = f"{self.last_msg:%d/%m %H:%M}" if self.last_msg else "ninguno desde que arrancó"
        await self.notify(f"{text}\nÚltimo mensaje del grupo procesado: {ultimo}")

    async def report_loop(self):
        """Envía el informe una vez por semana (el día y la hora de REPORT_WEEKDAY / REPORT_HOUR).
        Si el PC estaba apagado a esa hora, lo envía en cuanto arranca ese mismo día o los siguientes."""
        while True:
            await asyncio.sleep(60)
            if not config.REPORT_WEEKDAY:
                continue
            now = datetime.now()
            # inicio del último periodo programado (p. ej. el sábado más reciente a las 10:00)
            back = (now.isoweekday() - config.REPORT_WEEKDAY) % 7
            due = (now - timedelta(days=back)).replace(hour=config.REPORT_HOUR, minute=0, second=0, microsecond=0)
            if due > now:
                due -= timedelta(days=7)
            sent = self.store.data.get("informe_enviado", "")
            if sent >= due.isoformat():
                continue
            try:
                await self.send_report()
            except Exception:  # noqa: BLE001
                log.exception("Error generando el informe semanal")
            self.store.data["informe_enviado"] = now.isoformat()
            self.store.save()


# ---------------------------------------------------------------------- arranque
def make_client():
    from telethon import TelegramClient
    if not config.TG_API_ID or not config.TG_API_HASH:
        sys.exit("Falta TG_API_ID / TG_API_HASH en el archivo .env")
    return TelegramClient(config.TG_SESSION, config.TG_API_ID, config.TG_API_HASH)


async def list_chats():
    client = make_client()
    await client.start(phone=config.TG_PHONE or None)
    async for d in client.iter_dialogs():
        print(f"{d.id:>16}  {d.name}")
    await client.disconnect()


async def test_parse(text):
    from parser_ai import SignalParser
    res = await SignalParser().parse(text)
    print(json.dumps(res, indent=2, ensure_ascii=False))


_LOCK = None


def single_instance():
    """Impide que haya dos bots abiertos a la vez (abrirían cada operación dos veces)."""
    import socket
    global _LOCK
    _LOCK = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        _LOCK.bind(("127.0.0.1", 47653))
    except OSError:
        sys.exit("⛔ Ya hay otro bot funcionando en este PC. Ciérralo antes de abrir otro.")


async def run():
    from telethon import events
    single_instance()
    if not config.TG_CHATS:
        sys.exit("Configura TG_CHATS en .env (usa --listar-chats para ver los IDs)")
    client = make_client()
    await client.start(phone=config.TG_PHONE or None)
    bot = Bot(client)
    bot.ex.connect()

    @client.on(events.NewMessage(chats=config.TG_CHATS))
    async def on_new(event):
        await bot.handle(event.message, event.chat_id)

    @client.on(events.MessageEdited(chats=config.TG_CHATS))
    async def on_edit(event):
        await bot.handle(event.message, event.chat_id, edited=True)

    # Señales tuyas: escribe en Mensajes guardados "/s XAUUSD BUY 4180 TP 4190 SL 4170".
    # Para gestionarla, responde a ese mensaje con "/s cerrar", "/s mover el SL a BE", "/s TP 4200"...
    @client.on(events.NewMessage(chats="me", outgoing=True, pattern=r"^\s*/"))
    async def on_command(event):
        log.info("Comando recibido en Mensajes guardados: %s", (event.raw_text or "")[:80].replace("\n", " | "))

    @client.on(events.NewMessage(chats="me", outgoing=True, pattern=r"(?is)^\s*/(?:s|se[ñn]al)\s+(.+)"))
    async def on_manual(event):
        await bot.handle(event.message, event.chat_id, text=event.pattern_match.group(1))

    # Escribe "/informe" (o "/informe 30") en tus Mensajes guardados para recibirlo al momento
    @client.on(events.NewMessage(chats="me", outgoing=True, pattern=r"(?i)^/informe(?:\s+(\d+))?\s*$"))
    async def on_report(event):
        try:
            await bot.send_report(int(event.pattern_match.group(1) or 0))
        except Exception as e:  # noqa: BLE001
            log.exception("Error generando el informe")
            await bot.notify(f"⚠️ Error generando el informe: {e}")

    # Escribe "/estado" para saber si el bot está vivo y qué tiene abierto
    @client.on(events.NewMessage(chats="me", outgoing=True, pattern=r"(?i)^/estado\s*$"))
    async def on_status(event):
        try:
            await bot.send_status()
        except Exception as e:  # noqa: BLE001
            log.exception("Error en /estado")
            await bot.notify(f"⚠️ Error en /estado: {e}")

    modo = "SIMULACIÓN (DRY_RUN)" if config.DRY_RUN else "REAL"
    lotaje = (f"{config.LOT_PER_100} lotes por cada 100 € de capital" if config.LOT_PER_100
              else f"Riesgo {config.RISK_PERCENT}% por señal")
    await bot.notify(f"🤖 Bot iniciado en modo {modo}. {lotaje}. "
                     f"Escuchando {len(config.TG_CHATS)} chat(s). Escribe /estado para ver cómo va.")
    asyncio.create_task(bot.maintenance_loop())
    asyncio.create_task(bot.report_loop())
    await client.run_until_disconnected()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--listar-chats", action="store_true")
    ap.add_argument("--probar", metavar="TEXTO")
    args = ap.parse_args()
    if args.listar_chats:
        asyncio.run(list_chats())
    elif args.probar:
        asyncio.run(test_parse(args.probar))
    else:
        asyncio.run(run())
