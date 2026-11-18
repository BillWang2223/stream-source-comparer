"""Record small RGB frames from several stream URLs at the same time."""

from __future__ import annotations

import shutil
import subprocess
import threading
import time
from urllib.parse import urlparse

from compare import FPS, HEIGHT, WIDTH

DEFAULT_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"
)
_VIDEO_FILTER = (
    f"fps={FPS},"
    f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=decrease,"
    f"pad={WIDTH}:{HEIGHT}:(ow-iw)/2:(oh-ih)/2:color=black,"
    "format=rgb24"
)
_FRAME_BYTES = WIDTH * HEIGHT * 3


class CaptureError(Exception):
    """One source could not be recorded."""


def capture_many(
    sources: list[tuple[str, list[tuple[str, str]]]],
    seconds: float,
) -> list[list[tuple[float, bytes]]]:
    """Record every source. Timestamps share the clock that starts just before they open."""
    started = time.monotonic()
    results: dict[int, list[tuple[float, bytes]]] = {}
    errors: dict[int, BaseException] = {}

    def run(index: int, source: str, headers: list[tuple[str, str]]) -> None:
        try:
            results[index] = capture_stream(source, seconds, headers, started)
        except BaseException as exc:
            errors[index] = exc

    threads = []
    for index, (source, headers) in enumerate(sources):
        thread = threading.Thread(target=run, args=(index, source, headers), daemon=True)
        threads.append(thread)
        thread.start()
    for thread in threads:
        thread.join()
    if errors:
        raise CaptureError(_join_errors(errors))
    return [results[index] for index in range(len(sources))]


def capture_pair(
    source_a: str,
    source_b: str,
    seconds: float,
    headers_a: list[tuple[str, str]] | None = None,
    headers_b: list[tuple[str, str]] | None = None,
) -> tuple[list[tuple[float, bytes]], list[tuple[float, bytes]]]:
    """Record two sources on one shared clock."""
    frames = capture_many(
        [(source_a, headers_a or []), (source_b, headers_b or [])],
        seconds,
    )
    return frames[0], frames[1]


def capture_stream(
    source: str,
    seconds: float,
    headers: list[tuple[str, str]],
    started: float,
) -> list[tuple[float, bytes]]:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise CaptureError("ffmpeg is not installed, so the streams cannot be recorded.")
    user_agent, header_blob = _split_headers(headers)
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-probesize",
        "5000000",
        "-analyzeduration",
        "5000000",
    ]
    scheme = urlparse(source).scheme.lower()
    if scheme in {"http", "https", "rtmp", "rtmps", "rtsp", "rtsps"}:
        # user_agent is a protocol option. Passing it to a local file makes ffmpeg stop.
        command.extend(["-user_agent", user_agent])
    if scheme in {"rtsp", "rtsps"}:
        command.extend(["-timeout", "15000000"])
    elif scheme in {"http", "https", "rtmp", "rtmps"}:
        command.extend(["-rw_timeout", "15000000"])
    if header_blob:
        command.extend(["-headers", header_blob])
    command.extend([
        "-i",
        source,
        "-t",
        f"{seconds:.3f}",
        "-an",
        "-vf",
        _VIDEO_FILTER,
        "-f",
        "rawvideo",
        "-pix_fmt",
        "rgb24",
        "pipe:1",
    ])

    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    assert process.stdout is not None
    assert process.stderr is not None
    frames: list[tuple[float, bytes]] = []
    errors: list[bytes] = []
    reader = threading.Thread(target=_read_frames, args=(process.stdout, started, frames), daemon=True)
    logger = threading.Thread(target=_read_all, args=(process.stderr, errors), daemon=True)
    reader.start()
    logger.start()
    deadline = seconds + 30
    finished = _wait(process, deadline)
    reader.join(timeout=2)
    logger.join(timeout=2)
    process.stdout.close()
    process.stderr.close()
    if not finished:
        _stop(process)
        raise CaptureError("Recording timed out before the stream produced enough pictures.")
    if process.returncode not in (0, None) and len(frames) < 6:
        detail = _clean_ffmpeg_error(errors[0] if errors else b"", source)
        raise CaptureError(detail or "ffmpeg could not read that stream.")
    if len(frames) < 6:
        detail = _clean_ffmpeg_error(errors[0] if errors else b"", source)
        raise CaptureError(detail or "The stream ended before a usable picture sequence was recorded.")
    return frames


def _read_frames(stream, started: float, sink: list[tuple[float, bytes]]) -> None:
    pending = b""
    first: float | None = None
    index = 0
    while True:
        chunk = stream.read(65536)
        if not chunk:
            break
        pending += chunk
        while len(pending) >= _FRAME_BYTES:
            frame = pending[:_FRAME_BYTES]
            pending = pending[_FRAME_BYTES:]
            if first is None:
                first = time.monotonic() - started
            sink.append((first + index / FPS, frame))
            index += 1


def _read_all(stream, sink: list[bytes]) -> None:
    sink.append(stream.read())


def _wait(process: subprocess.Popen, timeout: float) -> bool:
    try:
        process.wait(timeout=timeout)
        return True
    except subprocess.TimeoutExpired:
        return False


def _stop(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    try:
        process.kill()
    except OSError:
        return
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        return


def _split_headers(headers: list[tuple[str, str]]) -> tuple[str, str]:
    user_agent = DEFAULT_UA
    lines = []
    for name, value in headers:
        if name.lower() == "user-agent":
            user_agent = value
        else:
            lines.append(f"{name}: {value}")
    blob = "\r\n".join(lines) + "\r\n" if lines else ""
    return user_agent, blob


def _clean_ffmpeg_error(raw: bytes, source: str) -> str:
    text = raw.decode("utf-8", errors="replace").strip()
    text = text.replace(source, "the stream")
    parsed = urlparse(source)
    if parsed.netloc:
        text = text.replace(parsed.netloc, parsed.hostname or "the host")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return ""
    return " ".join(lines[-3:])[:400]


def _join_errors(errors: dict[int, BaseException]) -> str:
    return " ".join(f"Source {index + 1}: {errors[index]}" for index in sorted(errors))
