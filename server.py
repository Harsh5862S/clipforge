"""
ClipForge backend server — pure Python, no pip installs needed (besides
yt-dlp for the download feature).

Features:
- AI clip analysis powered by Google Gemini's free API
- Email + password signup/login with OTP verification via Brevo
- Admin panel to view/manage users and activity
- Clip download with optional vertical/square reframing

Run with:  python3 server.py
Then open: http://localhost:3000
"""

import json
import os
import glob
import re
import uuid
import shutil
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.cookies import SimpleCookie
import urllib.request
import urllib.error
import urllib.parse

import db
import email_service

# ── GOOGLE GEMINI API KEY ───────────────────────────────────────────────────
# Locally: paste your key below, OR set the GEMINI_API_KEY environment variable.
# On Render: set GEMINI_API_KEY in the dashboard's Environment tab — never
# commit a real key into this file if this repo is public.
# Get a free key at: https://aistudio.google.com/app/apikey
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "YOUR_GEMINI_API_KEY_HERE")
# ───────────────────────────────────────────────────────────────────────────

# Render assigns a dynamic port via the PORT env var; 3000 is used locally.
PORT = int(os.environ.get("PORT", 3000))
HERE = os.path.dirname(os.path.abspath(__file__))
DOWNLOAD_DIR = os.path.join(HERE, "downloads")
GEMINI_MODEL = "gemini-2.5-flash"
GEMINI_URL = (
    f"https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
)

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# ── YOUTUBE COOKIES (for yt-dlp, to avoid "Sign in to confirm you're not a
# bot" errors when downloading from cloud/datacenter IPs like Render's) ────
# Locally: not usually needed.
# On Render: paste the FULL CONTENTS of your exported cookies.txt file
# (Netscape format) as the YOUTUBE_COOKIES environment variable. The server
# writes it to a temp file at startup and passes it to yt-dlp automatically.
YOUTUBE_COOKIES_CONTENT = os.environ.get("YOUTUBE_COOKIES", "")
COOKIES_FILE_PATH = os.path.join(HERE, "youtube_cookies.txt") if YOUTUBE_COOKIES_CONTENT else None

if YOUTUBE_COOKIES_CONTENT:
    with open(COOKIES_FILE_PATH, "w") as _f:
        _f.write(YOUTUBE_COOKIES_CONTENT)
# ───────────────────────────────────────────────────────────────────────────

# Pages that don't require login
PUBLIC_PAGES = {"/login", "/signup", "/verify-otp"}
PUBLIC_API = {"/api/signup", "/api/login", "/api/verify-otp", "/api/resend-otp"}

# ── Background download job tracker ────────────────────────────────────────
# Download requests run in a background thread so the initial HTTP request
# returns instantly (avoiding Render's ~30-100s proxy timeout on long
# requests). The frontend polls /api/download/status until the job is done,
# then fetches the finished file from /api/download/file.
_download_jobs = {}
_download_jobs_lock = threading.Lock()
JOB_TTL_SECONDS = 30 * 60  # finished jobs/files are cleaned up after 30 min
# ───────────────────────────────────────────────────────────────────────────


def _run_download_job(job_id, user_id, video_id, start, end, layout):
    """Runs yt-dlp + optional ffmpeg reframe in a background thread.
    Updates the shared _download_jobs dict so the polling endpoint can
    report progress/completion/errors back to the frontend."""

    def fail(message):
        with _download_jobs_lock:
            _download_jobs[job_id]["status"] = "error"
            _download_jobs[job_id]["error"] = message

    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    file_id = uuid.uuid4().hex
    out_template = os.path.join(DOWNLOAD_DIR, f"{file_id}.%(ext)s")
    url = f"https://www.youtube.com/watch?v={video_id}"
    section = f"*{start}-{end}"

    has_cookies = COOKIES_FILE_PATH and os.path.exists(COOKIES_FILE_PATH)

    # android client does NOT support cookies and will skip them silently.
    # When cookies are available, use web client which supports both cookies
    # and the Node.js-based n-challenge solver.
    # When no cookies, use tv_embedded which avoids the n-challenge entirely.
    player_client = "web" if has_cookies else "tv_embedded,web"

    cmd = [
        "yt-dlp",
        "--download-sections", section,
        "-f", "bv*[height<=720]+ba/b[height<=720]/bv*+ba/b/best",
        "--merge-output-format", "mp4",
        "--no-playlist",
        "--extractor-args", f"youtube:player_client={player_client}",
        "-o", out_template,
    ]

    if has_cookies:
        cmd += ["--cookies", COOKIES_FILE_PATH]

    cmd.append(url)

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=240,
            env={**os.environ, "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")},
        )
    except subprocess.TimeoutExpired:
        fail("Download timed out after 4 minutes. Try a shorter clip or check your connection.")
        return

    if result.returncode != 0:
        err_tail = (result.stderr or "")[-600:]
        if "429" in err_tail or "Too Many Requests" in err_tail:
            fail(
                "YouTube is rate-limiting this server (HTTP 429 - too many requests). "
                "This usually clears up after a few minutes. If it persists, your "
                "YOUTUBE_COOKIES may be stale - try re-exporting fresh cookies from "
                "a logged-in browser session. Raw error: " + err_tail
            )
            return
        if "Sign in to confirm" in err_tail or "not a bot" in err_tail:
            fail(
                "YouTube is blocking this server's IP address as a suspected bot. "
                "Fix: export your YouTube cookies and set them as the YOUTUBE_COOKIES "
                "environment variable (see README). Raw error: " + err_tail
            )
            return
        if "PO Token" in err_tail or "Requested format is not available" in err_tail:
            fail(
                "YouTube's video format protections blocked this download. "
                "Try redeploying for a fresh yt-dlp version, or try a different video. "
                "Raw error: " + err_tail
            )
            return
        fail("yt-dlp failed: " + err_tail)
        return

    matches = glob.glob(os.path.join(DOWNLOAD_DIR, f"{file_id}.*"))
    if not matches:
        fail("Download succeeded but no output file was found.")
        return

    filepath = matches[0]

    if layout in ("vertical", "square"):
        if layout == "vertical":
            vf = "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920"
        else:
            vf = "scale=1080:1080:force_original_aspect_ratio=increase,crop=1080:1080"

        converted_path = os.path.join(DOWNLOAD_DIR, f"{file_id}_out.mp4")
        ffmpeg_cmd = [
            "ffmpeg", "-y",
            "-i", filepath,
            "-vf", vf,
            "-c:v", "libx264", "-preset", "fast", "-crf", "23",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "128k", "-ac", "2",
            "-movflags", "+faststart",
            converted_path,
        ]

        try:
            conv_result = subprocess.run(ffmpeg_cmd, capture_output=True, text=True, timeout=240)
        except subprocess.TimeoutExpired:
            try:
                os.remove(filepath)
            except OSError:
                pass
            fail("Layout conversion timed out after 4 minutes. Try the Original layout instead.")
            return

        try:
            os.remove(filepath)
        except OSError:
            pass

        out_size = os.path.getsize(converted_path) if os.path.exists(converted_path) else 0

        if conv_result.returncode != 0 or out_size < 10_000:
            try:
                os.remove(converted_path)
            except OSError:
                pass
            err_tail = (conv_result.stderr or "")[-600:]
            fail("ffmpeg reframe failed: " + (err_tail or "output file was empty"))
            return

        filepath = converted_path

    layout_suffix = "" if layout == "original" else f"_{layout}"
    filename = f"clip_{video_id}_{int(start)}-{int(end)}{layout_suffix}.mp4"

    with _download_jobs_lock:
        _download_jobs[job_id]["status"] = "done"
        _download_jobs[job_id]["filepath"] = filepath
        _download_jobs[job_id]["filename"] = filename

    db.log_activity(user_id, "download", f"video={video_id} layout={layout} window={start}-{end}")


def _cleanup_stale_jobs():
    """Background sweeper: removes finished jobs and their files after
    JOB_TTL_SECONDS, in case the frontend never picks them up."""
    while True:
        time.sleep(300)
        now = time.time()
        with _download_jobs_lock:
            stale_ids = [
                jid for jid, job in _download_jobs.items()
                if now - job.get("created_at", now) > JOB_TTL_SECONDS
            ]
            for jid in stale_ids:
                job = _download_jobs.pop(jid, None)
                if job and job.get("filepath") and os.path.exists(job["filepath"]):
                    try:
                        os.remove(job["filepath"])
                    except OSError:
                        pass


class Handler(BaseHTTPRequestHandler):

    # ── Helpers ──────────────────────────────────────────────────────────

    def _set_cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Credentials", "true")

    def _send_json(self, status, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self._set_cors()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html_file(self, filename, status=200):
        filepath = os.path.join(HERE, filename)
        try:
            with open(filepath, "rb") as f:
                body = f.read()
            self.send_response(status)
            self._set_cors()
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except FileNotFoundError:
            self.send_response(500)
            self._set_cors()
            self.end_headers()
            self.wfile.write(f"Could not load {filename}".encode())

    def _read_json_body(self):
        length = int(self.headers.get("Content-Length", 0))
        raw_body = self.rfile.read(length) if length else b"{}"
        return json.loads(raw_body)

    def _get_session_token(self):
        cookie_header = self.headers.get("Cookie")
        if not cookie_header:
            return None
        cookie = SimpleCookie()
        cookie.load(cookie_header)
        if "session" in cookie:
            return cookie["session"].value
        return None

    def _current_user(self):
        token = self._get_session_token()
        return db.get_session_user(token)

    def _set_session_cookie(self, token):
        self.send_header(
            "Set-Cookie",
            f"session={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age={60*60*24*14}",
        )

    def _clear_session_cookie(self):
        self.send_header("Set-Cookie", "session=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0")

    def _get_query_params(self):
        if "?" not in self.path:
            return {}
        query = self.path.split("?", 1)[1]
        params = {}
        for pair in query.split("&"):
            if "=" in pair:
                k, v = pair.split("=", 1)
                params[urllib.parse.unquote(k)] = urllib.parse.unquote(v)
        return params

    # ── Routing entrypoints ─────────────────────────────────────────────

    def do_OPTIONS(self):
        self.send_response(204)
        self._set_cors()
        self.end_headers()

    def do_GET(self):
        path = self.path.split("?")[0]

        page_map = {
            "/login": "login.html",
            "/signup": "signup.html",
            "/verify-otp": "verify_otp.html",
        }

        if path in page_map:
            self._send_html_file(page_map[path])
            return

        if path == "/admin":
            user = self._current_user()
            if user is None:
                self.send_response(302)
                self._set_cors()
                self.send_header("Location", "/login")
                self.end_headers()
                return
            if not user["is_admin"]:
                self.send_response(302)
                self._set_cors()
                self.send_header("Location", "/")
                self.end_headers()
                return
            self._send_html_file("admin.html")
            return

        if path in ("/", "/index.html"):
            user = self._current_user()
            if user is None:
                self.send_response(302)
                self._set_cors()
                self.send_header("Location", "/login")
                self.end_headers()
                return
            if user["is_banned"]:
                self.send_response(302)
                self._set_cors()
                self.send_header("Location", "/login")
                self.end_headers()
                return
            self._send_html_file("index.html")
            return

        if path == "/api/me":
            user = self._current_user()
            if user is None:
                self._send_json(401, {"error": {"message": "Not logged in"}})
                return
            self._send_json(200, {
                "id": user["id"], "email": user["email"],
                "is_admin": bool(user["is_admin"]),
            })
            return

        if path == "/api/admin/cookie-debug":
            user = self._current_user()
            if user is None or not user["is_admin"]:
                self._send_json(403, {"error": {"message": "Admins only"}})
                return
            cookie_env_len = len(YOUTUBE_COOKIES_CONTENT)
            cookie_file_exists = bool(COOKIES_FILE_PATH and os.path.exists(COOKIES_FILE_PATH))
            cookie_file_lines = 0
            cookie_file_size = 0
            cookie_first_line = ""
            cookie_last_line = ""
            if cookie_file_exists:
                with open(COOKIES_FILE_PATH) as cf:
                    lines = cf.readlines()
                    cookie_file_lines = len(lines)
                    cookie_file_size = os.path.getsize(COOKIES_FILE_PATH)
                    non_comment = [l.strip() for l in lines if l.strip() and not l.startswith("#")]
                    cookie_first_line = lines[0].strip() if lines else ""
                    cookie_last_line = lines[-1].strip() if lines else ""
                    youtube_cookies = [l for l in non_comment if "youtube" in l.lower()]
            self._send_json(200, {
                "env_var_length": cookie_env_len,
                "env_var_set": cookie_env_len > 0,
                "cookie_file_exists": cookie_file_exists,
                "cookie_file_lines": cookie_file_lines,
                "cookie_file_size_bytes": cookie_file_size,
                "first_line": cookie_first_line,
                "last_line": cookie_last_line,
                "youtube_cookie_rows": len(youtube_cookies) if cookie_file_exists else 0,
            })
            return

        if path == "/api/admin/users":
            user = self._current_user()
            if user is None or not user["is_admin"]:
                self._send_json(403, {"error": {"message": "Admins only"}})
                return
            rows = db.list_all_users()
            self._send_json(200, {"users": [dict(r) for r in rows]})
            return

        if path == "/api/admin/activity":
            user = self._current_user()
            if user is None or not user["is_admin"]:
                self._send_json(403, {"error": {"message": "Admins only"}})
                return
            rows = db.get_all_activity()
            self._send_json(200, {"activity": [dict(r) for r in rows]})
            return

        if path == "/api/history":
            user = self._current_user()
            if user is None:
                self._send_json(401, {"error": {"message": "Please log in to continue."}})
                return
            rows = db.get_video_history_for_user(user["id"])
            self._send_json(200, {"history": [dict(r) for r in rows]})
            return

        if path == "/api/download/status":
            user = self._current_user()
            if user is None:
                self._send_json(401, {"error": {"message": "Please log in to continue."}})
                return
            params = self._get_query_params()
            job_id = params.get("id", "")
            with _download_jobs_lock:
                job = _download_jobs.get(job_id)
            if job is None or job.get("user_id") != user["id"]:
                self._send_json(404, {"error": {"message": "Job not found."}})
                return
            self._send_json(200, {
                "status": job["status"],
                "error": job.get("error"),
                "filename": job.get("filename"),
            })
            return

        if path == "/api/download/file":
            user = self._current_user()
            if user is None:
                self._send_json(401, {"error": {"message": "Please log in to continue."}})
                return
            params = self._get_query_params()
            job_id = params.get("id", "")
            with _download_jobs_lock:
                job = _download_jobs.get(job_id)
            if job is None or job.get("user_id") != user["id"]:
                self._send_json(404, {"error": {"message": "Job not found."}})
                return
            if job["status"] != "done":
                self._send_json(409, {"error": {"message": "File is not ready yet."}})
                return

            filepath = job.get("filepath")
            if not filepath or not os.path.exists(filepath):
                self._send_json(410, {"error": {"message": "File has expired or was already downloaded."}})
                return

            try:
                with open(filepath, "rb") as f:
                    data = f.read()
            finally:
                try:
                    os.remove(filepath)
                except OSError:
                    pass
                with _download_jobs_lock:
                    _download_jobs.pop(job_id, None)

            self.send_response(200)
            self._set_cors()
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Disposition", f'attachment; filename="{job["filename"]}"')
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return

        self.send_response(404)
        self._set_cors()
        self.end_headers()
        self.wfile.write(b"Not found")

    def do_POST(self):
        path = self.path.split("?")[0]

        # ── Auth routes (no login required) ────────────────────────────

        if path == "/api/signup":
            self._handle_signup()
            return

        if path == "/api/login":
            self._handle_login()
            return

        if path == "/api/verify-otp":
            self._handle_verify_otp()
            return

        if path == "/api/resend-otp":
            self._handle_resend_otp()
            return

        if path == "/api/logout":
            token = self._get_session_token()
            if token:
                db.delete_session(token)
            self.send_response(200)
            self._set_cors()
            self._clear_session_cookie()
            self.send_header("Content-Type", "application/json")
            body = json.dumps({"ok": True}).encode()
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        # ── Everything below requires login ────────────────────────────

        user = self._current_user()
        if user is None:
            self._send_json(401, {"error": {"message": "Please log in to continue."}})
            return
        if user["is_banned"]:
            self._send_json(403, {"error": {"message": "Your account has been banned."}})
            return

        if path == "/api/admin/ban":
            if not user["is_admin"]:
                self._send_json(403, {"error": {"message": "Admins only"}})
                return
            payload = self._read_json_body()
            target_id = payload.get("userId")
            banned = bool(payload.get("banned"))
            db.set_user_banned(target_id, banned)
            db.log_activity(user["id"], "admin_ban" if banned else "admin_unban", f"target_user_id={target_id}")
            self._send_json(200, {"ok": True})
            return

        if path == "/api/admin/delete":
            if not user["is_admin"]:
                self._send_json(403, {"error": {"message": "Admins only"}})
                return
            payload = self._read_json_body()
            target_id = payload.get("userId")
            db.delete_user(target_id)
            db.log_activity(user["id"], "admin_delete_user", f"target_user_id={target_id}")
            self._send_json(200, {"ok": True})
            return

        if path == "/api/analyze":
            self._handle_analyze(user)
            return

        if path == "/api/download/start":
            self._handle_download_start(user)
            return

        if path == "/api/history/save":
            self._handle_save_history(user)
            return

        if path == "/api/history/delete":
            payload = self._read_json_body()
            history_id = payload.get("id")
            if not history_id:
                self._send_json(400, {"error": {"message": "Missing id"}})
                return
            deleted = db.delete_video_history_entry(history_id, user["id"])
            if not deleted:
                self._send_json(404, {"error": {"message": "History entry not found"}})
                return
            self._send_json(200, {"ok": True})
            return

        self.send_response(404)
        self._set_cors()
        self.end_headers()
        self.wfile.write(b"Not found")

    # ── Auth handlers ───────────────────────────────────────────────────

    def _handle_signup(self):
        try:
            payload = self._read_json_body()
        except json.JSONDecodeError as e:
            self._send_json(400, {"error": {"message": "Bad request: " + str(e)}})
            return

        email = (payload.get("email") or "").strip().lower()
        password = payload.get("password") or ""

        if not EMAIL_RE.match(email):
            self._send_json(400, {"error": {"message": "Please enter a valid email address."}})
            return
        if len(password) < 8:
            self._send_json(400, {"error": {"message": "Password must be at least 8 characters."}})
            return

        existing = db.get_user_by_email(email)
        if existing is not None and existing["is_verified"]:
            self._send_json(400, {"error": {"message": "An account with this email already exists."}})
            return

        if existing is None:
            db.create_user(email, password)
        else:
            # Unverified leftover signup — reset their password and re-send OTP
            db.set_password(email, password)

        code = db.create_otp(email, "signup")
        ok, msg = email_service.send_otp_email(email, code, "signup")
        if not ok:
            print(f"  ⚠️  Could not send OTP email: {msg}")
            print(f"  📋  DEV FALLBACK — OTP code for {email}: {code}")

        self._send_json(200, {"ok": True})

    def _handle_login(self):
        try:
            payload = self._read_json_body()
        except json.JSONDecodeError as e:
            self._send_json(400, {"error": {"message": "Bad request: " + str(e)}})
            return

        email = (payload.get("email") or "").strip().lower()
        password = payload.get("password") or ""

        user = db.get_user_by_email(email)
        if user is None or not db.verify_password(password, user["password_hash"], user["password_salt"]):
            self._send_json(401, {"error": {"message": "Incorrect email or password."}})
            return

        if user["is_banned"]:
            self._send_json(403, {"error": {"message": "Your account has been banned."}})
            return

        code = db.create_otp(email, "login")
        ok, msg = email_service.send_otp_email(email, code, "login")
        if not ok:
            print(f"  ⚠️  Could not send OTP email: {msg}")
            print(f"  📋  DEV FALLBACK — OTP code for {email}: {code}")

        self._send_json(200, {"ok": True})

    def _handle_verify_otp(self):
        try:
            payload = self._read_json_body()
        except json.JSONDecodeError as e:
            self._send_json(400, {"error": {"message": "Bad request: " + str(e)}})
            return

        email = (payload.get("email") or "").strip().lower()
        code = (payload.get("code") or "").strip()
        purpose = payload.get("purpose") or "signup"

        if not db.verify_otp(email, code, purpose):
            self._send_json(400, {"error": {"message": "Invalid or expired code. Please try again."}})
            return

        user = db.get_user_by_email(email)
        if user is None:
            self._send_json(400, {"error": {"message": "Account not found."}})
            return

        if purpose == "signup":
            db.mark_verified(email)
            db.log_activity(user["id"], "signup", "")
        else:
            db.log_activity(user["id"], "login", "")

        token = db.create_session(user["id"])
        self.send_response(200)
        self._set_cors()
        self._set_session_cookie(token)
        self.send_header("Content-Type", "application/json")
        body = json.dumps({"ok": True}).encode()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_resend_otp(self):
        try:
            payload = self._read_json_body()
        except json.JSONDecodeError as e:
            self._send_json(400, {"error": {"message": "Bad request: " + str(e)}})
            return

        email = (payload.get("email") or "").strip().lower()
        purpose = payload.get("purpose") or "signup"

        code = db.create_otp(email, purpose)
        ok, msg = email_service.send_otp_email(email, code, purpose)
        if not ok:
            print(f"  ⚠️  Could not send OTP email: {msg}")
            print(f"  📋  DEV FALLBACK — OTP code for {email}: {code}")

        self._send_json(200, {"ok": True})

    # ── App handlers (require login) ───────────────────────────────────

    def _handle_analyze(self, user):
        try:
            payload = self._read_json_body()
        except json.JSONDecodeError as e:
            self._send_json(400, {"error": {"message": "Bad request: " + str(e)}})
            return

        messages = payload.get("messages", [])
        prompt_text = ""
        for m in messages:
            if m.get("role") == "user":
                prompt_text += m.get("content", "")

        gemini_payload = {
            "contents": [{"parts": [{"text": prompt_text}]}],
            "generationConfig": {
                "maxOutputTokens": 4096,
                "temperature": 0.7,
                "responseMimeType": "application/json",
            },
        }

        req = urllib.request.Request(
            GEMINI_URL,
            data=json.dumps(gemini_payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with urllib.request.urlopen(req) as resp:
                status = resp.status
                data = resp.read()
        except urllib.error.HTTPError as e:
            status = e.code
            data = e.read()
        except urllib.error.URLError as e:
            self._send_json(500, {"error": {"message": str(e.reason)}})
            return

        try:
            gemini_data = json.loads(data)
        except json.JSONDecodeError:
            self._send_json(status, {"error": {"message": "Invalid response from Gemini"}})
            return

        if status != 200:
            err_msg = gemini_data.get("error", {}).get("message", "Gemini API error")
            self._send_json(status, {"error": {"message": err_msg}})
            return

        try:
            candidate = gemini_data["candidates"][0]
            text = candidate["content"]["parts"][0]["text"]
            finish_reason = candidate.get("finishReason", "")
            if finish_reason == "MAX_TOKENS":
                self._send_json(500, {"error": {"message": (
                    "Response was cut off (too long). Try again — this usually works on a retry."
                )}})
                return
        except (KeyError, IndexError):
            self._send_json(500, {"error": {"message": (
                "No content returned from Gemini. The response may have been blocked by safety filters."
            )}})
            return

        db.log_activity(user["id"], "analyze", "")
        self._send_json(200, {"content": [{"type": "text", "text": text}]})

    def _handle_save_history(self, user):
        try:
            payload = self._read_json_body()
        except json.JSONDecodeError as e:
            self._send_json(400, {"error": {"message": "Bad request: " + str(e)}})
            return

        video_id = payload.get("videoId", "")
        video_url = payload.get("videoUrl", "")
        video_title = payload.get("videoTitle", "")
        video_author = payload.get("videoAuthor", "")
        video_thumbnail = payload.get("videoThumbnail", "")
        content_type = payload.get("contentType", "")
        video_summary = payload.get("videoSummary", "")
        clip_duration = payload.get("clipDuration", 0)
        clips = payload.get("clips", [])

        if not video_id or not clips:
            self._send_json(400, {"error": {"message": "Missing videoId or clips"}})
            return

        history_id = db.save_video_history(
            user_id=user["id"],
            video_id=video_id,
            video_url=video_url,
            video_title=video_title,
            video_author=video_author,
            video_thumbnail=video_thumbnail,
            content_type=content_type,
            video_summary=video_summary,
            clip_duration=clip_duration,
            clip_count=len(clips),
            clips_json=json.dumps(clips),
        )

        db.log_activity(user["id"], "save_history", f"video={video_id} clips={len(clips)}")
        self._send_json(200, {"ok": True, "id": history_id})

    def _handle_download_start(self, user):
        """Validates the request, creates a job, and spawns a background
        thread to do the actual yt-dlp/ffmpeg work. Returns instantly with
        a job_id so the HTTP request never risks hitting Render's proxy
        timeout on long downloads."""
        try:
            payload = self._read_json_body()
        except json.JSONDecodeError as e:
            self._send_json(400, {"error": {"message": "Bad request: " + str(e)}})
            return

        video_id = payload.get("videoId", "")
        start = payload.get("start", 0)
        end = payload.get("end", 20)
        layout = payload.get("layout", "original")

        if not video_id:
            self._send_json(400, {"error": {"message": "Missing videoId"}})
            return

        if layout not in ("original", "vertical", "square"):
            layout = "original"

        if shutil.which("yt-dlp") is None:
            self._send_json(500, {"error": {"message": (
                "yt-dlp is not installed or not on PATH. Install it with: pip install yt-dlp"
            )}})
            return

        if shutil.which("ffmpeg") is None:
            self._send_json(500, {"error": {"message": (
                "ffmpeg is not installed or not on PATH. Install it from ffmpeg.org "
                "and make sure it's added to your system PATH."
            )}})
            return

        job_id = uuid.uuid4().hex
        with _download_jobs_lock:
            _download_jobs[job_id] = {
                "user_id": user["id"],
                "status": "processing",
                "error": None,
                "filepath": None,
                "filename": None,
                "created_at": time.time(),
            }

        thread = threading.Thread(
            target=_run_download_job,
            args=(job_id, user["id"], video_id, start, end, layout),
            daemon=True,
        )
        thread.start()

        self._send_json(200, {"jobId": job_id})

    def log_message(self, fmt, *args):
        pass


if __name__ == "__main__":
    if not db.DATABASE_URL and os.environ.get("RENDER"):
        print("")
        print("  ❌  DATABASE_URL is not set, but this looks like a Render deployment.")
        print("      This usually means the database wasn't linked to this service.")
        print("      Fix: redeploy using Render's 'New → Blueprint' option so it reads")
        print("      render.yaml and provisions + links the database automatically.")
        print("      Alternatively, manually create a PostgreSQL instance on Render and")
        print("      add its 'Internal Database URL' as the DATABASE_URL env var here.")
        print("")
        raise SystemExit(1)

    db.init_db()

    if GEMINI_API_KEY.startswith("YOUR_GEMINI"):
        print("")
        print("  ⚠️   Gemini API key not set! Set it in server.py or as the")
        print("       GEMINI_API_KEY environment variable.")
        print("  👉  Get one free at: https://aistudio.google.com/app/apikey")

    if email_service.BREVO_API_KEY.startswith("YOUR_BREVO"):
        print("")
        print("  ⚠️   Brevo API key not set in email_service.py — OTP codes will")
        print("       print to this terminal instead of being emailed.")

    cleanup_thread = threading.Thread(target=_cleanup_stale_jobs, daemon=True)
    cleanup_thread.start()

    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print("")
    print("  ✅  ClipForge is running!")
    print(f"  👉  Open http://localhost:{PORT} in your browser")
    print("")
    print("  Press Ctrl+C to stop the server")
    print("")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Stopped.")
        server.shutdown()
