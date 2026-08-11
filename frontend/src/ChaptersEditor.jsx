import { newChapter } from "./chapters.js"

/**
 * Optional chapter markers, shared by every source that can start a job.
 *
 * Rows carry a client-side `id` purely as a React key; `cleanChapters` in
 * chapters.js is what turns them into the shape the backend wants, so the three
 * sources cannot disagree on how "36:44" becomes a number of seconds.
 */
function ChaptersEditor({ chapters, onChange, open, onToggle }) {
  function update(id, field, value) {
    onChange(chapters.map((c) => (c.id === id ? { ...c, [field]: value } : c)))
  }

  return (
    <details className="chapters-editor" open={open} onToggle={onToggle}>
      <summary>📑 Capítulos (opcional) {chapters.length > 0 && `· ${chapters.length}`}</summary>

      <div className="chapters-list">
        {chapters.map((ch) => (
          <div key={ch.id} className="chapter-row-edit">
            <input
              type="text"
              placeholder="00:00"
              value={ch.time}
              onChange={(e) => update(ch.id, "time", e.target.value)}
              className="chapter-time-input"
            />
            <input
              type="text"
              placeholder="Título del capítulo"
              value={ch.title}
              onChange={(e) => update(ch.id, "title", e.target.value)}
              className="chapter-title-input"
            />
            <button
              type="button"
              className="chapter-remove"
              onClick={() => onChange(chapters.filter((c) => c.id !== ch.id))}
              aria-label="Eliminar capítulo"
            >
              ✕
            </button>
          </div>
        ))}
      </div>

      <button
        type="button"
        className="btn-add-chapter"
        onClick={() => onChange([...chapters, newChapter()])}
      >
        + Agregar capítulo
      </button>
      <p className="chapters-hint">
        Formato: <code>MM:SS</code> o <code>H:MM:SS</code>, ej. <code>36:44</code> o <code>1:21:25</code>
      </p>
    </details>
  )
}

export default ChaptersEditor
