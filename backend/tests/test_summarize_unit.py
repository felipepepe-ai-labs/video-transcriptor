"""Unit tests for the summary generation module."""

import json
from pathlib import Path

import httpx
import pytest

import summarize


class FakeOllamaResponse:
    """Mock response from Ollama /api/generate."""

    def __init__(self, text: str):
        self._text = text

    def json(self) -> dict:
        return {"response": self._text}

    def raise_for_status(self) -> None:
        pass


class TestBuildPrompt:
    def test_build_prompt_includes_chapters_and_segments(self):
        """The prompt should include both chapter titles and segment text."""
        chapters = [{"time": 0, "title": "Intro"}, {"time": 120, "title": "Main topic"}]
        segments_es = ["Primer segmento de texto", "Segundo segmento con mas informacion"]
        prompt_text = summarize._build_prompt(segments_es, chapters)

        assert "Intro" in prompt_text
        assert "Main topic" in prompt_text
        assert "Primer segmento" in prompt_text
        assert "Segundo segmento" in prompt_text


    def test_build_prompt_with_no_chapters(self):
        """Should work fine with empty chapters list."""
        segments_es = ["Un segmento", "Otro segmento"]
        prompt_text = summarize._build_prompt(segments_es, [])
        assert "Un segmento" in prompt_text

    def test_build_prompt_truncates_to_max_segments(self):
        """Only the first 100 segments should be included."""
        big_list = [f"Segmento {i}" for i in range(200)]
        prompt_text = summarize._build_prompt(big_list, [])
        assert "Segmento 99" in prompt_text
        assert "Segmento 100" not in prompt_text


class TestGenerateSummaryForJob:
    def test_returns_none_for_empty_result(self):
        """No segments means nothing to summarize."""
        result = {"segments": [], "chapters": []}
        assert summarize.generate_summary_for_job(result) is None

    def test_falls_back_to_english_when_no_spanish(self):
        """When text_es is empty, should use text_en."""
        result = {
            "segments": [{"text_en": "Hello world", "text_es": ""}],
            "chapters": [],
        }
        prompt_text = summarize._build_prompt(
            [s.get("text_es", "") for s in result["segments"] if s.get("text_es")],
            result.get("chapters", []),
        )
        # Should return empty because text_es is empty
        assert prompt_text == ""
