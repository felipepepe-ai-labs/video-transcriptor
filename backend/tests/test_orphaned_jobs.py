"""A job whose worker died must not keep presenting itself as alive.

Workers run as FastAPI BackgroundTasks inside the server process. When that
process goes away -- a restart, a crash, a reload on file save -- the thread
goes with it, but the row keeps whatever status it last wrote. One such job sat
at 'running' for 21 hours with nothing behind it, and the UI polled it forever.

A freshly started process has no workers by definition, so anything still marked
running or queued at startup is dead.
"""

import pytest
from fastapi.testclient import TestClient

import app as app_module
import jobs as jobs_module


@pytest.fixture
def clean_jobs():
    """conftest points the whole session at one throwaway DB; these tests count
    what they reap, so they need it empty rather than merely disposable."""
    conn = jobs_module._get_conn()
    conn.execute("DELETE FROM jobs")
    conn.commit()
    conn.close()
    yield


def test_running_jobs_are_failed(clean_jobs):
    job_id = jobs_module.create_job("charla.mp4")
    jobs_module.update_job(job_id, status="running", stage="uploading")

    reaped = jobs_module.fail_orphaned_jobs()

    job = jobs_module.get_job(job_id)
    assert reaped == 1
    assert job["status"] == "failed"
    assert job["error"]


def test_queued_jobs_are_failed_too(clean_jobs):
    """Queued means the task was accepted but never picked up -- equally dead."""
    job_id = jobs_module.create_job("charla.mp4")

    assert jobs_module.fail_orphaned_jobs() == 1
    assert jobs_module.get_job(job_id)["status"] == "failed"


def test_finished_jobs_are_left_alone(clean_jobs):
    done = jobs_module.create_job("done.mp4")
    jobs_module.update_job(done, status="done", stage="done", result={"segments": []})
    failed = jobs_module.create_job("failed.mp4")
    jobs_module.update_job(failed, status="failed", error="boom")

    assert jobs_module.fail_orphaned_jobs() == 0
    assert jobs_module.get_job(done)["status"] == "done"
    assert jobs_module.get_job(failed)["error"] == "boom"


def test_the_error_says_why_rather_than_just_failing(clean_jobs):
    """'failed' with no reason would look like the transcription itself broke."""
    job_id = jobs_module.create_job("charla.mp4")
    jobs_module.update_job(job_id, status="running", stage="uploading")

    jobs_module.fail_orphaned_jobs()

    error = jobs_module.get_job(job_id)["error"].lower()
    assert "interrupt" in error or "restart" in error


def test_starting_the_app_reconciles_them(clean_jobs):
    """The whole point: it happens on boot, without anyone asking."""
    job_id = jobs_module.create_job("charla.mp4")
    jobs_module.update_job(job_id, status="running", stage="transcribing")

    # The context manager is what triggers lifespan startup.
    with TestClient(app_module.app):
        pass

    assert jobs_module.get_job(job_id)["status"] == "failed"
