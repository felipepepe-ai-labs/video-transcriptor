"""The jobs list must carry enough to draw a card without fetching the blob.

list_jobs() deliberately leaves result_json out -- it holds every segment of a
transcript and gets large. But the history grid needs four scalars that only
live inside that blob (is there narration, is there a dubbed video, how long,
how many chapters), and fetching the full result of every job just to draw a
row would defeat the exclusion it is working around.

So the query lifts those four out with SQLite's JSON1 functions, which read the
document without materializing it. These tests pin both halves of that bargain:
the four fields are there, and result_json still is not.
"""

import pytest

import jobs as jobs_module


@pytest.fixture
def clean_jobs():
    """conftest points the session at one throwaway DB; these tests assert on
    the whole listing, so they need it empty rather than merely disposable."""
    conn = jobs_module._get_conn()
    conn.execute("DELETE FROM jobs")
    conn.commit()
    conn.close()
    yield


def _finish(job_id, **result_overrides):
    """Mark a job done with a result shaped like run_transcription_job's."""
    result = {
        "filename": "charla.mp4",
        "duration_seconds": 754.5,
        "chapters": [
            {"time": 0.0, "title": "Intro"},
            {"time": 300.0, "title": "Demo"},
        ],
        "segments": [{"start": "00:00:00.000", "text_es": "Hola"}],
        "audio_available": True,
        "dubbed_video_available": True,
        "chapter_clips_available": True,
    }
    result.update(result_overrides)
    jobs_module.update_job(job_id, status="done", stage="done", result=result)


def test_list_exposes_the_four_card_fields(clean_jobs):
    job_id = jobs_module.create_job("charla.mp4")
    _finish(job_id)

    row = jobs_module.list_jobs()[0]

    assert row["id"] == job_id
    assert row["audio_available"]
    assert row["dubbed_video_available"]
    assert row["duration_seconds"] == 754.5
    assert row["chapter_count"] == 2


def test_list_still_withholds_the_result_blob(clean_jobs):
    """The whole point of extracting scalars is not to ship the transcript."""
    job_id = jobs_module.create_job("charla.mp4")
    _finish(job_id)

    row = jobs_module.list_jobs()[0]

    assert "result_json" not in row
    assert "segments" not in row


def test_a_job_without_a_result_reports_the_fields_as_empty(clean_jobs):
    """Queued/running/failed jobs have no result_json at all. They must still
    list -- the grid shows them with a running badge -- with the derived fields
    falsy rather than the query erroring or dropping the row."""
    job_id = jobs_module.create_job("charla.mp4")
    jobs_module.update_job(job_id, status="running", stage="transcribing")

    rows = jobs_module.list_jobs()

    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == job_id
    assert not row["audio_available"]
    assert not row["dubbed_video_available"]
    assert not row["duration_seconds"]
    assert not row["chapter_count"]


def test_dubbing_that_failed_reports_media_as_unavailable(clean_jobs):
    """Narration and dubbing are best-effort: the job completes with the flags
    false, and the card must not offer players for files that were never made."""
    job_id = jobs_module.create_job("charla.mp4")
    _finish(
        job_id,
        audio_available=False,
        dubbed_video_available=False,
        chapter_clips_available=False,
    )

    row = jobs_module.list_jobs()[0]

    assert not row["audio_available"]
    assert not row["dubbed_video_available"]
    assert row["chapter_count"] == 2  # chapters exist even when clips do not


def test_a_job_without_chapters_counts_zero(clean_jobs):
    job_id = jobs_module.create_job("charla.mp4")
    _finish(job_id, chapters=[])

    row = jobs_module.list_jobs()[0]

    assert row["chapter_count"] == 0
