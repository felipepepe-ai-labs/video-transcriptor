import { useEffect, useRef, useState } from "react"

const API_URL = import.meta.env.VITE_API_URL ?? "http://localhost:8000"

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

// Statuses the backend accepts a download for; no_media is retryable.
const DOWNLOADABLE = ["interesting", "no_media"]

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

function XBookmarks() {
  const [bookmarks, setBookmarks] = useState([])
  const [loading, setLoading] = useState(false)
  const [syncing, setSyncing] = useState(false)
  const [error, setError] = useState("")
  const [downloadingIds, setDownloadingIds] = useState(new Set())
  const [statusFilter, setStatusFilter] = useState(null)
  const [syncMessage, setSyncMessage] = useState("")
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
        setDownloadingIds(
          new Set(event.jobs.filter((j) => j.job === "download").map((j) => j.bookmark_id))
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
        if (event.type === "error") setError(event.message ?? "Error de descarga")
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
          className="btn-primary"
          onClick={() => fileInputRef.current?.click()}
        >
          🍪 Importar Cookies
        </button>
        <button
          className="btn-secondary"
          disabled={syncing || loading}
          onClick={handleSync}
        >
          {syncing ? "Sincronizando…" : "🔄 Sync Bookmarks"}
        </button>
        {syncMessage && <span className="sync-message">{syncMessage}</span>}
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
          {bookmarks.map((bm) => (
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
                <p className="bookmark-text">{bm.text ?? bm.tweet_url ?? ""}</p>
                {bm.scraped_at && (
                  <small className="bookmark-date">{formatDate(bm.scraped_at)}</small>
                )}
              </div>
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
                  {DOWNLOADABLE.includes(bm.status) && (
                    <button
                      title={bm.status === "no_media" ? "Reintentar descarga" : "Descargar video"}
                      disabled={downloadingIds.has(bm.id)}
                      onClick={() => downloadBookmark(bm.id)}
                      aria-label={bm.status === "no_media" ? "Reintentar descarga" : "Descargar"}
                    >
                      {downloadingIds.has(bm.id) ? "⏳" : bm.status === "no_media" ? "🔁" : "⬇️"}
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
          ))}
        </div>
      )}
    </div>
  )
}

export default XBookmarks
