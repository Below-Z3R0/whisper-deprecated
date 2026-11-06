#!/usr/bin/env python3
"""
whisper_mcp_server.py — Layer 3 of the Whisper hybrid architecture.

JSON-RPC MCP server that exposes Whisper tools over stdio.
Connects to whisper_server.py via Unix socket (Layer 4).

MCP Tools exposed:
  - transcribe(video_path, lang="es", model="large-v3-turbo")
        Job-based: never blocks longer than POLL_DEADLINE (240s).
        Short videos return the full result; long ones return
        {status: "still_running", job_id} — the server keeps working
        and saves the outputs even if nobody polls again.
  - transcribe_status(job_id) — single poll, always fast.
  - transcribe_batch(dir_path, recursive=False, lang, model)
        Starts one job per video, returns job_ids immediately.
  - list_models()
  - get_status()

Fix 2026-08-28: the old `transcribe` blocked on the socket for the whole
transcription; the Hermes MCP client kills the call at 300s and the work
(and outputs) were lost. Now the job lives in Layer 4.
"""
import json
import logging
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

# Logging to stderr (stdout reserved for JSON-RPC)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("whisper-mcp")

SOCKET_PATH = "/tmp/whisper-server.sock"
WHISPER_SERVER = str(Path(__file__).resolve().parent.parent / "whisper" / "whisper_server.py")
# faster-whisper lives in the system interpreter, not in a venv: use whichever
# python is running this server unless a venv is present next to it.
_venv_python = Path(__file__).resolve().parent.parent / "venv" / "bin" / "python3"
WHISPER_PYTHON = str(_venv_python) if _venv_python.exists() else sys.executable

# Max time a single MCP tool call may spend polling (must stay under the
# Hermes MCP client timeout of ~300s). Overridable for tests.
POLL_DEADLINE = float(os.environ.get("WHISPER_MCP_POLL_DEADLINE", "240"))
POLL_INTERVAL = 2.0


def socket_request(req: dict, timeout: float = 30.0) -> dict:
    """Send one request to whisper-server, read one line response."""
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        client.settimeout(timeout)
        client.connect(SOCKET_PATH)
        client.sendall((json.dumps(req) + "\n").encode("utf-8"))
        response = b""
        while b"\n" not in response:
            data = client.recv(65536)
            if not data:
                break
            response += data
        return json.loads(response.decode("utf-8").strip())
    except FileNotFoundError:
        return {"ok": False, "error": f"whisper-server socket not found at {SOCKET_PATH}. Is the server running?"}
    except socket.timeout:
        return {"ok": False, "error": "whisper-server request timed out"}
    except Exception as e:
        return {"ok": False, "error": str(e)}
    finally:
        client.close()


def ensure_server_running() -> Optional[str]:
    """Ensure whisper-server is running. Start it if not. Returns error message if any."""
    if os.path.exists(SOCKET_PATH):
        result = socket_request({"id": "health", "action": "status"}, timeout=3.0)
        if result.get("ok"):
            return None
        # Stale socket: remove it
        try:
            os.unlink(SOCKET_PATH)
        except OSError:
            pass
    log.info("Starting whisper-server...")
    try:
        subprocess.Popen(
            [WHISPER_PYTHON, WHISPER_SERVER],
            stdout=open("/tmp/whisper-server-stdout.log", "w"),
            stderr=open("/tmp/whisper-server-stderr.log", "w"),
            start_new_session=True,
        )
        for _ in range(40):
            time.sleep(0.5)
            if os.path.exists(SOCKET_PATH):
                result = socket_request({"id": "health", "action": "status"}, timeout=3.0)
                if result.get("ok"):
                    log.info("whisper-server started")
                    return None
        return "whisper-server started but socket not responsive"
    except Exception as e:
        return f"Failed to start whisper-server: {e}"


# === MCP Tools ===

def tool_transcribe(video_path: str, lang: str = "es", model: str = "large-v3-turbo") -> dict:
    """Transcribe a video. Job-based: returns fast even for long videos."""
    if not video_path:
        return {"ok": False, "error": "video_path required"}

    err = ensure_server_running()
    if err:
        return {"ok": False, "error": err}

    # 1. Start job (fast)
    started = socket_request({
        "id": "job_start", "action": "transcribe_job",
        "video_path": video_path, "lang": lang, "model": model,
    }, timeout=15.0)
    if not started.get("ok"):
        return started
    job_id = started["job_id"]

    # 2. Poll until done or deadline
    deadline = time.time() + POLL_DEADLINE
    while time.time() < deadline:
        status = socket_request({"id": "job_poll", "action": "job_status", "job_id": job_id}, timeout=15.0)
        if not status.get("ok"):
            return {"ok": False, "error": f"job poll failed: {status.get('error')}"}
        if status["status"] == "done":
            result = dict(status.get("result", {}))
            result["ok"] = True
            return result
        if status["status"] == "error":
            return {"ok": False, "error": status.get("error", "transcription job failed")}
        time.sleep(POLL_INTERVAL)

    # 3. Deadline hit: return still_running — the server keeps working and
    #    will save the outputs on its own.
    return {
        "ok": True,
        "status": "still_running",
        "job_id": job_id,
        "message": f"Transcription still running after {int(POLL_DEADLINE)}s. "
                   f"Poll with transcribe_status(job_id). Outputs will be saved by the server.",
    }


def tool_transcribe_status(job_id: str) -> dict:
    """Poll a transcription job. Fast, single request."""
    if not job_id:
        return {"ok": False, "error": "job_id required"}
    err = ensure_server_running()
    if err:
        return {"ok": False, "error": err}
    status = socket_request({"id": "job_poll", "action": "job_status", "job_id": job_id}, timeout=15.0)
    if not status.get("ok"):
        return {"ok": False, "error": f"job poll failed: {status.get('error')}"}
    return status


def tool_transcribe_batch(dir_path: str, recursive: bool = False,
                          lang: str = "es", model: str = "large-v3-turbo") -> dict:
    """Start one job per video in a directory. Returns job_ids immediately."""
    if not os.path.isdir(dir_path):
        return {"ok": False, "error": f"directory not found: {dir_path}"}

    video_extensions = [".mp4", ".mov", ".mkv", ".avi", ".webm", ".flv", ".m4v"]
    videos = []
    if recursive:
        for root, _, files in os.walk(dir_path):
            for f in files:
                if any(f.lower().endswith(ext) for ext in video_extensions):
                    videos.append(os.path.join(root, f))
    else:
        for f in os.listdir(dir_path):
            full = os.path.join(dir_path, f)
            if os.path.isfile(full) and any(f.lower().endswith(ext) for ext in video_extensions):
                videos.append(full)

    if not videos:
        return {"ok": False, "error": f"no videos found in {dir_path}"}

    err = ensure_server_running()
    if err:
        return {"ok": False, "error": err}

    # Dedupe by md5 (exact duplicates skipped)
    import hashlib
    seen = {}
    job_ids = []
    skipped = []
    for video in sorted(videos):
        try:
            with open(video, "rb") as f:
                h = hashlib.md5(f.read()).hexdigest()
        except Exception:
            h = None
        if h and h in seen:
            skipped.append({"duplicate": video, "original": seen[h]})
            continue
        if h:
            seen[h] = video
        started = socket_request({
            "id": "job_start", "action": "transcribe_job",
            "video_path": video, "lang": lang, "model": model,
        }, timeout=15.0)
        if started.get("ok"):
            job_ids.append({"job_id": started["job_id"], "video_path": video})
        else:
            skipped.append({"video": video, "error": started.get("error")})

    return {
        "ok": True,
        "total": len(videos),
        "jobs_started": len(job_ids),
        "job_ids": job_ids,
        "skipped": skipped,
        "message": f"{len(job_ids)} jobs started. Poll with transcribe_status(job_id). "
                   f"Outputs saved server-side when each job finishes.",
    }


def tool_list_models() -> dict:
    return {
        "ok": True,
        "models": ["tiny", "base", "small", "medium", "large-v1", "large-v2",
                   "large-v3", "large-v3-turbo", "distil-large-v3"],
        "recommended": "large-v3-turbo",
    }


def tool_get_status() -> dict:
    err = ensure_server_running()
    if err:
        return {"ok": False, "error": err}
    return socket_request({"id": "status", "action": "status"}, timeout=10.0)


# === JSON-RPC over stdio ===

TOOLS = {
    "transcribe": {
        "function": tool_transcribe,
        "schema": {
            "type": "object",
            "properties": {
                "video_path": {"type": "string", "description": "Path to video file"},
                "lang": {"type": "string", "default": "es"},
                "model": {"type": "string", "default": "large-v3-turbo"},
            },
            "required": ["video_path"],
        },
    },
    "transcribe_status": {
        "function": tool_transcribe_status,
        "schema": {
            "type": "object",
            "properties": {"job_id": {"type": "string"}},
            "required": ["job_id"],
        },
    },
    "transcribe_batch": {
        "function": tool_transcribe_batch,
        "schema": {
            "type": "object",
            "properties": {
                "dir_path": {"type": "string"},
                "recursive": {"type": "boolean", "default": False},
                "lang": {"type": "string", "default": "es"},
                "model": {"type": "string", "default": "large-v3-turbo"},
            },
            "required": ["dir_path"],
        },
    },
    "list_models": {"function": tool_list_models, "schema": {"type": "object", "properties": {}}},
    "get_status": {"function": tool_get_status, "schema": {"type": "object", "properties": {}}},
}


def send_response(response: dict):
    sys.stdout.write(json.dumps(response) + "\n")
    sys.stdout.flush()


def handle_request(req: dict) -> dict:
    method = req.get("method")
    req_id = req.get("id")
    params = req.get("params", {})

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "serverInfo": {"name": "whisper-mcp", "version": "2.0.0"},
                "capabilities": {"tools": {}},
            },
        }

    if method == "tools/list":
        tools_list = []
        for name, info in TOOLS.items():
            tools_list.append({
                "name": name,
                "description": info["function"].__doc__ or name,
                "inputSchema": info["schema"],
            })
        return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": tools_list}}

    if method == "tools/call":
        tool_name = params.get("name")
        arguments = params.get("arguments", {})
        if tool_name not in TOOLS:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32602, "message": f"unknown tool: {tool_name}"},
            }
        try:
            result = TOOLS[tool_name]["function"](**arguments)
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, indent=2)}],
                    "isError": not result.get("ok", False),
                },
            }
        except Exception as e:
            log.exception(f"Tool {tool_name} failed: {e}")
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32603, "message": str(e)},
            }

    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": -32601, "message": f"unknown method: {method}"},
    }


def main():
    log.info("whisper-mcp-server starting (stdio mode, job-based v2.0.0)")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
            send_response(handle_request(req))
        except json.JSONDecodeError as e:
            send_response({
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": f"parse error: {e}"},
            })


if __name__ == "__main__":
    main()
