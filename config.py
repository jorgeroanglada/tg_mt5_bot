"""Carga la configuración desde el archivo .env"""
import os
from dotenv import load_dotenv

load_dotenv()


def _s(name, default=""):
    return os.getenv(name, default).strip()


def _b(name, default=False):
    return _s(name, str(default)).lower() in ("1", "true", "yes", "si", "sí")


def _f(name, default):
    v = _s(name)
    return float(v) if v else default


def _i(name, default):
    v = _s(name)
    return int(v) if v else default


def _list(name):
    return [x.strip() for x in _s(name).split(",") if x.strip()]


def _map(name, cast=str):
    """'GOLD=XAUUSD,ORO=XAUUSD' -> {'GOLD': 'XAUUSD', 'ORO': 'XAUUSD'}"""
    out = {}
    for pair in _list(name):
        if "=" in pair:
            k, v = pair.split("=", 1)
            out[k.strip().upper()] = cast(v.strip())
    return out


def _chat(x):
    try:
        return int(x)
    except ValueError:
        return x  # @usuario o enlace


# --- Telegram ---
TG_API_ID = _i("TG_API_ID", 0)
TG_API_HASH = _s("TG_API_HASH")
TG_PHONE = _s("TG_PHONE")
TG_CHATS = [_chat(c) for c in _list("TG_CHATS")]
TG_SESSION = _s("TG_SESSION", "sesion_telegram")
NOTIFY = _b("NOTIFY", True)  # avisos en tus "Mensajes guardados"

# --- IA (interpretación de mensajes) ---
# gemini (gratis) | groq (gratis) | ollama (gratis, en tu PC) | openrouter | anthropic
AI_PROVIDER = _s("AI_PROVIDER", "gemini").lower()
AI_API_KEY = _s("AI_API_KEY") or _s("ANTHROPIC_API_KEY")
AI_MODEL = _s("AI_MODEL")  # vacío = el recomendado para cada proveedor
AI_BASE_URL = _s("AI_BASE_URL")  # solo si usas otro proveedor compatible con OpenAI
MIN_CONFIDENCE = _f("MIN_CONFIDENCE", 0.7)

# --- MT5 ---
MT5_LOGIN = _s("MT5_LOGIN")
MT5_PASSWORD = _s("MT5_PASSWORD")
MT5_SERVER = _s("MT5_SERVER")
MT5_PATH = _s("MT5_PATH")  # opcional: ruta a terminal64.exe

# --- Riesgo y ejecución ---
DRY_RUN = _b("DRY_RUN", True)  # True = solo simula, no envía órdenes
RISK_PERCENT = _f("RISK_PERCENT", 0.5)  # % del balance arriesgado por señal
MAX_LOT = _f("MAX_LOT", 1.0)  # lote total máximo por señal (0 = sin límite)
# Lote fijo según el capital: p.ej. 0.01 = 0,01 lotes por cada 100 € de balance. 0 = usar RISK_PERCENT
LOT_PER_100 = _f("LOT_PER_100", 0)
MAX_OPEN_SIGNALS = _i("MAX_OPEN_SIGNALS", 3)
DAILY_LOSS_LIMIT_PERCENT = _f("DAILY_LOSS_LIMIT_PERCENT", 3.0)  # 0 = desactivado
# parciales = una sola operación que cierra una parte en cada TP y deja el resto al último
# dividir   = una posición por cada TP
# primero   = una sola operación con TP1
TP_MODE = _s("TP_MODE", "parciales").lower()
TP_MODE = TP_MODE if TP_MODE in ("parciales", "dividir", "primero") else "parciales"
PARTIAL_PERCENTS = [float(x) for x in _list("PARTIAL_PERCENTS")]  # p.ej. 50,30 (vacío = partes iguales)
MAX_TPS = _i("MAX_TPS", 3)
MOVE_BE_ON_TP1 = _b("MOVE_BE_ON_TP1", True)
BE_OFFSET_POINTS = _f("BE_OFFSET_POINTS", 0)
DEFAULT_SL_POINTS = _f("DEFAULT_SL_POINTS", 0)  # 0 = no operar señales sin SL
ENTRY_TOLERANCE_POINTS = _f("ENTRY_TOLERANCE_POINTS", 100)
MAX_SPREAD_POINTS = _f("MAX_SPREAD_POINTS", 0)  # 0 = sin filtro
DEVIATION_POINTS = _i("DEVIATION_POINTS", 50)
PENDING_EXPIRY_MINUTES = _i("PENDING_EXPIRY_MINUTES", 240)  # 0 = no caducan
MAX_MESSAGE_AGE_SEC = _i("MAX_MESSAGE_AGE_SEC", 120)
MAGIC = _i("MAGIC", 770077)

# --- Símbolos ---
SYMBOL_MAP = _map("SYMBOL_MAP")
SYMBOL_SUFFIX = _s("SYMBOL_SUFFIX")
ALLOWED_SYMBOLS = [s.upper() for s in _list("ALLOWED_SYMBOLS")]
PIP_SIZES = _map("PIP_SIZES", float)  # para señales con SL/TP en pips

STATE_FILE = _s("STATE_FILE", "estado.json")

# --- Informe semanal (a tus "Mensajes guardados") ---
REPORT_WEEKDAY = _i("REPORT_WEEKDAY", 6)  # 1=lunes ... 6=sábado, 7=domingo, 0 = desactivado
REPORT_HOUR = _i("REPORT_HOUR", 10)  # hora del PC
REPORT_DAYS = _i("REPORT_DAYS", 7)  # días que abarca
