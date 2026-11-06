#!/usr/bin/env python3
"""
whisper_server.py — Layer 4 of the Whisper hybrid architecture.

Unix socket server that exposes faster-whisper as a JSON-over-socket service.
Lazy-loads the model on first request, keeps it in memory for subsequent calls.

Architecture context (see 2026-08-27-plan-whisper-transcription-tool.md):
  - Layer 4 (this file): Unix socket + Whisper model + JOBS (async transcription)
  - Layer 3: whisper_mcp_server.py (JSON-RPC stdio MCP wrapper)
  - Layer 1: Hermes skill "video-transcribe"

Job-based design (fix 2026-08-28):
  Long transcriptions (~7 min CPU for a 20-min video) exceed the MCP client
  timeout (300s), which used to kill the request AND lose the work.
  Now `transcribe_job` returns a job_id immediately and the job runs in a
  background worker thread. Outputs (.md/.srt) are saved SERVER-SIDE when
  the job finishes — even if the client disconnects, work is never lost.

Protocol (line-delimited JSON):
  Request:  {"id": "...", "action": "...", ...}
  Response: {"id": "...", "ok": true, ...}

Actions:
  - transcribe_job:  {video_path, lang, model} → {job_id} immediately
  - job_status:      {job_id} → {status: queued|running|done|error, progress, result}
  - transcribe:      {audio_path, lang, model} → sync (small files / tests)
  - status:          server status (model loaded, uptime, jobs)
  - shutdown:        graceful shutdown
"""
import argparse
import asyncio
import json
import logging
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

# Logging setup (similar pattern to argos-embedder)
LOG_DIR = Path.home() / ".hermes" / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / "whisper-server.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stderr),
    ],
)
log = logging.getLogger("whisper-server")

DEFAULT_SOCKET = "/tmp/whisper-server.sock"
DEFAULT_MODEL = "large-v3-turbo"
DEFAULT_DEVICE = "cpu"
DEFAULT_COMPUTE = "int8"

# Lazy-loaded model (global)
_model = None
_model_name: Optional[str] = None
_loaded_at: Optional[float] = None
_server_started_at: float = time.time()

# ── Jobs registry (fix 2026-08-28) ───────────────────────────────────
_jobs: dict = {}          # job_id → job dict
_jobs_lock = threading.Lock()
_worker_busy = threading.Event()   # set = worker processing, clear = idle
_model_lock = threading.Lock()     # serialize model access (fastembed/ct2 not thread-safe)


def get_model(model_name: str = DEFAULT_MODEL, device: str = DEFAULT_DEVICE, compute: str = DEFAULT_COMPUTE):
    """Lazy-load Whisper model on first call."""
    global _model, _model_name, _loaded_at
    if _model is None or _model_name != model_name:
        log.info(f"Loading Whisper model: {model_name} (device={device}, compute={compute})")
        from faster_whisper import WhisperModel
        _model = WhisperModel(model_name, device=device, compute_type=compute)
        _model_name = model_name
        _loaded_at = time.time()
        log.info(f"Model loaded in {time.time() - _loaded_at:.1f}s")
    return _model


def format_timestamp(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def extract_audio(video_path: str, output_wav: str) -> None:
    """Extract 16kHz mono WAV from a video using ffmpeg."""
    subprocess.run(
        ["ffmpeg", "-i", video_path, "-ar", "16000", "-ac", "1",
         "-c:a", "pcm_s16le", "-y", output_wav],
        check=True, capture_output=True, timeout=300,
    )


def save_outputs(video_path: str, result: dict) -> dict:
    """Save transcript.md + transcript.srt next to the video (server-side)."""
    base = os.path.splitext(video_path)[0]
    segments = result.get("segments", [])
    duration = result.get("duration", 0)
    language = result.get("language", "es")
    model = result.get("model", DEFAULT_MODEL)
    text = result.get("text", "")

    # Markdown
    md_path = f"{base}.transcript.md"
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(f"""---
type: transcript
created: {time.strftime("%Y-%m-%d")}
source_video: {os.path.basename(video_path)}
duration_seconds: {duration}
language: {language}
model: {model}
---

# Transcripción: {os.path.basename(video_path)}

## Metadata

- **Duración**: {format_timestamp(duration)}
- **Idioma**: {language}
- **Modelo**: {model}

## Transcript

""")
        for seg in segments:
            f.write(f"[{format_timestamp(seg['start'])}] {seg['text']}\n")

    # SRT
    srt_path = f"{base}.transcript.srt"
    with open(srt_path, "w", encoding="utf-8") as f:
        for i, seg in enumerate(segments, 1):
            start = format_timestamp(seg["start"]).replace(":", ",")
            end = format_timestamp(seg["end"]).replace(":", ",")
            f.write(f"{i}\n{start}000 --> {end}000\n{seg['text']}\n\n")

    return {"transcript_md": md_path, "transcript_srt": srt_path}


def run_transcribe(video_path: str, lang: str, model_name: str) -> dict:
    """Full pipeline: extract audio → transcribe → build result dict."""
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        wav_path = tmp.name
    try:
        extract_audio(video_path, wav_path)
        start = time.time()
        with _model_lock:
            model = get_model(model_name)
            segments_gen, info = model.transcribe(
                wav_path, language=lang, beam_size=5, vad_filter=True,
                vad_parameters={"min_silence_duration_ms": 500},
            )
            result_segments = []
            full_text_parts = []
            for seg in segments_gen:
                result_segments.append({
                    "start": round(seg.start, 2),
                    "end": round(seg.end, 2),
                    "text": seg.text.strip(),
                })
                full_text_parts.append(seg.text.strip())
        elapsed = time.time() - start
        duration = info.duration
        rtf = elapsed / duration if duration > 0 else 0
        log.info(
            f"Transcribed {video_path}: {len(result_segments)} segments, "
            f"{duration:.1f}s audio in {elapsed:.1f}s (RTF={rtf:.2f})"
        )
        return {
            "ok": True,
            "language": info.language,
            "language_probability": round(info.language_probability, 4),
            "duration": round(duration, 2),
            "segments": result_segments,
            "text": " ".join(full_text_parts),
            "elapsed_seconds": round(elapsed, 2),
            "model": model_name,
        }
    finally:
        if os.path.exists(wav_path):
            os.unlink(wav_path)


def _job_worker():
    """Background worker: process jobs FIFO, one at a time."""
    while True:
        # Find next queued job
        with _jobs_lock:
            next_job = None
            for job in _jobs.values():
                if job["status"] == "queued":
                    next_job = job
                    break
        if next_job is None:
            _worker_busy.clear()
            return  # no more work; a new job will spawn a fresh worker

        _worker_busy.set()
        job_id = next_job["job_id"]
        next_job["status"] = "running"
        next_job["started_at"] = time.time()
        log.info(f"Job {job_id[:8]}: starting ({next_job['video_path']})")

        try:
            result = run_transcribe(
                next_job["video_path"], next_job["lang"], next_job["model"],
            )
            if result.get("ok"):
                saved = save_outputs(next_job["video_path"], result)
                result.update(saved)
                next_job["status"] = "done"
                next_job["result"] = result
                log.info(f"Job {job_id[:8]}: done, saved {saved.get('transcript_md')}")
            else:
                next_job["status"] = "error"
                next_job["error"] = result.get("error", "unknown")
                log.error(f"Job {job_id[:8]}: error {next_job['error']}")
        except Exception as e:
            log.exception(f"Job {job_id[:8]}: exception")
            next_job["status"] = "error"
            next_job["error"] = str(e)
        finally:
            next_job["finished_at"] = time.time()


def start_job(video_path: str, lang: str = "es", model: str = DEFAULT_MODEL) -> dict:
    """Create a job and spawn the worker if needed. Returns job_id immediately."""
    if not os.path.exists(video_path):
        return {"ok": False, "error": f"video not found: {video_path}"}
    job_id = str(uuid.uuid4())
    job = {
        "job_id": job_id,
        "video_path": video_path,
        "lang": lang,
        "model": model,
        "status": "queued",
        "created_at": time.time(),
        "started_at": None,
        "finished_at": None,
        "result": None,
        "error": None,
    }
    with _jobs_lock:
        _jobs[job_id] = job
    # Spawn worker if idle
    if not _worker_busy.is_set():
        t = threading.Thread(target=_job_worker, daemon=True)
        t.start()
    log.info(f"Job {job_id[:8]}: queued for {video_path}")
    return {"ok": True, "job_id": job_id}


def job_status_dict(job: dict) -> dict:
    """Serialize a job for a status response."""
    out = {
        "job_id": job["job_id"],
        "status": job["status"],
        "video_path": job["video_path"],
        "lang": job["lang"],
        "model": job["model"],
        "created_at": job["created_at"],
    }
    if job["status"] == "running":
        out["elapsed_seconds"] = round(time.time() - job["started_at"], 1)
    if job["status"] in ("done", "error") and job.get("result"):
        out["result"] = job["result"]
    if job.get("error"):
        out["error"] = job["error"]
    return out


def handle_transcribe_job(req: dict) -> dict:
    return start_job(
        req.get("video_path", ""),
        req.get("lang", "es"),
        req.get("model", DEFAULT_MODEL),
    )


def handle_job_status(req: dict) -> dict:
    job_id = req.get("job_id")
    if not job_id:
        return {"ok": False, "error": "job_id required"}
    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job:
        return {"ok": False, "error": f"unknown job_id: {job_id}"}
    result = job_status_dict(job)
    result["ok"] = True
    return result


def handle_transcribe(req: dict) -> dict:
    """Sync transcribe of a pre-extracted audio file (kept for tests/small files)."""
    audio_path = req.get("audio_path")
    if not audio_path:
        return {"ok": False, "error": "audio_path required"}
    if not os.path.exists(audio_path):
        return {"ok": False, "error": f"audio_path not found: {audio_path}"}
    lang = req.get("lang", "es")
    model_name = req.get("model", DEFAULT_MODEL)
    try:
        start = time.time()
        with _model_lock:
            model = get_model(model_name)
            segments_gen, info = model.transcribe(
                audio_path, language=lang, beam_size=5, vad_filter=True,
                vad_parameters={"min_silence_duration_ms": 500},
            )
            result_segments = []
            full_text_parts = []
            for seg in segments_gen:
                result_segments.append({
                    "start": round(seg.start, 2),
                    "end": round(seg.end, 2),
                    "text": seg.text.strip(),
                })
                full_text_parts.append(seg.text.strip())
        elapsed = time.time() - start
        duration = info.duration
        return {
            "ok": True,
            "language": info.language,
            "language_probability": round(info.language_probability, 4),
            "duration": round(duration, 2),
            "segments": result_segments,
            "text": " ".join(full_text_parts),
            "elapsed_seconds": round(elapsed, 2),
            "model": model_name,
        }
    except Exception as e:
        log.exception(f"Sync transcribe failed: {e}")
        return {"ok": False, "error": str(e)}


def handle_status(req: dict) -> dict:
    uptime = time.time() - _server_started_at
    with _jobs_lock:
        job_states = {}
        for jid, job in _jobs.items():
            if job["status"] in ("queued", "running"):
                job_states[jid] = job["status"]
    return {
        "ok": True,
        "model_loaded": _model is not None,
        "model_name": _model_name,
        "model_loaded_at": _loaded_at,
        "model_uptime_seconds": round(time.time() - _loaded_at, 2) if _loaded_at else 0,
        "server_uptime_seconds": round(uptime, 2),
        "socket_path": getattr(handle_status, "_socket_path", "unknown"),
        "active_jobs": job_states,
    }


async def handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    """Handle a single client connection."""
    peer = writer.get_extra_info("peername")
    log.info(f"Client connected: {peer}")
    try:
        while True:
            line = await reader.readline()
            if not line:
                break
            try:
                req = json.loads(line.decode("utf-8").strip())
                action = req.get("action")
                req_id = req.get("id", "unknown")
                if action == "transcribe":
                    result = handle_transcribe(req)
                elif action == "transcribe_job":
                    result = handle_transcribe_job(req)
                elif action == "job_status":
                    result = handle_job_status(req)
                elif action == "status":
                    result = handle_status(req)
                elif action == "shutdown":
                    log.info("Shutdown requested")
                    result = {"ok": True, "message": "shutting down"}
                    response = json.dumps({"id": req_id, **result}) + "\n"
                    writer.write(response.encode("utf-8"))
                    await writer.drain()
                    writer.close()
                    os.kill(os.getpid(), signal.SIGTERM)
                    return
                else:
                    result = {"ok": False, "error": f"unknown action: {action}"}
                response = json.dumps({"id": req_id, **result}) + "\n"
                writer.write(response.encode("utf-8"))
                await writer.drain()
            except json.JSONDecodeError as e:
                error_resp = json.dumps({"ok": False, "error": f"invalid JSON: {e}"}) + "\n"
                writer.write(error_resp.encode("utf-8"))
                await writer.drain()
    except asyncio.IncompleteReadError:
        pass
    except Exception as e:
        log.exception(f"Client handler error: {e}")
    finally:
        writer.close()
        await writer.wait_closed()
        log.info(f"Client disconnected: {peer}")


async def main():
    parser = argparse.ArgumentParser(description="Whisper Unix Socket Server")
    parser.add_argument("--socket", default=DEFAULT_SOCKET, help="Unix socket path")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Whisper model name")
    args = parser.parse_args()

    socket_path = args.socket
    handle_status._socket_path = socket_path  # type: ignore

    if os.path.exists(socket_path):
        log.info(f"Removing stale socket: {socket_path}")
        os.unlink(socket_path)

    log.info(f"Starting whisper-server on {socket_path}")
    log.info(f"Default model: {args.model} (lazy load)")

    server = await asyncio.start_unix_server(handle_client, path=socket_path)
    os.chmod(socket_path, 0o666)
    log.info(f"Server listening: {socket_path}")

    loop = asyncio.get_event_loop()
    stop_event = asyncio.Event()

    def shutdown_handler():
        log.info("Shutdown signal received")
        stop_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, shutdown_handler)

    try:
        await stop_event.wait()
    finally:
        log.info("Closing server")
        server.close()
        await server.wait_closed()
        if os.path.exists(socket_path):
            os.unlink(socket_path)
        log.info("Server stopped")


if __name__ == "__main__":
    asyncio.run(main())
