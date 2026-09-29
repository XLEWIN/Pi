"""ffmpeg merge for split YouTube streams — stream copy, no re-encode first.

Runs in a worker thread (asyncio.to_thread) so the event loop never blocks.
Uses the imageio-ffmpeg bundled binary when present, else a system ffmpeg.
If neither exists the caller falls back to sending both parts separately.
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from typing import List, Optional

from .metrics import ig_log

_EXE: Optional[str] = None
_EXE_CHECKED = False


def ffmpeg_exe() -> Optional[str]:
    """Path to an ffmpeg binary, or None when unavailable."""
    global _EXE, _EXE_CHECKED
    if _EXE_CHECKED:
        return _EXE
    _EXE_CHECKED = True
    _EXE = None
    try:
        import imageio_ffmpeg

        _EXE = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        _EXE = shutil.which("ffmpeg")
    return _EXE


def merge_command(video: Path, audio: Path, out: Path, *, reencode: bool = False) -> List[str]:
    """Build the ffmpeg argv (pure — unit-testable without running ffmpeg)."""
    exe = ffmpeg_exe() or "ffmpeg"
    cmd = [exe, "-y", "-i", str(video), "-i", str(audio)]
    if reencode:
        cmd += ["-c:v", "libx264", "-preset", "veryfast", "-c:a", "aac"]
    else:
        cmd += ["-c:v", "copy", "-c:a", "copy"]
    cmd += ["-movflags", "+faststart", str(out)]
    return cmd


def _run_merge_sync(video: Path, audio: Path, out: Path) -> bool:
    import subprocess

    exe = ffmpeg_exe()
    if not exe:
        return False
    # First try stream copy (fast, lossless); re-encode only if codecs
    # cannot live inside an MP4 container together.
    for reencode in (False, True):
        cmd = merge_command(video, audio, out, reencode=reencode)
        try:
            proc = subprocess.run(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=300,
            )
        except Exception as e:
            ig_log(f"ffmpeg error: {e}")
            return False
        if proc.returncode == 0 and out.exists() and out.stat().st_size > 0:
            return True
        out.unlink(missing_ok=True)
    return False


async def merge_streams(video: Path, audio: Path, out_dir: Path) -> Optional[Path]:
    """Merge *video*+*audio* into ``out_dir/media.mp4``. None = give up."""
    out = out_dir / "media.mp4"
    ok = await asyncio.to_thread(_run_merge_sync, video, audio, out)
    if ok:
        return out
    ig_log("merge failed — caller should fall back to separate parts")
    return None
