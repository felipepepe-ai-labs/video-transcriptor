# Video Transcriptor EN → ES

Transcribe videos en inglés y obtén la transcripción traducida al español, usando Whisper corriendo remotamente.

## Arquitectura

```
Frontend (React + Vite)  →  Backend (FastAPI:8000)  →  SSH → 192.168.1.60 (Whisper large-v3-turbo)
                                                                        ↓
                                                              MyMemory Translate API (EN→ES)
```

## Requisitos

- **Backend:** Python 3.12+ con SSH key-based auth a `felipe@192.168.1.60` (sin password)
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

## Endpoints

| Método | Path            | Description                                              |
|--------|-----------------|-----------------------------------------------------------|
| POST   | `/jobs`         | Subir video, encola el job y devuelve `{job_id, status}`   |
| GET    | `/jobs/{id}`    | Estado del job: `queued/running/done/failed`, `stage`, `result` |
| GET    | `/health`       | Verificar estado                                           |

## Flujo

1. Abrís `http://localhost:5173`
2. Arrastrás o seleccionás un video/audio en inglés
3. Hacés clic en "Transcribir" → el frontend hace `POST /jobs` y recibe un `job_id` al instante (no bloquea)
4. El frontend hace polling a `GET /jobs/{id}` cada ~2s mientras el job progresa por `uploading → transcribing → translating → done`
5. En background: el backend sube el archivo vía SSH a la máquina remota, corre Whisper (`large-v3-turbo`) y trae de vuelta el `.srt`
6. Traducción EN → ES vía MyMemory API (con reintentos y batching); si falla, cae automáticamente a una segunda pasada de Whisper con `--task translate` en el mismo archivo ya subido — el resultado indica qué proveedor se usó (`translation_provider`)
7. Al llegar a `done`, el frontend muestra los segmentos con timestamps, tabs para cambiar entre EN/ES
8. Podés copiar el texto completo o descargar como `.srt`

## Configuración

Todo configurable por variables de entorno (con los valores actuales como default), sin tocar código:

| Variable | Default | Uso |
|----------|---------|-----|
| `REMOTE_HOST` | `192.168.1.60` | Host GPU remoto |
| `REMOTE_USER` | `felipe` | Usuario SSH |
| `REMOTE_MODEL` | `large-v3-turbo` | Modelo Whisper |
| `REMOTE_DEVICE` | `cuda` | Device para Whisper |
| `REMOTE_DISK_SAFETY_MARGIN` | `3` | Múltiplo del tamaño del archivo que debe haber libre en `/tmp` remoto |
| `FRONTEND_ORIGIN` | `http://localhost:5173` | Origen permitido por CORS |
| `DATA_ROOT` | `backend/` | Raíz de los ficheros de media: `uploads/`, `audio/`, `video/` y `x-downloads/` |
| `LOG_LEVEL` | `INFO` | Nivel de log del backend; `DEBUG` añade el detalle por ronda del scroll de X |

### Por qué las bases de datos no cuelgan de `DATA_ROOT`

`DATA_ROOT` está pensado para apuntar a un disco grande o de red — un solo vídeo de X puede
ocupar cientos de MB. SQLite **no puede bloquear sobre un share de red**: con el proyecto en
un montaje CIFS, el backend ni siquiera arranca (`database is locked`). Por eso las dos
bases (`JOBS_DB_PATH`, `X_BOOKMARKS_DB`) y las credenciales de X (`X_DATA_DIR`) conservan
sus propias variables y se quedan en disco local salvo que las muevas a propósito.
