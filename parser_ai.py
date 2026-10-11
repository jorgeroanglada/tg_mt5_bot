"""Convierte un mensaje libre de Telegram en acciones de trading estructuradas usando IA (Gemini, Groq, Ollama o Claude)."""
import json
import logging
import re

import config

log = logging.getLogger("parser")

_num = {"type": ["number", "null"]}

TOOL = {
    "name": "registrar_acciones",
    "description": "Registra las acciones de trading que se deducen del mensaje del grupo de señales.",
    "input_schema": {
        "type": "object",
        "properties": {
            "acciones": {
                "type": "array",
                "description": "Vacío si el mensaje no es ni una señal ni una instrucción de gestión.",
                "items": {
                    "type": "object",
                    "properties": {
                        "tipo": {
                            "type": "string",
                            "enum": ["abrir", "mover_sl", "mover_tp", "breakeven", "cerrar_todo",
                                     "cerrar_parcial", "cancelar_pendiente", "tp_alcanzado"],
                        },
                        "simbolo": {"type": ["string", "null"], "description": "Tal como aparece: XAUUSD, GOLD, NAS100..."},
                        "direccion": {"type": ["string", "null"], "enum": ["buy", "sell", None]},
                        "tipo_orden": {"type": ["string", "null"], "enum": ["market", "limit", "stop", None]},
                        "entrada_min": _num,
                        "entrada_max": _num,
                        "sl": _num,
                        "sl_pips": _num,
                        "tps": {"type": "array", "items": {"type": "number"}},
                        "tps_pips": {"type": "array", "items": {"type": "number"}},
                        "nuevo_sl": _num,
                        "nuevos_tps": {"type": "array", "items": {"type": "number"}},
                        "porcentaje": {"type": ["number", "null"], "description": "Para cerrar_parcial, 0-100"},
                        "tp_numero": {"type": ["integer", "null"]},
                        "objetivo": {"type": "string", "enum": ["respondido", "ultima", "simbolo", "todas"],
                                     "description": "A qué señal(es) abiertas se aplica una acción de gestión"},
                        "senal_ids": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["tipo"],
                },
            },
            "confianza": {"type": "number", "description": "0 a 1"},
            "motivo": {"type": "string", "description": "Explicación breve en español"},
        },
        "required": ["acciones", "confianza", "motivo"],
    },
}

SYSTEM = """Eres el intérprete de un bot que copia señales de un grupo de Telegram de trading a MetaTrader 5.
Lee el mensaje y devuelve las acciones que se deducen de él.

Reglas:
- Si el mensaje es publicidad, análisis, resultados, capturas, motivación, una pregunta o cualquier cosa que no pida abrir/gestionar una operación: acciones = [].
- No inventes precios. Usa solo números que aparezcan en el mensaje (o en el mensaje al que responde).
- "abrir": una nueva señal. direccion buy/sell (compra/long = buy, venta/short = sell).
  * "buy/sell/buy now/sell now/ahora/market/ya" o sin indicar tipo -> tipo_orden "market" (ejecución por mercado).
  * SOLO si aparece la palabra LIMIT -> "limit"; SOLO si aparece "buy stop/sell stop" -> "stop". Si no, SIEMPRE "market".
  * Si la señal no trae SL, ábrela igual con sl = null (el grupo lo envía después).
  * Zona de entrada "2350-2345" -> entrada_min=2345, entrada_max=2350. Un solo precio -> entrada_min = entrada_max.
  * El número que va justo después de BUY/SELL es SIEMPRE el precio de entrada: no lo olvides.
  * SL/TP en precio -> sl / tps. Si vienen en pips -> sl_pips / tps_pips.
  * Varios TP (TP1, TP2, TP3...) -> tps en orden. "TP open" no es un número: ignóralo.
- Gestión de señales ya abiertas:
  * "SL a entrada", "BE", "breakeven", "risk free", "asegurar a 0" -> "breakeven".
  * "mover SL a 2345" -> "mover_sl" con nuevo_sl. Cambiar TP -> "mover_tp" con nuevos_tps.
  * Un mensaje que solo da el SL ("SL 4170", "SL: 4170") es el SL que faltaba de la última señal -> "mover_sl" con
    nuevo_sl (objetivo "respondido" si responde a la señal, si no "ultima").
  * "cerrar", "close now", "salir" -> "cerrar_todo". "cerrar la mitad / parciales / 50%" -> "cerrar_parcial" con porcentaje.
  * "TP1 hit ✅", "TP2 alcanzado" -> "tp_alcanzado" con tp_numero.
  * "cancelar la orden / delete limit" -> "cancelar_pendiente".
  * Un mismo mensaje puede tener varias acciones (p.ej. cerrar parcial + breakeven).
  * objetivo: "respondido" si el mensaje responde a una señal; "simbolo" si nombra un símbolo concreto; "todas" si dice todas; si no, "ultima".
  * Si en las señales abiertas reconoces claramente a cuál se refiere, pon su id en senal_ids.
- confianza: lo seguro que estás de la interpretación (baja si es ambiguo)."""


JSON_INSTRUCTIONS = """

Responde ÚNICAMENTE con un objeto JSON válido (sin texto antes ni después, sin ```), con esta forma:
{"acciones": [ {"tipo": "...", ...campos...} ], "confianza": 0.0-1.0, "motivo": "explicación breve"}
Campos posibles de cada acción (pon null o [] si no aplican):
tipo (abrir|mover_sl|mover_tp|breakeven|cerrar_todo|cerrar_parcial|cancelar_pendiente|tp_alcanzado),
simbolo, direccion (buy|sell), tipo_orden (market|limit|stop), entrada_min, entrada_max, sl, sl_pips,
tps [números], tps_pips [números], nuevo_sl, nuevos_tps [números], porcentaje, tp_numero,
objetivo (respondido|ultima|simbolo|todas), senal_ids [textos].
Los precios deben ser números, no textos.

Ejemplo 1: "GOLD BUY NOW 2350-2347 SL 2340 TP1 2355 TP2 2365"
{"acciones":[{"tipo":"abrir","simbolo":"GOLD","direccion":"buy","tipo_orden":"market","entrada_min":2347,"entrada_max":2350,"sl":2340,"tps":[2355,2365]}],"confianza":0.95,"motivo":"Señal de compra de oro"}
Ejemplo 1b: "XAUUSD SELL 4127 TP 4124 TP 4123 TP 4117 SL 4177"  (el número tras SELL/BUY es la ENTRADA)
{"acciones":[{"tipo":"abrir","simbolo":"XAUUSD","direccion":"sell","tipo_orden":"market","entrada_min":4127,"entrada_max":4127,"sl":4177,"tps":[4124,4123,4117]}],"confianza":0.95,"motivo":"Señal de venta de oro"}
Ejemplo 2: "TP1 ✅ mover SL a entrada"
{"acciones":[{"tipo":"tp_alcanzado","tp_numero":1,"objetivo":"ultima"},{"tipo":"breakeven","objetivo":"ultima"}],"confianza":0.9,"motivo":"TP1 alcanzado y BE"}
Ejemplo 3: "Buenos días traders, hoy hay NFP 🔥"
{"acciones":[],"confianza":0.95,"motivo":"No es una señal"}"""

PRESETS = {
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai/", "gemini-flash-lite-latest"),
    "groq": ("https://api.groq.com/openai/v1", "llama-3.3-70b-versatile"),
    "ollama": ("http://localhost:11434/v1", "qwen2.5:7b"),
    "openrouter": ("https://openrouter.ai/api/v1", "meta-llama/llama-3.3-70b-instruct:free"),
}

_NUM_FIELDS = ("entrada_min", "entrada_max", "sl", "sl_pips", "nuevo_sl", "porcentaje")
_LIST_FIELDS = ("tps", "tps_pips", "nuevos_tps")
_TIPOS = set(TOOL["input_schema"]["properties"]["acciones"]["items"]["properties"]["tipo"]["enum"])


def _num(v):
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    m = re.search(r"-?\d+(?:[.,]\d+)?", str(v))
    return float(m.group().replace(",", ".")) if m else None


def _clean(data):
    """Normaliza la respuesta (los modelos pequeños a veces ponen números como texto, etc.)."""
    if not isinstance(data, dict):
        return {"acciones": [], "confianza": 0, "motivo": "respuesta no válida"}
    out = []
    for a in data.get("acciones") or []:
        if not isinstance(a, dict) or a.get("tipo") not in _TIPOS:
            continue
        for f in _NUM_FIELDS:
            a[f] = _num(a.get(f))
        for f in _LIST_FIELDS:
            vals = a.get(f) or []
            vals = vals if isinstance(vals, list) else [vals]
            a[f] = [x for x in (_num(v) for v in vals) if x is not None]
        if a.get("direccion"):
            d = str(a["direccion"]).lower()
            a["direccion"] = "buy" if d in ("buy", "long", "compra", "comprar") else \
                             "sell" if d in ("sell", "short", "venta", "vender") else None
        if a.get("tipo_orden") not in ("market", "limit", "stop"):
            a["tipo_orden"] = None
        if a.get("tp_numero") is not None:
            a["tp_numero"] = int(_num(a["tp_numero"]) or 0) or None
        a["senal_ids"] = [str(x) for x in (a.get("senal_ids") or [])]
        out.append(a)
    return {"acciones": out, "confianza": _num(data.get("confianza")) or 0, "motivo": data.get("motivo", "")}


def _extract_json(text):
    text = (text or "").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            return json.loads(m.group())
        raise


class SignalParser:
    def __init__(self):
        self.provider = config.AI_PROVIDER
        if self.provider == "anthropic":
            from anthropic import AsyncAnthropic
            self.client = AsyncAnthropic(api_key=config.AI_API_KEY or None)
            self.model = config.AI_MODEL or "claude-haiku-4-5"
        else:
            from openai import AsyncOpenAI
            base, model = PRESETS.get(self.provider, (None, None))
            self.client = AsyncOpenAI(base_url=config.AI_BASE_URL or base, api_key=config.AI_API_KEY or "ollama",
                                      timeout=60, max_retries=1)
            self.model = config.AI_MODEL or model
        self._json_mode = True
        log.info("IA: proveedor=%s modelo=%s", self.provider, self.model)

    def _prompt(self, text, reply_text, open_signals):
        parts = [f'Mensaje nuevo:\n"""\n{text}\n"""']
        if reply_text:
            parts.append(f'Este mensaje responde a:\n"""\n{reply_text}\n"""')
        if open_signals:
            lines = [f"- id={s['id']}: {s['symbol']} {s['side']} entrada {s['entry']}" for s in open_signals]
            parts.append("Señales abiertas ahora mismo:\n" + "\n".join(lines))
        else:
            parts.append("No hay señales abiertas ahora mismo.")
        return "\n\n".join(parts)

    async def parse(self, text, reply_text=None, open_signals=None):
        user = self._prompt(text, reply_text, open_signals)
        if self.provider == "anthropic":
            res = _clean(await self._anthropic(user))
        else:
            res = _clean(await self._openai_compat(user))
        # Regla del grupo: si no pone LIMIT (o BUY/SELL STOP), es SIEMPRE ejecución por mercado
        low = text.lower()
        for a in res["acciones"]:
            if a["tipo"] == "abrir":
                if (a.get("tipo_orden") == "limit" and "limit" not in low) or \
                        (a.get("tipo_orden") == "stop" and not re.search(r"(buy|sell)\s*stop", low)):
                    a["tipo_orden"] = "market"
        return res

    async def _anthropic(self, user):
        resp = await self.client.messages.create(
            model=self.model, max_tokens=1024, system=SYSTEM, tools=[TOOL],
            tool_choice={"type": "tool", "name": "registrar_acciones"},
            messages=[{"role": "user", "content": user}],
        )
        for block in resp.content:
            if block.type == "tool_use":
                return block.input
        return {}

    async def _openai_compat(self, user):
        kwargs = dict(
            model=self.model, temperature=0,
            messages=[{"role": "system", "content": SYSTEM + JSON_INSTRUCTIONS},
                      {"role": "user", "content": user}],
        )
        for attempt in range(3):
            try:
                if self._json_mode:
                    kwargs["response_format"] = {"type": "json_object"}
                else:
                    kwargs.pop("response_format", None)
                resp = await self.client.chat.completions.create(**kwargs)
                return _extract_json(resp.choices[0].message.content)
            except json.JSONDecodeError:
                log.warning("La IA no devolvió JSON válido (intento %d)", attempt + 1)
            except Exception as e:  # noqa: BLE001
                msg = str(e).lower()
                if self._json_mode and ("response_format" in msg or "json" in msg):
                    log.info("El modelo no admite modo JSON; reintento sin él")
                    self._json_mode = False
                    continue
                if "429" in msg or "rate" in msg or "quota" in msg:
                    log.warning("Límite gratuito alcanzado, espero 20 s…")
                    import asyncio
                    await asyncio.sleep(20)
                    continue
                raise
        return {}
