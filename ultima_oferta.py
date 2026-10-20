#!/usr/bin/env python3
"""Vigila las ultimas ofertas publicadas en Computrabajo Colombia.

Cada `--intervalo` segundos revisa uno o varios listados, se queda con las
`--top` ofertas publicadas mas recientemente de cada uno y acumula titulo y
empresa en un JSON compartido, sin repetir las que ya estaban.

Computrabajo no expone un orden real por fecha (el parametro `by` solo acepta
`relevance`), asi que la antiguedad se interpreta a partir del texto de cada
tarjeta ("Hace 3 horas", "Ayer", "Mas de 30 dias") y el ordenamiento por fecha
se resuelve aqui. Solo usa la libreria estandar de Python.
"""

from __future__ import annotations

import argparse
import http.client
import http.server
import json
import os
import re
import signal
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo
from html import unescape
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

BASE_URL = "https://co.computrabajo.com"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

RE_ARTICULO = re.compile(r'<article class="box_offer.*?</article>', re.S)
RE_ID = re.compile(r"data-id='([^']+)'")
RE_TITULO = re.compile(r'<h2[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.S)
RE_EMPRESA = re.compile(
    r'<a[^>]*href="([^"]+)"[^>]*offer-grid-article-company-url>(.*?)</a>', re.S
)
RE_UBICACION = re.compile(
    r'<p class="fs16 fc_base mt5">\s*<span class="mr10">(.*?)</span>', re.S
)
RE_PUBLICADA = re.compile(r'<p class="fs13 fc_aux mt15">(.*?)</p>', re.S)
RE_TOTAL = re.compile(r'<h1[^>]*>\s*<span class="fwB">\s*([\d.]+)\s*</span>', re.S)
RE_NUMERO = re.compile(r"(\d+)")

HORAS_POR_UNIDAD: tuple[tuple[str, float], ...] = (
    ("minuto", 1 / 60),
    ("hora", 1.0),
    ("dia", 24.0),
    ("semana", 24.0 * 7),
    ("mes", 24.0 * 30),
    ("ano", 24.0 * 365),
)

# LinkedIn: endpoint publico de invitado, el mismo que sirve la web sin sesion.
# La pagina /jobs/search-results/ es la aplicacion y exige login (responde 307).
LINKEDIN_API = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
LINKEDIN_POR_PAGINA = 10

RE_LI_TARJETA = re.compile(r"<li>.*?</li>", re.S)
RE_LI_ID = re.compile(r'data-entity-urn="urn:li:jobPosting:(\d+)"')
RE_LI_URL = re.compile(r'base-card__full-link[^>]*href="([^"]+)"')
RE_LI_TITULO = re.compile(r'<span class="sr-only">\s*(.*?)\s*</span>', re.S)
RE_LI_EMPRESA = re.compile(r"hidden-nested-link[^>]*>\s*(.*?)\s*</a>", re.S)
RE_LI_UBICACION = re.compile(r'job-search-card__location">\s*(.*?)\s*</span>', re.S)
RE_LI_FECHA = re.compile(r'datetime="([^"]+)"')
# El atributo `datetime` solo trae la fecha; el texto del mismo <time>
# ("Hace 32 minutos", "Hace 1 hora") es el unico dato con resolucion
# intradia que publica el endpoint de invitado.
RE_LI_FECHA_TEXTO = re.compile(r"<time[^>]*>\s*(.*?)\s*</time>", re.S)

ZONA_POR_DEFECTO = "America/Bogota"

# Todo lo que puede fallar al hablar con Telegram. Incluye OSError porque una
# conexion cortada llega como ConnectionResetError, y dejarla escapar la
# confundiria con un fallo de escritura en disco.
ERRORES_TELEGRAM = (OSError, RuntimeError, ValueError, http.client.HTTPException)

# El host de la API se puede redirigir (pruebas, proxy) sin tocar el codigo.
TELEGRAM_API = os.environ.get("TELEGRAM_API", "https://api.telegram.org")

# Huso en el que se escriben todas las marcas de tiempo. Fijarlo aqui (y no
# depender de la variable TZ del contenedor) es lo que garantiza que el JSON no
# mezcle offsets aunque se lance con `docker run` sin `-e TZ`.
_zona: tzinfo | None = None

# Señal de parada, activada por SIGTERM/SIGINT (`docker stop`, Ctrl+C).
_parar = threading.Event()


@dataclass(slots=True)
class Oferta:
    """Una oferta de empleo tal y como aparece en el listado."""

    id: str
    titulo: str
    empresa: str | None
    url: str
    ubicacion: str | None
    publicada: str | None
    antiguedad_horas: float | None
    pagina: int
    posicion: int
    fuente: str = "computrabajo"
    # Fecha exacta cuando la fuente la publica (LinkedIn). Tiene prioridad
    # sobre la estimacion a partir del texto relativo.
    publicada_iso: str | None = None

    def a_registro(
        self, detectada: str, busqueda: str, notificable: bool = False
    ) -> dict[str, Any]:
        """Representacion que se guarda en el JSON.

        Nace con `estado: "nueva"`; el ciclo siguiente la pasa a "vista", de
        modo que la marca senala de verdad un hallazgo reciente y no un rango
        de horas.

        `notificable` agrega la marca `notificada: false`, que es lo que hace
        que la oferta entre en la cola de Telegram. Los registros guardados
        sin notificaciones activas no llevan la marca, asi que al activarlas
        despues no se dispara un aluvion con todo el historico.
        """
        registro: dict[str, Any] = {
            "titulo": self.titulo,
            "empresa": self.empresa,
            "id": self.id,
            "publicada": self.publicada,
            "url": self.url,
            "publicada_aprox": (
                a_zona(self.publicada_iso)
                if self.publicada_iso
                else estimar_publicacion(detectada, self.antiguedad_horas)
            ),
            "fuente": self.fuente,
            "busqueda": busqueda,
            "estado": "nueva",
            "detectada": detectada,
        }
        if notificable:
            registro["notificada"] = False
        return registro


def fijar_zona(nombre: str) -> str:
    """Fija el huso de todas las marcas. Devuelve el nombre realmente aplicado."""
    global _zona
    try:
        _zona = ZoneInfo(nombre)
        return nombre
    except (ZoneInfoNotFoundError, ValueError):
        _zona = datetime.now().astimezone().tzinfo
        registrar(f"AVISO: zona '{nombre}' desconocida; se usa la del sistema.")
        return str(_zona)


def ahora() -> str:
    return datetime.now(_zona).astimezone(_zona).isoformat(timespec="seconds")


def registrar(mensaje: str) -> None:
    print(f"[{ahora()}] {mensaje}", flush=True)


def limpiar(texto: str | None) -> str | None:
    """Quita etiquetas HTML, decodifica entidades y normaliza espacios."""
    if texto is None:
        return None
    plano = unescape(re.sub(r"<[^>]+>", " ", texto))
    plano = re.sub(r"\s+", " ", plano).strip()
    return plano or None


def sin_tildes(texto: str) -> str:
    normalizado = unicodedata.normalize("NFKD", texto.lower())
    return normalizado.encode("ascii", "ignore").decode("ascii")


def antiguedad_horas(publicada: str | None) -> float | None:
    """Traduce 'Hace 3 horas', 'Ayer' o 'Mas de 30 dias' a horas aproximadas.

    Devuelve None cuando el texto no se puede interpretar, para no confundir un
    dato ausente con una publicacion reciente.
    """
    if not publicada:
        return None
    texto = sin_tildes(publicada)
    if "hoy" in texto or "ahora" in texto or "reciente" in texto:
        return 0.0
    if "ayer" in texto:
        return 24.0
    numero = RE_NUMERO.search(texto)
    cantidad = int(numero.group(1)) if numero else 1
    for unidad, horas in HORAS_POR_UNIDAD:
        if unidad in texto:
            return cantidad * horas
    return None


def texto_antiguedad(horas: float) -> str:
    """De horas a texto, con el mismo vocabulario que usa Computrabajo.

    A partir de 30 dias devuelve "Mas de 30 dias" en vez de un numero exacto:
    el sitio nunca dio mas precision que esa, e imprimir "Hace 47 dias" seria
    inventarsela.
    """
    if horas < 1:
        minutos = max(int(horas * 60), 1)
        return f"Hace {minutos} minuto{'s' if minutos != 1 else ''}"
    if horas < 24:
        enteras = int(horas)
        return f"Hace {enteras} hora{'s' if enteras != 1 else ''}"
    dias = int(horas // 24)
    if dias == 1:
        return "Ayer"
    if dias < 30:
        return f"Hace {dias} días"
    return "Más de 30 días"


def antiguedad_actual(publicada_aprox: str | None) -> str | None:
    """Antiguedad de la oferta *ahora*, calculada desde su fecha estimada."""
    momento = instante(publicada_aprox)
    if momento is None:
        return None
    horas = (datetime.now(_zona) - momento).total_seconds() / 3600
    return texto_antiguedad(max(horas, 0.0))


def estimar_publicacion(detectada: str, horas: float | None) -> str | None:
    """Fecha absoluta aproximada de publicacion: el momento de deteccion menos
    la antiguedad que declaraba la tarjeta.

    El listado solo da antiguedad relativa ("Hace 4 horas", "Ayer"), y ese texto
    caduca en cuanto se guarda. Fijarlo contra el reloj al detectarlo es lo que
    permite ordenar el archivo de forma estable entre ciclos.
    """
    if horas is None:
        return None
    try:
        momento = datetime.fromisoformat(detectada)
    except ValueError:
        return None
    return (momento - timedelta(hours=horas)).astimezone(_zona).isoformat(timespec="seconds")


def instante(valor: str | None) -> datetime | None:
    """Convierte una marca ISO en datetime con huso, o None si no es valida."""
    if not valor:
        return None
    try:
        momento = datetime.fromisoformat(valor)
    except ValueError:
        return None
    return momento if momento.tzinfo else momento.astimezone()


def a_zona(valor: str | None) -> str | None:
    """Reexpresa una marca ISO en el huso configurado, sin mover el instante.

    Un archivo escrito antes de fijar la zona puede traer marcas en `+00:00`;
    convertirlas al cargar evita que el JSON mezcle offsets y se lea mal.
    """
    momento = instante(valor)
    return momento.astimezone(_zona).isoformat(timespec="seconds") if momento else None


def ordenar_por_publicacion(registros: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """De publicacion mas reciente a mas antigua; las de fecha desconocida, al final.

    Compara instantes y no cadenas: el archivo puede mezclar husos (`+00:00` y
    `-05:00`), y ordenar el texto ISO daria un resultado incorrecto entre ellos.
    """
    fechadas: list[tuple[datetime, datetime, dict[str, Any]]] = []
    sin_fecha: list[dict[str, Any]] = []
    for registro in registros:
        publicada = instante(registro.get("publicada_aprox"))
        if publicada is None:
            sin_fecha.append(registro)
        else:
            fechadas.append((publicada, instante(registro.get("detectada")) or publicada, registro))
    fechadas.sort(key=lambda t: (t[0], t[1]), reverse=True)
    return [registro for _, _, registro in fechadas] + sin_fecha


def slug(busqueda: str) -> str:
    """Convierte 'Desarrollador Full Stack' en 'desarrollador-full-stack'."""
    return re.sub(r"[^a-z0-9]+", "-", sin_tildes(busqueda.strip())).strip("-")


def clave_secundaria(titulo: str | None, empresa: str | None) -> str | None:
    """Clave de respaldo por titulo+empresa, insensible a tildes y puntuacion.

    Protege contra una rotacion de los ids del sitio, que haria pasar por nuevas
    ofertas ya guardadas. Devuelve None para ofertas anonimas: sin empresa, dos
    vacantes distintas con el mismo titulo colisionarian, asi que esas se
    comparan unicamente por id.
    """
    if not titulo or not empresa:
        return None
    return f"{normalizar(titulo)}|{normalizar(empresa)}"


def normalizar(texto: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", sin_tildes(texto)).strip()


def detectar_fuente(busqueda: str) -> tuple[str, str, dict[str, str]]:
    """Decide la fuente de una busqueda y le da una etiqueta legible.

    Un texto suelto es una busqueda de Computrabajo (comportamiento historico).
    Una URL se reconoce por su dominio, de modo que basta con pegar el enlace
    del buscador en `compose.yaml`.

    Devuelve (fuente, etiqueta, parametros).
    """
    texto = busqueda.strip()
    if not texto.lower().startswith("http"):
        return "computrabajo", texto, {"consulta": texto}

    partes = urllib.parse.urlsplit(texto)
    consulta = dict(urllib.parse.parse_qsl(partes.query))
    if "linkedin.com" in partes.netloc:
        # `f_TPR` (antiguedad), `geoId` (zona) y demas filtros se respetan tal
        # como vengan en el enlace; aqui solo se fija el termino y la pagina.
        parametros = {k: v for k, v in consulta.items() if k not in ("start", "currentJobId")}
        palabras = parametros.get("keywords", "").strip()
        return "linkedin", f"linkedin: {palabras or 'sin termino'}", parametros
    if "computrabajo.com" in partes.netloc:
        ruta = partes.path.rsplit("/trabajo-de-", 1)[-1]
        return "computrabajo", ruta.replace("-", " "), {"consulta": ruta}
    raise ValueError(f"fuente no reconocida en la URL: {texto}")


def etiquetar(busqueda: str) -> str:
    """Nombre corto y legible de una busqueda, aunque venga como URL."""
    try:
        return detectar_fuente(busqueda)[1]
    except ValueError:
        return busqueda


def url_listado(fuente: str, parametros: dict[str, str], pagina: int, orden: str | None) -> str:
    """Arma la URL de una pagina del listado segun la fuente."""
    if fuente == "linkedin":
        consulta = dict(parametros)
        consulta["start"] = str((pagina - 1) * LINKEDIN_POR_PAGINA)
        return f"{LINKEDIN_API}?{urllib.parse.urlencode(consulta)}"

    extra = []
    if orden:
        extra.append(f"by={orden}")
    if pagina > 1:
        extra.append(f"p={pagina}")
    ruta = f"{BASE_URL}/trabajo-de-{slug(parametros['consulta'])}"
    return f"{ruta}?{'&'.join(extra)}" if extra else ruta


def descargar(url: str, timeout: float) -> str:
    peticion = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "es-CO,es;q=0.9",
        },
    )
    with urllib.request.urlopen(peticion, timeout=timeout) as respuesta:
        crudo = respuesta.read()
        codificacion = respuesta.headers.get_content_charset() or "utf-8"
    return crudo.decode(codificacion, errors="replace")


def _grupo(patron: re.Pattern[str], html: str, indice: int = 1) -> str | None:
    coincidencia = patron.search(html)
    return limpiar(coincidencia.group(indice)) if coincidencia else None


def antiguedad_linkedin(bloque: str) -> tuple[str | None, float | None]:
    """Antiguedad de una tarjeta de LinkedIn: (fecha ISO estimada, horas).

    La tarjeta trae dos datos de tiempo y hay que quedarse con el bueno:

    - el atributo `datetime="2026-10-15"`, que es **solo la fecha** (y ademas
      en UTC, asi que de madrugada senala el dia siguiente al local);
    - el texto del mismo `<time>` ("Hace 32 minutos", "Hace 1 hora"), que si
      distingue las horas del dia.

    Tomar la fecha como medianoche hacia que todas las ofertas publicadas hoy
    compartieran antiguedad: una recien publicada se anunciaba como "Hace 13
    horas" y, peor, quedaba empatada con las demas, de modo que `mas_recientes`
    las desempataba por relevancia y las nuevas se detectaban tarde o nunca.
    Por eso el texto manda y la fecha solo se usa cuando falta.
    """
    texto = _grupo(RE_LI_FECHA_TEXTO, bloque)
    horas = antiguedad_horas(texto)
    if horas is not None:
        momento = datetime.now(_zona) - timedelta(hours=horas)
        return momento.isoformat(timespec="seconds"), horas

    fecha = RE_LI_FECHA.search(bloque)
    if fecha is None:
        return None, None
    try:
        momento = datetime.fromisoformat(fecha.group(1))
    except ValueError:
        return None, None
    if momento.tzinfo is None:
        momento = momento.replace(tzinfo=_zona)
    horas = max((datetime.now(_zona) - momento).total_seconds() / 3600, 0.0)
    return momento.isoformat(timespec="seconds"), horas


def parsear_linkedin(html: str, pagina: int) -> list[Oferta]:
    """Extrae las tarjetas del endpoint de invitado de LinkedIn."""
    ofertas: list[Oferta] = []
    for posicion, bloque in enumerate(RE_LI_TARJETA.findall(html), start=1):
        identificador = RE_LI_ID.search(bloque)
        titulo = RE_LI_TITULO.search(bloque)
        if identificador is None or titulo is None:
            continue
        enlace = RE_LI_URL.search(bloque)
        publicada_iso, horas = antiguedad_linkedin(bloque)
        ofertas.append(
            Oferta(
                id=f"li:{identificador.group(1)}",
                titulo=limpiar(titulo.group(1)) or "(sin titulo)",
                empresa=_grupo(RE_LI_EMPRESA, bloque),
                # Se quitan los parametros de seguimiento del enlace.
                url=(enlace.group(1).split("?", 1)[0] if enlace else ""),
                ubicacion=_grupo(RE_LI_UBICACION, bloque),
                publicada=texto_antiguedad(horas) if horas is not None else None,
                antiguedad_horas=horas,
                pagina=pagina,
                posicion=posicion,
                fuente="linkedin",
                publicada_iso=publicada_iso,
            )
        )
    return ofertas


def parsear_ofertas(fuente: str, html: str, pagina: int) -> list[Oferta]:
    """Extrae todas las tarjetas de oferta de una pagina del listado."""
    if fuente == "linkedin":
        return parsear_linkedin(html, pagina)
    ofertas: list[Oferta] = []
    for posicion, bloque in enumerate(RE_ARTICULO.findall(html), start=1):
        titulo = RE_TITULO.search(bloque)
        if titulo is None:
            continue
        empresa = RE_EMPRESA.search(bloque)
        identificador = RE_ID.search(bloque)
        publicada = _grupo(RE_PUBLICADA, bloque)
        ofertas.append(
            Oferta(
                id=identificador.group(1) if identificador else "",
                titulo=limpiar(titulo.group(2)) or "(sin titulo)",
                empresa=limpiar(empresa.group(2)) if empresa else None,
                url=BASE_URL + titulo.group(1).split("#", 1)[0],
                ubicacion=_grupo(RE_UBICACION, bloque),
                publicada=publicada,
                antiguedad_horas=antiguedad_horas(publicada),
                pagina=pagina,
                posicion=posicion,
            )
        )
    return ofertas


def recorrer(
    fuente: str,
    parametros: dict[str, str],
    *,
    orden: str | None = "relevance",
    max_paginas: int = 3,
    timeout: float = 20.0,
    pausa: float = 1.0,
) -> tuple[list[Oferta], int | None]:
    """Devuelve las ofertas encontradas y el total anunciado por el sitio.

    LinkedIn no publica un total en el endpoint de invitado, asi que ahi se
    devuelve None en vez de inventar una cifra.
    """
    todas: list[Oferta] = []
    total: int | None = None
    vistos: set[str] = set()

    for numero in range(1, max_paginas + 1):
        if _parar.is_set():
            break
        html = descargar(url_listado(fuente, parametros, numero, orden), timeout)
        if total is None and fuente == "computrabajo":
            encontrado = RE_TOTAL.search(html)
            if encontrado:
                total = int(encontrado.group(1).replace(".", ""))

        ofertas = parsear_ofertas(fuente, html, numero)
        if not ofertas:
            break
        # El sitio repite la ultima pagina cuando `p` se sale de rango.
        nuevas = [o for o in ofertas if o.id not in vistos]
        if not nuevas:
            break
        vistos.update(o.id for o in nuevas)
        todas.extend(nuevas)

        if numero < max_paginas and pausa:
            _parar.wait(pausa)

    return todas, total


def mas_recientes(
    ofertas: list[Oferta], top: int, max_antiguedad: float = 0.0
) -> list[Oferta]:
    """Las ofertas publicadas mas recientemente de un listado.

    Ordena por antiguedad estimada y desempata con el orden del listado, que es
    el de relevancia. Las de antiguedad desconocida quedan al final.

    `top` elige **la antiguedad de corte**, no cuantas ofertas caben: fijado el
    corte entran todas las que lo igualan. Cortar en el puesto `top` exacto
    dejaba los empates en manos de la relevancia, y una oferta recien publicada
    que el listado pusiera en el puesto 20 no se revisaba hasta que el listado
    la moviera sola, o nunca.

    `max_antiguedad` (en horas, 0 lo desactiva) es el techo de ese corte. Sin
    el, un listado con pocas ofertas frescas pondria el corte en las antiguas
    —"Mas de 30 dias"— y el empate arrastraria el listado entero.
    """
    ordenadas = sorted(
        ofertas,
        key=lambda o: (
            o.antiguedad_horas if o.antiguedad_horas is not None else float("inf"),
            o.pagina,
            o.posicion,
        ),
    )
    cabeza = ordenadas[: max(top, 1)]
    corte = cabeza[-1].antiguedad_horas if cabeza else None
    if corte is None:
        # Ninguna fecha legible hasta el corte: probablemente el listado cambio
        # de formato. Se mantiene el tope para no inundar el canal con un
        # listado entero por un parseo roto.
        return cabeza
    if max_antiguedad > 0:
        corte = min(corte, max_antiguedad)
    return [
        o for o in ordenadas if o.antiguedad_horas is not None and o.antiguedad_horas <= corte
    ]


def cargar_historial(ruta: Path) -> dict[str, Any]:
    """Lee el JSON acumulado; devuelve una estructura vacia si no existe."""
    vacio: dict[str, Any] = {
        "busquedas": [],
        "actualizado": None,
        "ciclos": 0,
        "ofertas": [],
    }
    if not ruta.exists():
        return vacio
    try:
        datos = json.loads(ruta.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as error:
        registrar(f"AVISO: no se pudo leer {ruta} ({error}); se empieza de cero.")
        return vacio
    if not isinstance(datos, dict) or not isinstance(datos.get("ofertas"), list):
        registrar(f"AVISO: {ruta} tiene un formato inesperado; se empieza de cero.")
        return vacio
    historial = vacio | datos

    validos: list[dict[str, Any]] = []
    descartados = 0
    for registro in historial["ofertas"]:
        # Sin `id` no se puede deduplicar: es basura, no un registro.
        if not isinstance(registro, dict) or not registro.get("id"):
            descartados += 1
            continue
        # Registros anteriores a la fecha estimada: se reconstruye desde el
        # texto relativo y el momento de deteccion, ambos ya guardados.
        if not registro.get("publicada_aprox"):
            registro["publicada_aprox"] = estimar_publicacion(
                registro.get("detectada") or "",
                antiguedad_horas(registro.get("publicada")),
            )
        # Un registro anterior a este campo ya se conocia: nunca es "nueva".
        registro.setdefault("estado", "vista")
        # `publicada` se recalcula desde la fecha absoluta: el texto del sitio
        # se guardo una vez y envejeceria mal ("Hace 5 horas" para siempre).
        if (actual := antiguedad_actual(registro.get("publicada_aprox"))) is not None:
            registro["publicada"] = actual
        # Todas las marcas al huso local, para que el archivo no mezcle offsets.
        for campo in ("publicada_aprox", "detectada"):
            if registro.get(campo):
                registro[campo] = a_zona(registro[campo])
        validos.append(registro)

    if descartados:
        registrar(f"AVISO: {descartados} registro(s) sin 'id' descartados de {ruta}.")
    historial["ofertas"] = validos

    # Formato anterior de una sola busqueda: se migra a la lista.
    if (antigua := historial.pop("busqueda", None)) and not historial["busquedas"]:
        historial["busquedas"] = [antigua]
    return historial


def guardar_json(ruta: Path, datos: dict[str, Any]) -> None:
    """Escribe el JSON de forma atomica para no corromperlo si el proceso muere."""
    ruta.parent.mkdir(parents=True, exist_ok=True)
    temporal = ruta.with_suffix(ruta.suffix + ".tmp")
    temporal.write_text(
        json.dumps(datos, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(temporal, ruta)


def escapar_html(texto: str | None) -> str:
    return (
        (texto or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def mensaje_oferta(registro: dict[str, Any]) -> str:
    """Mensaje HTML de una oferta para Telegram."""
    lineas = [f"🆕 <b>{escapar_html(registro.get('titulo'))}</b>"]
    if registro.get("empresa"):
        lineas.append(f"🏢 {escapar_html(registro['empresa'])}")
    if registro.get("publicada"):
        lineas.append(f"🕒 {escapar_html(registro['publicada'])}")
    if registro.get("busqueda"):
        lineas.append(f"🔎 {escapar_html(registro['busqueda'])}")
    if registro.get("url"):
        lineas.append(escapar_html(registro["url"]))
    return "\n".join(lineas)


def llamar_telegram(
    token: str, metodo: str, cuerpo: dict[str, Any] | None, timeout: float
) -> dict[str, Any]:
    """Invoca un metodo de la Bot API. Lanza RuntimeError si responde ok=false."""
    datos = json.dumps(cuerpo or {}).encode("utf-8")
    peticion = urllib.request.Request(
        f"{TELEGRAM_API}/bot{token}/{metodo}",
        data=datos,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(peticion, timeout=timeout) as respuesta:
        contenido = json.loads(respuesta.read().decode("utf-8"))
    if not contenido.get("ok"):
        raise RuntimeError(contenido.get("description") or "respuesta ok=false")
    return contenido.get("result") or {}


def enviar_oferta(
    token: str, chat: str, registro: dict[str, Any], timeout: float
) -> None:
    llamar_telegram(
        token,
        "sendMessage",
        {
            "chat_id": chat,
            "text": mensaje_oferta(registro),
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        },
        timeout,
    )


def notificar_pendientes(
    historial: dict[str, Any], token: str, chat: str, timeout: float, pausa: float
) -> tuple[int, int]:
    """Envia las ofertas con `notificada: false`. Devuelve (enviadas, pendientes).

    Ante el primer fallo corta el ciclo: las que queden conservan la marca y se
    reintentan en la revision siguiente, asi que una caida de Telegram retrasa
    los avisos pero no los pierde.
    """
    pendientes = [r for r in historial["ofertas"] if r.get("notificada") is False]
    enviadas = 0
    for indice, registro in enumerate(pendientes):
        if _parar.is_set():
            break
        try:
            enviar_oferta(token, chat, registro, timeout)
        except ERRORES_TELEGRAM as error:
            registrar(f"  ERROR de Telegram: {error}; se reintenta en el proximo ciclo.")
            break
        registro["notificada"] = True
        enviadas += 1
        if indice < len(pendientes) - 1 and pausa:
            _parar.wait(pausa)
    return enviadas, len(pendientes) - enviadas


def indexar(historial: dict[str, Any]) -> tuple[set[str], set[str]]:
    """Indices de lo ya guardado: ids y claves secundarias."""
    ids = {o.get("id") for o in historial["ofertas"]}
    claves = {
        clave
        for o in historial["ofertas"]
        if (clave := clave_secundaria(o.get("titulo"), o.get("empresa")))
    }
    return ids, claves


def ciclo(
    busquedas: list[str],
    ruta: Path,
    *,
    top: int,
    orden: str | None,
    max_paginas: int,
    timeout: float,
    pausa: float,
    max_registros: int,
    max_antiguedad: float = 0.0,
    usar_clave_secundaria: bool = True,
    telegram: tuple[str, str] | None = None,
) -> dict[str, Any]:
    """Revisa cada busqueda, guarda las ofertas nuevas y devuelve un resumen.

    Todas las busquedas comparten un mismo historial y un unico guardado al
    final: una oferta que aparezca en dos listados se guarda una sola vez, con
    la busqueda que la encontro primero.
    """
    historial = cargar_historial(ruta)
    # Lo marcado como nuevo en la revision anterior deja de serlo: "nueva"
    # significa "encontrada en este ciclo", no "reciente".
    for registro in historial["ofertas"]:
        if registro.get("estado") == "nueva":
            registro["estado"] = "vista"
    conocidos_id, conocidas_claves = indexar(historial)
    momento = ahora()
    arranque = time.monotonic()
    registros_nuevos: list[dict[str, Any]] = []
    resumenes: list[dict[str, Any]] = []

    for indice, entrada in enumerate(busquedas):
        if _parar.is_set():
            break

        comienzo = time.monotonic()
        try:
            fuente, busqueda, parametros = detectar_fuente(entrada)
        except ValueError as error:
            registrar(f"AVISO: {error}; se omite.")
            continue

        resumen: dict[str, Any] = {
            "busqueda": busqueda,
            "fuente": fuente,
            "url": url_listado(fuente, parametros, 1, orden),
            "ofertas_revisadas": 0,
            "total_publicadas": None,
            "paginas_recorridas": 0,
            "top": 0,
            "nuevas": 0,
            "omitidas_clave_secundaria": 0,
            "mas_reciente": None,
            "error": None,
        }

        try:
            ofertas, total = recorrer(
                fuente,
                parametros,
                orden=orden,
                max_paginas=max_paginas,
                timeout=timeout,
                pausa=pausa,
            )
        except (urllib.error.URLError, TimeoutError) as error:
            # Aislado por busqueda: que falle una no debe perder las demas.
            resumen["error"] = str(error)
            resumen["segundos"] = round(time.monotonic() - comienzo, 2)
            resumenes.append(resumen)
            registrar(f"[{busqueda}] ERROR de red: {error}")
            continue

        resumen["ofertas_revisadas"] = len(ofertas)
        resumen["total_publicadas"] = total
        resumen["paginas_recorridas"] = max((o.pagina for o in ofertas), default=0)

        if not ofertas:
            resumen["segundos"] = round(time.monotonic() - comienzo, 2)
            resumenes.append(resumen)
            registrar(f"[{busqueda}] sin ofertas.")
            continue

        seleccion = mas_recientes(ofertas, top, max_antiguedad)
        nuevas: list[Oferta] = []
        repetidas_por_clave: list[Oferta] = []
        for oferta in seleccion:
            if oferta.id in conocidos_id:
                continue
            clave = clave_secundaria(oferta.titulo, oferta.empresa)
            if usar_clave_secundaria and clave is not None and clave in conocidas_claves:
                repetidas_por_clave.append(oferta)
                continue
            nuevas.append(oferta)
            # Se actualizan sobre la marcha para no duplicar dentro del mismo
            # ciclo ni entre una busqueda y la siguiente.
            conocidos_id.add(oferta.id)
            if clave is not None:
                conocidas_claves.add(clave)

        registros_nuevos += [
            o.a_registro(momento, busqueda, telegram is not None) for o in nuevas
        ]
        resumen["top"] = len(seleccion)
        resumen["nuevas"] = len(nuevas)
        resumen["omitidas_clave_secundaria"] = len(repetidas_por_clave)
        resumen["mas_reciente"] = seleccion[0].publicada
        resumen["segundos"] = round(time.monotonic() - comienzo, 2)
        resumenes.append(resumen)

        revisadas = f"{len(ofertas)} revisadas"
        if total:
            revisadas += f" de {total}"
        registrar(f"[{busqueda}] {revisadas}; top {len(seleccion)}; {len(nuevas)} nuevas")
        for oferta in nuevas:
            registrar(f"  + {oferta.titulo} — {oferta.empresa or 'empresa no indicada'}")
        if repetidas_por_clave:
            # Un id distinto para un titulo+empresa ya guardado es justo la
            # señal de que el sitio roto sus identificadores.
            registrar(
                f"  AVISO: {len(repetidas_por_clave)} con id nuevo pero "
                f"titulo+empresa ya conocidos (posible rotacion de ids); omitidas:"
            )
            for oferta in repetidas_por_clave:
                registrar(f"  = {oferta.titulo} — {oferta.empresa} [{oferta.id}]")

        if indice < len(busquedas) - 1 and pausa:
            _parar.wait(pausa)

    historial["ofertas"] = ordenar_por_publicacion(
        registros_nuevos + historial["ofertas"]
    )
    if max_registros > 0:
        historial["ofertas"] = historial["ofertas"][:max_registros]
    historial["busquedas"] = [etiquetar(b) for b in busquedas]
    historial["actualizado"] = momento
    numero_ciclo = int(historial.get("ciclos") or 0) + 1
    historial["ciclos"] = numero_ciclo
    # Se guarda antes de notificar: los datos nunca dependen de que Telegram
    # responda.
    guardar_json(ruta, historial)

    enviadas = pendientes_envio = 0
    if telegram is not None:
        enviadas, pendientes_envio = notificar_pendientes(
            historial, telegram[0], telegram[1], timeout, pausa
        )
        if enviadas:
            registrar(f"  Telegram: {enviadas} oferta(s) enviada(s).")
            guardar_json(ruta, historial)
        if pendientes_envio:
            registrar(f"  Telegram: {pendientes_envio} pendiente(s) para el proximo ciclo.")

    registrar(
        f"Total: {len(registros_nuevos)} nuevas; "
        f"{len(historial['ofertas'])} acumuladas en {ruta}"
    )
    return {
        "ciclo": numero_ciclo,
        "fecha": momento,
        "segundos": round(time.monotonic() - arranque, 2),
        "ofertas_revisadas": sum(r["ofertas_revisadas"] for r in resumenes),
        "nuevas": len(registros_nuevos),
        "acumuladas": len(historial["ofertas"]),
        "errores": sum(1 for r in resumenes if r["error"]),
        "notificadas": enviadas,
        "pendientes_notificar": pendientes_envio,
        "busquedas": resumenes,
    }


def anexar_historial(ruta: Path, resumen: dict[str, Any], max_ciclos: int) -> None:
    """Agrega el resumen del ciclo al historico de busquedas, mas reciente primero."""
    vacio: dict[str, Any] = {"creado": resumen["fecha"], "actualizado": None, "total_ciclos": 0, "ciclos": []}
    historico = vacio
    if ruta.exists():
        try:
            leido = json.loads(ruta.read_text(encoding="utf-8"))
            if isinstance(leido, dict) and isinstance(leido.get("ciclos"), list):
                historico = vacio | leido
            else:
                registrar(f"AVISO: {ruta} tiene un formato inesperado; se empieza de cero.")
        except (json.JSONDecodeError, OSError) as error:
            registrar(f"AVISO: no se pudo leer {ruta} ({error}); se empieza de cero.")

    historico["ciclos"] = [resumen] + historico["ciclos"]
    if max_ciclos > 0:
        historico["ciclos"] = historico["ciclos"][:max_ciclos]
    historico["actualizado"] = resumen["fecha"]
    historico["total_ciclos"] = int(historico.get("total_ciclos") or 0) + 1
    guardar_json(ruta, historico)


def crear_manejador(
    ruta_html: Path, ruta_ofertas: Path, ruta_historial: Path | None
) -> type[http.server.BaseHTTPRequestHandler]:
    """Manejador del panel: la pagina y los dos JSON, nada mas."""

    class Panel(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _responder(self, cuerpo: bytes, tipo: str, codigo: int = 200) -> None:
            self.send_response(codigo)
            self.send_header("Content-Type", tipo)
            self.send_header("Content-Length", str(len(cuerpo)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(cuerpo)

        def _archivo(self, ruta: Path | None, tipo: str, respaldo: bytes) -> None:
            # El vigilante reescribe estos archivos de forma atomica, asi que
            # aqui nunca se lee un JSON a medias; si falta, se sirve el respaldo
            # para que el panel cargue igual en vez de quedarse colgado.
            cuerpo = respaldo
            if ruta is not None:
                try:
                    cuerpo = ruta.read_bytes()
                except OSError:
                    cuerpo = respaldo
            self._responder(cuerpo, tipo)

        def do_GET(self) -> None:
            ruta = self.path.split("?", 1)[0].rstrip("/") or "/"
            if ruta == "/":
                self._archivo(ruta_html, "text/html; charset=utf-8", b"<h1>panel.html no encontrado</h1>")
            elif ruta == "/api/ofertas":
                self._archivo(ruta_ofertas, "application/json; charset=utf-8", b'{"ofertas":[]}')
            elif ruta == "/api/historial":
                self._archivo(ruta_historial, "application/json; charset=utf-8", b'{"ciclos":[]}')
            else:
                self._responder(b"no encontrado", "text/plain; charset=utf-8", 404)

        do_HEAD = do_GET

        def log_message(self, *_args: Any) -> None:
            pass  # El log del vigilante ya es suficiente; no se ensucia con accesos.

    return Panel


def iniciar_panel(
    puerto: int, ruta_html: Path, ruta_ofertas: Path, ruta_historial: Path | None
) -> None:
    """Arranca el panel en un hilo demonio, sin bloquear el ciclo de revision."""
    servidor = http.server.ThreadingHTTPServer(
        ("0.0.0.0", puerto), crear_manejador(ruta_html, ruta_ofertas, ruta_historial)
    )
    hilo = threading.Thread(target=servidor.serve_forever, daemon=True)
    hilo.start()
    registrar(f"Panel web en http://localhost:{puerto}")


def configurar_telegram(args: argparse.Namespace) -> tuple[str, str] | None:
    """Devuelve (token, chat) si Telegram esta configurado y utilizable.

    Las credenciales se leen del entorno, no de la linea de ordenes, para que no
    queden a la vista en `docker ps`. Se valida el token al arrancar: es mejor
    enterarse ahora que descubrirlo cuando aparezca la primera oferta nueva.
    """
    if args.sin_telegram:
        return None
    token = os.environ.get("TELEGRAM_TOKEN", "").strip()
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat:
        registrar(
            "Telegram desactivado: faltan TELEGRAM_TOKEN o TELEGRAM_CHAT_ID."
        )
        return None
    try:
        info = llamar_telegram(token, "getMe", None, args.timeout)
    except ERRORES_TELEGRAM as error:
        registrar(f"AVISO: Telegram no responde ({error}); se sigue sin avisos.")
        return None
    registrar(f"Telegram activo: bot @{info.get('username', '?')} -> chat {chat}")
    return token, chat


def probar_telegram(args: argparse.Namespace) -> int:
    """Envia un mensaje de prueba al chat configurado."""
    credenciales = configurar_telegram(args)
    if credenciales is None:
        print("Telegram no esta configurado.", file=sys.stderr)
        return 2
    token, chat = credenciales
    prueba = {
        "titulo": "Mensaje de prueba",
        "empresa": "Vigilante de ofertas",
        "publicada": ahora(),
        "busqueda": ", ".join(args.busquedas),
        "url": "https://co.computrabajo.com/",
    }
    try:
        enviar_oferta(token, chat, prueba, args.timeout)
    except ERRORES_TELEGRAM as error:
        print(f"No se pudo enviar: {error}", file=sys.stderr)
        return 2
    registrar("Mensaje de prueba enviado.")
    return 0


def comprobar_escritura(ruta: Path) -> str | None:
    """Verifica que se pueda escribir la salida. Devuelve el motivo del fallo."""
    try:
        ruta.parent.mkdir(parents=True, exist_ok=True)
        testigo = ruta.with_suffix(ruta.suffix + ".tmp")
        testigo.write_text("", encoding="utf-8")
        testigo.unlink()
    except OSError as error:
        return str(error)
    return None


def vigilar(args: argparse.Namespace) -> int:
    ruta = Path(args.salida)
    orden = None if args.orden == "ninguno" else args.orden

    # Mejor fallar de entrada que descubrirlo tras el primer ciclo: una ruta no
    # escribible no se arregla sola por reintentar.
    ruta_historial = None if args.sin_historial else Path(args.historial)
    for destino in (ruta, ruta_historial):
        if destino is not None and (motivo := comprobar_escritura(destino)) is not None:
            print(f"No se puede escribir en {destino}: {motivo}", file=sys.stderr)
            return 2

    if args.web_puerto > 0:
        try:
            iniciar_panel(args.web_puerto, Path(args.panel), ruta, ruta_historial)
        except OSError as error:
            registrar(f"AVISO: no se pudo abrir el panel en {args.web_puerto} ({error}).")

    telegram = configurar_telegram(args)
    listado = ", ".join(f"'{etiquetar(b)}'" for b in args.busquedas)
    registrar(
        f"Vigilando {len(args.busquedas)} busqueda(s) cada {args.intervalo}s "
        f"(top {args.top} c/u, hasta {args.max_paginas} paginas) -> {ruta}"
    )
    registrar(f"Busquedas: {listado}")
    if ruta_historial is not None:
        registrar(f"Historico de ciclos -> {ruta_historial}")

    while not _parar.is_set():
        comienzo = time.monotonic()
        try:
            resumen = ciclo(
                args.busquedas,
                ruta,
                top=args.top,
                orden=orden,
                max_paginas=args.max_paginas,
                timeout=args.timeout,
                pausa=args.pausa,
                max_registros=args.max_registros,
                max_antiguedad=args.max_antiguedad,
                usar_clave_secundaria=not args.solo_id,
                telegram=telegram,
            )
            if ruta_historial is not None:
                anexar_historial(ruta_historial, resumen, args.max_historial)
        except (urllib.error.URLError, TimeoutError) as error:
            # Un fallo de red no debe tumbar el vigilante: se reintenta al
            # siguiente ciclo.
            registrar(f"ERROR de red: {error}; se reintenta en {args.intervalo}s.")
        except OSError as error:
            # Escribir el JSON es la razon de ser del proceso: si falla, no se
            # disfraza de problema de red ni se reintenta indefinidamente.
            print(f"ERROR al escribir {ruta}: {error}", file=sys.stderr)
            return 2

        if args.una_vez:
            break
        # Se descuenta lo que tardo el ciclo para mantener la cadencia real.
        espera = max(args.intervalo - (time.monotonic() - comienzo), 1.0)
        _parar.wait(espera)

    registrar("Detenido.")
    return 0


def construir_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Vigila periodicamente las ultimas ofertas publicadas en "
            "Computrabajo Colombia y acumula titulo y empresa en un JSON."
        )
    )
    parser.add_argument(
        "busquedas",
        nargs="*",
        default=["desarrollador full stack"],
        help=(
            "Una o mas busquedas (texto o slug). Comparten el mismo JSON y se "
            "deduplican entre si. Por defecto: 'desarrollador full stack'."
        ),
    )
    parser.add_argument(
        "-i",
        "--intervalo",
        type=float,
        default=300.0,
        help="Segundos entre revisiones (por defecto 300 = 5 minutos).",
    )
    parser.add_argument(
        "-t",
        "--top",
        type=int,
        default=5,
        help="Cuantas de las ofertas mas recientes se revisan por busqueda.",
    )
    parser.add_argument(
        "-s",
        "--salida",
        default="/datos/ofertas.json",
        help="Ruta del JSON acumulado.",
    )
    parser.add_argument(
        "--una-vez", action="store_true", help="Ejecutar un solo ciclo y salir."
    )
    parser.add_argument(
        "-o",
        "--orden",
        choices=("relevance", "ninguno"),
        default="relevance",
        help="Parametro 'by' del listado; 'ninguno' omite el parametro.",
    )
    parser.add_argument(
        "--max-antiguedad",
        type=float,
        default=24.0,
        help=(
            "Techo en horas de la antiguedad revisada; 0 lo desactiva. Evita "
            "que una busqueda con pocas ofertas frescas arrastre el listado "
            "viejo completo (por defecto 24)."
        ),
    )
    parser.add_argument(
        "--max-paginas",
        type=int,
        default=3,
        help="Paginas a recorrer por ciclo (las recientes salen en las primeras).",
    )
    parser.add_argument(
        "--web-puerto",
        type=int,
        default=8080,
        help="Puerto del panel web; 0 lo desactiva.",
    )
    parser.add_argument(
        "--panel",
        default="/app/panel.html",
        help="Ruta del HTML que sirve el panel.",
    )
    parser.add_argument(
        "--sin-telegram",
        action="store_true",
        help="No enviar avisos aunque TELEGRAM_TOKEN y TELEGRAM_CHAT_ID esten definidos.",
    )
    parser.add_argument(
        "--probar-telegram",
        action="store_true",
        help="Enviar un mensaje de prueba y salir.",
    )
    parser.add_argument(
        "--telegram-pausa",
        type=float,
        default=0.5,
        help="Segundos entre mensajes de Telegram.",
    )
    parser.add_argument(
        "--zona",
        default=ZONA_POR_DEFECTO,
        help=f"Huso de las marcas de tiempo del JSON (por defecto {ZONA_POR_DEFECTO}).",
    )
    parser.add_argument(
        "--historial",
        default="/datos/historial.json",
        help="Ruta del JSON con el historico de cada ciclo de busqueda.",
    )
    parser.add_argument(
        "--sin-historial", action="store_true", help="No escribir el historico."
    )
    parser.add_argument(
        "--max-historial",
        type=int,
        default=5,
        help="Tope de ciclos guardados en el historico; 0 para no limitar.",
    )
    parser.add_argument(
        "--solo-id",
        action="store_true",
        help=(
            "Comparar solo por id, sin la clave secundaria titulo+empresa. "
            "Detecta reposteos de la misma vacante, pero queda expuesto a una "
            "rotacion de ids del sitio."
        ),
    )
    parser.add_argument(
        "--max-registros",
        type=int,
        default=500,
        help="Tope de ofertas guardadas en el JSON; 0 para no limitar.",
    )
    parser.add_argument(
        "--timeout", type=float, default=20.0, help="Timeout HTTP en segundos."
    )
    parser.add_argument(
        "--pausa", type=float, default=1.0, help="Segundos de espera entre paginas."
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = construir_parser().parse_args(argv)

    def detener(_signum: int, _frame: object) -> None:
        _parar.set()

    signal.signal(signal.SIGTERM, detener)
    signal.signal(signal.SIGINT, detener)
    fijar_zona(args.zona)

    if args.probar_telegram:
        return probar_telegram(args)

    try:
        return vigilar(args)
    except KeyboardInterrupt:
        registrar("Detenido.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
