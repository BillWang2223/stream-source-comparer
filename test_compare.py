"""Delay alignment, verdicts, and the local page."""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from app import Handler, parse_headers, parse_max_delay, validate_url
from compare import compare_recordings, demo_recordings
from capture import capture_stream
from compare import FPS, HEIGHT, WIDTH, compare_sequences, demo_sequences, paint_scene, perturb


def _sequence(content_ids, stamps, kind="program", noisy=False, width=WIDTH, height=HEIGHT):
    frames = []
    for index, (content_id, stamp) in enumerate(zip(content_ids, stamps)):
        frame = paint_scene(content_id, width, height, kind)
        if noisy:
            frame = perturb(frame, index + 3)
        frames.append((stamp, frame))
    return frames


def _timeline(count, start=0.2, fps=FPS):
    return [start + index / fps for index in range(count)]


class CompareTests(unittest.TestCase):
    def test_delayed_copy_reports_the_shift(self):
        count = 48
        shift = 12
        origin = 20
        times = _timeline(count)
        frames_a = _sequence(range(origin, origin + count), times)
        frames_b = _sequence(range(origin - shift, origin - shift + count), [stamp + 0.02 for stamp in times], noisy=True)
        result = compare_sequences(frames_a, frames_b, fps=FPS, max_delay=8)
        self.assertEqual(result["verdict"], "same")
        self.assertTrue(result["delay_reliable"])
        self.assertAlmostEqual(result["delay_seconds"], shift / FPS, delta=0.3)
        self.assertGreater(result["aligned_score"], result["simultaneous_score"] + 0.1)
        self.assertIn("behind", result["summary"])

    def test_synced_sources_have_no_delay(self):
        count = 32
        times = _timeline(count)
        content = range(10, 10 + count)
        result = compare_sequences(
            _sequence(content, times),
            _sequence(content, [stamp + 0.01 for stamp in times], noisy=True),
            fps=FPS,
            max_delay=5,
        )
        self.assertEqual(result["verdict"], "same")
        self.assertTrue(result["delay_reliable"])
        self.assertAlmostEqual(result["delay_seconds"], 0, delta=0.3)
        self.assertIn("same instant", result["summary"])

    def test_late_connection_is_not_a_content_delay(self):
        fps = FPS
        frames_a = []
        frames_b = []
        for index in range(36):
            stamp_a = 0.2 + index / fps
            frames_a.append((stamp_a, paint_scene(round(stamp_a * fps), WIDTH, HEIGHT)))
            stamp_b = 2.0 + index / fps
            frames_b.append((stamp_b, perturb(paint_scene(round(stamp_b * fps), WIDTH, HEIGHT), index + 1)))
        result = compare_sequences(frames_a, frames_b, fps=fps, max_delay=6)
        self.assertEqual(result["verdict"], "same")
        self.assertAlmostEqual(result["delay_seconds"], 0, delta=0.5)

    def test_different_programs_stay_different(self):
        count = 32
        times = _timeline(count)
        content = range(4, 4 + count)
        result = compare_sequences(
            _sequence(content, times, "program"),
            _sequence(content, times, "other"),
            fps=FPS,
            max_delay=4,
        )
        self.assertEqual(result["verdict"], "different")
        self.assertFalse(result["delay_reliable"])

    def test_delay_outside_the_window_is_not_called_same(self):
        count = 24
        times = _timeline(count)
        frames_a = _sequence(range(30, 30 + count), times)
        frames_b = _sequence(range(30 - 20, 30 - 20 + count), times)
        result = compare_sequences(frames_a, frames_b, fps=FPS, max_delay=1)
        self.assertNotEqual(result["verdict"], "same")

    def test_still_picture_matches_without_a_measured_delay(self):
        frame = paint_scene(7, WIDTH, HEIGHT)
        frames_a = [(index / FPS, frame) for index in range(24)]
        frames_b = [(index / FPS + 0.02, perturb(frame, 1)) for index in range(24)]
        result = compare_sequences(frames_a, frames_b, fps=FPS, max_delay=4)
        self.assertEqual(result["verdict"], "same")
        self.assertFalse(result["delay_reliable"])
        self.assertIn("barely change", result["summary"])
        self.assertAlmostEqual(result["delay_seconds"], 0, delta=0.5)

    def test_blank_recording_is_rejected(self):
        black = bytes(WIDTH * HEIGHT * 3)
        frames = [(index / FPS, black) for index in range(16)]
        picture = _sequence(range(16), _timeline(16))
        with self.assertRaises(ValueError):
            compare_sequences(picture, frames, fps=FPS, max_delay=2)

    def test_demo_sample_finds_three_seconds(self):
        frames_a, frames_b, shift = demo_sequences(3)
        result = compare_sequences(frames_a, frames_b, fps=FPS, max_delay=8)
        self.assertEqual(result["verdict"], "same")
        self.assertTrue(result["delay_reliable"])
        self.assertAlmostEqual(result["delay_seconds"], shift, delta=0.3)

    def test_every_pair_is_compared(self):
        recordings, shift = demo_recordings(3)
        result = compare_recordings(recordings, fps=FPS, max_delay=8)
        self.assertEqual(result["source_count"], 3)
        self.assertEqual(result["verdict"], "mixed")
        pairs = {(item["left"], item["right"]): item for item in result["comparisons"]}
        self.assertEqual(set(pairs), {(1, 2), (1, 3), (2, 3)})
        self.assertEqual(pairs[(1, 2)]["verdict"], "same")
        self.assertAlmostEqual(pairs[(1, 2)]["delay_seconds"], shift, delta=0.3)
        self.assertEqual(pairs[(1, 3)]["verdict"], "different")
        self.assertEqual(pairs[(2, 3)]["verdict"], "different")
        self.assertNotIn("preview", result)


class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_page_and_demo(self):
        page = urllib.request.urlopen(self.base + "/", timeout=5)
        body = page.read().decode("utf-8")
        self.assertIn("Source Comparer", body)
        self.assertIn("Add source", body)
        self.assertIn("every other source", body)
        self.assertIn('max="60"', body)
        result = self._post("/api/demo", {"delay_seconds": 3})
        self.assertEqual(result["verdict"], "mixed")
        self.assertEqual(result["source_count"], 3)
        self.assertEqual(len(result["comparisons"]), 3)
        self.assertTrue(result["sample"])
        self.assertEqual(result["comparisons"][0]["left"], 1)
        self.assertEqual(result["comparisons"][0]["right"], 2)
        self.assertEqual(result["comparisons"][0]["verdict"], "same")
        self.assertAlmostEqual(result["comparisons"][0]["delay_seconds"], 3, delta=0.3)
        self.assertEqual(result["comparisons"][1]["verdict"], "different")
        self.assertEqual(result["comparisons"][2]["verdict"], "different")
        self.assertNotIn("preview", result)
        self.assertNotIn("png", str(result).lower())

    def test_rejects_a_bad_url_before_recording(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self._post("/api/compare", {
                "sources": [
                    {"url": "file:///etc/passwd"},
                    {"url": "http://example.com/a.m3u8"},
                ],
                "max_delay": 1,
            })
        self.assertEqual(caught.exception.code, 400)

    def test_header_and_url_checks(self):
        self.assertEqual(validate_url("https://cdn.example/live/a.m3u8"), "https://cdn.example/live/a.m3u8")
        with self.assertRaises(ValueError):
            validate_url("http://metadata.google.internal/latest")
        headers = parse_headers("Referer: https://player.example/\nUser-Agent: tester\n")
        self.assertEqual(headers[0], ("Referer", "https://player.example/"))
        self.assertEqual(parse_max_delay(60), 60)
        with self.assertRaises(ValueError):
            parse_max_delay(61)

    def _post(self, path, payload):
        request = urllib.request.Request(
            self.base + path,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)


@unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg is required")
class CaptureTests(unittest.TestCase):
    def test_encoded_delay_survives_ffmpeg(self):
        count = 44
        shift = 12
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            video_a = folder / "a.mp4"
            video_b = folder / "b.mp4"
            _write_video(video_a, [paint_scene(30 + index, WIDTH, HEIGHT) for index in range(count)])
            _write_video(video_b, [paint_scene(30 - shift + index, WIDTH, HEIGHT) for index in range(count)])
            started = time.monotonic()
            frames_a = capture_stream(str(video_a), 11, [], started)
            frames_b = capture_stream(str(video_b), 11, [], started)
        result = compare_sequences(frames_a, frames_b, fps=FPS, max_delay=6)
        self.assertEqual(result["verdict"], "same")
        self.assertTrue(result["delay_reliable"])
        self.assertAlmostEqual(result["delay_seconds"], shift / FPS, delta=0.5)

    def test_encoded_different_programs(self):
        count = 28
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            video_a = folder / "a.mp4"
            video_b = folder / "b.mp4"
            _write_video(video_a, [paint_scene(index, WIDTH, HEIGHT, "program") for index in range(count)])
            _write_video(video_b, [paint_scene(index, WIDTH, HEIGHT, "other") for index in range(count)])
            started = time.monotonic()
            frames_a = capture_stream(str(video_a), 7, [], started)
            frames_b = capture_stream(str(video_b), 7, [], started)
        result = compare_sequences(frames_a, frames_b, fps=FPS, max_delay=3)
        self.assertEqual(result["verdict"], "different")


def _write_video(path: Path, frames: list[bytes]) -> None:
    command = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "rgb24",
        "-s",
        f"{WIDTH}x{HEIGHT}",
        "-r",
        str(FPS),
        "-i",
        "pipe:0",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-preset",
        "ultrafast",
        str(path),
    ]
    subprocess.run(command, input=b"".join(frames), check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


if __name__ == "__main__":
    unittest.main()
