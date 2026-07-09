import { useEffect, useRef, useState } from "react"
import "./App.css"

const API_URL = import.meta.env.VITE_API_URL ?? "http://localhost:8000"
const POLL_INTERVAL_MS = 2000

const STAGE_LABELS = {
  uploading: "Subiendo video...",
  transcribing: "Transcribiendo con Whisper...",
  translating: "Traduciendo al español...",
  voicing: "Generando locución por segmento...",
  dubbing: "Mezclando el audio con el video...",
  splitting: "Recortando el video por capítulos...",
  done: "Listo",
}

const STATUS_ICONS = {
  queued: "⏳",
  running: "⏳",
  done: "✅",
  failed: "❌",
}

function App() {
  const [file, setFile] = useState(null)
  const [loading, setLoading] = useState(false)
  const [job, setJob] = useState(null) // { status, stage, progress, error, result }
  const [result, setResult] = useState(null)
  const [error, setError] = useState("")
  const [dragOver, setDragOver] = useState(false)
  const [tab, setTab] = useState("es") // "en" | "es"
  const [showChapters, setShowChapters] = useState(false)
  const [chapters, setChapters] = useState([])
  const [voice, setVoice] = useState("male")
  const [history, setHistory] = useState([])
  const pollRef = useRef(null)

  useEffect(() => {
    refreshHistory()
    return () => stopPolling()
  }, [])

  async function refreshHistory() {
    try {
      const res = await fetch(`${API_URL}/jobs`)
      if (!res.ok) return
      setHistory(await res.json())
    } catch {
      // history is a convenience panel; ignore failures silently
    }
  }

  async function deleteJob(jobId, e) {
    e.stopPropagation()
    if (!window.confirm("¿Eliminar este trabajo del historial?")) return
    try {
      const res = await fetch(`${API_URL}/jobs/${jobId}`, { method: "DELETE" })
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      if (job?.id === jobId) {
        setJob(null)
        setResult(null)
        stopPolling()
      }
      refreshHistory()
    } catch (e) {
      setError(e.message)
    }
  }

  async function openJob(jobId) {
    stopPolling()
    setError("")
    setResult(null)
    setJob(null)
    try {
      const res = await fetch(`${API_URL}/jobs/${jobId}`)
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      const data = await res.json()
      setJob(data)
      if (data.status === "done") {
        setResult(data.result)
        setLoading(false)
      } else if (data.status === "failed") {
        setError(data.error ?? "La transcripción falló")
        setLoading(false)
      } else {
        setLoading(true)
        pollJob(jobId)
      }
    } catch (e) {
      setError(e.message)
    }
  }

  function stopPolling() {
    if (pollRef.current) {
      clearTimeout(pollRef.current)
      pollRef.current = null
    }
  }

  function pollJob(jobId) {
    pollRef.current = setTimeout(async () => {
      try {
        const res = await fetch(`${API_URL}/jobs/${jobId}`)
        if (!res.ok) throw new Error(`HTTP ${res.status}`)
        const data = await res.json()
        setJob(data)

        if (data.status === "done") {
          setResult(data.result)
          setLoading(false)
          stopPolling()
          refreshHistory()
        } else if (data.status === "failed") {
          setError(data.error ?? "La transcripción falló")
          setLoading(false)
          stopPolling()
          refreshHistory()
        } else {
          pollJob(jobId)
        }
      } catch (e) {
        setError(e.message)
        setLoading(false)
        stopPolling()
      }
    }, POLL_INTERVAL_MS)
  }

  async function handleSubmit() {
    if (!file) return
    setLoading(true)
    setError("")
    setResult(null)
    setJob(null)
    stopPolling()

    const formData = new FormData()
    formData.append("video", file)
    formData.append("voice", voice)

    const cleanChapters = chapters
      .filter((c) => c.title.trim())
      .map((c) => ({ time: parseTimeToSeconds(c.time), title: c.title.trim() }))

    if (cleanChapters.length) {
      formData.append("chapters_json", JSON.stringify(cleanChapters))
    }

    try {
      const res = await fetch(`${API_URL}/jobs`, {
        method: "POST",
        body: formData,
      })
      if (!res.ok) {
        const err = await res.json().catch(() => ({}))
        throw new Error(err.detail ?? `HTTP ${res.status}`)
      }
      const { job_id } = await res.json()
      setJob({ status: "queued", stage: "uploading" })
      pollJob(job_id)
      refreshHistory()
    } catch (e) {
      setError(e.message)
      setLoading(false)
    }
  }

  function handleDrop(e) {
    e.preventDefault()
    setDragOver(false)
    const f = e.dataTransfer.files[0]
    if (f) setFile(f)
  }

  function addChapter() {
    setChapters([...chapters, { id: crypto.randomUUID(), time: "", title: "" }])
  }

  function updateChapter(id, field, value) {
    setChapters(chapters.map((c) => (c.id === id ? { ...c, [field]: value } : c)))
  }

  function removeChapter(id) {
    setChapters(chapters.filter((c) => c.id !== id))
  }

  function formatDate(unixSeconds) {
    if (!unixSeconds) return ""
    return new Date(unixSeconds * 1000).toLocaleString("es-AR", {
      day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit",
    })
  }

  function formatDuration(sec) {
    if (!sec) return "—"
    const m = Math.floor(sec / 60)
    const s = Math.floor(sec % 60)
    return `${m}:${s.toString().padStart(2, "0")}`
  }

  const groups = result ? groupByChapter(result.segments, result.chapters) : []

  return (
    <div className="app">
      <header>
        <h1>🎬 Video → Transcripción ES</h1>
        <p>Subí un video en inglés y obtené la transcripción con traducción al español</p>
      </header>

      {/* Upload area */}
      <section className="upload-section">
        <div
          className={`drop-zone ${dragOver ? "active" : ""}`}
          role="button"
          tabIndex={0}
          aria-label="Seleccionar video o audio"
          onDragOver={(e) => { e.preventDefault(); setDragOver(true) }}
          onDragLeave={() => setDragOver(false)}
          onDrop={handleDrop}
          onClick={() => document.getElementById("fileInput")?.click()}
          onKeyDown={(e) => {
            if (e.key === "Enter" || e.key === " ") {
              e.preventDefault()
              document.getElementById("fileInput")?.click()
            }
          }}
        >
          <input
            id="fileInput"
            type="file"
            accept="video/*,audio/*"
            hidden
            onChange={(e) => e.target.files[0] && setFile(e.target.files[0])}
          />
          {file ? (
            <div className="file-info">
              <span className="file-icon">📁</span>
              <strong>{file.name}</strong>
              <small>{(file.size / 1048576).toFixed(1)} MB</small>
            </div>
          ) : (
            <>
              <span className="upload-icon">☁️</span>
              <strong>Arrastrá tu video acá</strong>
              <small>o hacé clic para seleccionar</small>
            </>
          )}
        </div>

        {/* Voice selection */}
        <div className="voice-picker">
          <span className="voice-picker-label">Voz de la locución:</span>
          <label className="voice-option">
            <input
              type="radio"
              name="voice"
              value="male"
              checked={voice === "male"}
              onChange={() => setVoice("male")}
            />
            Masculina
          </label>
          <label className="voice-option">
            <input
              type="radio"
              name="voice"
              value="female"
              checked={voice === "female"}
              onChange={() => setVoice("female")}
            />
            Femenina
          </label>
        </div>

        {/* Chapters (optional) */}
        <details
          className="chapters-editor"
          open={showChapters}
          onToggle={(e) => setShowChapters(e.target.open)}
        >
          <summary>📑 Capítulos (opcional) {chapters.length > 0 && `· ${chapters.length}`}</summary>

          <div className="chapters-list">
            {chapters.map((ch) => (
              <div key={ch.id} className="chapter-row-edit">
                <input
                  type="text"
                  placeholder="00:00"
                  value={ch.time}
                  onChange={(e) => updateChapter(ch.id, "time", e.target.value)}
                  className="chapter-time-input"
                />
                <input
                  type="text"
                  placeholder="Título del capítulo"
                  value={ch.title}
                  onChange={(e) => updateChapter(ch.id, "title", e.target.value)}
                  className="chapter-title-input"
                />
                <button
                  type="button"
                  className="chapter-remove"
                  onClick={() => removeChapter(ch.id)}
                  aria-label="Eliminar capítulo"
                >
                  ✕
                </button>
              </div>
            ))}
          </div>

          <button type="button" className="btn-add-chapter" onClick={addChapter}>
            + Agregar capítulo
          </button>
          <p className="chapters-hint">
            Formato: <code>MM:SS</code> o <code>H:MM:SS</code>, ej. <code>36:44</code> o <code>1:21:25</code>
          </p>
        </details>

        <button
          className="btn-primary"
          disabled={!file || loading}
          onClick={handleSubmit}
        >
          {loading ? (
            <span className="spinner">⏳ Transcribiendo…</span>
          ) : (
            "Transcribir"
          )}
        </button>

        {error && <div className="error-banner">❌ {error}</div>}
      </section>

      {/* History */}
      {history.length > 0 && (
        <section className="history-section">
          <h2 className="history-heading">Historial</h2>
          <ul className="history-list">
            {history.map((h) => (
              <li key={h.id}>
                <button
                  className={`history-item ${job && h.id === job.id ? "active" : ""}`}
                  onClick={() => openJob(h.id)}
                >
                  <span className={`history-status status-${h.status}`}>
                    {STATUS_ICONS[h.status] ?? "•"}
                  </span>
                  <span className="history-filename">{h.filename}</span>
                  <span className="history-date">{formatDate(h.created_at)}</span>
                </button>
                <button
                  className="history-delete"
                  onClick={(e) => deleteJob(h.id, e)}
                  aria-label="Eliminar trabajo"
                  title="Eliminar"
                >
                  🗑️
                </button>
              </li>
            ))}
          </ul>
        </section>
      )}

      {/* Results */}
      {result && (
        <section className="results">
          <div className="meta-bar">
            <span>📄 {result.filename}</span>
            <span>⏱ {formatDuration(result.duration_seconds)}</span>
            <span>🧩 {result.segments.length} segmentos</span>
            {result.chapters?.length > 0 && <span>📑 {result.chapters.length} capítulos</span>}
          </div>

          {/* Narration audio */}
          {result.audio_available && job?.id && (
            <div className="audio-player">
              <audio controls src={`${API_URL}/jobs/${job.id}/audio`} />
              <a
                className="audio-download"
                href={`${API_URL}/jobs/${job.id}/audio`}
                download={`locucion.${result.voice}.wav`}
              >
                ⬇️ Descargar audio
              </a>
            </div>
          )}
          {result.audio_error && (
            <div className="audio-error">🔇 No se pudo generar la locución: {result.audio_error}</div>
          )}

          {/* Dubbed video */}
          {result.dubbed_video_available && job?.id && (
            <div className="dubbed-video">
              <video controls src={`${API_URL}/jobs/${job.id}/video`} />
              <a
                className="audio-download"
                href={`${API_URL}/jobs/${job.id}/video`}
                download={`${result.filename}.dubbed.mp4`}
              >
                ⬇️ Descargar video doblado
              </a>
            </div>
          )}
          {result.dubbed_video_error && (
            <div className="audio-error">🎬 No se pudo generar el video doblado: {result.dubbed_video_error}</div>
          )}

          {/* Chapter jump nav + per-chapter clip download */}
          {result.chapters?.length > 0 && (
            <nav className="chapter-nav">
              {result.chapters.map((ch, i) => (
                <span key={i} className="chapter-pill-group">
                  <a href={`#chapter-${i}`} className="chapter-pill">
                    <span className="pill-ts">{ch.timestamp}</span> {ch.title}
                  </a>
                  {result.chapter_clips_available && job?.id && (
                    <a
                      className="chapter-clip-download"
                      href={`${API_URL}/jobs/${job.id}/chapters/${i}/video`}
                      download={`${result.filename}.${ch.title}.mp4`}
                      title="Descargar este capítulo como video"
                      aria-label={`Descargar capítulo ${ch.title}`}
                    >
                      ⬇️
                    </a>
                  )}
                </span>
              ))}
            </nav>
          )}
          {result.chapter_clips_error && (
            <div className="audio-error">✂️ No se pudieron recortar los capítulos: {result.chapter_clips_error}</div>
          )}

          <div className="tabs">
            <button
              className={tab === "es" ? "active" : ""}
              onClick={() => setTab("es")}
            >
              Español
            </button>
            <button
              className={tab === "en" ? "active" : ""}
              onClick={() => setTab("en")}
            >
              English
            </button>
          </div>

          {/* Long transcripts (many chapters, thousands of segments) collapse
              every chapter but the first, so the DOM isn't flooded with rows
              the user hasn't scrolled to yet. */}
          {groups.map((group, gi) =>
            group.title ? (
              <details key={gi} className="chapter-block" id={group.id} open={gi === 0 || groups.length <= 3}>
                <summary className="chapter-heading">
                  <span className="chapter-heading-ts">{group.timestamp}</span>
                  {group.title}
                  <span className="chapter-heading-count">{group.segments.length}</span>
                </summary>
                <div className="segments">
                  {group.segments.map((seg) => (
                    <div key={seg.index} className="segment-row">
                      <span className="ts" title={`${seg.start}s → ${seg.end}s`}>
                        {formatTs(seg.start)}
                      </span>
                      <span className="text">{tab === "es" ? seg.text_es : seg.text_en}</span>
                    </div>
                  ))}
                </div>
              </details>
            ) : (
              <div key={gi} className="chapter-block" id={group.id}>
                <div className="segments">
                  {group.segments.map((seg) => (
                    <div key={seg.index} className="segment-row">
                      <span className="ts" title={`${seg.start}s → ${seg.end}s`}>
                        {formatTs(seg.start)}
                      </span>
                      <span className="text">{tab === "es" ? seg.text_es : seg.text_en}</span>
                    </div>
                  ))}
                </div>
              </div>
            )
          )}

          {/* Full text */}
          <details className="full-text">
            <summary>Vista completa ({tab === "es" ? "ES" : "EN"})</summary>
            <pre>{tab === "es" ? result.full_text_es : result.full_text_en}</pre>
          </details>

          {/* Copy buttons */}
          <div className="actions">
            <button onClick={() => {
              navigator.clipboard.writeText(
                tab === "es" ? result.full_text_es : result.full_text_en
              )
            }}>
              📋 Copiar texto
            </button>
            <button onClick={() => downloadSrt(result.segments, tab)}>
              ⬇️ Descargar SRT
            </button>
          </div>
        </section>
      )}

      {loading && (
        <div className="status-message">
          <span className="spinner"></span>
          {STAGE_LABELS[job?.stage] ?? "Enviando video a Whisper..."}
          {(job?.stage === "translating" || job?.stage === "voicing") && job?.segments_total > 0 && (
            <div className="progress-wrap">
              <div className="progress-bar">
                <div
                  className="progress-bar-fill"
                  style={{ width: `${(job.segments_done / job.segments_total) * 100}%` }}
                />
              </div>
              <span className="progress-bar-label">
                {job.segments_done} / {job.segments_total} segmentos
              </span>
            </div>
          )}
        </div>
      )}
    </div>
  )
}

/* ── Utilities ─────────────────────────────────────────────────────── */

function formatTs(ts) {
  // "00:00:01.234" → "01.2s"
  const parts = ts.split(":")
  const sec = parseFloat(parts[2] || "0")
  const m = parseInt(parts[0] || "0", 10)
  const s = parseInt(parts[1] || "0", 10)
  if (m === 0 && s < 60) return `${sec.toFixed(1)}s`
  return `${m}m ${s}s`
}

function parseTimeToSeconds(input) {
  // "36:44" → 2204, "1:21:25" → 4885
  const parts = input.split(":").map((p) => parseInt(p, 10) || 0)
  if (parts.length === 3) return parts[0] * 3600 + parts[1] * 60 + parts[2]
  if (parts.length === 2) return parts[0] * 60 + parts[1]
  return parts[0] || 0
}

function groupByChapter(segments, chapters) {
  if (!chapters || chapters.length === 0) {
    return [{ id: null, title: null, timestamp: null, segments }]
  }

  const sorted = [...chapters].sort((a, b) => a.time - b.time)
  return sorted.map((ch, i) => {
    const nextTime = sorted[i + 1]?.time ?? Infinity
    const segs = segments.filter((seg) => {
      const segSec = tsStringToSeconds(seg.start)
      return segSec >= ch.time && segSec < nextTime
    })
    return {
      id: `chapter-${i}`,
      title: ch.title,
      timestamp: ch.timestamp,
      segments: segs,
    }
  }).filter((g) => g.segments.length > 0)
}

function tsStringToSeconds(ts) {
  const parts = ts.split(":")
  const h = parseInt(parts[0] || "0", 10)
  const m = parseInt(parts[1] || "0", 10)
  const s = parseFloat(parts[2] || "0")
  return h * 3600 + m * 60 + s
}

function downloadSrt(segments, lang) {
  const key = lang === "es" ? "text_es" : "text_en"
  const text = segments.map((seg, i) => {
    const start = seg.start.replace(".", ",")
    const end = seg.end.replace(".", ",")
    return `${i + 1}\n${start} --> ${end}\n${seg[key]}`
  }).join("\n\n")

  const blob = new Blob([text], { type: "text/plain" })
  const url = URL.createObjectURL(blob)
  const a = document.createElement("a")
  a.href = url
  a.download = `transcripcion.${lang}.srt`
  a.click()
  URL.revokeObjectURL(url)
}

export default App
