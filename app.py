#!/usr/bin/env python3
"""Source Comparer.

Run:
    python3 app.py

Then open http://127.0.0.1:8766 and add the stream URLs.
ffmpeg records every source at once. Every source is compared with every
other source, and the page reports the delay where those pictures agree.
"""

from __future__ import annotations

import json
import re
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from capture import CaptureError, capture_many
from compare import (
    COMPARE_SECONDS,
    DEFAULT_MAX_DELAY,
    FPS,
    HEIGHT,
    MAX_MAX_DELAY,
    MAX_SOURCES,
    WIDTH,
    compare_recordings,
    demo_recordings,
)

_HEADER_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")
_ALLOWED_SCHEMES = {"http", "https", "rtmp", "rtmps", "rtsp", "rtsps"}
_BLOCKED_HOSTS = {"metadata.google.internal"}
_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()


def recording_seconds(max_delay: float) -> float:
    return max_delay + COMPARE_SECONDS


def validate_url(value: str) -> str:
    url = value.strip()
    if not url:
        raise ValueError("Enter a stream URL for every source.")
    if len(url) > 2048:
        raise ValueError("That URL is too long.")
    if any(char in url for char in "\r\n\t "):
        raise ValueError("A URL cannot contain spaces or line breaks.")
    parsed = urlparse(url)
    if parsed.scheme.lower() not in _ALLOWED_SCHEMES or not parsed.netloc:
        raise ValueError("Use an http, https, rtmp, or rtsp URL.")
    host = (parsed.hostname or "").lower()
    if not host or host in _BLOCKED_HOSTS:
        raise ValueError("That host is not allowed.")
    return url


def parse_headers(text: str) -> list[tuple[str, str]]:
    headers = []
    for raw in str(text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if ":" not in line:
            raise ValueError("Each header needs the form Name: value.")
        name, value = line.split(":", 1)
        name = name.strip()
        value = value.strip()
        if not _HEADER_NAME.fullmatch(name):
            raise ValueError("A header name contains characters that are not allowed.")
        if any(char in value for char in "\r\n"):
            raise ValueError("Header values cannot contain line breaks.")
        headers.append((name, value))
    if len(headers) > 20:
        raise ValueError("Use 20 headers or fewer on each source.")
    return headers


def parse_max_delay(value) -> float:
    if value is None or value == "":
        return DEFAULT_MAX_DELAY
    try:
        delay = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("Max delay must be a number of seconds.") from exc
    if delay < 0 or delay > MAX_MAX_DELAY:
        raise ValueError(f"Max delay must be from 0 to {MAX_MAX_DELAY:.0f} seconds.")
    return delay


def parse_sources(payload: dict) -> list[tuple[str, list[tuple[str, str]]]]:
    raw = payload.get("sources")
    if not isinstance(raw, list) or len(raw) < 2:
        raise ValueError("Add at least two sources.")
    if len(raw) > MAX_SOURCES:
        raise ValueError(f"Compare up to {MAX_SOURCES} sources at once.")
    sources = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("Each source needs a URL.")
        sources.append((validate_url(str(item.get("url") or "")), parse_headers(item.get("headers") or "")))
    return sources


def start_compare(sources: list[tuple[str, list[tuple[str, str]]]], max_delay: float) -> str:
    job_id = uuid.uuid4().hex[:16]
    seconds = recording_seconds(max_delay)
    count = len(sources)
    job = {
        "state": "recording",
        "message": f"Recording {count} sources for about {seconds:.0f} seconds.",
        "result": None,
        "error": None,
    }
    with _jobs_lock:
        _jobs[job_id] = job
        extra = list(_jobs)[:-30]
        for old in extra:
            _jobs.pop(old, None)
    threading.Thread(target=_run_job, args=(job, sources, max_delay), daemon=True).start()
    return job_id


def job_snapshot(job_id: str) -> dict | None:
    with _jobs_lock:
        job = _jobs.get(job_id)
        if job is None:
            return None
        return {
            "state": job["state"],
            "message": job["message"],
            "result": job["result"],
            "error": job["error"],
        }


def run_demo(delay_seconds: float = 3.0) -> dict:
    delay = parse_max_delay(delay_seconds)
    if delay <= 0 or delay > 8:
        delay = 3.0
    recordings, shift = demo_recordings(delay)
    result = compare_recordings(recordings, fps=FPS, max_delay=8, width=WIDTH, height=HEIGHT)
    result["sample"] = True
    result["notes"] = [
        (
            f"Sample only. Source 2 was generated {shift:.0f} seconds behind source 1. "
            "Source 3 is a different picture. No stream was opened."
        ),
        *result["notes"],
    ]
    return result


def _run_job(job, sources, max_delay) -> None:
    try:
        recordings = capture_many(sources, recording_seconds(max_delay))

        def progress(done: int, total: int, left: int, right: int) -> None:
            with _jobs_lock:
                job["state"] = "aligning"
                job["message"] = f"Comparing source {left} with source {right} ({done} of {total})."

        result = compare_recordings(
            recordings,
            fps=FPS,
            max_delay=max_delay,
            width=WIDTH,
            height=HEIGHT,
            progress=progress,
        )
        with _jobs_lock:
            job["state"] = "done"
            job["message"] = result["summary"]
            job["result"] = result
    except (CaptureError, ValueError) as exc:
        with _jobs_lock:
            job["state"] = "error"
            job["error"] = str(exc)
            job["message"] = str(exc)
    except Exception:
        with _jobs_lock:
            job["state"] = "error"
            job["error"] = "The comparison stopped on an unexpected error. Details are in the server log."
            job["message"] = job["error"]


PAGE = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <meta name="referrer" content="no-referrer" />
  <title>Source Comparer</title>
  <style>
    :root {
      color-scheme: dark;
      --bg: #12140f;
      --bg-2: #1c2018;
      --ink: #f3f1e6;
      --muted: #a4a594;
      --line: #34392c;
      --lamp: #d6ff4a;
      --lamp-ink: #172000;
      --danger: #ff8d6b;
      --amber: #f0c27a;
      --serif: "Liberation Serif", Georgia, "Times New Roman", serif;
      --sans: "Liberation Sans", "Segoe UI", sans-serif;
      --mono: "Liberation Mono", "DejaVu Sans Mono", ui-monospace, monospace;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      background:
        radial-gradient(900px 420px at 8% -10%, rgba(214, 255, 74, 0.08), transparent 55%),
        var(--bg);
      color: var(--ink);
      font-family: var(--sans);
      font-size: 16px;
      line-height: 1.5;
    }
    main {
      width: min(1120px, calc(100% - 32px));
      margin: 0 auto;
      padding: 40px 0 72px;
    }
    .eyebrow {
      display: flex;
      align-items: center;
      gap: 8px;
      margin: 0 0 8px;
      letter-spacing: 0.14em;
      text-transform: uppercase;
      font-size: 12px;
      color: var(--lamp);
    }
    .lamp {
      width: 9px;
      height: 9px;
      border-radius: 50%;
      background: var(--lamp);
      box-shadow: 0 0 12px var(--lamp);
    }
    h1 {
      margin: 0;
      font-family: var(--serif);
      font-size: clamp(40px, 6vw, 64px);
      font-weight: 500;
      letter-spacing: -0.03em;
      line-height: 0.95;
    }
    .lede {
      max-width: 68ch;
      color: var(--muted);
      font-size: 17px;
    }
    .layout {
      display: grid;
      grid-template-columns: 340px 1fr;
      gap: 28px;
      align-items: start;
      margin-top: 28px;
    }
    form, .results {
      background: rgba(28, 32, 24, 0.92);
      border: 1px solid var(--line);
      border-radius: 18px;
      padding: 18px;
    }
    form { position: sticky; top: 16px; }
    label { display: block; margin: 0 0 14px; font-size: 13px; color: var(--muted); }
    input, textarea {
      display: block;
      width: 100%;
      margin-top: 6px;
      padding: 10px 12px;
      border-radius: 10px;
      border: 1px solid var(--line);
      background: #10140e;
      color: var(--ink);
      font: 14px/1.4 var(--mono);
    }
    textarea { min-height: 72px; resize: vertical; }
    input:focus, textarea:focus { outline: 2px solid rgba(214, 255, 74, 0.45); border-color: transparent; }
    .hint { margin: 6px 0 0; font-size: 12px; color: var(--muted); }
    details { margin: 0 0 14px; }
    summary { cursor: pointer; color: var(--muted); font-size: 13px; }
    .actions { display: flex; gap: 8px; flex-wrap: wrap; }
    button {
      appearance: none;
      border: 0;
      border-radius: 999px;
      padding: 10px 16px;
      background: var(--lamp);
      color: var(--lamp-ink);
      font: 600 14px/1 var(--sans);
      cursor: pointer;
    }
    button.secondary { background: transparent; color: var(--ink); border: 1px solid var(--line); }
    button:disabled { opacity: 0.55; cursor: wait; }
    #status { min-height: 1.4em; margin: 12px 0 0; font-size: 14px; }
    #status.error { color: var(--danger); }
    .results h2 { margin: 0 0 8px; font-family: var(--serif); font-weight: 500; font-size: 32px; letter-spacing: -0.03em; }
    .empty { color: var(--muted); margin: 0; }
    .pill {
      display: inline-flex;
      align-items: center;
      border-radius: 999px;
      padding: 4px 10px;
      font-size: 12px;
      font-weight: 700;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      color: var(--lamp-ink);
      background: var(--lamp);
    }
    .pill.different { background: var(--danger); }
    .pill.inconclusive, .pill.mixed { background: var(--amber); }
    .source {
      border: 1px solid var(--line);
      border-radius: 12px;
      padding: 12px 12px 4px;
      margin: 0 0 12px;
    }
    .source-head {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 8px;
    }
    .source-head strong { color: var(--ink); font-size: 14px; }
    button.text {
      background: transparent;
      color: var(--muted);
      border: 0;
      padding: 0;
      font-weight: 500;
    }
    .comparison {
      border-top: 1px solid var(--line);
      margin-top: 16px;
      padding-top: 14px;
    }
    .comparison h3 { margin: 8px 0 0; font-size: 18px; font-weight: 600; }
    .summary { font-size: 18px; margin: 10px 0 16px; }
    dl {
      display: grid;
      grid-template-columns: auto 1fr;
      gap: 4px 16px;
      margin: 16px 0;
      font-variant-numeric: tabular-nums;
    }
    dt { color: var(--muted); }
    dd { margin: 0; font-family: var(--mono); }
    .chart-wrap { margin-top: 8px; }
    canvas {
      width: 100%;
      height: 160px;
      background: #10140e;
      border: 1px solid var(--line);
      border-radius: 12px;
    }
    .chart-caption { color: var(--muted); font-size: 12px; margin: 6px 0 0; }
    ul.notes { margin: 14px 0 0; padding-left: 18px; color: var(--muted); }
    ul.notes li + li { margin-top: 4px; }
    @media (max-width: 860px) {
      .layout { grid-template-columns: 1fr; }
      form { position: static; }
    }
  </style>
</head>
<body>
  <main>
    <p class="eyebrow"><span class="lamp" aria-hidden="true"></span>Picture match</p>
    <h1>Source Comparer</h1>
    <p class="lede">
      Add the stream URLs you want to compare. All of them are recorded together, then every source is compared with every other source.
      The shift where a pair agrees is the delay between those two.
    </p>
    <div class="layout">
      <form id="compare-form">
        <div id="sources"></div>
        <div class="actions">
          <button type="button" class="secondary" id="add-source">Add source</button>
        </div>
        <label>
          Max delay, seconds
          <input id="max-delay" name="max_delay" type="number" min="0" max="60" step="1" value="15" />
        </label>
        <p class="hint" id="delay-hint">Recording lasts the max delay plus 6 seconds. The longest search is 60 seconds, about 66 seconds of recording.</p>
        <div class="actions">
          <button type="submit" id="submit">Compare</button>
          <button type="button" class="secondary" id="sample">Try a 3 second delay</button>
        </div>
        <p id="status" role="status"></p>
      </form>
      <section class="results" aria-live="polite">
        <p class="empty" id="empty">Every pair is compared. The verdicts and delays show up here.</p>
        <div id="report" hidden></div>
      </section>
    </div>
  </main>
  <script>
    const MAX_SOURCES = 8;
    const form = document.querySelector("#compare-form");
    const sourcesEl = document.querySelector("#sources");
    const addButton = document.querySelector("#add-source");
    const statusEl = document.querySelector("#status");
    const submitBtn = document.querySelector("#submit");
    const sampleBtn = document.querySelector("#sample");
    const emptyEl = document.querySelector("#empty");
    const reportEl = document.querySelector("#report");
    const delayInput = document.querySelector("#max-delay");
    const hintEl = document.querySelector("#delay-hint");

    addSource();
    addSource();

    addButton.addEventListener("click", () => addSource());

    function addSource() {
      if (sourcesEl.children.length >= MAX_SOURCES) return;
      const block = document.createElement("div");
      block.className = "source";
      const head = document.createElement("div");
      head.className = "source-head";
      const title = document.createElement("strong");
      title.className = "source-label";
      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "text remove";
      remove.textContent = "Remove";
      remove.addEventListener("click", () => {
        block.remove();
        refreshSources();
      });
      head.append(title, remove);

      const urlLabel = document.createElement("label");
      urlLabel.append("Stream URL");
      const url = document.createElement("input");
      url.className = "url";
      url.required = true;
      url.placeholder = "https://cdn.example/live/channel.m3u8";
      url.spellcheck = false;
      url.autocomplete = "off";
      urlLabel.append(url);

      const details = document.createElement("details");
      const summary = document.createElement("summary");
      summary.textContent = "Headers";
      const headerLabel = document.createElement("label");
      headerLabel.append("One header per line");
      const headers = document.createElement("textarea");
      headers.className = "headers";
      headers.placeholder = "Referer: https://player.example/";
      headers.spellcheck = false;
      headerLabel.append(headers);
      details.append(summary, headerLabel);

      block.append(head, urlLabel, details);
      sourcesEl.append(block);
      refreshSources();
    }

    function refreshSources() {
      const blocks = [...sourcesEl.querySelectorAll(".source")];
      blocks.forEach((block, index) => {
        block.querySelector(".source-label").textContent = "Source " + (index + 1);
        block.querySelector(".remove").hidden = blocks.length <= 2;
      });
      addButton.disabled = blocks.length >= MAX_SOURCES;
    }

    function sourcePayload() {
      return [...sourcesEl.querySelectorAll(".source")].map((block) => ({
        url: block.querySelector(".url").value,
        headers: block.querySelector(".headers").value
      }));
    }

    function setBusy(busy, label) {
      submitBtn.disabled = busy;
      sampleBtn.disabled = busy;
      addButton.disabled = busy || sourcesEl.children.length >= MAX_SOURCES;
      sourcesEl.querySelectorAll(".remove").forEach((button) => { button.disabled = busy; });
      submitBtn.textContent = busy ? label : "Compare";
    }

    delayInput.addEventListener("input", () => {
      const seconds = Number(delayInput.value);
      if (!Number.isFinite(seconds) || seconds < 0) return;
      const total = Math.round(seconds + 6);
      hintEl.textContent = `Recording lasts the max delay plus 6 seconds, so ${seconds} seconds of delay takes about ${total} seconds.`;
    });

    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const sources = sourcePayload();
      await run("/api/compare", { sources, max_delay: delayInput.value }, true, sources.length);
    });

    sampleBtn.addEventListener("click", () => {
      run("/api/demo", { delay_seconds: 3 }, false, 3);
    });

    async function run(path, body, poll, count) {
      statusEl.classList.remove("error");
      statusEl.textContent = poll ? `Opening ${count} sources…` : "Building a delayed sample…";
      setBusy(true, "Working…");
      try {
        const response = await fetch(path, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body)
        });
        const payload = await response.json();
        if (!response.ok) throw new Error(payload.error || "The comparison could not start.");
        const result = poll ? await waitFor(payload.id) : payload;
        render(result);
        statusEl.textContent = result.title + ".";
      } catch (error) {
        statusEl.classList.add("error");
        statusEl.textContent = error.message || "The comparison failed.";
      } finally {
        setBusy(false);
      }
    }

    async function waitFor(id) {
      for (;;) {
        const response = await fetch("/api/jobs/" + id);
        const job = await response.json();
        if (!response.ok) throw new Error(job.error || "That comparison is no longer available.");
        if (job.state === "done") return job.result;
        if (job.state === "error") throw new Error(job.error || "The comparison failed.");
        statusEl.textContent = job.message || "Working…";
        await new Promise((resolve) => setTimeout(resolve, 500));
      }
    }

    function render(result) {
      emptyEl.hidden = true;
      reportEl.hidden = false;
      reportEl.replaceChildren();

      const pill = document.createElement("span");
      pill.className = "pill " + result.verdict;
      pill.textContent = result.title;
      const summary = document.createElement("p");
      summary.className = "summary";
      summary.textContent = result.summary;
      reportEl.append(pill, summary);

      for (const item of result.comparisons || []) {
        reportEl.append(comparisonCard(item));
      }

      const notes = document.createElement("ul");
      notes.className = "notes";
      for (const note of result.notes || []) {
        const entry = document.createElement("li");
        entry.textContent = note;
        notes.appendChild(entry);
      }
      reportEl.append(notes);
    }

    function comparisonCard(item) {
      const card = document.createElement("article");
      card.className = "comparison";
      const heading = document.createElement("h3");
      heading.textContent = "Source " + item.left + " and source " + item.right;
      const rowPill = document.createElement("span");
      rowPill.className = "pill " + item.verdict;
      rowPill.textContent = item.title;
      const line = document.createElement("p");
      line.className = "summary";
      line.textContent = item.summary;
      const scores = document.createElement("dl");
      addScore(scores, "Delay of source " + item.right + " behind source " + item.left, formatDelay(item));
      addScore(scores, "Score at that delay", String(item.aligned_score));
      addScore(scores, "Score at the same instant", item.simultaneous_score == null ? "—" : String(item.simultaneous_score));
      addScore(scores, "Structure", String(item.scores.dhash));
      addScore(scores, "Pixels", String(item.scores.pixels));
      addScore(scores, "Color", String(item.scores.color));
      addScore(scores, "SSIM", String(item.scores.ssim));
      addScore(scores, "Frames compared", String(item.pairs));
      const chartWrap = document.createElement("div");
      chartWrap.className = "chart-wrap";
      const canvas = document.createElement("canvas");
      canvas.width = 720;
      canvas.height = 160;
      canvas.setAttribute("aria-label", "Match score for source " + item.left + " and source " + item.right);
      const caption = document.createElement("p");
      caption.className = "chart-caption";
      caption.textContent = "Match score for this pair. The bright point is the delay used here. Zero is the same instant.";
      chartWrap.append(canvas, caption);
      card.append(heading, rowPill, line, scores, chartWrap);
      drawCurve(canvas, item.curve || [], item.delay_seconds);
      return card;
    }

    function addScore(list, label, value) {
      const dt = document.createElement("dt");
      dt.textContent = label;
      const dd = document.createElement("dd");
      dd.textContent = value;
      list.append(dt, dd);
    }

    function formatDelay(result) {
      const seconds = Number(result.delay_seconds);
      const text = (seconds > 0 ? "+" : "") + seconds.toFixed(2) + " s";
      if (!result.delay_reliable && result.verdict === "same") return text + " · weak";
      if (!result.delay_reliable) return "not measured";
      return text;
    }

    function drawCurve(canvas, curve, bestLag) {
      const ctx = canvas.getContext("2d");
      const width = canvas.width;
      const height = canvas.height;
      ctx.clearRect(0, 0, width, height);
      ctx.fillStyle = "#10140e";
      ctx.fillRect(0, 0, width, height);
      if (!curve.length) return;
      const pad = 28;
      const lags = curve.map((point) => point.lag);
      const minLag = Math.min(...lags);
      const maxLag = Math.max(...lags);
      const span = Math.max(0.001, maxLag - minLag);
      const xOf = (lag) => pad + ((lag - minLag) / span) * (width - pad * 2);
      const yOf = (score) => height - pad - Math.max(0, Math.min(1, score)) * (height - pad * 2);

      ctx.strokeStyle = "#34392c";
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(pad, yOf(0));
      ctx.lineTo(width - pad, yOf(0));
      ctx.moveTo(pad, yOf(1));
      ctx.lineTo(width - pad, yOf(1));
      const zeroX = xOf(0);
      ctx.moveTo(zeroX, pad);
      ctx.lineTo(zeroX, height - pad);
      ctx.stroke();

      ctx.beginPath();
      curve.forEach((point, index) => {
        const x = xOf(point.lag);
        const y = yOf(point.score);
        if (index === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
      });
      ctx.strokeStyle = "#d6ff4a";
      ctx.lineWidth = 2;
      ctx.stroke();

      const best = curve.reduce((pick, point) => Math.abs(point.lag - bestLag) < Math.abs(pick.lag - bestLag) ? point : pick, curve[0]);
      ctx.fillStyle = "#f3f1e6";
      ctx.beginPath();
      ctx.arc(xOf(best.lag), yOf(best.score), 4.5, 0, Math.PI * 2);
      ctx.fill();

      ctx.fillStyle = "#a4a594";
      ctx.font = "12px ui-monospace, monospace";
      ctx.fillText(minLag.toFixed(0) + "s", pad, height - 8);
      ctx.fillText("0", zeroX - 4, height - 8);
      ctx.fillText("+" + maxLag.toFixed(0) + "s", width - pad - 28, height - 8);
      ctx.fillText("1", 8, yOf(1) + 4);
      ctx.fillText("0", 8, yOf(0) + 4);
    }
  </script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    server_version = "SourceComparer/1.0"

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/":
            self._send(200, "text/html; charset=utf-8", PAGE.encode("utf-8"))
            return
        if path == "/favicon.ico":
            self._send(204, "image/x-icon", b"")
            return
        if path.startswith("/api/jobs/"):
            snapshot = job_snapshot(path.removeprefix("/api/jobs/"))
            if snapshot is None:
                self._json(404, {"error": "That comparison is no longer available."})
                return
            self._json(200, snapshot)
            return
        self._send(404, "text/plain; charset=utf-8", b"Not found")

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        payload = self._read_json()
        if payload is None:
            return
        try:
            if path == "/api/compare":
                sources = parse_sources(payload)
                max_delay = parse_max_delay(payload.get("max_delay"))
                job_id = start_compare(sources, max_delay)
                self._json(202, {"id": job_id})
                return
            if path == "/api/demo":
                self._json(200, run_demo(payload.get("delay_seconds", 3)))
                return
        except ValueError as exc:
            self._json(400, {"error": str(exc)})
            return
        except Exception:
            self._json(500, {"error": "The comparer hit an unexpected error. Details are in the server log."})
            return
        self._json(404, {"error": "Not found"})

    def _read_json(self) -> dict | None:
        length = int(self.headers.get("Content-Length") or "0")
        if length > 100000:
            self._json(413, {"error": "Request is too large."})
            return None
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._json(400, {"error": "Request body must be JSON."})
            return None
        if not isinstance(payload, dict):
            self._json(400, {"error": "Request body must be a JSON object."})
            return None
        return payload

    def _json(self, status: int, payload: dict) -> None:
        self._send(status, "application/json; charset=utf-8", json.dumps(payload).encode("utf-8"))

    def _send(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if body:
            self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:
        path = urlparse(self.path).path
        if path.startswith("/api/jobs/"):
            path = "/api/jobs/…"
        print(f"{self.command} {path}", flush=True)


def main() -> None:
    import os
    import sys

    port = int(sys.argv[1]) if len(sys.argv) > 1 else int(os.environ.get("PORT", "8766"))
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"Source Comparer at http://127.0.0.1:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.", flush=True)
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
