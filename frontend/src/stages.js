// Every source runs the same pipeline, so every screen that reports on it must
// name the stages the same way. Kept here rather than inside App so the X panel
// cannot drift into its own vocabulary.
export const STAGE_LABELS = {
  downloading: "Descargando video de YouTube...",
  uploading: "Subiendo video...",
  transcribing: "Transcribiendo con Whisper...",
  translating: "Traduciendo al español...",
  voicing: "Generando locución por segmento...",
  voicing_chapter: "Regenerando locución del capítulo...",
  dubbing: "Mezclando el audio con el video...",
  splitting: "Recortando el video por capítulos...",
  done: "Listo",
}
