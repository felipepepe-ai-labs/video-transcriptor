"""
EN -> ES translation with a resilient primary provider and a local fallback.

MyMemoryTranslator is the primary (free HTTP API, batched + retried). If it
fails outright, OllamaTranslator asks a local/remote Ollama model to
translate instead. Whisper's own --task translate is NOT usable as a
fallback here: OpenAI Whisper only translates speech INTO English, never
into an arbitrary target language, so it can't produce Spanish output. The
orchestrator records which provider actually produced the result so
degraded output is never silent.
"""
import os
import re
import time
from typing import Callable, Protocol

import httpx

ProgressCallback = Callable[[int, int], None]  # (segments_done, segments_total)

MYMEMORY_URL = "https://api.mymemory.translated.net/get"
BATCH_DELIMITER = " ||| "
MAX_BATCH_CHARS = 480

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://192.168.1.60:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen3.6:27b")
OLLAMA_BATCH_SIZE = 20


class TranslationFailed(Exception):
    """Primary translation provider exhausted its retries."""


class Translator(Protocol):
    def translate(self, segments: list[str]) -> list[str]: ...


class MyMemoryTranslator:
    """Batches short segments together (under MyMemory's ~500 char query cap)
    and retries with exponential backoff before giving up."""

    def __init__(
        self,
        retries: int = 3,
        backoff_base: float = 0.5,
        max_batch_chars: int = MAX_BATCH_CHARS,
        email: str | None = None,
    ):
        self.retries = retries
        self.backoff_base = backoff_base
        self.max_batch_chars = max_batch_chars
        # An email raises MyMemory's free quota from 5k to 50k words/day
        # (tracked separately from the anonymous per-IP quota).
        self.email = email or os.getenv("MYMEMORY_EMAIL")

    def translate(self, segments: list[str], on_progress: ProgressCallback | None = None) -> list[str]:
        batches = self._make_batches(segments)
        results: list[str] = []
        for batch in batches:
            joined = BATCH_DELIMITER.join(batch)
            translated = self._translate_query(joined)
            parts = translated.split(BATCH_DELIMITER.strip())
            parts = [p.strip(" |") for p in parts]
            if len(parts) == len(batch):
                results.extend(parts)
            else:
                # Batch translation didn't preserve the delimiter cleanly;
                # fall back to translating this batch's segments one by one.
                results.extend(self._translate_query(text) for text in batch)
            if on_progress:
                on_progress(len(results), len(segments))
        return results

    def _make_batches(self, segments: list[str]) -> list[list[str]]:
        batches: list[list[str]] = []
        current: list[str] = []
        current_len = 0
        for seg in segments:
            seg_len = len(seg) + len(BATCH_DELIMITER)
            if current and current_len + seg_len > self.max_batch_chars:
                batches.append(current)
                current, current_len = [], 0
            current.append(seg)
            current_len += seg_len
        if current:
            batches.append(current)
        return batches

    def _translate_query(self, text: str) -> str:
        params = {"q": text, "langpair": "en|es"}
        if self.email:
            params["de"] = self.email
        last_error: Exception | None = None
        for attempt in range(self.retries):
            try:
                resp = httpx.get(MYMEMORY_URL, params=params, timeout=5)
                if resp.status_code == 200:
                    data = resp.json()
                    translated = data.get("responseData", {}).get("translatedText", "")
                    if translated:
                        return translated
                    last_error = RuntimeError("MyMemory returned empty translation")
                else:
                    last_error = RuntimeError(f"MyMemory HTTP {resp.status_code}")
            except Exception as e:
                last_error = e
            if attempt < self.retries - 1:
                time.sleep(self.backoff_base * (2 ** attempt))
        raise TranslationFailed(str(last_error))


class OllamaTranslator:
    """Batch-translates via a local/remote Ollama model using numbered-list
    prompting, so one HTTP call handles many segments at once. Used when
    MyMemory's free quota is exhausted or unreachable."""

    def __init__(
        self,
        url: str = OLLAMA_URL,
        model: str = OLLAMA_MODEL,
        batch_size: int = OLLAMA_BATCH_SIZE,
        retries: int = 2,
    ):
        self.url = url
        self.model = model
        self.batch_size = batch_size
        self.retries = retries

    def translate(self, segments: list[str], on_progress: ProgressCallback | None = None) -> list[str]:
        results: list[str] = []
        for i in range(0, len(segments), self.batch_size):
            results.extend(self._translate_batch(segments[i : i + self.batch_size]))
            if on_progress:
                on_progress(len(results), len(segments))
        return results

    def _translate_batch(self, batch: list[str]) -> list[str]:
        numbered = "\n".join(f"{i + 1}. {text}" for i, text in enumerate(batch))
        prompt = (
            "Translate each numbered line from English to Spanish. "
            "Reply with the same numbering, one translated line per number, "
            "and nothing else.\n\n" + numbered
        )
        for attempt in range(self.retries):
            response = self._generate(prompt)
            if response is not None:
                parsed = _parse_numbered(response, len(batch))
                if parsed is not None:
                    return parsed
        # Degrade one segment at a time rather than losing the whole batch.
        return [self._translate_one(text) for text in batch]

    def _translate_one(self, text: str) -> str:
        prompt = f"Translate to Spanish, output only the translation:\n{text}"
        response = self._generate(prompt)
        return response.strip() if response else text

    def _generate(self, prompt: str) -> str | None:
        try:
            resp = httpx.post(
                f"{self.url}/api/generate",
                json={"model": self.model, "prompt": prompt, "stream": False, "think": False},
                timeout=120,
            )
            resp.raise_for_status()
            return resp.json().get("response", "")
        except Exception:
            return None


def _parse_numbered(text: str, expected_count: int) -> list[str] | None:
    lines: dict[int, str] = {}
    for line in text.strip().splitlines():
        m = re.match(r"\s*(\d+)[.)]\s*(.+)", line)
        if m:
            lines[int(m.group(1))] = m.group(2).strip()
    if len(lines) != expected_count:
        return None
    return [lines[i] for i in range(1, expected_count + 1)]


def translate_with_fallback(
    segments_en: list[str], on_progress: ProgressCallback | None = None
) -> tuple[list[str], str]:
    """Try MyMemory first; fall back to a local Ollama model if it fails.
    Returns (translated_segments, provider_used)."""
    try:
        translator = MyMemoryTranslator()
        return translator.translate(segments_en, on_progress=on_progress), "mymemory"
    except Exception:
        pass
    try:
        return OllamaTranslator().translate(segments_en, on_progress=on_progress), "ollama"
    except Exception:
        return list(segments_en), "untranslated"
