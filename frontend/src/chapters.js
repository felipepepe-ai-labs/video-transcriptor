// Chapter helpers, shared by every source that can start a job. Kept out of
// ChaptersEditor.jsx so that file exports only its component: mixing the two
// breaks React Fast Refresh (and oxlint says so).

export function newChapter() {
  return {
    id: `${Date.now()}-${Math.random().toString(36).slice(2, 9)}`,
    time: "",
    title: "",
  }
}

export function parseTimeToSeconds(input) {
  // "36:44" → 2204, "1:21:25" → 4885
  const parts = input.split(":").map((p) => parseInt(p, 10) || 0)
  if (parts.length === 3) return parts[0] * 3600 + parts[1] * 60 + parts[2]
  if (parts.length === 2) return parts[0] * 60 + parts[1]
  return parts[0] || 0
}

/** Drop the untitled rows and hand back what `chapters_json` expects. */
export function cleanChapters(chapters) {
  return chapters
    .filter((c) => c.title.trim())
    .map((c) => ({ time: parseTimeToSeconds(c.time), title: c.title.trim() }))
}
