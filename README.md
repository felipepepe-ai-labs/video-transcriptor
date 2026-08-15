# Video Transcriptor EN → ES

Transcribe videos en inglés y obtén la transcripción traducida al español, la
narración en castellano y el video doblado, usando Whisper y Piper corriendo
remotamente en una máquina con GPU.

## Arquitectura

```
Frontend (React 19 + Vite :5173)
        │
        ▼
Backend (FastAPI :8000) ──SSH──► 192.168.1.60
        │                         ├─ Whisper large-v3-turbo  (transcripción)
        │                         └─ Piper                   (TTS español)
        │
        ├─► MyMemory Translate API   (EN→ES, primario)
        ├─► Ollama qwen3.6:27b       (EN→ES fallback + resúmenes)
        ├─► yt-dlp                   (YouTube y videos de X)
        └─► ffmpeg (local)           (narración, mux, corte por capítulos)
```

Tres vías de entrada: **subir un fichero**, **una URL de YouTube** o **los
bookmarks de X (Twitter)**. Las tres terminan en el mismo Whisper remoto.

## Requisitos

- **Backend:** Python 3.12+ con SSH key-based auth a `felipe@192.168.1.60` (sin
  password). La máquina remota necesita `whisper` instalado y una GPU.
- **ffmpeg** local, para la pista de narración, el muxing y el corte por capítulos.
- **X bookmarks:** Chromium de Playwright instalado localmente
  (`playwright install chromium`). Si falta, el backend lo dice con un
  `ScrapingError` claro en vez de fallar de forma oscura.
- **Frontend:** Node 22+

## Quick Start

```bash
# Terminal 1 — Backend
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py           # → http://localhost:8000

# Terminal 2 — Frontend
cd frontend
npm install
npm run dev             # → http://localhost:5173
```

Tests del backend: `cd backend && source .venv/bin/activate && pytest -v`
(el borde SSH está mockeado). El frontend no tiene suite de tests.

## Flujo

1. Abres `http://localhost:5173`
2. Eliges la fuente: arrastras un video/audio, pegas una URL de YouTube, o vas a
   la pestaña de X bookmarks
3. Haces clic en "Transcribir" → el frontend hace `POST /jobs` y recibe un
   `job_id` al instante (no bloquea)
4. El frontend hace polling a `GET /jobs/{id}` cada ~2s mientras el job avanza por
   `downloading → uploading → transcribing → translating → voicing → dubbing → splitting → done`
5. En background: el backend sube el fichero vía SSH, corre Whisper
   (`large-v3-turbo`) y trae de vuelta el `.srt`
6. Traducción EN → ES vía MyMemory (con reintentos y batching); si falla, cae a
   **Ollama**; si también falla, devuelve el texto sin traducir en vez de tumbar
   el job. El resultado siempre indica qué proveedor se usó
   (`translation_provider`)
7. Con la traducción lista, Piper sintetiza la narración segmento a segmento y
   ffmpeg la muxea sobre el video original, cortándolo por capítulos si los hay
8. Al llegar a `done`, el frontend muestra los segmentos con timestamps, tabs
   EN/ES, el audio y el video doblado
9. Puedes copiar el texto, descargar el `.srt`, pedir un resumen, o regenerar la
   narración de un capítulo suelto

Los videos de YouTube se transcriben con **detección automática de idioma**: uno
en inglés recorre el pipeline completo, mientras que uno detectado como español
conserva su transcripción tal cual (`translation_provider = "source-es"`) y se
salta la traducción.

**La narración, el doblaje y el corte por capítulos son bonus best-effort**: si
alguno falla queda anotado en los campos `*_error` del resultado, pero el job
termina igual. Lo que se garantiza es transcripción + traducción.

## Endpoints

### Jobs

| Método | Path | Descripción |
|--------|------|-------------|
| POST   | `/jobs` | Multipart: `video` + `chapters_json` y `voice` opcionales. Encola y devuelve `{job_id, status}` |
| POST   | `/jobs/youtube` | Form: `url` + los mismos opcionales. Añade una etapa `downloading` |
| GET    | `/jobs` | Lista todos los jobs |
| GET    | `/jobs/{id}` | Estado: `queued/running/done/failed`, `stage`, `progress`, `result` |
| DELETE | `/jobs/{id}` | Borra el job y sus ficheros locales |
| POST   | `/jobs/{id}/summarize` | Genera un resumen en español vía Ollama, en background |

### Media

| Método | Path | Descripción |
|--------|------|-------------|
| GET    | `/jobs/{id}/audio` | Descarga la narración WAV |
| GET    | `/jobs/{id}/video` | Descarga el video doblado |
| GET    | `/jobs/{id}/chapters/{i}/video` | Descarga el clip de un capítulo |
| GET    | `/jobs/{id}/chapters/{i}/audio` | Descarga la narración de un capítulo |
| POST   | `/jobs/{id}/retts` | Regenera la narración completa |
| POST   | `/jobs/{id}/chapters/{i}/retts` | Regenera la narración de un capítulo |
| POST   | `/jobs/{id}/chapters/{i}/audio` | Regenera el audio de un capítulo |

Los tres endpoints de descarga devuelven **404 si esa etapa no llegó a
completarse**. El nombre del fichero lo decide el `Content-Disposition` del
servidor, no el navegador: los enlaces son cross-origin (`:5173` → `:8000`), así
que el atributo `download="..."` del frontend se ignora.

### X bookmarks

| Método | Path | Descripción |
|--------|------|-------------|
| POST   | `/x/import-cookies` | Multipart: sube un `cookies.txt` de X |
| POST   | `/x/sync` | Lanza el scrape headless de los bookmarks |
| GET    | `/x/progress` | Canal SSE con el progreso del sync |
| GET    | `/x/bookmarks` | Lista, opcionalmente filtrada por `status` |
| PATCH  | `/x/bookmarks/{id}/interesting` | Alterna `new` ↔ `interesting` |
| DELETE | `/x/bookmarks/{id}` | Borra el bookmark |
| POST   | `/x/bookmarks/{id}/download` | Descarga el video con yt-dlp |
| POST   | `/x/bookmarks/{id}/transcribe` | Transcribe reusando la conexión Whisper |

### Sistema

| Método | Path | Descripción |
|--------|------|-------------|
| GET    | `/health` | Host, modelo y device remotos |
| GET    | `/config` | Settings efectivos y el origen de cada uno |
| GET    | `/config/browse` | Explora el filesystem para elegir `data_root` |
| PUT    | `/config` | Guarda settings (validados antes de escribir) |

## Configuración

Todo es configurable por variables de entorno, con los valores actuales como
default, sin tocar código.

| Variable | Default | Uso |
|----------|---------|-----|
| `REMOTE_HOST` | `192.168.1.60` | Host GPU remoto |
| `REMOTE_USER` | `felipe` | Usuario SSH |
| `REMOTE_MODEL` | `large-v3-turbo` | Modelo Whisper |
| `REMOTE_DEVICE` | `cuda` | Device para Whisper |
| `REMOTE_DISK_SAFETY_MARGIN` | `3` | Múltiplo del tamaño del fichero que debe haber libre en `/tmp` remoto |
| `OLLAMA_URL` | `http://192.168.1.60:11434` | Ollama, para traducción de fallback y resúmenes |
| `OLLAMA_MODEL` | `qwen3.6:27b` | Modelo de Ollama |
| `MYMEMORY_EMAIL` | *(sin valor)* | Email opcional para subir la cuota de MyMemory |
| `FRONTEND_ORIGIN` | `http://localhost:5173` | Origen permitido por CORS |
| `DATA_ROOT` | `backend/` | Raíz de los ficheros de media: `uploads/`, `audio/`, `video/` y `x-downloads/` |
| `JOBS_DB_PATH` | `backend/jobs.db` | SQLite de los jobs |
| `X_BOOKMARKS_DB` | *(bajo el tempdir del SO)* | SQLite de los bookmarks de X |
| `X_DATA_DIR` | `backend/data/x-bookmarks/` | Sesión de Playwright y cookies de X |
| `LOG_LEVEL` | `INFO` | Nivel de log; `DEBUG` añade el detalle por ronda del scroll de X |
| `RELOAD` | activado | Autorreload del dev server |

### Precedencia: el panel gana al entorno

`data_root` y `log_level` también se editan desde el panel de configuración de la
UI, que los persiste en `backend/settings.json`. El orden es
**`settings.json` → entorno → default**: el fichero guarda lo que el usuario acaba
de escribir en el panel, así que tiene que ganar, mientras que una clave sin
guardar deja intacto lo que ya dictaba el entorno. `GET /config` devuelve el
origen de cada valor, de modo que el panel distingue un valor elegido de un mero
default.

Un `settings.json` corrupto o ausente se lee como vacío a propósito: la
configuración nunca debería ser el motivo por el que el backend no arranca. Ojo:
solo `log_level` y `x_downloads` se aplican sin reiniciar — `uploads`, `audio` y
`video` se congelan en constantes de módulo al importar.

### Por qué las bases de datos no cuelgan de `DATA_ROOT`

`DATA_ROOT` está pensado para apuntar a un disco grande o de red — un solo video
de X puede ocupar cientos de MB (uno pesó 681 MB). SQLite **no puede bloquear
sobre un share de red**: con el proyecto en un montaje CIFS, el backend ni
siquiera arranca (`database is locked`). Por eso las dos bases (`JOBS_DB_PATH`,
`X_BOOKMARKS_DB`) y las credenciales de X (`X_DATA_DIR`) conservan sus propias
variables y se quedan en disco local salvo que las muevas a propósito. Las
credenciales, además, no tienen por qué viajar junto a gigabytes de video.

## Alcance

No hay auth más allá del CORS restringido a `FRONTEND_ORIGIN`, y toda la
persistencia vive en `uploads/`, `audio/`, `video/`, `jobs.db` y el store de X.
Es una utilidad personal, no un servicio multiusuario.
