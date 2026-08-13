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

function App() {
  const [inputMode, setInputMode] = useState("file") // "file" | "youtube" | "videos" | "x" | "settings"
  const [youtubeUrl, setYoutubeUrl] = useState("")
  const [file, setFile] = useState(null)
  const [loading, setLoading] = useState(false)
  const [job, setJob] = useState(null) // { status, stage, progress, error, result }
  const [result, setResult] = useState(null)
  const [error, setError] = useState("")
  const [dragOver, setDragOver] = useState(false)
  const [tab, setTab] = useState("es") // "en" | "es"
  // Videos procesados: history list from GET /jobs. Opening a card loads it
  // into the main results viewer below, so there is no second detail view.
  const [allJobs, setAllJobs] = useState([])
  const [videosLoading, setVideosLoading] = useState(false)
  const [videosError, setVideosError] = useState("")
  // Set when the user explicitly asks to see a job (the 📄 button on an X
  // bookmark, a history card). The X panel hides the viewer otherwise, so a
  // result left over from another tab does not land under the bookmark list.
  const [viewerRequested, setViewerRequested] = useState(false)
  const [showChapters, setShowChapters] = useState(false)
  const [chapters, setChapters] = useState([])
  const [voice, setVoice] = useState("male")
  const [uploadProgress, setUploadProgress] = useState(null) // 0-100 while sending, null otherwise
  const [regenVoice, setRegenVoice] = useState("male")
  const [chapterAudioReady, setChapterAudioReady] = useState({}) // { [chapterIndex]: true }
  const [chapterAudioLoading, setChapterAudioLoading] = useState(null) // chapterIndex currently generating, or null
  const [playingChapterVideo, setPlayingChapterVideo] = useState(null) // chapterIndex playing inline video, or null
  const pollRef = useRef(null)

  useEffect(() => {
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

  // Leaving a tab ends the explicit request: coming back to X should show the
  // bookmark list on its own, not whatever was last opened from it.
  useEffect(() => {
    setViewerRequested(false)
  }, [inputMode])

  useEffect(() => {
    // Covers the "failed" path too, not just success -- otherwise a failed
    // chapter-audio generation left the button stuck showing its spinner.
    if (job && job.status !== "running") setChapterAudioLoading(null)
  }, [job])

  // Load the jobs list on entering the videos tab. A failed request has to be
  // told apart from an empty history: silently keeping the empty array would
  // render "no hay videos" while the backend is simply unreachable.
  useEffect(() => {
    if (inputMode !== "videos") return
    let cancelled = false
    setVideosLoading(true)
    setVideosError("")
    fetchJobs()
      .then((rows) => {
        if (cancelled) return
        if (rows) setAllJobs(rows)
        else setVideosError("No se pudo cargar el historial de videos.")
      })
      .finally(() => { if (!cancelled) setVideosLoading(false) })
    return () => { cancelled = true }
  }, [inputMode])

  // Keep badges/stages live while something is still running. The dependency
  // is the boolean, not allJobs itself: depending on the array would re-run
  // this on every refresh (a new array identity each time) and re-fetch in a
  // loop, while the boolean only flips when the last running job finishes --
  // which is exactly when the interval should stop.
  const hasRunningJob = allJobs.some((j) => j.status === "running")
  useEffect(() => {
    if (inputMode !== "videos" || !hasRunningJob) return
    let cancelled = false
    const timer = setInterval(async () => {
      const rows = await fetchJobs()
      if (!cancelled && rows) setAllJobs(rows)
    }, POLL_INTERVAL_MS)
    return () => { cancelled = true; clearInterval(timer) }
  }, [inputMode, hasRunningJob])

  // Opening a card reuses the one results viewer the app already has (the same
  // route XBookmarks takes), rather than rendering a second, poorer one inside
  // the card. It stays mounted under this tab, so the user keeps their place.
  function openJobFromHistory(jobId) {
    openJob(jobId)
    requestAnimationFrame(() =>
      document.querySelector(".results, .status-message")
        ?.scrollIntoView({ behavior: "smooth", block: "start" })
    )
  }

  async function deleteJobFromHistory(j) {
    const label = truncate(j.title || j.filename, 60) || j.id
    if (!window.confirm(`¿Borrar "${label}"?\n\nSe eliminan también su audio y video.`)) return
    setVideosError("")
    try {
      const res = await fetch(`${API_URL}/jobs/${j.id}`, { method: "DELETE" })
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      setAllJobs((rows) => rows.filter((r) => r.id !== j.id))
      // Clear the viewer too if it was showing the job just deleted.
      if (job?.id === j.id) {
        setJob(null)
        setResult(null)
      }
    } catch (e) {
      setVideosError(`No se pudo borrar: ${e.message}`)
    }
  }

  async function openJob(jobId) {
    stopPolling()
    setError("")
    setResult(null)
    setJob(null)
    setViewerRequested(true)
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
          onDone?.()
        } else if (data.status === "failed") {
          setError(data.error ?? "La transcripción falló")
          setLoading(false)
          stopPolling()
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
            aria-selected={inputMode === "videos"}
            className={inputMode === "videos" ? "active" : ""}
            onClick={() => setInputMode("videos")}
          >
            🎞️ Videos procesados
          </button>
          {/* Settings is not an input source like the tabs before it, so it
              sits apart, pushed to the far end of the row. */}
          <button
            type="button"
            role="tab"
            aria-selected={inputMode === "settings"}
            className={`tab-settings${inputMode === "settings" ? " active" : ""}`}
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

      {/* Videos procesados panel */}
      {inputMode === "videos" && (
        <section className="videos-list">
          <h2>Videos procesados</h2>

          {videosError && <div className="error-banner">❌ {videosError}</div>}

          {videosLoading && allJobs.length === 0 ? (
            <p className="x-empty">Cargando…</p>
          ) : allJobs.length === 0 && !videosError ? (
            <p className="x-empty">No hay videos procesados todav&#237;a.</p>
          ) : (
            <div className="videos-grid">
              {allJobs.map((j) => {
                const isOpen = job?.id === j.id
                return (
                  <article key={j.id} className={`video-card${isOpen ? " open" : ""}`}>
                    <div className="video-card-head">
                      <StatusBadge status={j.status} stage={j.stage} />
                      {j.source && <span className="video-card-source">{sourceLabel(j.source)}</span>}
                      <span className="video-card-date">{formatDate(j.created_at)}</span>
                    </div>

                    {/* The card body is one button, and nothing interactive
                        nests inside it -- the YouTube link and the delete
                        button are siblings in the footer below. */}
                    <button
                      type="button"
                      className="video-card-body"
                      onClick={() => openJobFromHistory(j.id)}
                      aria-current={isOpen ? "true" : undefined}
                    >
                      {/* YouTube jobs carry the video title; uploads only have
                          the filename. Both come from the listing. */}
                      <span className="video-card-title">
                        {truncate(j.title || j.filename, 60) || "—"}
                      </span>

                      <span className="video-card-meta">
                        {resultDuration(j) && <span>⏱ {resultDuration(j)}</span>}
                        {segmentsCount(j) && <span>{segmentsCount(j)} segmentos</span>}
                        {chapterCount(j) > 0 && <span>{chapterCount(j)} cap&#237;tulos</span>}
                        {hasAudio(j) && <span title="Con locuci&#243;n">🔊</span>}
                        {hasDubbed(j) && <span title="Video doblado">🎬</span>}
                        {hasSummary(j) && <span title="Resumen disponible">📝</span>}
                      </span>

                      {j.status === "failed" && j.error && (
                        <span className="video-card-error">{truncate(j.error, 140)}</span>
                      )}

                      {j.summary_es && (
                        <span className="video-card-summary">{truncate(j.summary_es, 120)}</span>
                      )}
                    </button>

                    <div className="video-card-foot">
                      {j.source === "youtube" && j.url && (
                        <a href={j.url} target="_blank" rel="noreferrer">▶️ YouTube ↗</a>
                      )}
                      {hasAudio(j) && (
                        <a href={`${API_URL}/jobs/${j.id}/audio`} download>⬇️ Audio</a>
                      )}
                      {hasDubbed(j) && (
                        <a href={`${API_URL}/jobs/${j.id}/video`} download>⬇️ Video</a>
                      )}
                      <button
                        type="button"
                        className="video-card-delete"
                        title="Borrar el job y sus archivos"
                        onClick={() => deleteJobFromHistory(j)}
                      >
                        🗑️
                      </button>
                    </div>
                  </article>
                )
              })}
            </div>
          )}
        </section>
      )}

      {/* X Bookmarks panel */}
      {inputMode === "x" && (
        <XBookmarks
          onOpenJob={(jobId) => {
            // Same pipeline, same results screen. openJob marks the viewer as
            // explicitly requested, which is what lets it render here at all --
            // so the user keeps their place in the list instead of being sent
            // to the upload tab to read a transcript.
            openJob(jobId)
            requestAnimationFrame(() =>
              document.querySelector(".results, .status-message")
                ?.scrollIntoView({ behavior: "smooth", block: "start" })
            )
          }}
        />
      )}
      {inputMode === "settings" && <Settings />}


      {/* Results. In the X panel several jobs run at once, so the viewer only
          appears for a job the user actually asked to see -- otherwise the
          per-card progress stays the single source of truth there. */}
      {result && (inputMode !== "x" || viewerRequested) && (
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

      {loading && uploadProgress === null && inputMode !== "x" && (
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

/* ── Videos procesados helpers ───────────────────────────────────── */

// The listing carries everything the collapsed card needs; only the expanded
// detail hits GET /jobs/{id}, from the effect in App().
async function fetchJobs() {
  try {
    const res = await fetch(`${API_URL}/jobs`)
    if (!res.ok) return null
    return await res.json()
  } catch {
    return null
  }
}

function sourceLabel(src) {
  if (src === "youtube") return "▶️ YouTube"
  return "📁 Archivo"
}

// duration_seconds is a number of seconds, unlike a segment's "HH:MM:SS.mmm"
// start -- so this does not go through formatTs, which parses that format.
function resultDuration(j) {
  const total = j.duration_seconds
  if (!total) return null
  const h = Math.floor(total / 3600)
  const m = Math.floor((total % 3600) / 60)
  const s = Math.floor(total % 60)
  if (h > 0) return `${h}h ${m}m`
  return `${m}m ${s}s`
}

// Map backend stage to a human label. Uses the same STAGE_LABELS as the live UI.
function stageLabel(stage) {
  return STAGE_LABELS[stage] ?? stage
}

function segmentsCount(j) {
  const r = j.segments_done
  const t = j.segments_total
  if (r == null || t == null || t === 0) return null
  return `${r}/${t}`
}

function chapterCount(j) {
  return j.chapter_count ?? 0
}

function hasAudio(j) {
  return Boolean(j.audio_available)
}

function hasDubbed(j) {
  return Boolean(j.dubbed_video_available)
}

function hasSummary(j) {
  return Boolean(j.summary_es)
}

// Titles scraped from X arrive as multi-line tweet text ("Marco\n@handle\n·\n6
// ago. — ..."), so a raw slice would drop line breaks into the card. Collapse
// whitespace first, then cut.
function truncate(str, max) {
  if (!str) return ""
  const flat = str.replace(/\s+/g, " ").trim()
  return flat.length > max ? flat.slice(0, max) + "…" : flat
}

// Status badge component used inline in the grid. 'queued' means accepted but
// not yet picked up -- it belongs with running, not with failed.
function StatusBadge({ status, stage }) {
  if (status === "done") return <span className="status-badge badge-done">Listo</span>
  if (status === "failed") return <span className="status-badge badge-failed">Fall&oacute;</span>
  return <span className="status-badge badge-running">{stageLabel(stage) ?? "En cola"}</span>
}

// created_at/updated_at are REAL columns holding a Unix timestamp in seconds
// (jobs.py), not an ISO string -- Date wants milliseconds.
function formatDate(seconds) {
  if (!seconds) return ""
  const d = new Date(seconds * 1000)
  if (Number.isNaN(d.getTime())) return ""
  return d.toLocaleString("es-AR", {
    day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit",
    hour12: false,
  })
}
