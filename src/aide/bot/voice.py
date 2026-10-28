"""Local transcription of voice notes with mlx-whisper.

The audio is converted with ffmpeg to 16 kHz, mono, and passed to mlx-whisper as samples, so it does
not depend on ffmpeg being in the library's PATH. The Homebrew ffmpeg is used if it exists and, if
not, the one `imageio-ffmpeg` brings inside the project environment. Everything happens on the Mac:
the audio does not leave for any service.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
import threading
from pathlib import Path

import numpy as np

from aide.common.config import Config

log = logging.getLogger(__name__)

SAMPLING = 16_000
MAX_SECONDS = 20 * 60


def ffmpeg_path() -> str:
    """Returns the system ffmpeg path, or the one bundled with imageio_ffmpeg."""
    system = shutil.which("ffmpeg")
    if system:
        return system
    import imageio_ffmpeg

    return imageio_ffmpeg.get_ffmpeg_exe()


def load_audio(path: Path) -> np.ndarray:
    """Audio as float32 at 16 kHz and mono (what Whisper expects)."""
    order = [
        ffmpeg_path(),
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(path),
        "-t",
        str(MAX_SECONDS),
        "-f",
        "s16le",
        "-ac",
        "1",
        "-acodec",
        "pcm_s16le",
        "-ar",
        str(SAMPLING),
        "-",
    ]
    result = subprocess.run(order, capture_output=True, check=False, timeout=300)
    if result.returncode != 0:
        message = result.stderr.decode(errors="replace").strip()[-300:]
        raise RuntimeError(f"ffmpeg could not read the audio: {message}")
    return np.frombuffer(result.stdout, np.int16).astype(np.float32) / 32768.0


def model_path(repo: str) -> str:
    """Local model folder: from the cache without querying the network and, if missing,
    downloaded once."""
    if Path(repo).expanduser().is_dir():
        return str(Path(repo).expanduser())
    from huggingface_hub import snapshot_download

    try:
        return snapshot_download(repo, local_files_only=True)
    except Exception:
        log.info("downloading the voice model %s", repo)
        return snapshot_download(repo)


class Transcriber:
    """Transcribes voice notes to text with the configured model and language."""

    def __init__(self, config: Config) -> None:
        self.model = config.settings.voice.model
        self.language = config.settings.voice.language or None
        # mlx uses the GPU: one transcription at a time.
        self._lock = threading.Lock()
        self._path: str | None = None

    async def transcribe(self, path: Path) -> str:
        return await asyncio.to_thread(self._transcribe, Path(path))

    def _transcribe(self, path: Path) -> str:
        audio = load_audio(path)
        if audio.size < SAMPLING // 4:
            return ""
        import mlx_whisper

        with self._lock:
            if self._path is None:
                self._path = model_path(self.model)
            result = mlx_whisper.transcribe(
                audio, path_or_hf_repo=self._path, language=self.language, verbose=None
            )
        text = str(result.get("text", "")).strip()
        log.info("voice note transcribed (%.1f s, %d characters)", audio.size / SAMPLING, len(text))
        return text
