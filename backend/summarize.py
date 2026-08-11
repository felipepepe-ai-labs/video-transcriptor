"""Generate concise Spanish summaries of transcribed videos using Ollama."""

import os
from typing import Protocol

import httpx

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://192.168.1.60:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen3.6:27b")


class SummaryFailed(Exception):
    """Ollama failed to produce a summary."""


def _generate(prompt: str, timeout: int = 120) -> str | None:
    """Call Ollama /api/generate and return the response text, or None on failure."""
    try:
        resp = httpx.post(
            f"{OLLAMA_URL}/api/generate",
            json={"model": OLLAMA_MODEL, "prompt": prompt, "stream": False, "think": False},
            timeout=timeout,
        )
        resp.raise_for_status()
        return resp.json().get("response", "")
    except Exception:
        return None


# ── Prompt building ─────────────────────────────────────────────────────

_MAX_SEGMENTS = 100  # Enough to capture the course's main topics (~5-10 min)


def _build_prompt(segments_es: list[str], chapters: list[dict]) -> str:
    """Build a summary prompt from Spanish segments and chapter titles."""
    parts = []

    # Chapters section (always included for context)
    if chapters:
        cap_lines = "\n".join(f"{i+1}. {ch.get('title', 'Sin titulo')} ({ch.get('time', '')})" for i, ch in enumerate(chapters))
        parts.append(f"[CAPITULOS]\n{cap_lines}\n")

    # Text section (first N segments in Spanish, or English as fallback)
    text_parts = []
    for seg_text in segments_es[:_MAX_SEGMENTS]:
        stripped = seg_text.strip()
        if stripped:
            text_parts.append(stripped)

    if not text_parts:
        return ""  # Nothing to summarize

    parts.append(f"[CONVERSACION - PRIMERA SECCION]\n" + "\n".join(text_parts))
    return "\n\n".join(parts)


# ── Public API ──────────────────────────────────────────────────────────

def generate_summary(segments_es: list[str], segments_en: list[str], chapters: list[dict]) -> str | None:
    """Generate a concise Spanish summary of a video's content.

    Uses the first ``_MAX_SEGMENTS`` segments from segments_es (falls back to
    segments_en if no Spanish text is available).  Includes chapter titles for
    structure context.

    Returns the summary string, or None if Ollama is unreachable / empty response.
    """
    text_list = segments_es if segments_es else segments_en
    prompt = _build_prompt(text_list, chapters)
    if not prompt:
        return None

    prompt += (
        "\n\nEres un asistente que resume videos educativos en espana. Resume el "
        "contenido del siguiente video-curso en 3-5 parrafos cortos (maximo 600 "
        "caracteres), incluyendo los temas principales y las ideas clave de cada "
        "capitulo. Responde SOLO con el resumen, sin ningun comentario adicional o "
        "introduccion."
    )

    response = _generate(prompt)
    if not response:
        return None
    return response.strip()


def generate_summary_for_job(job_result: dict) -> str | None:
    """Convenience wrapper that extracts fields from a completed job result."""
    segments_es = [s.get("text_es", "") for s in job_result.get("segments", []) if s.get("text_es")]
    segments_en = [s.get("text_en", "") for s in job_result.get("segments", []) if s.get("text_en")]
    chapters = job_result.get("chapters", [])
    return generate_summary(segments_es, segments_en, chapters)
