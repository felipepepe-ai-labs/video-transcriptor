"""
YouTube ingestion boundary: downloads a video as MP4 with yt-dlp so the rest
of the pipeline can treat it exactly like an uploaded file. Kept as its own
module (like remote.py for SSH) so tests can fake it at a single seam.
"""
from pathlib import Path

from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError


class DownloadFailed(Exception):
    """The video could not be downloaded (bad URL, private/removed video,
    geo-block, network failure...)."""


def download_video(url: str, dest_path: Path) -> dict:
    """Download `url` as an MP4 at exactly `dest_path`. Returns metadata:
    {"title": str, "duration": float, "chapters": [{"time": s, "title": str}]}
    -- chapters come from the video's own chapter markers when it has them,
    already in the same shape assign_chapters() expects."""
    opts = {
        # Capped at 1080p: the dubbed output doesn't benefit from 4K and the
        # remote disk pre-flight (file size x safety margin) would balk.
        "format": "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "merge_output_format": "mp4",
        "outtmpl": str(dest_path.with_suffix("")) + ".%(ext)s",
        "quiet": True,
        "noprogress": True,
        "noplaylist": True,
    }
    try:
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
    except DownloadError as e:
        raise DownloadFailed(str(e)) from e

    downloaded = Path(ydl.prepare_filename(info)).with_suffix(".mp4")
    if not downloaded.exists():
        raise DownloadFailed(f"yt-dlp reported success but {downloaded.name} is missing")
    if downloaded != dest_path:
        downloaded.replace(dest_path)

    chapters = [
        {"time": ch.get("start_time", 0), "title": ch.get("title", "")}
        for ch in (info.get("chapters") or [])
        if ch.get("title")
    ]
    return {
        "title": info.get("title") or url,
        "duration": float(info.get("duration") or 0),
        "chapters": chapters,
    }
