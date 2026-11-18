# Source Comparer

Compare live streams by picture, not by clock. Paste two or more stream URLs. The app records them together with ffmpeg, then slides one recording against the other until the pictures agree. That shift is the delay between those sources.

Every source is compared with every other source. A pair can come back as same content, different content, or inconclusive. Up to 8 sources at once.

## Run

Python 3 and ffmpeg. No packages to install.

```bash
cd /home/stream_source_comparer
python3 app.py
```

Open http://127.0.0.1:8766. Add at least two URLs and click Compare.

The port is the first argument, or the `PORT` environment variable:

```bash
python3 app.py 9000
PORT=9000 python3 app.py
```

ffmpeg must be on `PATH`. Check with `ffmpeg -version`. On Debian or Ubuntu, `sudo apt install ffmpeg`. Without it, Compare stops with "ffmpeg is not installed". The sample button still works.

## Using it

Each source is an http, https, rtmp, rtmps, rtsp, or rtsps URL. Optional headers go one per line, such as `Referer: https://player.example/`. A `User-Agent` header replaces the built-in browser user agent. Other headers are passed to ffmpeg.

Max delay is how far apart the pictures are allowed to be, from 0 to 60 seconds. The default is 15. Recording lasts that value plus 6 seconds, so the default records about 21 seconds. Frames are sampled at 4 per second and scaled to 160×90.

**Try a 3 second delay** builds a local sample. Source 2 is generated 3 seconds behind source 1, and source 3 is a different picture. No stream is opened.

## Tests

```bash
cd /home/stream_source_comparer
python3 -m unittest test_compare.py
```

The tests check delay alignment, verdicts, and the local page. They do not open a live stream.
