import { useEffect, useRef, useState } from "react"
import "./App.css"
import XBookmarks from "./XBookmarks.jsx"
import Settings from "./Settings.jsx"
import VoicePicker from "./VoicePicker.jsx"
import ChaptersEditor from "./ChaptersEditor.jsx"
import { cleanChapters } from "./chapters.js"
import { STAGE_LABELS } from "./stages.js"

const API_URL = ""  // relative so it always goes through Vite's proxy, regardless of LAN address
const POLL_INTERVAL_MS = 2000

const STATUS_ICONS = {
  queued: "⏳",
  running: "⏳",
  done: "✅",
  failed: "❌",
}

function App() {
  const [inputMode, setInputMode] = useState("file") // "file" | "youtube" | "x"
  const [youtubeUrl, setYoutubeUrl] = useState("")
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
  const [uploadProgress, setUploadProgress] = useState(null) // 0-100 while sending, null otherwise
  const [regenVoice, setRegenVoice] = useState("male")
  const [chapterAudioReady, setChapterAudioReady] = useState({}) // { [chapterIndex]: true }
  const [chapterAudioLoading, setChapterAudioLoading] = useState(null) // chapterIndex currently generating, or null
  const [playingChapterVideo, setPlayingChapterVideo] = useState(null) // chapterIndex playing inline video, or null
  const [summaryLoading, setSummaryLoading] = useState(null) // jobId currently generating summary, or null
  const pollRef = useRef(null)

  useEffect(() => {
    refreshHistory()
    return () => stopPolling()
  }, [])

  useEffect(() => {
    if (result?.voice) setRegenVoice(result.voice)
    // A Spanish-source video has no EN text to show -- force the ES tab.
    if (result?.source_language === "es") setTab("es")
  }, [result])

  useEffect(() => {
    setChapterAudioReady({})
    setChapterAudioLoading(null)
  }, [job?.id])

  useEffect(() => {
    // Covers the "failed" path too, not just success -- otherwise a failed
    // chapter-audio generation left the button stuck showing its spinner.
    if (job && job.status !== "running") setChapterAudioLoading(null)
  }, [job])

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

  function pollJob(jobId, onDone) {
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
          onDone?.()
        } else if (data.status === "failed") {
          setError(data.error ?? "La transcripción falló")
          setLoading(false)
          stopPolling()
          refreshHistory()
        } else {
          pollJob(jobId, onDone)
        }
      } catch (e) {
        setError(e.message)
        setLoading(false)
        stopPolling()
      }
    }, POLL_INTERVAL_MS)
  }

  async function handleSubmit() {
    if (inputMode === "file" ? !file : !youtubeUrl.trim()) return
    setLoading(true)
    setError("")
    setResult(null)
    setJob(null)
    stopPolling()

    const formData = new FormData()
    formData.append("voice", voice)

    const cleaned = cleanChapters(chapters)
    if (cleaned.length) {
      formData.append("chapters_json", JSON.stringify(cleaned))
    }

    if (inputMode === "youtube") {
      formData.append("url", youtubeUrl.trim())
      try {
        const res = await fetch(`${API_URL}/jobs/youtube`, { method: "POST", body: formData })
        const body = await res.json().catch(() => ({}))
        if (!res.ok) throw new Error(body.detail ?? `HTTP ${res.status}`)
        setJob({ status: "queued", stage: "downloading" })
        pollJob(body.job_id)
        refreshHistory()
      } catch (e) {
        setError(e.message)
        setLoading(false)
      }
      return
    }

    formData.append("video", file)
    setUploadProgress(0)
    try {
      const { job_id } = await uploadWithProgress(formData, setUploadProgress)
      setUploadProgress(null)
      setJob({ status: "queued", stage: "uploading" })
      pollJob(job_id)
      refreshHistory()
    } catch (e) {
      setUploadProgress(null)
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

  async function retts(url, voiceValue) {
    if (!job?.id) return
    setLoading(true)
    setError("")
    const formData = new FormData()
    if (voiceValue) formData.append("voice", voiceValue)
    try {
      const res = await fetch(url, { method: "POST", body: formData })
      if (!res.ok) {
        const err = await res.json().catch(() => ({}))
        throw new Error(err.detail ?? `HTTP ${res.status}`)
      }
      pollJob(job.id)
    } catch (e) {
      setError(e.message)
      setLoading(false)
    }
  }

  function regenerateNarration() {
    retts(`${API_URL}/jobs/${job.id}/retts`, regenVoice)
  }

  function regenerateChapter(index) {
    retts(`${API_URL}/jobs/${job.id}/chapters/${index}/retts`)
  }

  async function generateChapterAudio(index) {
    if (!job?.id) return
    setLoading(true)
    setChapterAudioLoading(index)
    setError("")
    try {
      const res = await fetch(`${API_URL}/jobs/${job.id}/chapters/${index}/audio`, { method: "POST" })
      if (!res.ok) {
        const err = await res.json().catch(() => ({}))
        throw new Error(err.detail ?? `HTTP ${res.status}`)
      }
      pollJob(job.id, () => {
        setChapterAudioReady((prev) => ({ ...prev, [index]: true }))
        setChapterAudioLoading(null)
      })
    } catch (e) {
      setError(e.message)
      setLoading(false)
      setChapterAudioLoading(null)
    }
  }

  async function generateSummary(jobId, btnEl) {
    setSummaryLoading(jobId)
    setError("")
    try {
      const res = await fetch(`${API_URL}/jobs/${jobId}/summarize`, { method: "POST" })
      if (!res.ok) throw new Error(`HTTP ${res.status}`)

      // Poll until summary appears on this job.
      const poll = setInterval(async () => {
        const jRes = await fetch(`${API_URL}/jobs/${jobId}`)
        const jData = await jRes.json()
        if (jData.result?.summary_es) {
          clearInterval(poll)
          setSummaryLoading(null)
          openJob(jobId)
          refreshHistory()
        }
      }, 2000)

      // Timeout after 5 minutes.
      setTimeout(() => { clearInterval(poll); setSummaryLoading(null) }, 300_000)
    } catch (e) {
      setError(e.message)
      setSummaryLoading(null)
    }
  }

  function formatDate(unixSeconds) {
    if (!unixSeconds) return ""
    return new Date(unixSeconds * 1000).toLocaleString("es-ES", {
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
  // Spanish-source videos (YouTube auto-detect) have no translation and no
  // narration/dub -- hide every EN/TTS affordance for them.
  const spanishSource = result?.source_language === "es"

  return (
    <div className="app">
      <header>
        <h1>🎬 Video → Transcripción ES</h1>
        <p>Sube un video o pega una URL de YouTube: transcripción, traducción al español y doblaje</p>
      </header>

      {/* Upload area */}
      <section className="upload-section">
        <div className="input-mode-toggle" role="tablist">
          <button
            type="button"
            role="tab"
            aria-selected={inputMode === "file"}
            className={inputMode === "file" ? "active" : ""}
            onClick={() => setInputMode("file")}
          >
            📁 Archivo
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={inputMode === "youtube"}
            className={inputMode === "youtube" ? "active" : ""}
            onClick={() => setInputMode("youtube")}
          >
            ▶️ YouTube
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={inputMode === "x"}
            className={inputMode === "x" ? "active" : ""}
            onClick={() => setInputMode("x")}
          >
            🐦 X
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={inputMode === "settings"}
            className={inputMode === "settings" ? "active" : ""}
            onClick={() => setInputMode("settings")}
          >
            ⚙️ Ajustes
          </button>
        </div>

        {inputMode === "youtube" && (
          <div className="youtube-input">
            <input
              type="url"
              placeholder="https://www.youtube.com/watch?v=..."

              value={youtubeUrl}
              onChange={(e) => setYoutubeUrl(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter") handleSubmit() }}
              aria-label="URL del video de YouTube"
            />
            <p className="youtube-hint">
              El idioma se detecta automáticamente: un video en inglés se traduce y dobla;
              uno en español solo se transcribe. Si el video tiene capítulos propios, se usan.
            </p>
          </div>
        )}

        {inputMode === "file" && (
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
              <strong>Arrastra tu video aquí</strong>
              <small>o haz clic para seleccionar</small>
            </>
          )}
        </div>
        )}

        {/* Voice, chapters and the submit button only belong to the two
            transcription modes; the X and settings panels drive themselves. */}
        {(inputMode === "file" || inputMode === "youtube") && (
        <>
        <VoicePicker value={voice} onChange={setVoice} />

        <ChaptersEditor
          chapters={chapters}
          onChange={setChapters}
          open={showChapters}
          onToggle={(e) => setShowChapters(e.target.open)}
        />

        <button
          className="btn-primary"
          disabled={(inputMode === "file" ? !file : !youtubeUrl.trim()) || loading}
          onClick={handleSubmit}
        >
          {loading ? (
            <span className="spinner">
              {uploadProgress !== null ? `Subiendo… ${uploadProgress}%` : "Transcribiendo…"}
            </span>
          ) : (
            "Transcribir"
          )}
        </button>

        {uploadProgress !== null && (
          <div className="progress-wrap">
            <div className="progress-bar">
              <div className="progress-bar-fill" style={{ width: `${uploadProgress}%` }} />
            </div>
            <span className="progress-bar-label">Subiendo video: {uploadProgress}%</span>
          </div>
        )}
        </>
        )}

        {error && <div className="error-banner">❌ {error}</div>}
      </section>

      {/* X Bookmarks panel */}
      {inputMode === "x" && (
        <XBookmarks
          onOpenJob={(jobId) => {
            // Same pipeline, same results screen -- and it already renders
            // under whichever tab is open, so stay on X rather than dumping
            // the user on the upload tab and losing their place in the list.
            openJob(jobId)
            requestAnimationFrame(() =>
              document.querySelector(".results, .status-message")
                ?.scrollIntoView({ behavior: "smooth", block: "start" })
            )
          }}
        />
      )}
      {inputMode === "settings" && <Settings />}

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
                  <span className="history-source" title={h.source === "youtube" ? "Video de YouTube" : "Archivo subido"}>
                    {h.source === "youtube" ? "▶️" : "📁"}
                  </span>
                  <span className="history-filename">{h.title || h.filename}</span>
                  <span className="history-date">{formatDate(h.created_at)}</span>
                </button>
                {h.status === "done" && !h.summary_es && (
                  <button
                    className="history-generate-summary"
                    onClick={(e) => { e.stopPropagation(); generateSummary(h.id, e); }}
                    title="Generar resumen"
                    disabled={summaryLoading === h.id}
                  >
                    {summaryLoading === h.id ? "⏳" : "📝"}
                  </button>
                )}
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
            {result.source === "youtube" && result.url && (
              <a href={result.url} target="_blank" rel="noreferrer">▶️ Ver en YouTube</a>
            )}
            {result.source_language === "es" && <span>🌐 Video en español (sin traducir)</span>}
            <span>⏱ {formatDuration(result.duration_seconds)}</span>
            <span>🧩 {result.segments.length} segmentos</span>
            {result.chapters?.length > 0 && <span>📑 {result.chapters.length} capítulos</span>}
          </div>

          {/* Summary */}
          {result.summary_es && (
            <details className="summary-panel" open>
              <summary>📝 Resumen</summary>
              <p className="summary-text">{result.summary_es}</p>
            </details>
          )}

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

          {/* Regenerate narration (whole job) */}
          <div className="regen-narration">
            <label className="voice-option">
              <input
                type="radio"
                name="regenVoice"
                value="male"
                checked={regenVoice === "male"}
                onChange={() => setRegenVoice("male")}
              />
              Masculina
            </label>
            <label className="voice-option">
              <input
                type="radio"
                name="regenVoice"
                value="female"
                checked={regenVoice === "female"}
                onChange={() => setRegenVoice("female")}
              />
              Femenina
            </label>
            <button
              type="button"
              className="btn-regen"
              disabled={loading}
              onClick={regenerateNarration}
            >
              🔁 Regenerar locución
            </button>
          </div>

          {/* Chapter jump nav + per-chapter clip download */}
          {result.chapters?.length > 0 && (
            <nav className="chapter-nav">
              {result.chapters.map((ch, i) => (
                <span key={i} className="chapter-pill-group">
                  <a href={`#chapter-${i}`} className="chapter-pill">
                    <span className="pill-ts">{ch.timestamp}</span> {ch.title}
                  </a>
                  {result.chapter_clips_available && job?.id && (
                    <>
                      <button
                        type="button"
                        className="chapter-clip-play"
                        onClick={(e) => {
                          e.preventDefault();
                          setPlayingChapterVideo(playingChapterVideo === i ? null : i);
                        }}
                        title={playingChapterVideo === i ? "Detener reproducción" : "Reproducir capítulo"}
                        aria-label={playingChapterVideo === i ? `Detener ${ch.title}` : `Reproducir ${ch.title}`}
                      >
                        {playingChapterVideo === i ? "⏸️" : "▶️"}
                      </button>
                      <a
                        className="chapter-clip-download"
                        href={`${API_URL}/jobs/${job.id}/chapters/${i}/video`}
                        download={`${result.filename}.${ch.title}.mp4`}
                        title="Descargar este capítulo como video"
                        aria-label={`Descargar capítulo ${ch.title}`}
                      >
                        ⬇️
                      </a>
                    </>
                  )}
                  <button
                    type="button"
                    className="chapter-clip-regen"
                    disabled={loading}
                    onClick={() => regenerateChapter(i)}
                    title="Regenerar la locución de este capítulo"
                    aria-label={`Regenerar locución del capítulo ${ch.title}`}
                  >
                    🔁
                  </button>
                </span>
              ))}
            </nav>
          )}
          {result.chapter_clips_error && (
            <div className="audio-error">✂️ No se pudieron recortar los capítulos: {result.chapter_clips_error}</div>
          )}

          {!spanishSource && (
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
          )}

          {/* Long transcripts (many chapters, thousands of segments) collapse
              every chapter but the first, so the DOM isn't flooded with rows
              the user hasn't scrolled to yet. */}
          {groups.map((group, gi) =>
            group.title ? (
              <div key={gi}>
                <details
                  className="chapter-block"
                  id={group.id}
                  open={gi === 0 || groups.length <= 3}
                  onToggle={(e) => {
                    if (!e.target.open && playingChapterVideo === group.chapterIndex) {
                      setPlayingChapterVideo(null);
                    }
                  }}
                >
                  <summary className="chapter-heading">
                    <span className="chapter-heading-ts">{group.timestamp}</span>
                    {group.title}
                    <span className="chapter-heading-count">{group.segments.length}</span>
                    <button
                      type="button"
                      className="chapter-tts-btn"
                      disabled={loading}
                      onClick={(e) => { e.preventDefault(); e.stopPropagation(); generateChapterAudio(group.chapterIndex) }}
                      title="Generar audio de este capítulo"
                    >
                      {chapterAudioLoading === group.chapterIndex ? "⏳" : "🔊"} Generar audio
                    </button>
                  </summary>
                  {playingChapterVideo === group.chapterIndex && result.chapter_clips_available && job?.id && (
                    <div className="chapter-video-player">
                      <video
                        controls
                        src={`${API_URL}/jobs/${job.id}/chapters/${group.chapterIndex}/video`}
                        onEnded={() => setPlayingChapterVideo(null)}
                      />
                    </div>
                  )}
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
                {/* Rendered outside <details> so it stays visible even when
                    this chapter is collapsed -- <details> content besides
                    <summary> is hidden by the browser while closed.
                    `result.chapter_audio_indexes` (persisted on the job) is
                    the source of truth -- `chapterAudioReady` only covers
                    the same-session "just generated it" case before the
                    next poll response lands. */}
                {(chapterAudioReady[group.chapterIndex] || result.chapter_audio_indexes?.includes(group.chapterIndex)) && (
                  <div className="chapter-audio-player">
                    <audio controls src={`${API_URL}/jobs/${job.id}/chapters/${group.chapterIndex}/audio`} />
                  </div>
                )}
              </div>
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
            <button onClick={() => downloadSrt(result.segments, tab, result.filename)}>
              ⬇️ Descargar SRT
            </button>
          </div>
        </section>
      )}

      {loading && uploadProgress === null && (
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
          {/* Sending the file to the GPU box takes minutes for a large video;
              a bare label here reads the same whether it is moving or wedged. */}
          {job?.stage === "uploading" && job?.progress > 0 && (
            <div className="progress-wrap">
              <div className="progress-bar">
                <div className="progress-bar-fill" style={{ width: `${job.progress}%` }} />
              </div>
              <span className="progress-bar-label">
                Enviando al servidor de transcripción: {Math.round(job.progress)}%
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
  // "00:00:01.234" → "1.2s", "00:05:30.123" → "5m 30s", "01:02:03.456" → "1h 2m"
  const parts = ts.split(":")
  const sec = parseFloat(parts[2] || "0")
  const m = parseInt(parts[0] || "0", 10)
  const s = parseInt(parts[1] || "0", 10)
  if (m === 0 && s === 0) return `${sec.toFixed(1)}s`
  if (m > 0) return `${m}h ${s}m`
  return `${s}m ${Math.floor(sec)}s`
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
      chapterIndex: i,
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

function uploadWithProgress(formData, onProgress) {
  // fetch() has no cross-browser event for upload (request body) progress,
  // only for the response -- XMLHttpRequest is the only way to report how
  // much of a large video has actually left the browser.
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest()
    xhr.open("POST", `${API_URL}/jobs`)

    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable) onProgress(Math.round((e.loaded / e.total) * 100))
    }

    xhr.onload = () => {
      let body = {}
      try { body = JSON.parse(xhr.responseText) } catch { /* non-JSON error body */ }
      if (xhr.status >= 200 && xhr.status < 300) {
        resolve(body)
      } else {
        reject(new Error(body.detail ?? `HTTP ${xhr.status}`))
      }
    }

    xhr.onerror = () => reject(new Error("No se pudo conectar con el servidor"))
    xhr.send(formData)
  })
}

/** Filesystem-safe stem from a video's name, so three exports do not collide. */
function srtStem(filename) {
  const stem = (filename ?? "").replace(/\.[^.]+$/, "")
  const slug = stem
    .normalize("NFKD")
    .replace(/[^\w\s-]/g, "")
    .trim()
    .replace(/[\s_]+/g, "-")
    .slice(0, 60)
    .replace(/-+$/, "")
    .toLowerCase()
  return slug || "transcripcion"
}

function downloadSrt(segments, lang, filename) {
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
  a.download = `${srtStem(filename)}.${lang}.srt`
  a.click()
  URL.revokeObjectURL(url)
}

export default App
