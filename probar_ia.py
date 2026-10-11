"""Diagnóstico rápido de la clave de IA (Gemini/Groq). Uso:  python probar_ia.py"""
import json
import time
import urllib.error
import urllib.request

import config

KEY = config.AI_API_KEY
print(f"Proveedor: {config.AI_PROVIDER} | clave empieza por: {KEY[:6]}... ({len(KEY)} caracteres)")


def llamar(nombre, url, data=None, headers=None):
    print(f"\n--- {nombre}")
    req = urllib.request.Request(url, data=json.dumps(data).encode() if data else None,
                                 headers={"Content-Type": "application/json", **(headers or {})})
    t = time.time()
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            body = r.read().decode()
            print(f"✅ OK ({r.status}) en {time.time() - t:.1f}s")
            return json.loads(body)
    except urllib.error.HTTPError as e:
        print(f"❌ Error {e.code}: {e.read().decode()[:600]}")
    except Exception as e:  # noqa: BLE001
        print(f"❌ Sin respuesta en {time.time() - t:.1f}s: {e}")
    return None


if config.AI_PROVIDER == "gemini":
    base = "https://generativelanguage.googleapis.com/v1beta"
    modelos = llamar("1) ¿La clave es válida? (lista de modelos)", f"{base}/models?key={KEY}")
    if modelos:
        flash = [m["name"].split("/")[-1] for m in modelos.get("models", []) if "flash" in m["name"]]
        print("Modelos flash disponibles:", ", ".join(flash[:12]))
    candidatos = [config.AI_MODEL] if config.AI_MODEL else []
    candidatos += ["gemini-3.1-flash-lite", "gemini-2.5-flash-lite", "gemini-flash-lite-latest", "gemini-2.5-flash"]
    buenos = []
    for modelo in dict.fromkeys(candidatos):
        t = time.time()
        r = llamar(f"2) Pregunta de prueba al modelo {modelo}",
                   f"{base}/models/{modelo}:generateContent?key={KEY}",
                   {"contents": [{"parts": [{"text": "Responde solo: hola"}]}]})
        if r:
            try:
                print("Respuesta:", r["candidates"][0]["content"]["parts"][0]["text"].strip())
                buenos.append((time.time() - t, modelo))
            except (KeyError, IndexError):
                print("Respuesta rara:", str(r)[:300])
    if buenos:
        mejor = min(buenos)[1]
        print(f"\n👉 Funciona. Pon en tu .env:   AI_MODEL={mejor}")
    else:
        print("\nNingún modelo de Gemini responde ahora. Prueba más tarde o usa Groq (AI_PROVIDER=groq).")
elif config.AI_PROVIDER == "groq":
    r = llamar("Pregunta de prueba a Groq", "https://api.groq.com/openai/v1/chat/completions",
               {"model": config.AI_MODEL or "llama-3.3-70b-versatile",
                "messages": [{"role": "user", "content": "Responde solo: hola"}]},
               {"Authorization": f"Bearer {KEY}"})
    if r:
        print("Respuesta:", r["choices"][0]["message"]["content"])
else:
    print("Este diagnóstico es para gemini o groq.")
