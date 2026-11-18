"""Compare two recorded picture sequences, including a time shift.

Frames are (timestamp, rgb24 bytes). Timestamps share one clock: the moment
the first picture arrived, plus the frame index divided by the sample rate.
Sliding one sequence along that clock finds a delay that a same-time snapshot
would miss.
"""

from __future__ import annotations

import bisect
import statistics
from dataclasses import dataclass

FPS = 4
COMPARE_SECONDS = 6.0
DEFAULT_MAX_DELAY = 15.0
MAX_MAX_DELAY = 60.0
MAX_SOURCES = 8
WIDTH = 160
HEIGHT = 90

_MIN_PAIRS = 6
_SHARP_GAP = 0.08


@dataclass(frozen=True)
class FrameFeatures:
    dhash: int
    ahash: int
    color: tuple[int, ...]
    blank: bool
    small: tuple[int, ...]


def compare_sequences(
    frames_a: list[tuple[float, bytes]],
    frames_b: list[tuple[float, bytes]],
    *,
    fps: float = FPS,
    max_delay: float = DEFAULT_MAX_DELAY,
    width: int = WIDTH,
    height: int = HEIGHT,
) -> dict:
    """Return the verdict, the delay that best aligns the pictures, and a score curve."""
    if fps <= 0:
        raise ValueError("Sample rate must be positive.")
    if max_delay < 0 or max_delay > MAX_MAX_DELAY:
        raise ValueError(f"Max delay must be from 0 to {MAX_MAX_DELAY:.0f} seconds.")
    if len(frames_a) < _MIN_PAIRS or len(frames_b) < _MIN_PAIRS:
        raise ValueError(
            "Not enough pictures came back from one of the sources. "
            "Check that both URLs are playing video."
        )

    feats_a = [_features(frame, width, height) for _, frame in frames_a]
    feats_b = [_features(frame, width, height) for _, frame in frames_b]
    times_a = [stamp for stamp, _ in frames_a]
    times_b = [stamp for stamp, _ in frames_b]

    steps = int(round(max_delay * fps))
    lags = [index / fps for index in range(-steps, steps + 1)]
    tolerance = 0.75 / fps
    scored: list[tuple[float, float, list[tuple[int, int]]]] = []
    for lag in lags:
        pairs = _pairs_at_lag(times_a, times_b, feats_a, feats_b, lag, tolerance)
        if len(pairs) < _MIN_PAIRS:
            continue
        rank = statistics.median(_rank(feats_a[i], feats_b[j]) for i, j in pairs)
        scored.append((lag, rank, pairs))

    if not scored:
        blank_a = sum(feature.blank for feature in feats_a)
        blank_b = sum(feature.blank for feature in feats_b)
        if blank_a > len(feats_a) * 0.8 or blank_b > len(feats_b) * 0.8:
            raise ValueError("One recording is blank, so the pictures cannot be compared.")
        raise ValueError(
            "The recordings did not overlap at any delay inside the selected range. "
            "Raise the max delay, or check that both streams stayed up."
        )

    # A flat plateau has many near-equal scores. Use its center so a still
    # picture is not reported as delayed by the whole search window.
    top = max(item[1] for item in scored)
    plateau = [item for item in scored if top - item[1] <= 0.015]
    plateau.sort(key=lambda item: item[0])
    best_lag, best_rank, best_pairs = plateau[len(plateau) // 2]
    by_lag = {lag: rank for lag, rank, _ in scored}
    zero = by_lag.get(0.0)
    if zero is None and steps == 0:
        zero = best_rank

    detail = _detail(feats_a, feats_b, best_pairs)
    verdict = _verdict(detail)
    reliable, steady = _delay_confidence(feats_a, feats_b, scored, best_lag, best_rank, verdict)

    return {
        "verdict": verdict,
        "title": {"same": "Same content", "different": "Different content", "inconclusive": "Inconclusive"}[verdict],
        "summary": _summary(verdict, best_lag, fps, max_delay, reliable, steady, zero, best_rank),
        "delay_seconds": round(best_lag, 4),
        "delay_reliable": reliable,
        "simultaneous_score": None if zero is None else round(zero, 3),
        "aligned_score": round(best_rank, 3),
        "scores": {name: round(value, 3) for name, value in detail.items()},
        "pairs": len(best_pairs),
        "frames_a": len(frames_a),
        "frames_b": len(frames_b),
        "curve": [{"lag": round(lag, 4), "score": round(rank, 3)} for lag, rank, _ in sorted(scored, key=lambda item: item[0])],
        "notes": _notes(reliable, steady, verdict),
        "max_delay": max_delay,
        "sample": False,
    }


def compare_recordings(
    recordings: list[list[tuple[float, bytes]]],
    *,
    fps: float = FPS,
    max_delay: float = DEFAULT_MAX_DELAY,
    width: int = WIDTH,
    height: int = HEIGHT,
    progress=None,
) -> dict:
    """Compare every recording with every other recording."""
    count = len(recordings)
    if count < 2:
        raise ValueError("Add at least two sources.")
    if count > MAX_SOURCES:
        raise ValueError(f"Compare up to {MAX_SOURCES} sources at once.")
    pair_indexes = [(left, right) for left in range(count) for right in range(left + 1, count)]
    comparisons = []
    for done, (left, right) in enumerate(pair_indexes, start=1):
        if progress is not None:
            progress(done, len(pair_indexes), left + 1, right + 1)
        result = compare_sequences(
            recordings[left],
            recordings[right],
            fps=fps,
            max_delay=max_delay,
            width=width,
            height=height,
        )
        comparisons.append({
            "left": left + 1,
            "right": right + 1,
            "verdict": result["verdict"],
            "title": result["title"],
            "summary": _pair_summary(left + 1, right + 1, result, fps),
            "delay_seconds": result["delay_seconds"],
            "delay_reliable": result["delay_reliable"],
            "simultaneous_score": result["simultaneous_score"],
            "aligned_score": result["aligned_score"],
            "scores": result["scores"],
            "pairs": result["pairs"],
            "curve": result["curve"],
        })
    verdicts = {item["verdict"] for item in comparisons}
    if verdicts == {"same"}:
        verdict, title = "same", "Same content"
    elif verdicts == {"different"}:
        verdict, title = "different", "Different content"
    else:
        verdict, title = "mixed", "Mixed"
    return {
        "verdict": verdict,
        "title": title,
        "summary": " ".join(item["summary"] for item in comparisons),
        "source_count": count,
        "comparisons": comparisons,
        "notes": [
            "Every source is compared with every other source. A positive delay means the second source is behind the first.",
            "Hashes and colors are taken from the center of the frame, so a corner logo weighs less.",
        ],
        "max_delay": max_delay,
        "sample": False,
    }


def _pair_summary(left: int, right: int, result: dict, fps: float) -> str:
    delay = result["delay_seconds"]
    if result["verdict"] == "same" and result["delay_reliable"] and abs(delay) <= 0.5 / fps:
        return f"Source {left} and source {right} match at the same instant."
    if result["verdict"] == "same" and result["delay_reliable"] and delay > 0:
        return f"Source {right} matches source {left} and is {_seconds(delay)} behind it."
    if result["verdict"] == "same" and result["delay_reliable"]:
        return f"Source {left} matches source {right} and is {_seconds(abs(delay))} behind it."
    if result["verdict"] == "same":
        return f"Source {left} and source {right} match, but the delay cannot be measured."
    if result["verdict"] == "different":
        return f"Source {left} and source {right} are different."
    return f"Source {left} and source {right} are only a partial match."


def _features(frame: bytes, width: int, height: int) -> FrameFeatures:
    expected = width * height * 3
    if len(frame) != expected:
        raise ValueError("A captured frame had the wrong size.")
    luma = _luma(frame, width, height)
    x0, x1 = width // 7, width - width // 7
    y0, y1 = height // 7, height - height // 7
    crop_w = x1 - x0
    crop_h = y1 - y0
    crop = [luma[y * width + x] for y in range(y0, y1) for x in range(x0, x1)]
    small = tuple(_box(crop, crop_w, crop_h, 32, 18))
    mean = sum(small) / len(small)
    variance = sum((value - mean) ** 2 for value in small) / len(small)
    blank = variance < 36 or mean < 8 or (mean > 247 and variance < 64)
    return FrameFeatures(
        dhash=_dhash(_box(crop, crop_w, crop_h, 9, 8)),
        ahash=_ahash(_box(crop, crop_w, crop_h, 8, 8)),
        color=_color_bins(frame, width, height, x0, x1, y0, y1),
        blank=blank,
        small=small,
    )


def _luma(frame: bytes, width: int, height: int) -> list[int]:
    pixels = width * height
    out = [0] * pixels
    for index in range(pixels):
        offset = index * 3
        out[index] = (frame[offset] * 77 + frame[offset + 1] * 150 + frame[offset + 2] * 29) >> 8
    return out


def _box(src: list[int], src_w: int, src_h: int, dst_w: int, dst_h: int) -> list[int]:
    out = [0] * (dst_w * dst_h)
    for y in range(dst_h):
        y0 = y * src_h // dst_h
        y1 = max(y0 + 1, (y + 1) * src_h // dst_h)
        for x in range(dst_w):
            x0 = x * src_w // dst_w
            x1 = max(x0 + 1, (x + 1) * src_w // dst_w)
            total = 0
            count = 0
            for yy in range(y0, y1):
                row = yy * src_w
                for xx in range(x0, x1):
                    total += src[row + xx]
                    count += 1
            out[y * dst_w + x] = total // count
    return out


def _dhash(grid: list[int]) -> int:
    bits = 0
    for y in range(8):
        row = y * 9
        for x in range(8):
            bits = (bits << 1) | (1 if grid[row + x] > grid[row + x + 1] else 0)
    return bits


def _ahash(grid: list[int]) -> int:
    average = sum(grid) / len(grid)
    bits = 0
    for value in grid:
        bits = (bits << 1) | (1 if value >= average else 0)
    return bits


def _color_bins(frame: bytes, width: int, height: int, x0: int, x1: int, y0: int, y1: int) -> tuple[int, ...]:
    bins = [0] * 24
    for y in range(y0, y1):
        row = y * width
        for x in range(x0, x1):
            offset = (row + x) * 3
            bins[min(7, frame[offset] * 8 // 256)] += 1
            bins[8 + min(7, frame[offset + 1] * 8 // 256)] += 1
            bins[16 + min(7, frame[offset + 2] * 8 // 256)] += 1
    return tuple(bins)


def _pairs_at_lag(
    times_a: list[float],
    times_b: list[float],
    feats_a: list[FrameFeatures],
    feats_b: list[FrameFeatures],
    lag: float,
    tolerance: float,
) -> list[tuple[int, int]]:
    used: set[int] = set()
    pairs: list[tuple[int, int]] = []
    for index_a, stamp_a in enumerate(times_a):
        if feats_a[index_a].blank:
            continue
        target = stamp_a + lag
        nearest = bisect.bisect_left(times_b, target)
        choices = []
        if nearest < len(times_b):
            choices.append(nearest)
        if nearest > 0:
            choices.append(nearest - 1)
        if not choices:
            continue
        index_b = min(choices, key=lambda choice: abs(times_b[choice] - target))
        if index_b in used or feats_b[index_b].blank:
            continue
        if abs(times_b[index_b] - target) <= tolerance:
            used.add(index_b)
            pairs.append((index_a, index_b))
    return pairs


def _rank(left: FrameFeatures, right: FrameFeatures) -> float:
    """Prefer structure. Hashes alone treat a small shift of a flat picture as a match."""
    dhash = 1 - _hamming(left.dhash, right.dhash) / 64
    pixels = _pixel_similarity(left.small, right.small)
    structure = max(0.0, _ssim(left.small, right.small))
    return 0.15 * dhash + 0.25 * pixels + 0.60 * structure


def _detail(feats_a: list[FrameFeatures], feats_b: list[FrameFeatures], pairs: list[tuple[int, int]]) -> dict[str, float]:
    dhashes = []
    colors = []
    pixels = []
    ssims = []
    for index_a, index_b in pairs:
        left = feats_a[index_a]
        right = feats_b[index_b]
        dhashes.append(1 - _hamming(left.dhash, right.dhash) / 64)
        colors.append(_cosine(left.color, right.color))
        pixels.append(_pixel_similarity(left.small, right.small))
        ssims.append(_ssim(left.small, right.small))
    return {
        "dhash": statistics.median(dhashes),
        "color": statistics.median(colors),
        "pixels": statistics.median(pixels),
        "ssim": statistics.median(ssims),
    }


def _verdict(detail: dict[str, float]) -> str:
    structure = detail["ssim"]
    if structure >= 0.72 and detail["pixels"] >= 0.80 and detail["dhash"] >= 0.68:
        return "same"
    if structure <= 0.40:
        return "different"
    if structure <= 0.55 and detail["color"] <= 0.50:
        return "different"
    return "inconclusive"


def _delay_confidence(
    feats_a: list[FrameFeatures],
    feats_b: list[FrameFeatures],
    scored: list[tuple[float, float, list[tuple[int, int]]]],
    best_lag: float,
    best_rank: float,
    verdict: str,
) -> tuple[bool, bool]:
    far = [rank for lag, rank, _ in scored if abs(lag - best_lag) >= 1]
    gap = None if not far else best_rank - statistics.median(far)
    steady = _motion(feats_a) < 0.04 and _motion(feats_b) < 0.04
    if verdict != "same":
        return False, steady
    # A flat curve means every delay looks alike, including zero. That is not
    # evidence the sources are in step; a still frame matches at every shift.
    if gap is None:
        return True, steady
    return gap >= _SHARP_GAP, steady


def _motion(feats: list[FrameFeatures]) -> float:
    distances = [
        _hamming(left.dhash, right.dhash) / 64
        for left, right in zip(feats, feats[1:])
        if not left.blank and not right.blank
    ]
    if not distances:
        return 0.0
    return statistics.median(distances)


def _summary(
    verdict: str,
    delay: float,
    fps: float,
    max_delay: float,
    reliable: bool,
    steady: bool,
    simultaneous: float | None,
    aligned: float,
) -> str:
    if verdict == "same" and reliable and abs(delay) <= 0.5 / fps:
        return "The pictures agree at the same instant. Any delay is under one frame."
    if verdict == "same" and reliable:
        behind = "B" if delay > 0 else "A"
        amount = _seconds(abs(delay))
        same_time = ""
        if simultaneous is not None and aligned - simultaneous >= 0.08:
            same_time = " A snapshot taken at the same instant would have missed this."
        return f"Source {behind} is {amount} behind the other source.{same_time}"
    if verdict == "same" and steady:
        return "The pictures match, but they barely change, so the delay cannot be measured."
    if verdict == "same":
        return (
            f"The pictures match, but several delays score about as well. "
            f"The best shift is {_seconds(abs(delay))}, and that estimate is weak."
        )
    if verdict == "different":
        window = _seconds(max_delay)
        return f"The pictures stay different at every shift from -{window} to +{window}."
    return "The best alignment is only a partial match, so this is not a clear same or different."


def _notes(reliable: bool, steady: bool, verdict: str) -> list[str]:
    notes = [
        "The chart slides source B relative to source A. A positive delay means B is behind A.",
        "Hashes and colors are taken from the center of the frame, so a corner logo weighs less.",
    ]
    if verdict == "same" and not reliable and steady:
        notes.append("Both pictures stay almost still, so many delays look equally good.")
    elif verdict == "same" and reliable:
        notes.append("The peak is sharper than the delays around it, so the time shift is usable.")
    return notes


def _seconds(value: float) -> str:
    text = f"{value:.2f}".rstrip("0").rstrip(".")
    unit = "second" if text == "1" else "seconds"
    return f"{text} {unit}"


def _hamming(left: int, right: int) -> int:
    return (left ^ right).bit_count()


def _cosine(left: tuple[int, ...], right: tuple[int, ...]) -> float:
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = sum(a * a for a in left) ** 0.5
    right_norm = sum(b * b for b in right) ** 0.5
    if left_norm == 0 or right_norm == 0:
        return 1.0 if left_norm == 0 and right_norm == 0 else 0.0
    return dot / (left_norm * right_norm)


def _pixel_similarity(left: tuple[int, ...], right: tuple[int, ...]) -> float:
    error = sum(abs(a - b) for a, b in zip(left, right)) / len(left)
    return 1 - error / 255


def _ssim(left: tuple[int, ...], right: tuple[int, ...]) -> float:
    count = len(left)
    mean_left = sum(left) / count
    mean_right = sum(right) / count
    var_left = sum((value - mean_left) ** 2 for value in left) / count
    var_right = sum((value - mean_right) ** 2 for value in right) / count
    cov = sum((a - mean_left) * (b - mean_right) for a, b in zip(left, right)) / count
    c1 = (0.01 * 255) ** 2
    c2 = (0.03 * 255) ** 2
    return ((2 * mean_left * mean_right + c1) * (2 * cov + c2)) / (
        (mean_left ** 2 + mean_right ** 2 + c1) * (var_left + var_right + c2)
    )


def paint_scene(content_id: int, width: int, height: int, kind: str = "program") -> bytes:
    """A moving picture whose identity is content_id. Different kinds do not match."""
    buffer = bytearray(width * height * 3)
    # 251 is prime and larger than any delay the tests search, so a distant
    # frame does not alias back into a nearby one.
    if kind == "program":
        for y in range(height):
            row = y * width
            for x in range(width):
                offset = (row + x) * 3
                phase = (x * 3 + y * 5 + content_id * 29) % 251
                buffer[offset] = 20 + phase % 90
                buffer[offset + 1] = 30 + (phase * 2) % 70
                buffer[offset + 2] = 40 + (phase * 3) % 140
        panel_w = max(8, width // 2)
        panel_h = max(8, height // 2)
        left = (content_id * 13) % (width - panel_w)
        top = (content_id * 7) % (height - panel_h)
        color = (236, 168, 36)
    else:
        for y in range(height):
            row = y * width
            for x in range(width):
                offset = (row + x) * 3
                phase = (x * 5 + y * 2 + content_id * 37) % 251
                checker = 210 if ((x // 6) + (y // 6) + content_id) % 2 == 0 else 20
                buffer[offset] = checker
                buffer[offset + 1] = 25 + phase % 80
                buffer[offset + 2] = 60 + (phase % 50)
        panel_w = max(8, width // 3)
        panel_h = max(8, height // 2)
        left = (content_id * 11) % (width - panel_w)
        top = (content_id * 5) % (height - panel_h)
        color = (24, 196, 154)
    for y in range(top, min(height, top + panel_h)):
        row = y * width
        for x in range(left, min(width, left + panel_w)):
            offset = (row + x) * 3
            buffer[offset] = color[0]
            buffer[offset + 1] = color[1]
            buffer[offset + 2] = color[2]
    return bytes(buffer)


def perturb(frame: bytes, seed: int) -> bytes:
    """Mild brightness and noise, the sort of change a second encode introduces."""
    out = bytearray(frame)
    state = seed & 0xFFFFFFFF or 1
    for index in range(0, len(out), 3):
        state = (1664525 * state + 1013904223) & 0xFFFFFFFF
        delta = (state % 17) - 6
        for channel in range(3):
            out[index + channel] = max(0, min(255, out[index + channel] + 10 + delta))
    return bytes(out)


def demo_sequences(
    delay_seconds: float = 3.0,
    *,
    fps: float = FPS,
    width: int = WIDTH,
    height: int = HEIGHT,
    max_delay: float = 8.0,
) -> tuple[list[tuple[float, bytes]], list[tuple[float, bytes]], float]:
    """Build two recordings of one moving scene, with source B behind by delay_seconds."""
    shift = int(round(delay_seconds * fps))
    count = int(round((max_delay + COMPARE_SECONDS) * fps))
    origin = shift + 4
    frames_a = []
    frames_b = []
    for index in range(count):
        stamp = 0.4 + index / fps
        frames_a.append((stamp, paint_scene(origin + index, width, height, "program")))
        frames_b.append((stamp + 0.03, perturb(paint_scene(origin + index - shift, width, height, "program"), index + 9)))
    return frames_a, frames_b, delay_seconds


def demo_recordings(
    delay_seconds: float = 3.0,
    *,
    fps: float = FPS,
    width: int = WIDTH,
    height: int = HEIGHT,
    max_delay: float = 8.0,
) -> tuple[list[list[tuple[float, bytes]]], float]:
    """Three recordings: source 2 is behind source 1, and source 3 is a different picture."""
    frames_a, frames_b, shift = demo_sequences(
        delay_seconds, fps=fps, width=width, height=height, max_delay=max_delay
    )
    frames_c = [(stamp, paint_scene(index + 3, width, height, "other")) for index, (stamp, _) in enumerate(frames_a)]
    return [frames_a, frames_b, frames_c], shift
