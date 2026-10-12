"""Pruebas automáticas del bot (no necesitan MT5 ni Telegram ni IA).
Uso:  python pruebas.py
"""
import asyncio
import os
import tempfile
import unittest

import parser_ai
from parser_ai import parse_simple


class LectorSinIA(unittest.TestCase):
    def test_formato_del_grupo(self):
        a = parse_simple("XAUUSD SELL 4177\n\nTP 4172\n\nSL 4200")["acciones"][0]
        self.assertEqual((a["simbolo"], a["direccion"], a["tipo_orden"]), ("XAUUSD", "sell", "market"))
        self.assertEqual((a["entrada_min"], a["sl"], a["tps"]), (4177, 4200, [4172]))

    def test_varios_tp_en_una_linea(self):
        a = parse_simple("XAUUSD SELL 4127 TP 4124 TP 4123 TP 4117 SL 4177")["acciones"][0]
        self.assertEqual(a["tps"], [4124, 4123, 4117])

    def test_zona_now_y_tp_numerados(self):
        a = parse_simple("GOLD BUY NOW 2350-2347 SL 2340 TP1 2355 TP2 2365")["acciones"][0]
        self.assertEqual((a["entrada_min"], a["entrada_max"], a["tps"]), (2347, 2350, [2355, 2365]))

    def test_limit_y_emojis(self):
        a = parse_simple("🔥 XAUUSD BUY LIMIT 4180 🔥\nTP: 4190\nSL: 4170 ✅")["acciones"][0]
        self.assertEqual((a["tipo_orden"], a["entrada_min"], a["sl"]), ("limit", 4180, 4170))

    def test_sin_sl(self):
        a = parse_simple("XAUUSD BUY 4180 TP 4190")["acciones"][0]
        self.assertIsNone(a["sl"])

    def test_lo_que_no_es_formato_va_a_la_ia(self):
        for t in ["TP +40PIPS✅", "Mover el sl a be", "Cerrar a be", "Be",
                  "Estamos en una posible zona de reversión alcista (long).",
                  "XAUUSD SELL 4177 TP 4172 SL 4200 cerrar la mitad en TP1",
                  "SL 4170", "XAUUSD SELL ahora"]:
            self.assertIsNone(parse_simple(t), t)


class FiltroRespuestaIA(unittest.TestCase):
    def _parse(self, texto, respuesta):
        p = parser_ai.SignalParser.__new__(parser_ai.SignalParser)
        p.provider = "gemini"

        async def falsa(_):
            return respuesta
        p._openai_compat = falsa
        p._prompt = lambda *a: ""
        return asyncio.run(p.parse(texto))

    def test_senal_nueva_sin_acciones_extra(self):
        r = self._parse("Vendemos oro ya a 4172, objetivo 4168, stop 4200",
                        {"acciones": [{"tipo": "abrir", "direccion": "sell"}, {"tipo": "cerrar_todo"},
                                      {"tipo": "mover_sl"}], "confianza": 0.9})
        self.assertEqual([a["tipo"] for a in r["acciones"]], ["abrir"])

    def test_cerrar_a_be(self):
        r = self._parse("Cerrar a be", {"acciones": [{"tipo": "breakeven"}], "confianza": 0.9})
        self.assertEqual(r["acciones"][0]["tipo"], "cerrar_be")

    def test_ia_caida_avisa(self):
        p = parser_ai.SignalParser.__new__(parser_ai.SignalParser)
        p.provider = "gemini"

        async def caida(_):
            raise RuntimeError("la IA no ha respondido")
        p._openai_compat = caida
        p._prompt = lambda *a: ""
        with self.assertRaises(RuntimeError):
            asyncio.run(p.parse("Cerrar"))


class AQueSenalVa(unittest.TestCase):
    def setUp(self):
        import main
        from store import Store
        self.tmp = tempfile.mkdtemp()
        self.bot = main.Bot.__new__(main.Bot)
        self.bot.store = Store(os.path.join(self.tmp, "estado.json"))
        s = self.bot.store
        for i, mid in enumerate((100, 101, 102)):
            s.add(f"-1:{mid}", -1, "XAUUSD", "sell", 4170 + i, 4200, [mid], [4160])

    def ids(self, a, reply):
        r = self.bot.targets(-1, a, reply)
        return r if r is None else [x["id"] for x in r]

    def test_la_respuesta_manda_sobre_la_ia(self):
        a = {"tipo": "cerrar_todo", "objetivo": "respondido", "senal_ids": ["-1:100", "-1:101", "-1:102"]}
        self.assertEqual(self.ids(a, 101), ["-1:101"])

    def test_respuesta_a_senal_desconocida_no_toca_nada(self):
        self.assertIsNone(self.ids({"tipo": "cerrar_todo", "objetivo": "respondido"}, 999))

    def test_respuesta_a_mensaje_de_gestion(self):
        self.bot.store.add_ref("-1:200", ["-1:100"])  # "Mover el sl a be" sobre la señal 100
        self.assertEqual(self.ids({"tipo": "breakeven", "objetivo": "respondido"}, 200), ["-1:100"])

    def test_respuesta_a_senal_cerrada(self):
        self.bot.store.update("-1:100", closed=True)
        self.assertEqual(self.ids({"tipo": "cerrar_todo", "objetivo": "respondido"}, 100), [])

    def test_sin_respuesta_va_a_la_ultima(self):
        self.assertEqual(self.ids({"tipo": "cerrar_todo"}, None), ["-1:102"])

    def test_todas(self):
        self.assertEqual(len(self.ids({"tipo": "cerrar_todo", "objetivo": "todas"}, 101)), 3)


class Comentarios(unittest.TestCase):
    def test_grupo_y_manual_no_se_mezclan(self):
        from executor import Executor
        self.assertEqual(Executor._comment("-1002329472075:11431"), "tg11431")
        self.assertEqual(Executor._comment("123456:11431"), "tg11431m")


if __name__ == "__main__":
    unittest.main(verbosity=2)
