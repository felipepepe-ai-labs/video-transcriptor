import { useEffect, useRef, useState } from "react"

const API_URL = import.meta.env.VITE_API_URL ?? "http://localhost:8000"

const STATUS_FILTERS = [
  { label: "Todos", value: null },
  { label: "Nuevos", value: "new" },
  { label: "Interesantes", value: "interesting" },
  { label: "Descargados", value: "downloaded" },
]

function statusClass(status) {
  if (status === "downloaded") return "downloaded"
  if (status === "interesting") return "interesting"
  return "new"
}

function formatDate(unix) {
  if (!unix) return ""
  return new Date(unix * 1000).toLocaleDateString("es-AR", {
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

  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => { refreshBookmarks() }, [])

  async function refreshBookmarks() {
    setLoading(true)
    setError("")
    try {
      const url = `${API_URL}/x/bookmarks${statusFilter ? `?status_filter=${statusFilter}` : ""}`
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
    setSyncMessage("Sincronizando bookmarks de X...")
    setSyncing(true)
    try {
      const res = await fetch(`${API_URL}/x/sync`, { method: "POST" })
      if (!res.ok) throw new Error((await res.json()).detail ?? `HTTP ${res.status}`)
      setSyncMessage("Sincronización completada")
      refreshBookmarks()
    } catch (e) {
      setError(e.message)
    } finally {
      setSyncing(false)
      setTimeout(() => setSyncMessage(""), 4000)
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

  async function downloadBookmark(id, url) {
    setDownloadingIds((prev) => new Set(prev).add(id))
    try {
      const res = await fetch(`${API_URL}/x/bookmarks/${id}/download`, { method: "POST" })
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      refreshBookmarks()
    } catch (e) {
      setError(e.message)
    } finally {
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
            onClick={() => { setStatusFilter(f.value); refreshBookmarks() }}
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
                  {bm.status}
                </span>
                <div className="bookmark-actions">
                  <button
                    title="Toggle Interesting"
                    onClick={() => toggleInteresting(bm.id)}
                    aria-label="Cambiar estado"
                  >
                    {bm.status === "interesting" ? "⭐" : "☆"}
                  </button>
                  {bm.status !== "downloaded" && (
                    <button
                      title="Descargar video"
                      disabled={downloadingIds.has(bm.id)}
                      onClick={() => downloadBookmark(bm.id, bm.tweet_url)}
                      aria-label="Descargar"
                    >
                      {downloadingIds.has(bm.id) ? "⏳" : "⬇️"}
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
