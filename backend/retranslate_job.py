"""One-off: re-translate a job's stored English segments and overwrite its
result_json in place. Used to fix jobs whose Spanish text was never actually
translated (e.g. produced by the old broken Whisper-translate fallback).

Writes progress back to the DB after every batch (not just at the end) so
a long run (large jobs can take tens of minutes on Ollama) is inspectable
mid-flight. Resumability is tracked per-segment via a "retranslated" marker
set only by THIS script -- comparing text_es to text_en is not a reliable
signal, since the old broken Whisper-translate fallback re-transcribes the
audio slightly differently each run, producing text that differs from
text_en without being real Spanish.
"""
import json
import sqlite3
import sys
from pathlib import Path

from translate import MyMemoryTranslator, OllamaTranslator, TranslationFailed

JOBS_DB = Path(__file__).parent / "jobs.db"
BATCH_SIZE = 20


def load(conn, job_id):
    row = conn.execute("SELECT result_json FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if row is None:
        sys.exit(f"job {job_id} not found")
    return json.loads(row[0])


def save(conn, job_id, data):
    conn.execute("UPDATE jobs SET result_json = ? WHERE id = ?", (json.dumps(data), job_id))
    conn.commit()


def main(job_id: str) -> None:
    conn = sqlite3.connect(JOBS_DB)
    data = load(conn, job_id)
    segments = data["segments"]

    pending_idx = [i for i, s in enumerate(segments) if not s.get("retranslated")]
    print(f"{len(segments)} segments total, {len(pending_idx)} pending translation.", flush=True)

    try:
        MyMemoryTranslator().translate(["ping"])
        mymemory_up = True
    except TranslationFailed as e:
        mymemory_up = False
        print(f"MyMemory unavailable ({e}); using Ollama for all segments.", flush=True)

    translator = MyMemoryTranslator() if mymemory_up else OllamaTranslator()
    provider = "mymemory" if mymemory_up else "ollama"

    for start in range(0, len(pending_idx), BATCH_SIZE):
        batch_idx = pending_idx[start : start + BATCH_SIZE]
        batch_texts = [segments[i]["text_en"] for i in batch_idx]
        translated = translator.translate(batch_texts)
        for i, text_es in zip(batch_idx, translated):
            segments[i]["text_es"] = text_es
            segments[i]["retranslated"] = True
        data["translation_provider"] = provider
        save(conn, job_id, data)
        done = start + len(batch_idx)
        print(f"{done}/{len(pending_idx)} translated (provider={provider})", flush=True)

    for s in segments:
        s.pop("retranslated", None)
    data["full_text_es"] = "\n".join(s["text_es"] for s in segments)
    save(conn, job_id, data)
    conn.close()
    print("Done. First segment text_es:", segments[0]["text_es"])


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(f"usage: python {Path(__file__).name} <job_id>")
    main(sys.argv[1])
