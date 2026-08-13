import { useEffect, useRef, useState } from "react"
import VoicePicker from "./VoicePicker.jsx"
import ChaptersEditor from "./ChaptersEditor.jsx"
import { cleanChapters } from "./chapters.js"
import { STAGE_LABELS } from "./stages.js"

// Relative so it always goes through Vite's proxy, regardless of LAN address.
const API_URL = ""
const JOB_POLL_INTERVAL_MS = 2000

const STATUS_FILTERS = [
  { label: "Todos", value: null },
  { label: "Nuevos", value: "new" },
  { label: "Interesantes", value: "interesting" },
  { label: "Descargados", value: "downloaded" },
  { label: "Sin video", value: "no_media" },
]

const STATUS_LABELS = {
  new: "nuevo",
  interesting: "interesante",
  downloaded: "descargado",
  no_media: "sin video",
}

// Roughly what fits in the four clamped lines of .bookmark-text. Below this the
// toggle would expand to exactly what is already on screen.
const TEXT_CLAMP_CHARS = 180

function statusClass(status) {
  if (status === "downloaded") return "downloaded"
  if (status === "interesting") return "interesting"
  if (status === "no_media") return "no-media"
  return "new"
}

function formatDate(scrapedAt) {
  if (!scrapedAt) return ""
  // scraped_at is an ISO-8601 string from the backend, not a unix timestamp.
  const date = new Date(scrapedAt)
  if (Number.isNaN(date.getTime())) return ""
  return date.toLocaleDateString("es-AR", {
    day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit",
  })
}

function XBookmarks({ onOpenJob }) {
  const [bookmarks, setBookmarks] = useState([])
  const [loading, setLoading] = useState(false)
  const [syncing, setSyncing] = useState(false)
  const [exporting, setExporting] = useState(false)
  const [error, setError] = useState("")
  const [downloadingIds, setDownloadingIds] = useState(new Set())
  const [transcribingIds, setTranscribingIds] = useState(new Set())
  // {bookmarkId: {percent, size, speed}} while yt-dlp is running.
  const [downloadProgress, setDownloadProgress] = useState({})
  const [statusFilter, setStatusFilter] = useState(null)
  const [syncMessage, setSyncMessage] = useState("")
  // Same two knobs the upload and YouTube tabs offer. They belong to the panel
  // rather than to a card: you pick them, then transcribe whichever bookmarks.
  const [voice, setVoice] = useState("male")
  const [chapters, setChapters] = useState([])
  const [showChapters, setShowChapters] = useState(false)
  // {jobId: {stage, segments_done, segments_total}} for jobs still running.
  const [jobStages, setJobStages] = useState({})
  // Which cards have their full text unfolded. Kept here rather than in a
  // per-card component so the grid stays one component, as it was.
  const [expandedIds, setExpandedIds] = useState(new Set())
  const fileInputRef = useRef(null)

  // Refetch whenever the filter changes: calling refreshBookmarks() straight
  // after setStatusFilter() would still read the previous filter value.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => { refreshBookmarks() }, [statusFilter])

  // Kept in a ref so the SSE handler below always calls the current version
  // without having to tear down and reopen the stream on every render.
  const refreshRef = useRef(refreshBookmarks)
  refreshRef.current = refreshBookmarks

  // Live progress. Sync and download are fire-and-forget background tasks on
  // the backend, so this stream is the only honest signal that they finished.
  useEffect(() => {
    const source = new EventSource(`${API_URL}/x/progress`)

    source.onmessage = (message) => {
      const event = JSON.parse(message.data)

      if (event.type === "snapshot") {
        setSyncing(event.jobs.some((j) => j.job === "sync"))
        const downloads = event.jobs.filter((j) => j.job === "download")
        setDownloadingIds(new Set(downloads.map((j) => j.bookmark_id)))
        // The snapshot carries each worker's last event, so a reload mid-download
        // gets its bar back where it was instead of a bare spinner.
        setDownloadProgress(
          Object.fromEntries(
            downloads
              .filter((j) => j.percent != null)
              .map((j) => [j.bookmark_id, { percent: j.percent, size: j.size, speed: j.speed }])
          )
        )
        return
      }

      const finished = event.type === "done" || event.type === "error"

      if (event.job === "sync") {
        setSyncing(!finished)
        setSyncMessage(event.message ?? "")
        if (event.type === "error") setError(event.message ?? "Error de sincronización")
      }

      if (event.job === "download") {
        setDownloadingIds((prev) => {
          const next = new Set(prev)
          if (finished) next.delete(event.bookmark_id)
          else next.add(event.bookmark_id)
          return next
        })
        setDownloadProgress((prev) => {
          const next = { ...prev }
          if (finished) delete next[event.bookmark_id]
          else if (event.percent != null) {
            next[event.bookmark_id] = {
              percent: event.percent, size: event.size, speed: event.speed,
            }
          }
          return next
        })
        if (event.type === "error") setError(event.message ?? "Error de descarga")
      }

      if (event.job === "transcribe") {
        setTranscribingIds((prev) => {
          const next = new Set(prev)
          if (finished) next.delete(event.bookmark_id)
          else next.add(event.bookmark_id)
          return next
        })
        if (event.type === "error") setError(event.message ?? "Error de transcripción")
      }

      if (finished) {
        refreshRef.current()
        if (event.job === "sync") setTimeout(() => setSyncMessage(""), 4000)
      }
    }

    // EventSource reconnects on its own; nothing to do but stop shouting.
    source.onerror = () => {}

    return () => source.close()
  }, [])

  // Which jobs are worth asking about: a bookmark that is mid-transcription.
  const watchedKey = bookmarks
    .filter((bm) => bm.job_id && transcribingIds.has(bm.id))
    .map((bm) => bm.job_id)
    .join(",")

  // Stage of a running transcription, read from the job record the pipeline is
  // already updating. Polling GET /jobs/{id} rather than teaching the pipeline
  // to publish onto the X progress stream keeps the shared code unaware of X.
  useEffect(() => {
    if (!watchedKey) {
      setJobStages({})
      return
    }

    const jobIds = watchedKey.split(",")
    let cancelled = false
    let timer

    async function tick() {
      const entries = await Promise.all(
        jobIds.map(async (jobId) => {
          try {
            const res = await fetch(`${API_URL}/jobs/${jobId}`)
            return res.ok ? [jobId, await res.json()] : null
          } catch {
            return null // a blip here must not kill the poll loop
          }
        })
      )
      if (cancelled) return
      setJobStages(Object.fromEntries(entries.filter(Boolean)))
      timer = setTimeout(tick, JOB_POLL_INTERVAL_MS)
    }

    tick()
    return () => {
      cancelled = true
      clearTimeout(timer)
    }
  }, [watchedKey])

  async function refreshBookmarks() {
    setLoading(true)
    setError("")
    try {
      const url = `${API_URL}/x/bookmarks${statusFilter ? `?status=${statusFilter}` : ""}`
      const res = await fetch(url)
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      setBookmarks(await res.json())
    } catch (e) {
      setError(e.message)
    } finally {
      setLoading(false)
    }
  }

  async function handleImportCookies(e) {
    const file = e.target.files?.[0]
    if (!file) return
    setError("")
    setSyncMessage("Importando cookies...")
    const fd = new FormData()
    fd.append("file", file)
    try {
      const res = await fetch(`${API_URL}/x/import-cookies`, { method: "POST", body: fd })
      if (!res.ok) throw new Error((await res.json()).detail ?? `HTTP ${res.status}`)
      setSyncMessage("Cookies importadas correctamente")
    } catch (e) {
      setError(e.message)
    } finally {
      setSyncMessage("")
      // Reset file input so same file can be re-uploaded.
      if (fileInputRef.current) fileInputRef.current.value = ""
    }
  }

  async function handleSync() {
    setError("")
    setSyncMessage("Sincronizando bookmarks de X…")
    setSyncing(true)
    try {
      const res = await fetch(`${API_URL}/x/sync`, { method: "POST" })
      if (!res.ok) throw new Error((await res.json()).detail ?? `HTTP ${res.status}`)
      // Deliberately no success message here: the request only enqueues the
      // scrape. The progress stream reports when it actually finishes.
    } catch (e) {
      setError(e.message)
      setSyncing(false)
      setSyncMessage("")
    }
  }

  async function exportBackup() {
    setError("")
    setExporting(true)
    try {
      const res = await fetch(`${API_URL}/x/export`, { method: "POST" })
      const body = await res.json()
      if (!res.ok) throw new Error(body.detail ?? `HTTP ${res.status}`)
      // Naming the directory matters: the whole point is that these files can
      // be opened without this application.
      setSyncMessage(`Backup guardado: ${body.markdown} ficheros en ${body.dir}`)
      setTimeout(() => setSyncMessage(""), 8000)
    } catch (e) {
      setError(e.message)
    } finally {
      setExporting(false)
    }
  }

  async function toggleInteresting(id) {
    try {
      const res = await fetch(`${API_URL}/x/bookmarks/${id}/interesting`, { method: "PATCH" })
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      refreshBookmarks()
    } catch (e) {
      setError(e.message)
    }
  }

  async function downloadBookmark(id) {
    setDownloadingIds((prev) => new Set(prev).add(id))
    try {
      const res = await fetch(`${API_URL}/x/bookmarks/${id}/download`, { method: "POST" })
      if (!res.ok) throw new Error((await res.json()).detail ?? `HTTP ${res.status}`)
      // The spinner is cleared by the progress stream's terminal event, not
      // here: this response only means the download was enqueued.
    } catch (e) {
      setError(e.message)
      setDownloadingIds((prev) => { const n = new Set(prev); n.delete(id); return n })
    }
  }

  async function transcribeBookmark(id) {
    setTranscribingIds((prev) => new Set(prev).add(id))
    try {
      const formData = new FormData()
      formData.append("voice", voice)
      const cleaned = cleanChapters(chapters)
      if (cleaned.length) formData.append("chapters_json", JSON.stringify(cleaned))

      const res = await fetch(`${API_URL}/x/bookmarks/${id}/transcribe`, {
        method: "POST",
        body: formData,
      })
      const body = await res.json()
      if (!res.ok) throw new Error(body.detail ?? `HTTP ${res.status}`)
      refreshBookmarks()
      // Jump to the job just like uploading a file does: from here on it is the
      // same pipeline, so it deserves the same screen.
      onOpenJob?.(body.job_id)
    } catch (e) {
      setError(e.message)
      setTranscribingIds((prev) => { const n = new Set(prev); n.delete(id); return n })
    }
  }

  function toggleText(id) {
    setExpandedIds((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  async function deleteBookmark(id) {
    if (!window.confirm("¿Eliminar este bookmark?")) return
    try {
      const res = await fetch(`${API_URL}/x/bookmarks/${id}`, { method: "DELETE" })
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      refreshBookmarks()
    } catch (e) {
      setError(e.message)
    }
  }

  return (
    <div className="x-bookmarks-panel">
      {/* Import + Sync */}
      <section className="x-import-section">
        <input
          ref={fileInputRef}
          type="file"
          accept=".txt,.txt"
          hidden
          onChange={handleImportCookies}
        />
        <button
          type="button"
          className="btn-primary"
          onClick={() => fileInputRef.current?.click()}
        >
          🍪 Importar Cookies
        </button>
        <button
          type="button"
          className="btn-secondary"
          disabled={syncing || loading}
          onClick={handleSync}
        >
          {syncing ? "Sincronizando…" : "🔄 Sync Bookmarks"}
        </button>
        {syncMessage && <span className="sync-message">{syncMessage}</span>}
      </section>

      {/* The same knobs the other two sources offer, applied to whichever
          bookmark you transcribe next. */}
      <section className="x-job-options">
        <VoicePicker value={voice} onChange={setVoice} />
        <ChaptersEditor
          chapters={chapters}
          onChange={setChapters}
          open={showChapters}
          onToggle={(e) => setShowChapters(e.target.open)}
        />
      </section>

      {/* Filter bar */}
      <div className="bookmark-filters" role="tablist">
        {STATUS_FILTERS.map((f) => (
          <button
            key={f.label ?? "all"}
            type="button"
            role="tab"
            aria-selected={statusFilter === f.value}
            className={statusFilter === f.value ? "active" : ""}
            onClick={() => setStatusFilter(f.value)}
          >
            {f.label}
          </button>
        ))}
      </div>

      {error && <div className="error-banner">❌ {error}</div>}

      {/* Bookmark grid */}
      {loading ? (
        <p className="x-loading">Cargando bookmarks…</p>
      ) : bookmarks.length === 0 ? (
        <p className="x-empty">No hay bookmarks. Importá cookies y sincronizá primero.</p>
      ) : (
        <div className="bookmark-grid">
          {bookmarks.map((bm) => {
            // The permalink pass fetches the untruncated version; fall back to
            // the timeline's ~280 chars when it could not be reached.
            const text = bm.expanded_text ?? bm.text ?? ""
            const isExpanded = expandedIds.has(bm.id)
            // Parse auto-chapters from backend JSON (if present on this bookmark).
            const parsedChapters = bm.chapters_json ? (() => {
              try { return JSON.parse(bm.chapters_json) } catch { return [] }
            })() : null
            return (
            <div key={bm.id} className="bookmark-card">
              {bm.thumbnail_url && (
                <img
                  src={bm.thumbnail_url}
                  alt=""
                  className="bookmark-thumb"
                  loading="lazy"
                  onError={(e) => { e.target.style.display = "none" }}
                />
              )}
              <div className="bookmark-body">
                <span className="bookmark-author">{bm.author ?? "—"}</span>
                {text && (
                  <p className={`bookmark-text${isExpanded ? " expanded" : ""}`}>{text}</p>
                )}
                {text.length > TEXT_CLAMP_CHARS && (
                  <button
                    type="button"
                    className="bookmark-more"
                    onClick={() => toggleText(bm.id)}
                  >
                    {isExpanded ? "Ver menos" : "Ver más"}
                  </button>
                )}
                <div className="bookmark-meta">
                  {/* The card used to print the URL as its body text, so this is
                      the only way left to reach the tweet itself. */}
                  <a
                    href={bm.tweet_url}
                    target="_blank"
                    rel="noreferrer"
                    className="bookmark-link"
                  >
                    Abrir en X ↗
                  </a>
                  {bm.scraped_at && (
                    <small className="bookmark-date">{formatDate(bm.scraped_at)}</small>
                  )}
                </div>
              </div>
              {downloadProgress[bm.id] && (
                <div className="download-progress">
                  <div className="download-progress-bar">
                    <div
                      className="download-progress-fill"
                      style={{ width: `${downloadProgress[bm.id].percent}%` }}
                    />
                  </div>
                  <span className="download-progress-label">
                    {Math.round(downloadProgress[bm.id].percent)}%
                    {downloadProgress[bm.id].size && ` de ${downloadProgress[bm.id].size}`}
                    {downloadProgress[bm.id].speed && ` · ${downloadProgress[bm.id].speed}`}
                  </span>
                </div>
              )}
              {jobStages[bm.job_id] && jobStages[bm.job_id].status === "running" && (
                <div className="bookmark-stage">
                  <span className="spinner"></span>
                  <span className="bookmark-stage-label">
                    {STAGE_LABELS[jobStages[bm.job_id].stage] ?? "Procesando…"}
                    {jobStages[bm.job_id].segments_total > 0 &&
                      ` · ${jobStages[bm.job_id].segments_done}/${jobStages[bm.job_id].segments_total}`}
                  </span>
                </div>
              )}
              <div className="bookmark-footer">
                <span className={`status-badge ${statusClass(bm.status)}`}>
                  {STATUS_LABELS[bm.status] ?? bm.status}
                </span>
                <div className="bookmark-actions">
                  <button
                    title="Toggle Interesting"
                    onClick={() => toggleInteresting(bm.id)}
                    aria-label="Cambiar estado"
                  >
                    {bm.status === "interesting" ? "⭐" : "☆"}
                  </button>
                  <button
                    title="Descargar video"
                    disabled={downloadingIds.has(bm.id)}
                    onClick={() => downloadBookmark(bm.id)}
                    aria-label="Descargar"
                  >
                    {downloadingIds.has(bm.id) ? "⏳" : "⬇️"}
                  </button>
                  {bm.status === "downloaded" && !bm.job_id && (
                    <button
                      title="Transcribir y traducir (el mismo proceso que YouTube)"
                      disabled={transcribingIds.has(bm.id)}
                      onClick={() => transcribeBookmark(bm.id)}
                      aria-label="Transcribir"
                    >
                      {transcribingIds.has(bm.id) ? "⏳" : "📝"}
                    </button>
                  )}
                  {bm.job_id && (
                    <button
                      title="Ver la transcripción"
                      onClick={() => onOpenJob?.(bm.job_id)}
                      aria-label="Ver transcripción"
                    >
                      👁️
                    </button>
                  )}
                  <button
                    title="Eliminar bookmark"
                    onClick={() => deleteBookmark(bm.id)}
                    aria-label="Eliminar"
                  >
                    🗑️
                  </button>
                </div>
              </div>
            </div>
            )
          })}
        </div>
      )}
    </div>
  )
}

export default XBookmarks
