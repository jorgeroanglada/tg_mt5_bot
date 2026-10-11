"""Prueba la conexión con MetaTrader 5 y busca dónde está instalado.
Uso:  python probar_mt5.py
"""
import glob
import os

import MetaTrader5 as mt5

import config

print("\n=== Buscando MetaTrader 5 en el PC... ===")
rutas = []
if config.MT5_PATH:
    rutas.append(config.MT5_PATH.strip('"'))
for base in (os.environ.get("ProgramFiles", r"C:\Program Files"),
             os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
             os.path.join(os.environ.get("APPDATA", ""), "MetaQuotes"),
             os.environ.get("LOCALAPPDATA", "")):
    if base and os.path.isdir(base):
        rutas += glob.glob(os.path.join(base, "*", "terminal64.exe"))
        rutas += glob.glob(os.path.join(base, "*", "*", "terminal64.exe"))
rutas = list(dict.fromkeys(rutas))

if not rutas:
    print("No he encontrado terminal64.exe. Busca el acceso directo de MT5 > clic derecho > Propiedades > Destino.")
for r in rutas:
    print(f"  Encontrado: {r}  (existe: {os.path.exists(r)})")

print("\n=== Probando conexión (MT5 debe estar abierto y con sesión iniciada) ===")
intentos = [None] + [r for r in rutas if os.path.exists(r)]
for r in intentos:
    nombre = r or "(sin ruta, MT5 ya abierto)"
    ok = mt5.initialize(r, timeout=60000) if r else mt5.initialize(timeout=60000)
    if ok:
        acc = mt5.account_info()
        print(f"\n✅ CONECTADO con: {nombre}")
        if acc:
            print(f"   Cuenta {acc.login} | servidor {acc.server} | balance {acc.balance} {acc.currency}")
        term = mt5.terminal_info()
        if term and not term.trade_allowed:
            print("   ⚠️  'Algo Trading' está APAGADO en MT5: actívalo (botón verde).")
        for s in ("XAUUSD", "NDX100", "GOLD", "USTEC", "US100", "NAS100"):
            hits = [x.name for x in (mt5.symbols_get(f"*{s}*") or [])][:5]
            if hits:
                print(f"   Símbolos parecidos a {s}: {', '.join(hits)}")
        if r:
            print(f"\n👉 Pon en tu .env:  MT5_PATH={r}")
        mt5.shutdown()
        break
    print(f"❌ {nombre}: {mt5.last_error()}")
    mt5.shutdown()
else:
    print("\nNo se pudo conectar. Comprueba que MT5 está abierto, con sesión iniciada,"
          " y que no abriste uno de los dos programas 'como administrador'.")
