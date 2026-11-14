import json
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Literal

import bcrypt
import httpx
from deep_translator import GoogleTranslator
from fastapi import Depends, FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, field_validator
from starlette.concurrency import run_in_threadpool
from starlette.middleware.sessions import SessionMiddleware
from youtube_transcript_api import YouTubeTranscriptApi

# ---------- Paths & environment ----------

BASE_DIR = Path(__file__).parent
STATIC_DIR = BASE_DIR / "static"
DATA_DIR = Path(os.getenv("DATA_DIR", BASE_DIR / "data"))
SETTINGS_FILE = DATA_DIR / "settings.json"
ADMIN_FILE = DATA_DIR / "admin.json"
DATA_DIR.mkdir(parents=True, exist_ok=True)

WORK_DIR = Path(os.getenv("WORK_DIR", Path(tempfile.gettempdir()) / "tubepulse"))
WORK_DIR.mkdir(parents=True, exist_ok=True)

SESSION_SECRET = os.getenv("SESSION_SECRET", "")
if len(SESSION_SECRET) < 32:
    # Fallback: random secret banata hai. Restart par ye badlega, isliye admin
    # ko dobara login karna padega. Stable rakhne ke liye Render Environment
    # mein SESSION_SECRET set karein.
    SESSION_SECRET = secrets.token_urlsafe(48)
    print("WARNING: SESSION_SECRET set nahi tha, temporary secret use ho raha hai.")

ENV_ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "").strip().lower()
ENV_ADMIN_HASH = os.getenv("ADMIN_PASSWORD_HASH", "")
HTTPS_ONLY = os.getenv("HTTPS_ONLY", "1") == "1"  # local testing ke liye HTTPS_ONLY=0

YT_API = "https://www.googleapis.com/youtube/v3"
VIDEO_ID_RE = re.compile(r"(?:v=|youtu\.be/|shorts/|embed/)([A-Za-z0-9_-]{11})|^([A-Za-z0-9_-]{11})$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_MB", "150")) * 1024 * 1024
JOB_TTL_SECONDS = 20 * 60
MAX_PARALLEL_FFMPEG = 2
ALLOWED_EXT = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".mp3", ".m4a", ".wav", ".ogg", ".aac", ".flac"}
MP3_BITRATES = {64, 128, 192, 320}
MP4_HEIGHTS = {360, 480, 720, 1080}
MEDIA_TYPES = {"mp3": "audio/mpeg", "mp4": "video/mp4"}

app = FastAPI(title="TubePulse AI")
app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET,
    session_cookie="tp_session",
    max_age=8 * 60 * 60,
    same_site="lax",
    https_only=HTTPS_ONLY,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["*"],
)


# ---------- Site settings (server-side JSON) ----------

DEFAULT_SETTINGS = {
    "site_title": "TubePulse AI",
    "accent_color": "#ff2a85",
    "header_ad_script": "",
    "whatsapp_url": "",
    "telegram_url": "",
    "youtube_api_key": "",
}
PRIVATE_KEYS = {"youtube_api_key"}  # kabhi public API mein nahi jaayenge
_settings_lock = threading.RLock()


def _atomic_write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def read_settings() -> dict:
    with _settings_lock:
        try:
            raw = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            raw = {}
        return {**DEFAULT_SETTINGS, **raw}


def update_settings(changes: dict) -> None:
    with _settings_lock:
        data = read_settings()
        data.update(changes)
        _atomic_write(SETTINGS_FILE, data)


def get_youtube_key() -> str:
    # Pehle admin panel wali key, phir Render env (fallback)
    return read_settings().get("youtube_api_key") or os.getenv("YOUTUBE_API_KEY", "")


class SiteSettingsIn(BaseModel):
    site_title: str = Field(min_length=1, max_length=60)
    accent_color: str = Field(pattern=r"^#[0-9a-fA-F]{6}$")
    header_ad_script: str = Field(default="", max_length=20000)
    whatsapp_url: str = Field(default="", max_length=300)
    telegram_url: str = Field(default="", max_length=300)

    @field_validator("whatsapp_url", "telegram_url")
    @classmethod
    def must_be_https(cls, v: str) -> str:
        if v and not v.startswith("https://"):
            raise ValueError("Community links https:// se shuru hone chahiye")
        return v


class YouTubeKeyIn(BaseModel):
    key: str = Field(min_length=20, max_length=100)

    @field_validator("key")
    @classmethod
    def valid_chars(cls, v: str) -> str:
        v = v.strip()
        if not re.fullmatch(r"[A-Za-z0-9_\-]+", v):
            raise ValueError("API key mein sirf letters, numbers, - aur _ hone chahiye")
        return v


@app.get("/api/settings")
def public_settings():
    return {k: v for k, v in read_settings().items() if k not in PRIVATE_KEYS}


# ---------- Admin auth ----------

_admin_lock = threading.RLock()
_failed_logins: dict[str, list[float]] = {}
_login_lock = threading.Lock()
MAX_ATTEMPTS = 5
WINDOW_SECONDS = 15 * 60


def load_admin() -> dict:
    try:
        return json.loads(ADMIN_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {"email": ENV_ADMIN_EMAIL, "password_hash": ENV_ADMIN_HASH}


def verify_password(password: str, password_hash: str) -> bool:
    if not password_hash:
        return False
    try:
        return bcrypt.checkpw(password.encode(), password_hash.encode())
    except ValueError:
        return False


def check_rate_limit(ip: str) -> None:
    now = time.time()
    with _login_lock:
        recent = [t for t in _failed_logins.get(ip, []) if now - t < WINDOW_SECONDS]
        _failed_logins[ip] = recent
        if len(recent) >= MAX_ATTEMPTS:
            raise HTTPException(429, "Bahut zyada galat attempts. 15 minute baad try karein.")


def record_failure(ip: str) -> None:
    with _login_lock:
        _failed_logins.setdefault(ip, []).append(time.time())


def require_admin(request: Request) -> None:
    if not request.session.get("admin"):
        raise HTTPException(401, "Admin login required")


class LoginIn(BaseModel):
    email: str = Field(min_length=3, max_length=200)
    password: str = Field(min_length=1, max_length=72)


class ChangeCredentialsIn(BaseModel):
    current_password: str = Field(min_length=1, max_length=72)
    new_email: str = Field(min_length=5, max_length=200)
    new_password: str = Field(min_length=10, max_length=72)

    @field_validator("new_email")
    @classmethod
    def valid_email(cls, v: str) -> str:
        v = v.strip().lower()
        if not EMAIL_RE.match(v):
            raise ValueError("Email format sahi nahi hai")
        return v

    @field_validator("new_password")
    @classmethod
    def strong_enough(cls, v: str) -> str:
        if len(v.encode()) > 72:
            raise ValueError("Password 72 bytes se chhota hona chahiye")
        if not (re.search(r"[A-Za-z]", v) and re.search(r"\d", v)):
            raise ValueError("Password mein kam se kam ek letter aur ek number hona chahiye")
        return v


@app.post("/api/admin/login")
def admin_login(body: LoginIn, request: Request):
    ip = request.client.host if request.client else "unknown"
    check_rate_limit(ip)

    admin = load_admin()
    email_ok = secrets.compare_digest(
        body.email.strip().lower().encode(), (admin.get("email") or "").lower().encode()
    )
    pass_ok = verify_password(body.password, admin.get("password_hash", ""))
    if not (email_ok and pass_ok):
        record_failure(ip)
        raise HTTPException(401, "Email ya password galat hai")

    request.session.clear()  # session fixation se bachav
    request.session["admin"] = True
    return {"ok": True}


@app.post("/api/admin/logout")
def admin_logout(request: Request):
    request.session.clear()
    return {"ok": True}


@app.get("/api/admin/me")
def admin_me(request: Request):
    return {"authenticated": bool(request.session.get("admin"))}


@app.post("/api/admin/change-credentials", dependencies=[Depends(require_admin)])
def change_credentials(body: ChangeCredentialsIn, request: Request):
    with _admin_lock:
        admin = load_admin()
        if not verify_password(body.current_password, admin.get("password_hash", "")):
            raise HTTPException(401, "Current password galat hai")
        new_hash = bcrypt.hashpw(body.new_password.encode(), bcrypt.gensalt()).decode()
        _atomic_write(ADMIN_FILE, {"email": body.new_email, "password_hash": new_hash})
    request.session.clear()  # purana session band
    return {"ok": True}


# ---------- Admin settings & YouTube key ----------

@app.get("/api/admin/settings", dependencies=[Depends(require_admin)])
def admin_get_settings():
    s = read_settings()
    key = s.get("youtube_api_key", "")
    public = {k: v for k, v in s.items() if k not in PRIVATE_KEYS}
    return {
        **public,
        "youtube_key_set": bool(key or os.getenv("YOUTUBE_API_KEY")),
        "youtube_key_hint": f"••••{key[-4:]}" if key else "",
    }


@app.put("/api/admin/settings", dependencies=[Depends(require_admin)])
def admin_update_settings(body: SiteSettingsIn):
    update_settings(body.model_dump())
    return {"ok": True}


@app.post("/api/admin/youtube-key", dependencies=[Depends(require_admin)])
def admin_save_youtube_key(body: YouTubeKeyIn):
    update_settings({"youtube_api_key": body.key})
    return {"ok": True}


@app.delete("/api/admin/youtube-key", dependencies=[Depends(require_admin)])
def admin_remove_youtube_key():
    update_settings({"youtube_api_key": ""})
    return {"ok": True}


@app.post("/api/admin/youtube-key/test", dependencies=[Depends(require_admin)])
async def admin_test_youtube_key():
    key = get_youtube_key()
    if not key:
        return {"ok": False, "message": "Key set nahi hai."}
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(f"{YT_API}/videos", params={"part": "id", "id": "dQw4w9WgXcQ", "key": key})
    if r.status_code == 200:
        return {"ok": True, "message": "Key kaam kar rahi hai."}
    return {
        "ok": False,
        "message": f"YouTube ne key reject ki (HTTP {r.status_code}). Check karein ki key sahi hai, "
                   "YouTube Data API v3 enable hai, aur quota baaki hai.",
    }


# ---------- Pages & health ----------

@app.get("/")
def health():
    return {"status": "TubePulse AI Backend is Live and Running at Full Capacity!"}


@app.get("/app", include_in_schema=False)
def public_page():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/admin", include_in_schema=False)
def admin_page():
    return FileResponse(STATIC_DIR / "admin.html")


# ---------- YouTube search ----------

def extract_video_id(text: str):
    m = VIDEO_ID_RE.search(text.strip())
    return (m.group(1) or m.group(2)) if m else None


def best_thumbnail(thumbs: dict) -> str:
    for key in ("maxres", "standard", "high", "medium", "default"):
        if key in thumbs:
            return thumbs[key]["url"]
    return ""


@app.get("/api/search")
async def search_youtube(q: str = Query(..., min_length=1, max_length=200)):
    key = get_youtube_key()
    if not key:
        raise HTTPException(503, "YouTube API key abhi set nahi hai. Admin panel (/admin) se add karein.")

    video_id = extract_video_id(q)
    async with httpx.AsyncClient(timeout=15) as client:
        if video_id:
            r = await client.get(f"{YT_API}/videos", params={
                "part": "snippet", "id": video_id, "key": key,
            })
        else:
            r = await client.get(f"{YT_API}/search", params={
                "part": "snippet", "type": "video", "maxResults": 8,
                "q": q, "key": key,
            })

    if r.status_code != 200:
        raise HTTPException(502, "YouTube API key invalid ya quota khatam hai. Admin panel mein key check karein.")

    results = []
    for item in r.json().get("items", []):
        vid = item["id"] if video_id else item["id"]["videoId"]
        s = item["snippet"]
        results.append({
            "video_id": vid,
            "title": s["title"],
            "uploader": s["channelTitle"],
            "thumbnail": best_thumbnail(s.get("thumbnails", {})),
        })
    return {"results": results}


# ---------- Transcripts ----------

def format_time(seconds: float) -> str:
    total = int(seconds)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


@app.get("/api/transcript")
def get_transcript(
    video_id: str = Query(..., pattern=r"^[A-Za-z0-9_-]{11}$"),
    lang: str = Query("en", pattern=r"^[a-z]{2,3}(-[A-Za-z]{2,4})?$"),
):
    try:
        fetched = YouTubeTranscriptApi().fetch(video_id, languages=[lang, "en"])
    except Exception:
        raise HTTPException(404, "Is video ke liye captions nahi mile")

    lines = [
        {"start": s.start, "time": format_time(s.start), "text": s.text}
        for s in fetched.snippets
    ]
    return {"success": True, "video_id": video_id, "lines": lines}


# ---------- Translation ----------

class TranslateIn(BaseModel):
    lines: list[str] = Field(max_length=500)
    target: Literal["en", "hi", "or", "bn", "te"] = "hi"


@app.post("/api/translate-batch")
def translate_batch(body: TranslateIn):
    texts = [line[:1000] for line in body.lines]
    idx = [i for i, t in enumerate(texts) if t.strip()]
    out = [""] * len(texts)

    if idx:
        try:
            translated = GoogleTranslator(source="auto", target=body.target).translate_batch(
                [texts[i] for i in idx]
            )
        except Exception:
            raise HTTPException(502, "Translation failed")
        for i, t in zip(idx, translated):
            out[i] = t or texts[i]

    return {"lines": out}


# ---------- Media converter (user ki apni uploaded files) ----------

jobs: dict[str, dict] = {}
jobs_lock = threading.Lock()
ffmpeg_slots = threading.BoundedSemaphore(MAX_PARALLEL_FFMPEG)


def cleanup_expired_jobs():
    now = time.time()
    with jobs_lock:
        for job_id, job in list(jobs.items()):
            if now - job["created"] > JOB_TTL_SECONDS:
                shutil.rmtree(job["dir"], ignore_errors=True)
                jobs.pop(job_id, None)


def cleanup_loop():
    while True:
        time.sleep(60)
        cleanup_expired_jobs()


threading.Thread(target=cleanup_loop, daemon=True).start()


def probe(path: Path) -> dict:
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_type",
         "-of", "json", str(path)],
        capture_output=True, text=True, timeout=60,
    )
    if r.returncode != 0:
        raise ValueError("ffprobe failed")
    data = json.loads(r.stdout or "{}")
    duration = float(data.get("format", {}).get("duration") or 0)
    types = {s.get("codec_type") for s in data.get("streams", [])}
    return {"duration": duration, "has_audio": "audio" in types, "has_video": "video" in types}


def build_ffmpeg_cmd(src: Path, out: Path, fmt: str, quality: int) -> list[str]:
    base = ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error",
            "-progress", "pipe:1", "-nostats", "-y", "-i", str(src)]
    if fmt == "mp3":
        return base + ["-vn", "-map", "0:a:0", "-c:a", "libmp3lame",
                       "-b:a", f"{quality}k", str(out)]
    return base + [
        "-map", "0:v:0", "-map", "0:a:0?",
        "-vf", f"scale=-2:{quality}",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        str(out),
    ]


def run_ffmpeg(job_id: str, cmd: list[str], out: Path):
    with jobs_lock:
        job = jobs.get(job_id)
    if not job:
        return
    duration = job["duration"]

    with ffmpeg_slots:
        with jobs_lock:
            job["status"] = "converting"
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            for line in proc.stdout:
                line = line.strip()
                if line.startswith(("out_time_us=", "out_time_ms=")):
                    try:
                        seconds = int(line.split("=", 1)[1]) / 1_000_000
                    except ValueError:
                        continue
                    pct = min(99, int(seconds / duration * 100)) if duration else 0
                    with jobs_lock:
                        job["progress"] = pct
            err = proc.stderr.read()
            if proc.wait() != 0 or not out.exists():
                raise RuntimeError(err[-300:] or "ffmpeg failed")
            with jobs_lock:
                job.update(status="done", progress=100, output=out)
        except Exception as e:
            print("ffmpeg error:", e)
            with jobs_lock:
                job.update(status="error", error="Conversion nahi ho payi. Dusri quality ya file try karein.")


@app.post("/api/convert/upload")
async def convert_upload(file: UploadFile = File(...)):
    if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
        raise HTTPException(503, "Server par ffmpeg install nahi hai")

    original = file.filename or "upload"
    ext = Path(original).suffix.lower()
    if ext not in ALLOWED_EXT:
        raise HTTPException(400, "Ye file type support nahi hai")

    job_id = uuid.uuid4().hex
    job_dir = WORK_DIR / job_id
    job_dir.mkdir(parents=True)
    src = job_dir / f"input{ext}"

    try:
        size = 0
        with src.open("wb") as out:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    raise HTTPException(413, "File size limit se badi hai")
                out.write(chunk)
        info = await run_in_threadpool(probe, src)
    except HTTPException:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise
    except Exception:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(400, "File padhi nahi ja saki. Check karein ki ye sahi media file hai.")

    if info["duration"] <= 0 or not (info["has_audio"] or info["has_video"]):
        shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(400, "Is file mein audio ya video nahi mila")

    name = Path(original).stem[:80] or "media"
    with jobs_lock:
        jobs[job_id] = {
            "dir": job_dir, "src": src, "name": name,
            "duration": info["duration"],
            "has_audio": info["has_audio"], "has_video": info["has_video"],
            "created": time.time(), "status": "uploaded", "progress": 0,
            "error": None, "output": None, "format": None, "quality": None,
        }

    return {
        "job_id": job_id, "name": name, "duration": info["duration"],
        "has_audio": info["has_audio"], "has_video": info["has_video"],
    }


class ConvertIn(BaseModel):
    job_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    format: Literal["mp3", "mp4"]
    quality: int


@app.post("/api/convert/start")
def convert_start(body: ConvertIn):
    allowed = MP3_BITRATES if body.format == "mp3" else MP4_HEIGHTS
    if body.quality not in allowed:
        raise HTTPException(400, "Ye quality available nahi hai")

    with jobs_lock:
        job = jobs.get(body.job_id)
        if not job:
            raise HTTPException(404, "File expire ho gayi. Dobara upload karein.")
        if job["status"] in ("queued", "converting"):
            raise HTTPException(409, "Ek conversion pehle se chal raha hai")
        if body.format == "mp4" and not job["has_video"]:
            raise HTTPException(400, "Is file mein video track nahi hai")
        if body.format == "mp3" and not job["has_audio"]:
            raise HTTPException(400, "Is file mein audio track nahi hai")

        out = job["dir"] / f"output_{body.format}_{body.quality}.{body.format}"
        job.update(status="queued", progress=0, error=None, output=None,
                   format=body.format, quality=body.quality)

    cmd = build_ffmpeg_cmd(job["src"], out, body.format, body.quality)
    threading.Thread(target=run_ffmpeg, args=(body.job_id, cmd, out), daemon=True).start()
    return {"ok": True}


@app.get("/api/convert/status/{job_id}")
def convert_status(job_id: str):
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            raise HTTPException(404, "File expire ho gayi. Dobara upload karein.")
        return {"status": job["status"], "progress": job["progress"], "error": job["error"]}


@app.get("/api/convert/download/{job_id}")
def convert_download(job_id: str):
    with jobs_lock:
        job = jobs.get(job_id)
        if not job or job["status"] != "done" or not job["output"]:
            raise HTTPException(404, "Download ready nahi hai")
        fmt, quality = job["format"], job["quality"]
        output, name = job["output"], job["name"]

    suffix = f"{quality}kbps" if fmt == "mp3" else f"{quality}p"
    return FileResponse(output, media_type=MEDIA_TYPES[fmt], filename=f"{name}_{suffix}.{fmt}")
