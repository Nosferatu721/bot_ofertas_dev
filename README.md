# Vigilante de ofertas — Computrabajo Colombia

Servicio en **Python 3.14** (solo librería estándar) que cada 5 minutos revisa las
5 ofertas publicadas más recientemente de **una o varias búsquedas** en
[Computrabajo Colombia](https://co.computrabajo.com) y acumula **título y empresa**
en un JSON, sin repetir las que ya estaban. Corre en Docker.

Búsquedas vigiladas por defecto (ver `compose.yaml`):

- [`desarrollador full stack`](https://co.computrabajo.com/trabajo-de-desarrollador-full-stack)
- [`desarrollador software`](https://co.computrabajo.com/trabajo-de-desarrollador-software)
- [`desarrollador backend`](https://co.computrabajo.com/trabajo-de-desarrollador-backend)
- LinkedIn: `Desarrollador` en Colombia, últimas 24 h

## Fuentes

| Fuente | Cómo se configura |
|---|---|
| **Computrabajo** | Texto suelto: `"desarrollador backend"` → `/trabajo-de-desarrollador-backend` |
| **LinkedIn** | La URL del buscador pegada tal cual |

La fuente se deduce del dominio, así que basta con pegar el enlace en
`compose.yaml`. Todo lo demás —deduplicación, orden por fecha, avisos de
Telegram y panel— funciona igual para ambas.

### Notas sobre LinkedIn

- Se consulta el **endpoint público de invitado**
  (`/jobs-guest/jobs/api/seeMoreJobPostings/search`), no la página
  `/jobs/search-results/`, que exige sesión y responde `307`.
- Los filtros del enlace (`geoId`, `f_TPR`, `distance`…) se respetan tal cual;
  solo se controla la paginación (`start`, de 10 en 10).
- **La antigüedad sale del texto del `<time>`** (`Hace 32 minutos`, `Hace 1
  hora`), no del atributo `datetime="2026-10-15"`. El atributo trae **solo la
  fecha** —y en UTC, así que de madrugada apunta al día siguiente al local—, de
  modo que leerlo como medianoche dejaba todas las ofertas del día con la misma
  antiguedad: el aviso decía «Hace 13 horas» para una oferta de una hora y,
  al quedar todas empatadas, la selección de las más recientes la decidía la
  relevancia. El atributo solo se usa como respaldo si falta el texto.
- Los ids llevan prefijo `li:` para no mezclarse con los de Computrabajo.
- LinkedIn limita el tráfico automatizado con dureza; si empieza a responder
  `429`, el ciclo lo registra como error de esa búsqueda y las demás siguen.

## Panel web

Mientras el vigilante corre, sirve una vista en **http://localhost:8080** que se
refresca sola cada 20 s leyendo los mismos JSON.

Muestra las ofertas guardadas (con enlace, empresa, antigüedad y de qué búsqueda
vienen), métricas arriba —guardadas, nuevas en el último ciclo, detectadas en 24 h,
última revisión, avisos pendientes—, filtros por búsqueda y por texto, y una tabla con los
ciclos guardados. Las encontradas en el último ciclo van marcadas como «nueva».

El indicador de la cabecera se pone en rojo si el panel pierde contacto con el
vigilante, para que un contenedor caído no parezca «sin ofertas nuevas».

| Ruta | Devuelve |
|---|---|
| `/` | La página (`panel.html`) |
| `/api/ofertas` | `ofertas.json` tal cual |
| `/api/historial` | `historial.json` tal cual |

El puerto se publica en `127.0.0.1:8080`, así que **solo es accesible desde este
equipo**, no desde la red local. Para desactivar el panel: `--web-puerto 0`.
Para cambiarlo, ajusta `--web-puerto` y el mapeo de `ports` en `compose.yaml`.

`panel.html` se copia dentro de la imagen; si lo editas hay que reconstruir
(`docker compose up -d --build`).

## Avisos por Telegram

Cuando aparece una oferta **nueva**, se envía un mensaje al chat configurado.
Las que ya estaban en `ofertas.json` no se reenvían nunca.

### Configuración

1. Crea el bot: escribe a [@BotFather](https://t.me/BotFather) en Telegram,
   manda `/newbot` y copia el token.
2. Escribe cualquier mensaje a tu bot (si no, no puede responderte).
3. Consulta tu chat id abriendo en el navegador:
   `https://api.telegram.org/bot<TU_TOKEN>/getUpdates` — el número está en
   `result[0].message.chat.id`.
4. Copia `.env.example` a `.env` y rellena los dos valores.
5. `docker compose up -d`

Comprueba que funciona antes de esperar a una oferta nueva:

```bash
docker compose run --rm vigilante --probar-telegram
```

Las credenciales se leen **del entorno**, nunca de la línea de órdenes, para que
no queden a la vista en `docker ps`. Si faltan, el vigilante sigue guardando los
JSON y solo avisa que las notificaciones están desactivadas.

### Qué se envía

```
🆕 Desarrollador Full Stack
🏢 PERSOM S.A.S
🕒 Hace 5 horas
🔎 desarrollador full stack
https://co.computrabajo.com/ofertas-de-trabajo/...
```

### Cómo se garantiza que no se pierda ni se repita ningún aviso

Cada oferta guardada mientras Telegram está activo lleva la marca
`"notificada": false`, y solo pasa a `true` cuando el envío se confirma. Cada
ciclo reintenta las que sigan pendientes, así que **una caída de Telegram
retrasa los avisos pero no los pierde**. El JSON se guarda *antes* de notificar:
los datos nunca dependen de que Telegram responda.

La marca solo se añade a las ofertas guardadas con las notificaciones activas.
Por eso, al configurar Telegram por primera vez **no se dispara un aluvión** con
todo el histórico ya acumulado: esos registros no tienen la marca y se
consideran ya vistos.

## Arranque

```bash
docker compose up -d          # arranca el vigilante en segundo plano
docker compose logs -f        # ver la actividad
# panel web: http://localhost:8080
cat datos/ofertas.json        # las ofertas encontradas
cat datos/historial.json      # el histórico de cada ciclo
docker compose down           # detener
```

Ambos JSON quedan en `./datos/` (volumen montado en `/datos`), así que sobreviven
a reinicios del contenedor:

| Archivo | Contenido |
|---|---|
| `ofertas.json` | Las ofertas encontradas, sin repetir |
| `historial.json` | Una entrada por ciclo: cuándo, qué se revisó, cuántas nuevas, notificadas, errores |

### Sin Compose

```bash
docker build -t ultima-oferta:3.14 .
docker run -d --name ofertas-vigilante --restart unless-stopped \
  -e TZ=America/Bogota -v "$PWD/datos:/datos" ultima-oferta:3.14

# Un solo ciclo, para probar
docker run --rm -v "$PWD/datos:/datos" ultima-oferta:3.14 --una-vez

# Varias búsquedas: se pasan como argumentos sueltos
docker run --rm -v "$PWD/datos:/datos" ultima-oferta:3.14 --una-vez \
  "desarrollador full stack" "desarrollador software" "ingeniero de datos"
```

En **Git Bash sobre Windows** antepón `MSYS_NO_PATHCONV=1`, o las rutas tipo
`/datos` se convierten en rutas de Windows.

## `historial.json` — histórico de búsquedas

Una entrada por ciclo, la más reciente primero:

```json
{
  "creado": "2026-08-30T14:35:07-05:00",
  "actualizado": "2026-08-30T14:40:16-05:00",
  "total_ciclos": 2,
  "ciclos": [
    {
      "ciclo": 2,
      "fecha": "2026-08-30T14:40:16-05:00",
      "segundos": 10.02,
      "ofertas_revisadas": 98,
      "nuevas": 0,
      "acumuladas": 9,
      "errores": 0,
      "busquedas": [
        {
          "busqueda": "desarrollador full stack",
          "url": "https://co.computrabajo.com/trabajo-de-desarrollador-full-stack?by=relevance",
          "ofertas_revisadas": 38,
          "total_publicadas": 38,
          "paginas_recorridas": 2,
          "top": 5,
          "nuevas": 0,
          "omitidas_clave_secundaria": 0,
          "mas_reciente": "Hace 4 horas",
          "error": null,
          "segundos": 4.33
        }
      ]
    }
  ]
}
```

Campos útiles para vigilar el propio vigilante:

- **`ofertas_revisadas` vs `total_publicadas`** — cuánto del listado se está
  cubriendo. Si la distancia es grande, considera subir `--max-paginas`.
- **`omitidas_clave_secundaria`** — si deja de ser 0 de forma sostenida, los ids
  del sitio rotaron.
- **`error`** — se registra **por búsqueda**: si una falla, las demás siguen
  corriendo y el ciclo queda documentado en vez de perderse.
- **`mas_reciente`** — la antigüedad de la oferta más nueva vista en ese ciclo.

Se guardan los **últimos 5 ciclos** (`--max-historial`, `0` = sin tope). El
contador `total_ciclos` sigue siendo acumulativo: cuenta todas las revisiones
hechas, no solo las que quedan en el archivo. Con `--sin-historial` no se
escribe.

## `ofertas.json` — formato

```json
{
  "busquedas": ["desarrollador full stack", "desarrollador software"],
  "actualizado": "2026-08-30T14:29:01-05:00",
  "ciclos": 12,
  "ofertas": [
    {
      "titulo": "Desarrollador Full Stack",
      "empresa": "PERSOM S.A.S",
      "id": "476821BDFA1CC94F61373E686DCF3405",
      "publicada": "Hace 4 horas",
      "url": "https://co.computrabajo.com/ofertas-de-trabajo/...",
      "publicada_aprox": "2026-08-30T10:29:01-05:00",
      "busqueda": "desarrollador full stack",
      "estado": "nueva",
      "detectada": "2026-08-30T14:29:01-05:00"
    }
  ]
}
```

`publicada` **no es el texto que guardó el sitio**, sino la antigüedad recalculada
en cada ciclo a partir de `publicada_aprox`. El texto original envejecía mal: una
oferta detectada ayer seguiría diciendo «Hace 5 horas» para siempre. El panel lo
recalcula además en el navegador, así que es exacto incluso entre ciclos.

Se usa el vocabulario del sitio (`Hace 3 horas`, `Ayer`, `Hace 6 días`), y a
partir de 30 días se muestra `Más de 30 días` en vez de un número exacto:
Computrabajo nunca dio más precisión que esa, e imprimir «Hace 47 días» sería
inventarla.

`estado` vale `"nueva"` solo durante el ciclo en que se encontró; en la
revisión siguiente pasa a `"vista"` y ya no vuelve atrás. Es lo que marca el
panel, en vez de una ventana de horas: así la etiqueta señala un hallazgo real
y no «detectada hace poco».

`busqueda` indica **qué listado encontró la oferta primero**. Todas las búsquedas
comparten el mismo historial, así que una vacante que aparezca en dos listados se
guarda una sola vez. Ocurre a menudo: de las 5 más recientes de *desarrollador
software*, 3 ya estaban vía *desarrollador full stack*.

### Orden: `publicada_aprox`

El archivo va **de publicación más reciente a más antigua**. No se puede ordenar
por `publicada` directamente, porque es texto relativo que caduca: una oferta
guardada hace tres días como `"Hace 4 horas"` seguiría diciendo eso para siempre.

Por eso, al detectarla se fija contra el reloj una fecha absoluta estimada:

```
publicada_aprox = detectada − antigüedad del texto
```

Esa marca ya no cambia, así que el orden se mantiene coherente entre ciclos y
entre ofertas capturadas en momentos distintos. Es una **estimación**: `"Ayer"`
se toma como 24 h y `"Más de 30 días"` como 30 días, así que entre ofertas del
mismo día la precisión es limitada. Las de antigüedad no interpretable van al
final, con `publicada_aprox: null`.

### Formato y huso de las fechas

`publicada_aprox` y `detectada` van en **ISO 8601 con offset explícito y
precisión de segundos**: `2026-08-30T06:45:22-05:00`. Se leen con
`datetime.fromisoformat()` en Python, `Date.parse()` en JS, o cualquier parser
ISO 8601.

El huso está **fijado en el script** (`--zona`, por defecto `America/Bogota`), no
heredado de la variable `TZ` del contenedor. Eso hace que el archivo mantenga
siempre el mismo offset aunque se lance con un `docker run` suelto sin `-e TZ`.
Colombia no aplica horario de verano, así que `-05:00` es estable todo el año.

Al cargar el archivo, cualquier marca en otro huso se reexpresa en la zona
configurada **sin mover el instante**, de modo que el JSON nunca queda con
offsets mezclados. El orden, además, compara instantes y no cadenas: ordenar
texto ISO entre husos distintos da un resultado incorrecto.

`ofertas` está ordenado de más reciente a más antiguo por detección: cada ciclo
inserta al principio solo las que no estaban. `empresa` es `null` cuando la
oferta es anónima.

## Cómo se evita duplicar

Se usan dos claves, en este orden:

1. **`id`** — el identificador de Computrabajo (32 hex), que además es el sufijo
   de la URL permanente de la oferta. Verificado: único dentro de cada listado,
   sin colisiones entre búsquedas distintas, y estable entre descargas y entre
   listados (la misma vacante conserva su id en `/trabajo-de-desarrollador` y en
   `/trabajo-de-desarrollador-full-stack`).
2. **`título + empresa`** normalizados (sin tildes ni puntuación) — red de
   seguridad ante una eventual rotación de ids del sitio. Los 38 ids observados
   terminan en la misma secuencia (`61373E686DCF3405`) y solo varían los
   primeros 16 hex, lo que sugiere un identificador cifrado por bloques; si esa
   parte constante cambiara, todos los ids cambiarían a la vez y sin esta
   segunda clave el vigilante volvería a guardar ofertas ya conocidas.

Cuando la segunda clave descarta una oferta, queda en el log como aviso, porque
es justamente la señal de que los ids rotaron:

```
AVISO: 4 con id nuevo pero titulo+empresa ya conocidos (posible rotacion de ids); omitidas:
  = Desarrollador Full Stack — PERSOM S.A.S [476821BDFA1CC94F61373E686DCF3405]
```

Dos matices:

- **Ofertas anónimas** (sin empresa) se comparan **solo por id**. Sin nombre de
  empresa, dos vacantes distintas con el mismo título colisionarían, y perder
  una oferta real es peor que guardarla dos veces.
- **Un reposteo legítimo** de la misma vacante por la misma empresa no se
  detecta como oferta nueva. Si prefieres detectarlo, usa `--solo-id` y quedas
  expuesto a la rotación.

## Opciones

| Opción | Descripción | Por defecto |
|---|---|---|
| `busquedas` | Una o más búsquedas (posicional, variádico) | `desarrollador full stack` |
| `-i`, `--intervalo SEG` | Segundos entre revisiones | `300` (5 min) |
| `-t`, `--top N` | Antigüedad de corte: fija el puesto que la define, y entran todas las ofertas que lo igualan | `5` |
| `--max-antiguedad H` | Techo en horas de ese corte (`0` lo desactiva) | `24` |
| `-s`, `--salida RUTA` | Ruta del JSON de ofertas | `/datos/ofertas.json` |
| `--web-puerto N` | Puerto del panel web (`0` lo desactiva) | `8080` |
| `--panel RUTA` | HTML que sirve el panel | `/app/panel.html` |
| `--sin-telegram` | No enviar avisos aunque haya credenciales | desactivado |
| `--probar-telegram` | Enviar un mensaje de prueba y salir | — |
| `--telegram-pausa SEG` | Espera entre mensajes de Telegram | `0.5` |
| `--zona ZONA` | Huso de las marcas de tiempo del JSON | `America/Bogota` |
| `--historial RUTA` | Ruta del JSON con el histórico de ciclos | `/datos/historial.json` |
| `--sin-historial` | No escribir el histórico | desactivado |
| `--max-historial N` | Tope de ciclos guardados (`0` = sin tope) | `5` |
| `--una-vez` | Un solo ciclo y salir | bucle infinito |
| `-o`, `--orden` | Parámetro `by` del listado: `relevance` o `ninguno` | `relevance` |
| `--solo-id` | Comparar solo por `id`, sin la clave secundaria | desactivado |
| `--max-paginas N` | Páginas a recorrer por ciclo | `3` |
| `--max-registros N` | Tope de ofertas en el JSON (`0` = sin tope) | `500` |
| `--timeout SEG` | Timeout HTTP | `20` |
| `--pausa SEG` | Espera entre páginas | `1.0` |

## Sobre el orden y las fechas

Computrabajo **no ofrece un orden real por fecha**. Comprobado sobre el listado
de `desarrollador full stack`:

- `?by=relevance` produce exactamente el mismo resultado que la URL sin parámetro.
- `?by=publicationtime` es ignorado (no ordena por fecha).
- Las tres variantes devuelven el mismo marcador interno `ListOffers-Score4-*`,
  es decir, siempre el ranking de relevancia.

Por eso el script resuelve la fecha por su cuenta: convierte el texto de cada
tarjeta (`Hace 4 horas`, `Ayer`, `Más de 30 días`) a horas aproximadas y ordena
con ese valor, desempatando con el orden de relevancia. Las ofertas cuyo texto
no se puede interpretar quedan al final en vez de contarse como recientes.

`--top` elige **la antigüedad de corte**, no cuántas ofertas caben: fijado el
corte entran todas las que lo igualan. Cortar en el puesto `--top` exacto dejaba
los empates en manos de la relevancia, y una oferta recién publicada que el
listado pusiera en el puesto 20 no se revisaba hasta que el listado la moviera
solo, o nunca. `--max-antiguedad` pone techo a ese corte: sin él, una búsqueda
con pocas ofertas frescas pondría el corte en las antiguas y el empate
arrastraría el listado viejo completo.

Dos límites que conviene tener presentes:

- **La antigüedad es aproximada.** El listado no publica fecha exacta, así que
  entre varias ofertas de "Ayer" no hay forma de saber cuál se publicó primero
  sin abrir cada oferta.
- **Solo se recorren 3 páginas por ciclo** (`--max-paginas`), unas 60 ofertas.
  Recorrer el listado completo cada 5 minutos sería abusivo con el sitio, y las
  ofertas recientes salen bien rankeadas en las primeras páginas. Si tu búsqueda
  es muy amplia y quieres más cobertura, sube el valor y baja la frecuencia.

## Comportamiento operativo

- **Escritura atómica**: el JSON se escribe en un `.tmp` y se renombra, para que
  un corte a mitad de ciclo no lo deje corrupto.
- **Parada limpia**: atiende `SIGTERM`, así que `docker stop` / `docker compose
  down` cortan la espera al instante y salen con código 0.
- **Errores de red**: se registran y se reintenta en el siguiente ciclo; no
  tumban el proceso.
- **Errores de escritura**: se verifica que la salida sea escribible *antes* de
  empezar y el proceso sale con código 2 si no lo es, en vez de reintentar en
  vano.
- **Cadencia real**: el tiempo que tarda el ciclo se descuenta del intervalo.

## Pruebas

`pruebas_vigilante.py` cubre, solo con librería estándar y sin salir de la
máquina, el parseo de fechas de LinkedIn, la selección de las más recientes y un
ciclo completo contra un LinkedIn y un Telegram de juguete (servidores locales).

```bash
# En el mismo Python 3.14 de la imagen, sin instalar nada:
docker run --rm -v "$PWD:/pruebas" -w /pruebas -e TZ=America/Bogota   --entrypoint python python:3.14-slim -m unittest pruebas_vigilante -v
```

## Códigos de salida

| Código | Significado |
|---|---|
| `0` | Parada limpia |
| `2` | La ruta de salida no es escribible |

## Notas

- El scraping depende del HTML de Computrabajo; si el sitio cambia su maquetado,
  hay que ajustar las expresiones regulares al inicio de `ultima_oferta.py`.
- El contenedor corre como usuario sin privilegios (`scraper`, uid 10001), dueño
  de `/datos`.
