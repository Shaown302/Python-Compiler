import os
import re
import sys
import დრო
import uuid
import json
import time
import queue
import tempfile
import threading
import subprocess
from datetime import datetime, timezone

import requests
from flask import Flask, jsonify, render_template, request

app = Flask(__name__)

AI_API_URL = os.getenv("AI_API_URL", "https://addy-chatgpt-api.vercel.app/?text=")
RUN_TIMEOUT = int(os.getenv("RUN_TIMEOUT", "20"))
MAX_OUTPUT_CHARS = int(os.getenv("MAX_OUTPUT_CHARS", "50000"))
JOB_CLEANUP_SECONDS = int(os.getenv("JOB_CLEANUP_SECONDS", "1800"))

# In-memory jobs store. Use one Gunicorn worker, which we set in render.yaml.
jobs = {}
jobs_lock = threading.Lock()

DANGEROUS_PATTERNS = [
    r"\bos\.system\s*\(",
    r"\bos\.popen\s*\(",
    r"\bsubprocess\.",
    r"\bsocket\.",
    r"\bctypes\.",
    r"\bpty\.",
    r"\bshutil\.rmtree\s*\(",
    r"\bos\.remove\s*\(",
    r"\bos\.unlink\s*\(",
    r"\bos\.rmdir\s*\(",
    r"\beval\s*\(",
    r"\bexec\s*\(",
    r"\b__import__\s*\(",
    r"\bpickle\.loads\s*\(",
]

def now_iso():
    return datetime.now(timezone.utc).isoformat()

def create_job(code: str, mode: str):
    job_id = uuid.uuid4().hex[:12]
    with jobs_lock:
        jobs[job_id] = {
            "id": job_id,
            "code": code,
            "fixed_code": None,
            "mode": mode,
            "status": "queued",
            "phase": "waiting",
            "return_code": None,
            "output": "",
            "error": None,
            "created_at": now_iso(),
            "updated_at": now_iso(),
            "finished": False,
        }
    return job_id

def get_job(job_id):
    with jobs_lock:
        return jobs.get(job_id)

def set_job(job_id, **fields):
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            return
        job.update(fields)
        job["updated_at"] = now_iso()

def append_output(job_id, text):
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            return
        current = job.get("output", "")
        new_val = current + text
        if len(new_val) > MAX_OUTPUT_CHARS:
            new_val = new_val[-MAX_OUTPUT_CHARS:]
        job["output"] = new_val
        job["updated_at"] = now_iso()

def cleanup_old_jobs():
    while True:
        time.sleep(60)
        cutoff = time.time() - JOB_CLEANUP_SECONDS
        with jobs_lock:
            to_delete = []
            for job_id, job in jobs.items():
                try:
                    dt = datetime.fromisoformat(job["updated_at"].replace("Z", "+00:00"))
                    if dt.timestamp() < cutoff:
                        to_delete.append(job_id)
                except Exception:
                    pass
            for job_id in to_delete:
                jobs.pop(job_id, None)

def normalize_code(text: str) -> str:
    text = (text or "").strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines)
    return text.strip()

def is_dangerous(code: str) -> bool:
    for pattern in DANGEROUS_PATTERNS:
        if re.search(pattern, code, re.IGNORECASE):
            return True
    return False

def extract_code_from_ai(reply: str) -> str:
    reply = (reply or "").strip()

    if reply.startswith("```"):
        lines = reply.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        reply = "\n".join(lines).strip()

    if reply.lower().startswith("python\n"):
        reply = reply[7:].lstrip()

    return reply.strip()

def ai_fix_code(code: str, error_output: str):
    prompt = f"""
Fix this Python code.

Rules:
- Return only the full corrected Python code.
- No explanation.
- No markdown.
- Keep the original intent.
- Make it run in normal Python.

Code:
{code}

Error/output:
{error_output}
""".strip()

    try:
        r = requests.get(
            AI_API_URL,
            params={"text": prompt},
            timeout=90,
        )
        r.raise_for_status()
        data = r.json()
        reply = data.get("reply", "")
        fixed = extract_code_from_ai(reply)
        if not fixed:
            return None, "AI returned an empty fix."
        return fixed, None
    except Exception as e:
        return None, f"AI fix failed: {e}"

def run_python_code(job_id: str, code: str):
    tmp_path = None
    try:
        set_job(job_id, status="running", phase="running original", error=None)
        append_output(job_id, "▶️ Starting execution...\n\n")

        with tempfile.NamedTemporaryFile(delete=False, suffix=".py", mode="w", encoding="utf-8") as f:
            f.write(code)
            tmp_path = f.name

        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"

        proc = subprocess.Popen(
            [sys.executable, "-u", tmp_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=env,
        )

        start = time.time()

        while True:
            if time.time() - start > RUN_TIMEOUT:
                proc.kill()
                append_output(job_id, "\n⏱ Timeout reached.\n")
                return -1, (get_job(job_id) or {}).get("output", "")

            line = proc.stdout.readline()
            if line:
                append_output(job_id, line)
            else:
                if proc.poll() is not None:
                    break
                time.sleep(0.05)

        rc = proc.wait()
        output = (get_job(job_id) or {}).get("output", "")
        return rc, output

    except Exception as e:
        append_output(job_id, f"\n❌ Runner error: {e}\n")
        return 1, (get_job(job_id) or {}).get("output", "")
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass

def worker(job_id: str):
    job = get_job(job_id)
    if not job:
        return

    code = job["code"]
    mode = job["mode"]

    if is_dangerous(code):
        set_job(
            job_id,
            status="blocked",
            phase="security check",
            return_code=1,
            finished=True,
            error="Dangerous pattern detected.",
        )
        append_output(
            job_id,
            "🚫 Dangerous code detected and blocked by the safety filter.\n"
        )
        return

    rc, output = run_python_code(job_id, code)
    set_job(job_id, return_code=rc)

    if mode == "run":
        set_job(
            job_id,
            status="done" if rc == 0 else "failed",
            phase="finished",
            finished=True,
        )
        if rc == 0:
            append_output(job_id, "\n✅ Execution finished successfully.\n")
        else:
            append_output(job_id, f"\n❌ Execution failed with exit code {rc}.\n")
        return

    # Auto-fix mode
    if rc == 0:
        set_job(
            job_id,
            status="done",
            phase="finished",
            finished=True,
        )
        append_output(job_id, "\n✅ Code already worked. No fix needed.\n")
        return

    set_job(job_id, status="fixing", phase="asking AI for fix")
    append_output(job_id, "\n🧠 Original run failed. Asking AI to repair the code...\n")

    fixed_code, err = ai_fix_code(code, output[-8000:])

    if not fixed_code:
        set_job(
            job_id,
            status="failed",
            phase="fix failed",
            finished=True,
            error=err,
        )
        append_output(job_id, f"\n❌ {err}\n")
        return

    set_job(job_id, fixed_code=fixed_code)
    append_output(job_id, "\n🛠 AI fix received. Running corrected code...\n\n")
    append_output(job_id, "—" * 40 + "\n")
    append_output(job_id, fixed_code + "\n")
    append_output(job_id, "—" * 40 + "\n\n")

    fixed_rc, _ = run_python_code(job_id, fixed_code)
    set_job(job_id, return_code=fixed_rc)

    if fixed_rc == 0:
        set_job(
            job_id,
            status="done",
            phase="fixed and finished",
            finished=True,
        )
        append_output(job_id, "\n✅ Auto-fix worked successfully.\n")
    else:
        set_job(
            job_id,
            status="failed",
            phase="fixed but failed",
            finished=True,
        )
        append_output(job_id, f"\n❌ Fixed version still failed with exit code {fixed_rc}.\n")

@app.route("/")
def home():
    return render_template("index.html")

@app.route("/api/jobs", methods=["POST"])
def api_create_job():
    data = request.get_json(force=True, silent=True) or {}
    code = normalize_code(data.get("code", ""))
    mode = (data.get("mode", "run") or "run").strip().lower()

    if not code:
        return jsonify({"ok": False, "error": "Code is required."}), 400

    if mode not in {"run", "fix"}:
        return jsonify({"ok": False, "error": "Invalid mode."}), 400

    job_id = create_job(code, mode)

    t = threading.Thread(target=worker, args=(job_id,), daemon=True)
    t.start()

    return jsonify({"ok": True, "job_id": job_id})

@app.route("/api/jobs/<job_id>", methods=["GET"])
def api_get_job(job_id):
    job = get_job(job_id)
    if not job:
        return jsonify({"ok": False, "error": "Job not found."}), 404

    return jsonify({
        "ok": True,
        "id": job["id"],
        "status": job["status"],
        "phase": job["phase"],
        "return_code": job["return_code"],
        "output": job["output"],
        "fixed_code": job["fixed_code"],
        "error": job["error"],
        "created_at": job["created_at"],
        "updated_at": job["updated_at"],
        "finished": job["finished"],
    })

@app.route("/api/jobs/<job_id>/delete", methods=["POST"])
def api_delete_job(job_id):
    with jobs_lock:
        existed = jobs.pop(job_id, None)
    return jsonify({"ok": True, "deleted": bool(existed)})

@app.route("/health")
def health():
    return jsonify({"ok": True})

if __name__ == "__main__":
    threading.Thread(target=cleanup_old_jobs, daemon=True).start()
    app.run(host="0.0.0.0", port=5000, debug=True)