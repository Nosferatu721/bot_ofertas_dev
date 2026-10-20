#!/usr/bin/env python3
"""Pruebas del vigilante. Solo libreria estandar: `python -m unittest pruebas_vigilante`.

El nucleo de estas pruebas es la regresion del fallo de LinkedIn: el endpoint de
invitado trae la fecha de publicacion en dos sitios y el atributo `datetime` solo
tiene el dia, asi que leer unicamente ese atributo hacia que todas las ofertas
del mismo dia quedaran con la misma antiguedad. El efecto era doble: el aviso de
Telegram mentia sobre la antiguedad y la seleccion de "las mas recientes" la
decidia la relevancia, de modo que una oferta recien publicada podia esperar
horas, o no llegar nunca.
"""

from __future__ import annotations

import http.server
import json
import tempfile
import threading
import unittest
import urllib.parse
from datetime import datetime, timedelta
from pathlib import Path

import ultima_oferta as uo

ZONA = "America/Bogota"


def setUpModule() -> None:
    uo.fijar_zona(ZONA)


def tarjeta(
    identificador: str,
    titulo: str,
    empresa: str = "Empresa S.A.S",
    *,
    fecha: str | None = "2026-10-15",
    texto: str | None = "Hace 1 hora",
) -> str:
    """Una tarjeta `<li>` como la que sirve el endpoint de invitado."""
    if fecha is None and texto is None:
        tiempo = ""
    else:
        atributo = f' datetime="{fecha}"' if fecha else ""
        tiempo = (
            f'<time class="job-search-card__listdate--new"{atributo}>'
            f" {texto or ''} </time>"
        )
    return f"""<li>
      <div class="base-card job-search-card" data-entity-urn="urn:li:jobPosting:{identificador}">
        <a class="base-card__full-link" href="https://co.linkedin.com/jobs/view/x-{identificador}?refId=abc">
          <span class="sr-only"> {titulo} </span>
        </a>
        <div class="base-search-card__info">
          <h4 class="base-search-card__subtitle">
            <a class="hidden-nested-link" href="https://www.linkedin.com/company/x"> {empresa} </a>
          </h4>
          <div class="base-search-card__metadata">
            <span class="job-search-card__location"> Bogota, Colombia </span>
            {tiempo}
          </div>
        </div>
      </div>
    </li>"""


class AntiguedadLinkedIn(unittest.TestCase):
    """De que dato sale la hora de publicacion de una tarjeta."""

    def test_el_texto_del_time_manda_sobre_la_fecha(self) -> None:
        # El fallo original: `datetime="2026-10-15"` leido como medianoche daba
        # la misma antiguedad a todo lo publicado hoy.
        html = tarjeta("4479885587", "Desarrollador Full Stack Senior", texto="Hace 1 hora")
        (oferta,) = uo.parsear_linkedin(html, 1)

        self.assertEqual(oferta.publicada, "Hace 1 hora")
        self.assertAlmostEqual(oferta.antiguedad_horas, 1.0, places=2)
        momento = datetime.fromisoformat(oferta.publicada_iso)
        self.assertEqual(momento.hour, (datetime.now(uo._zona) - timedelta(hours=1)).hour)
        self.assertNotEqual(momento.strftime("%H:%M"), "00:00")

    def test_minutos(self) -> None:
        html = tarjeta("1", "Oferta", texto="Hace 32 minutos")
        (oferta,) = uo.parsear_linkedin(html, 1)

        self.assertAlmostEqual(oferta.antiguedad_horas, 32 / 60, places=3)
        self.assertEqual(oferta.publicada, "Hace 32 minutos")

    def test_ofertas_del_mismo_dia_ya_no_empatan(self) -> None:
        html = "".join(
            (
                tarjeta("1", "Recien publicada", texto="Hace 10 minutos"),
                tarjeta("2", "De esta manana", texto="Hace 6 horas"),
                tarjeta("3", "De anoche", texto="Hace 14 horas"),
            )
        )
        horas = [o.antiguedad_horas for o in uo.parsear_linkedin(html, 1)]

        self.assertEqual(len(set(horas)), 3, "las tres comparten antiguedad")
        self.assertLess(horas[0], horas[1])
        self.assertLess(horas[1], horas[2])

    def test_sin_texto_cae_a_la_fecha(self) -> None:
        html = tarjeta("1", "Oferta", texto="")
        (oferta,) = uo.parsear_linkedin(html, 1)

        self.assertTrue(oferta.publicada_iso.startswith("2026-10-15T00:00:00"))

    def test_sin_time_no_inventa_fecha(self) -> None:
        html = tarjeta("1", "Oferta", fecha=None, texto=None)
        (oferta,) = uo.parsear_linkedin(html, 1)

        self.assertIsNone(oferta.publicada_iso)
        self.assertIsNone(oferta.antiguedad_horas)
        self.assertIsNone(oferta.publicada)

    def test_fecha_invalida_no_rompe_el_ciclo(self) -> None:
        html = tarjeta("1", "Oferta", fecha="no-es-una-fecha", texto="")
        (oferta,) = uo.parsear_linkedin(html, 1)

        self.assertIsNone(oferta.publicada_iso)


def oferta(identificador: str, horas: float | None, posicion: int, pagina: int = 1) -> uo.Oferta:
    return uo.Oferta(
        id=identificador,
        titulo=f"Oferta {identificador}",
        empresa="Empresa",
        url="https://ejemplo/x",
        ubicacion=None,
        publicada=None,
        antiguedad_horas=horas,
        pagina=pagina,
        posicion=posicion,
    )


class MasRecientes(unittest.TestCase):
    """`top` acota la antiguedad, no el numero de ofertas."""

    def test_un_empate_no_descarta_por_relevancia(self) -> None:
        # Asi se veia el listado de LinkedIn con el fallo: 30 ofertas con la
        # misma antiguedad calculada. Cortar en el puesto 10 dejaba fuera 20
        # por su posicion en el listado, no por su fecha.
        ofertas = [oferta(str(i), 13.0, i, pagina=1 + (i - 1) // 10) for i in range(1, 31)]

        seleccion = uo.mas_recientes(ofertas, 10, 24.0)

        self.assertEqual(len(seleccion), 30)
        self.assertIn("20", {o.id for o in seleccion})

    def test_elige_por_fecha_y_no_por_posicion(self) -> None:
        # La recien publicada va de ultima en relevancia; las viejas, primero.
        ofertas = [oferta(f"vieja-{i}", 20.0, i) for i in range(1, 20)]
        ofertas.append(oferta("recien-publicada", 1.0, 20, pagina=2))

        seleccion = uo.mas_recientes(ofertas, 10, 24.0)

        self.assertEqual(seleccion[0].id, "recien-publicada")

    def test_la_ventana_corta_el_listado_viejo(self) -> None:
        # El caso que obliga a poner techo al corte: solo dos ofertas frescas y
        # un listado de ofertas de hace mas de 30 dias detras. Sin techo, el
        # corte caeria en las viejas y entrarian todas.
        ofertas = [oferta("fresca-1", 0.5, 1), oferta("fresca-2", 3.0, 2)]
        ofertas += [oferta(f"vieja-{i}", 720.0, 2 + i) for i in range(1, 50)]

        seleccion = uo.mas_recientes(ofertas, 10, 24.0)

        self.assertEqual([o.id for o in seleccion], ["fresca-1", "fresca-2"])

    def test_ventana_en_cero_la_desactiva(self) -> None:
        ofertas = [oferta("fresca", 0.5, 1), oferta("vieja", 720.0, 2)]

        self.assertEqual(len(uo.mas_recientes(ofertas, 10, 0.0)), 2)

    def test_sin_fechas_legibles_mantiene_el_tope(self) -> None:
        # Si el parseo se rompe, mas vale quedarse corto que anunciar el
        # listado entero.
        ofertas = [oferta(str(i), None, i) for i in range(1, 31)]

        self.assertEqual(len(uo.mas_recientes(ofertas, 10, 24.0)), 10)

    def test_respeta_el_orden_de_mas_nueva_a_mas_vieja(self) -> None:
        ofertas = [oferta("vieja", 9.0, 1), oferta("nueva", 0.5, 2), oferta("media", 4.0, 3)]

        self.assertEqual(
            [o.id for o in uo.mas_recientes(ofertas, 3, 24.0)], ["nueva", "media", "vieja"]
        )


class Falso(http.server.BaseHTTPRequestHandler):
    """LinkedIn y Telegram de juguete, para recorrer un ciclo completo."""

    paginas: list[str] = []
    enviados: list[dict] = []

    def _json(self, cuerpo: dict) -> None:
        crudo = json.dumps(cuerpo).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(crudo)))
        self.end_headers()
        self.wfile.write(crudo)

    def do_GET(self) -> None:
        consulta = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(self.path).query))
        indice = int(consulta.get("start", "0")) // uo.LINKEDIN_POR_PAGINA
        cuerpo = (self.paginas[indice] if indice < len(self.paginas) else "").encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(cuerpo)))
        self.end_headers()
        self.wfile.write(cuerpo)

    def do_POST(self) -> None:
        cuerpo = json.loads(self.rfile.read(int(self.headers["Content-Length"] or 0)) or b"{}")
        if self.path.endswith("/getMe"):
            self._json({"ok": True, "result": {"username": "bot_de_prueba"}})
            return
        self.enviados.append(cuerpo)
        self._json({"ok": True, "result": {"message_id": len(self.enviados)}})

    def log_message(self, *_args) -> None:
        pass


class CicloCompleto(unittest.TestCase):
    """Del listado al mensaje de Telegram, sin salir de la maquina.

    Reproduce el escenario del fallo: el vigilante lleva horas corriendo (ya
    conoce el listado) y aparece una oferta publicada hace una hora que el
    buscador coloca en el puesto 20 por relevancia.
    """

    def setUp(self) -> None:
        Falso.enviados = []
        self.viejas = [
            tarjeta(f"100{i}", f"Oferta vieja {i}", texto="Hace 20 horas") for i in range(1, 20)
        ]
        self.reciente = tarjeta(
            "4479880725", "Software Engineer (Python+React)", "Globant", texto="Hace 1 hora"
        )
        self.servir(self.viejas)

        self.servidor = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Falso)
        threading.Thread(target=self.servidor.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{self.servidor.server_port}"
        self.linkedin_api, uo.LINKEDIN_API = uo.LINKEDIN_API, f"{base}/search"
        self.telegram_api, uo.TELEGRAM_API = uo.TELEGRAM_API, base

        self.temporal = tempfile.TemporaryDirectory()
        self.ruta = Path(self.temporal.name) / "ofertas.json"
        # Primer ciclo sin Telegram: deja el historial poblado como el que ya
        # tiene el contenedor en marcha, sin avisos de arranque.
        self.un_ciclo(telegram=None)

    def tearDown(self) -> None:
        uo.LINKEDIN_API = self.linkedin_api
        uo.TELEGRAM_API = self.telegram_api
        self.servidor.shutdown()
        self.servidor.server_close()
        self.temporal.cleanup()

    def servir(self, tarjetas: list[str]) -> None:
        """Reparte las tarjetas en paginas de 10, como el endpoint real."""
        Falso.paginas = [
            "".join(tarjetas[i : i + uo.LINKEDIN_POR_PAGINA])
            for i in range(0, len(tarjetas), uo.LINKEDIN_POR_PAGINA)
        ] + [""]

    def un_ciclo(self, telegram: tuple[str, str] | None = ("TOKEN", "CHAT")) -> dict:
        return uo.ciclo(
            ["https://www.linkedin.com/jobs/search-results/?keywords=Desarrollador&f_TPR=r86400"],
            self.ruta,
            top=10,
            orden="relevance",
            max_paginas=3,
            timeout=5.0,
            pausa=0.0,
            max_registros=500,
            max_antiguedad=24.0,
            telegram=telegram,
        )

    def test_avisa_de_la_reciente_aunque_el_listado_la_ponga_de_ultima(self) -> None:
        self.servir(self.viejas + [self.reciente])

        resumen = self.un_ciclo()

        self.assertEqual(resumen["errores"], 0)
        textos = [m["text"] for m in Falso.enviados]
        self.assertEqual(len(textos), 1, f"se esperaba un solo aviso: {textos}")
        self.assertIn("Software Engineer (Python+React)", textos[0])
        # La antiguedad del mensaje es la real, no la de medianoche.
        self.assertIn("🕒 Hace 1 hora", textos[0])
        self.assertNotIn("Hace 13 horas", textos[0])

    def test_el_json_guarda_la_hora_real_de_publicacion(self) -> None:
        self.servir(self.viejas + [self.reciente])

        self.un_ciclo()

        guardadas = json.loads(self.ruta.read_text(encoding="utf-8"))["ofertas"]
        reciente = next(o for o in guardadas if o["id"] == "li:4479880725")
        publicada = datetime.fromisoformat(reciente["publicada_aprox"])
        esperada = datetime.now(uo._zona) - timedelta(hours=1)
        self.assertLess(abs((publicada - esperada).total_seconds()), 120)
        # La mas reciente encabeza el archivo.
        self.assertEqual(guardadas[0]["id"], "li:4479880725")

    def test_no_repite_el_aviso_en_el_ciclo_siguiente(self) -> None:
        self.servir(self.viejas + [self.reciente])

        self.un_ciclo()
        resumen = self.un_ciclo()

        self.assertEqual(resumen["nuevas"], 0)
        self.assertEqual(len(Falso.enviados), 1)

    def test_sin_novedades_no_envia_nada(self) -> None:
        resumen = self.un_ciclo()

        self.assertEqual(resumen["nuevas"], 0)
        self.assertEqual(Falso.enviados, [])


if __name__ == "__main__":
    unittest.main()
