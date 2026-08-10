import { useEffect, useState } from "react"

const API_URL = import.meta.env.VITE_API_URL ?? "http://localhost:8000"

const LOG_LEVELS = ["CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"]

// Where a value comes from, so a deliberate choice doesn't look like a default.
const ORIGIN_LABELS = {
  settings: "guardado aquí",
  environment: "variable de entorno",
  default: "valor por defecto",
}

const DIRECTORY_LABELS = {
  uploads: "Subidas",
  audio: "Narraciones",
  video: "Vídeos doblados",
  x_downloads: "Descargas de X",
}

function Settings() {
  const [config, setConfig] = useState(null)
  const [dataRoot, setDataRoot] = useState("")
  const [logLevel, setLogLevel] = useState("INFO")
  const [fieldErrors, setFieldErrors] = useState({})
  const [error, setError] = useState("")
  const [notice, setNotice] = useState("")
  const [saving, setSaving] = useState(false)
  // Folder browser: the server has to list directories because the browser
  // never reveals a real filesystem path.
  const [browser, setBrowser] = useState(null)
  const [browsing, setBrowsing] = useState(false)

  useEffect(() => { loadConfig() }, [])

  async function browseTo(path) {
    setBrowsing(true)
    setError("")
    try {
      const url = new URL(`${API_URL}/config/browse`)
      if (path) url.searchParams.set("path", path)
      const res = await fetch(url)
      const body = await res.json()
      if (!res.ok) throw new Error(body.detail ?? `HTTP ${res.status}`)
      setBrowser(body)
    } catch (e) {
      setError(e.message)
    } finally {
      setBrowsing(false)
    }
  }

  async function loadConfig() {
    setError("")
    try {
      const res = await fetch(`${API_URL}/config`)
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      const body = await res.json()
      setConfig(body)
      setDataRoot(body.data_root.value)
      setLogLevel(body.log_level.value)
    } catch (e) {
      setError(`No se pudo leer la configuración: ${e.message}`)
    }
  }

  async function save(changes) {
    setSaving(true)
    setFieldErrors({})
    setError("")
    setNotice("")
    try {
      const res = await fetch(`${API_URL}/config`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(changes),
      })
      const body = await res.json()
      if (!res.ok) {
        // The backend answers {detail: {campo: motivo}} so each message can sit
        // next to the input that caused it.
        if (body.detail && typeof body.detail === "object") setFieldErrors(body.detail)
        else setError(body.detail ?? `HTTP ${res.status}`)
        return
      }
      setConfig(body)
      setDataRoot(body.data_root.value)
      setLogLevel(body.log_level.value)
      setNotice(
        body.restart_required?.length
          ? `Guardado. Las descargas de X ya usan la ruta nueva; ${body.restart_required.join(", ")} necesitan reiniciar el backend.`
          : "Guardado."
      )
      setTimeout(() => setNotice(""), 8000)
    } catch (e) {
      setError(e.message)
    } finally {
      setSaving(false)
    }
  }

  const unsaved = Boolean(config) && dataRoot.trim() && dataRoot !== config.data_root.value

  if (!config) {
    return (
      <div className="settings-panel">
        {error ? <p className="settings-error">{error}</p> : <p>Cargando configuración…</p>}
      </div>
    )
  }

  return (
    <div className="settings-panel">
      {error && <p className="settings-error">{error}</p>}
      {notice && <p className="settings-notice">{notice}</p>}

      <div className="settings-field">
        <label htmlFor="data-root">Carpeta raíz de los ficheros</label>
        <p className="settings-hint">
          Ruta absoluta donde se guardan vídeos, audios y descargas. Útil para sacarlos a
          un disco con más espacio: un solo vídeo de X puede ocupar cientos de MB.
        </p>
        <div className="settings-row">
          <input
            id="data-root"
            type="text"
            value={dataRoot}
            onChange={(e) => setDataRoot(e.target.value)}
            placeholder="/mnt/disco/video-transcriptor"
            spellCheck={false}
          />
          <button
            type="button"
            className="btn-secondary"
            disabled={browsing}
            onClick={() => (browser ? setBrowser(null) : browseTo(dataRoot.trim() || null))}
          >
            {/* Listing a network share takes seconds; without this the button
                looks dead and invites a second click. */}
            {browsing ? "Abriendo…" : browser ? "Cerrar" : "Examinar…"}
          </button>
          <button
            type="button"
            className="btn-primary"
            disabled={saving || !unsaved}
            title={unsaved ? "Guardar la carpeta" : "No hay cambios que guardar"}
            onClick={() => save({ data_root: dataRoot.trim() })}
          >
            Guardar
          </button>
          {!unsaved && <span className="settings-origin">sin cambios</span>}
        </div>

        {browser && (
          <div className="settings-browser">
            <div className="settings-browser-head">
              <button
                type="button"
                className="btn-secondary"
                disabled={!browser.parent || browsing}
                onClick={() => browseTo(browser.parent)}
              >
                ⬆ Subir
              </button>
              <code>{browser.path}</code>
            </div>
            <ul className="settings-browser-list" aria-busy={browsing}>
              {browsing && <li className="settings-browser-empty">Cargando…</li>}
              {!browsing && browser.entries.length === 0 && (
                <li className="settings-browser-empty">No hay subcarpetas aquí</li>
              )}
              {browser.entries.map((entry) => (
                <li key={entry.path}>
                  <button type="button" onClick={() => browseTo(entry.path)}>
                    📁 {entry.name}
                  </button>
                </li>
              ))}
            </ul>
            <button
              type="button"
              className="btn-primary"
              onClick={() => {
                setDataRoot(browser.path)
                setBrowser(null)
              }}
            >
              Usar esta carpeta
            </button>
          </div>
        )}

        {fieldErrors.data_root && <p className="settings-error">{fieldErrors.data_root}</p>}
        <p className="settings-origin">Origen: {ORIGIN_LABELS[config.data_root.origin]}</p>
      </div>

      <div className="settings-field">
        <label htmlFor="log-level">Nivel de log del backend</label>
        <p className="settings-hint">
          DEBUG añade el detalle ronda a ronda del scroll de X y la salida de yt-dlp.
          Se aplica al instante, sin reiniciar.
        </p>
        <div className="settings-row">
          <select
            id="log-level"
            value={logLevel}
            onChange={(e) => {
              setLogLevel(e.target.value)
              save({ log_level: e.target.value })
            }}
            disabled={saving}
          >
            {LOG_LEVELS.map((level) => (
              <option key={level} value={level}>{level}</option>
            ))}
          </select>
        </div>
        {fieldErrors.log_level && <p className="settings-error">{fieldErrors.log_level}</p>}
        <p className="settings-origin">Origen: {ORIGIN_LABELS[config.log_level.origin]}</p>
      </div>

      <div className="settings-field">
        <label>Carpetas en uso</label>
        <p className="settings-hint">
          Rutas reales que está usando el backend ahora mismo.
        </p>
        <ul className="settings-dirs">
          {Object.entries(config.directories).map(([key, path]) => (
            <li key={key}>
              <span className="settings-dir-name">{DIRECTORY_LABELS[key] ?? key}</span>
              <span className="settings-dir-path">
                <code>{path}</code>
                {!config.applies_immediately.includes(key) && (
                  <span className="settings-restart-tag" title="Cambiar la carpeta raíz no mueve ésta hasta reiniciar el backend">
                    requiere reiniciar
                  </span>
                )}
              </span>
            </li>
          ))}
        </ul>
      </div>

      <p className="settings-footnote">
        Las bases de datos y las cookies de X no se mueven con esta carpeta: SQLite no
        funciona sobre unidades de red, así que se quedan en disco local.
      </p>
    </div>
  )
}

export default Settings
