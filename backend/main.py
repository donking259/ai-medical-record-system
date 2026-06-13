from __future__ import annotations

import asyncio
import html
import json
import os
import re
import secrets
import sqlite3
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import Cookie, Depends, FastAPI, File, Form, Header, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from dotenv import load_dotenv


ROOT_DIR = Path(__file__).resolve().parent.parent
load_dotenv(ROOT_DIR / ".env")

DATA_DIR = Path(os.getenv("APP_DATA_DIR", ROOT_DIR / "data")).resolve()
UPLOAD_DIR = DATA_DIR / "uploads"
EXPORT_DIR = DATA_DIR / "exports"
DB_PATH = DATA_DIR / "ai_emr.sqlite3"
DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{DB_PATH}")
ASR_PROVIDER = os.getenv("ASR_PROVIDER", "mock").lower()
STREAM_ASR_PROVIDER = os.getenv("STREAM_ASR_PROVIDER", "sherpa_onnx").lower()
VOSK_MODEL_PATH = Path(os.getenv("VOSK_MODEL_PATH", ROOT_DIR / "models" / "vosk-model-small-cn-0.22")).resolve()
VOSK_SAMPLE_RATE = int(os.getenv("VOSK_SAMPLE_RATE", "16000"))
VOSK_MODEL = None
SHERPA_MODEL_PATH = Path(os.getenv("SHERPA_MODEL_PATH", ROOT_DIR / "models" / "sherpa-onnx-streaming-paraformer-bilingual-zh-en")).resolve()
SHERPA_SAMPLE_RATE = int(os.getenv("SHERPA_SAMPLE_RATE", "16000"))
SHERPA_NUM_THREADS = int(os.getenv("SHERPA_NUM_THREADS", "2"))
SHERPA_RECOGNIZER = None
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "rules").lower()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash")
DEEPSEEK_TIMEOUT_SECONDS = float(os.getenv("DEEPSEEK_TIMEOUT_SECONDS", "20"))
EMR_MAX_TRANSCRIPT_CHARS = int(os.getenv("EMR_MAX_TRANSCRIPT_CHARS", "4500"))
ASR_AI_STANDARDIZE = os.getenv("ASR_AI_STANDARDIZE", "true").lower() in {"1", "true", "yes", "on"}
ASR_AI_STANDARDIZE_TIMEOUT_SECONDS = float(os.getenv("ASR_AI_STANDARDIZE_TIMEOUT_SECONDS", "6"))
ASR_AI_STANDARDIZE_MAX_CHARS = int(os.getenv("ASR_AI_STANDARDIZE_MAX_CHARS", "1500"))
WHISPER_MODEL_SIZE = os.getenv("WHISPER_MODEL_SIZE", "small")
WHISPER_DEVICE = os.getenv("WHISPER_DEVICE", "cpu")
WHISPER_COMPUTE_TYPE = os.getenv("WHISPER_COMPUTE_TYPE", "int8")
WHISPER_LANGUAGE = os.getenv("WHISPER_LANGUAGE", "zh")
WHISPER_BEAM_SIZE = int(os.getenv("WHISPER_BEAM_SIZE", "8"))
WHISPER_TEMPERATURE = float(os.getenv("WHISPER_TEMPERATURE", "0"))
WHISPER_NO_SPEECH_THRESHOLD = float(os.getenv("WHISPER_NO_SPEECH_THRESHOLD", "0.8"))
WHISPER_FALLBACK_MIN_CHARS = int(os.getenv("WHISPER_FALLBACK_MIN_CHARS", "18"))
WHISPER_RETRY_ON_SHORT = os.getenv("WHISPER_RETRY_ON_SHORT", "true").lower() in {"1", "true", "yes", "on"}
WHISPER_VAD_MIN_SILENCE_MS = int(os.getenv("WHISPER_VAD_MIN_SILENCE_MS", "600"))
WHISPER_VAD_SPEECH_PAD_MS = int(os.getenv("WHISPER_VAD_SPEECH_PAD_MS", "250"))
REALTIME_MIN_CHUNK_BYTES = int(os.getenv("REALTIME_MIN_CHUNK_BYTES", "2048"))
WHISPER_MODEL = None
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin123")
ADMIN_DOCTOR_NAME = os.getenv("ADMIN_DOCTOR_NAME", "王医生")
SESSION_TOKENS: dict[str, dict[str, str]] = {}
JOB_STORE: dict[str, dict[str, Any]] = {}
JOB_EXECUTOR = ThreadPoolExecutor(max_workers=int(os.getenv("ASR_WORKER_CONCURRENCY", "1")))
ASR_STANDARDIZER_EXECUTOR = ThreadPoolExecutor(max_workers=int(os.getenv("ASR_STANDARDIZER_CONCURRENCY", "2")))

DATA_DIR.mkdir(parents=True, exist_ok=True)
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
EXPORT_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="AI 门诊病历生成系统", version="0.2.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("CORS_ALLOW_ORIGINS", "http://localhost:8000,http://127.0.0.1:8000").split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class PatientInfo(BaseModel):
    name: str = Field(default="")
    gender: str = Field(default="")
    age: str = Field(default="")
    department: str = Field(default="")
    visit_no: str = Field(default="")


class GenerateEmrRequest(BaseModel):
    transcript: str
    patient: Optional[PatientInfo] = None


class ConfirmEmrRequest(BaseModel):
    patient: PatientInfo
    transcript: str
    raw_transcript: str = Field(default="")
    emr: dict[str, Any]
    evidence: list[dict[str, str]] = Field(default_factory=list)


class TranscribeTextRequest(BaseModel):
    text: str


class LoginRequest(BaseModel):
    username: str
    password: str


class JobCreateResponse(BaseModel):
    job_id: str
    status: str


class EmrSyncRequest(BaseModel):
    record_id: str
    target_system: str = "external_emr"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def model_json(model: BaseModel) -> str:
    if hasattr(model, "model_dump_json"):
        return model.model_dump_json()
    return model.json(ensure_ascii=False)


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS audio_files (
                id TEXT PRIMARY KEY,
                original_name TEXT NOT NULL,
                stored_name TEXT NOT NULL,
                content_type TEXT,
                size_bytes INTEGER NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS emr_records (
                id TEXT PRIMARY KEY,
                patient_json TEXT NOT NULL,
                transcript TEXT NOT NULL,
                raw_transcript TEXT,
                emr_json TEXT NOT NULL,
                evidence_json TEXT NOT NULL,
                confirmed_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS audit_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT,
                action TEXT NOT NULL,
                resource_type TEXT NOT NULL,
                resource_id TEXT,
                detail_json TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS integration_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                record_id TEXT NOT NULL,
                target_system TEXT NOT NULL,
                status TEXT NOT NULL,
                request_json TEXT,
                response_json TEXT,
                created_at TEXT NOT NULL
            );
            """
        )
        ensure_column(conn, "audit_logs", "user_id", "TEXT")
        ensure_column(conn, "audit_logs", "detail_json", "TEXT")
        ensure_column(conn, "emr_records", "raw_transcript", "TEXT")


def ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl_type: str) -> None:
    cols = [row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}")


@app.on_event("startup")
def on_startup() -> None:
    init_db()


def current_user(
    authorization: Optional[str] = Header(default=None),
    ai_emr_token: Optional[str] = Cookie(default=None),
) -> dict[str, str]:
    if ai_emr_token and ai_emr_token in SESSION_TOKENS:
        return SESSION_TOKENS[ai_emr_token]
    if not authorization:
        raise HTTPException(status_code=401, detail="缺少 Authorization")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(status_code=401, detail="Authorization 格式错误")
    user = SESSION_TOKENS.get(token)
    if not user:
        raise HTTPException(status_code=401, detail="登录已失效")
    return user


def optional_user(authorization: Optional[str] = Header(default=None)) -> Optional[dict[str, str]]:
    if not authorization:
        return None


def create_session(username: str) -> tuple[str, dict[str, str]]:
    token = secrets.token_urlsafe(32)
    user = {"username": username, "role": "admin", "doctor_name": ADMIN_DOCTOR_NAME}
    SESSION_TOKENS[token] = user
    return token, user
    try:
        return current_user(authorization)
    except HTTPException:
        return None


def mask_database_url(url: str) -> str:
    return re.sub(r"://([^:/@]+):([^@]+)@", r"://\1:***@", url)


@app.get("/api/health")
def health() -> dict[str, str]:
    return {
        "status": "ok",
        "database_url": mask_database_url(DATABASE_URL),
        "asr_provider": ASR_PROVIDER,
        "llm_provider": LLM_PROVIDER,
        "openai_model": OPENAI_MODEL,
        "deepseek_model": DEEPSEEK_MODEL,
        "stream_asr_provider": STREAM_ASR_PROVIDER,
        "sherpa_sample_rate": str(SHERPA_SAMPLE_RATE),
        "sherpa_num_threads": str(SHERPA_NUM_THREADS),
        "vosk_sample_rate": str(VOSK_SAMPLE_RATE),
        "whisper_model_size": WHISPER_MODEL_SIZE,
        "whisper_device": WHISPER_DEVICE,
    }


@app.post("/api/auth/login")
def login(payload: LoginRequest) -> dict[str, Any]:
    if payload.username != ADMIN_USERNAME or payload.password != ADMIN_PASSWORD:
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    token, user = create_session(payload.username)
    with db() as conn:
        audit(conn, "login", "user", payload.username, payload.username)
    return {"access_token": token, "token_type": "bearer", "user": user}


@app.post("/api/auth/login-form")
def login_form(username: str = Form(...), password: str = Form(...)) -> RedirectResponse:
    if username != ADMIN_USERNAME or password != ADMIN_PASSWORD:
        return RedirectResponse(url="/login?login_error=1", status_code=303)
    token, user = create_session(username)
    with db() as conn:
        audit(conn, "login_form", "user", username, username)
    response = RedirectResponse(url="/", status_code=303)
    response.set_cookie("ai_emr_token", token, httponly=False, samesite="lax")
    return response


@app.get("/api/me")
def me(user: dict[str, str] = Depends(current_user)) -> dict[str, str]:
    return user


@app.post("/api/auth/logout")
def logout(ai_emr_token: Optional[str] = Cookie(default=None)) -> RedirectResponse:
    if ai_emr_token:
        SESSION_TOKENS.pop(ai_emr_token, None)
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie("ai_emr_token")
    response.delete_cookie("ai_emr_user")
    return response


@app.get("/login", response_class=HTMLResponse)
def login_page(login_error: Optional[str] = None) -> str:
    error_html = (
        '<p class="login-error">用户名或密码错误，请重新输入。</p>'
        if login_error
        else ""
    )
    return f"""<!doctype html>
<html lang="zh-CN">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>医生登录 - AI 门诊病历系统</title>
    <link rel="stylesheet" href="/styles.css?v=20260608-login-rebuild-1" />
  </head>
  <body>
    <section class="login-screen">
      <div class="login-card">
        <p class="eyebrow">AI 门诊病历系统</p>
        <h1>医生登录</h1>
        {error_html}
        <form action="/api/auth/login-form" method="post">
          <label>
            用户名
            <input name="username" value="{html.escape(ADMIN_USERNAME)}" autocomplete="username" />
          </label>
          <label>
            密码
            <input name="password" type="password" value="{html.escape(ADMIN_PASSWORD)}" autocomplete="current-password" />
          </label>
          <button class="primary" type="submit">登录系统</button>
        </form>
        <p class="login-tip">登录后可使用录音转写、AI 病历生成、打印和导出功能。</p>
      </div>
    </section>
  </body>
</html>"""


@app.get("/", response_class=HTMLResponse)
def app_page(ai_emr_token: Optional[str] = Cookie(default=None)) -> Any:
    user = SESSION_TOKENS.get(ai_emr_token or "")
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    index_path = ROOT_DIR / "index.html"
    if not index_path.exists():
        raise HTTPException(status_code=404, detail="前端页面不存在")

    doctor_name = html.escape(user.get("doctor_name") or user.get("username") or "已登录")
    page = index_path.read_text(encoding="utf-8")
    page = page.replace('<section class="login-screen" id="loginScreen">', '<section class="login-screen" id="loginScreen" hidden>')
    page = page.replace('<main class="app-shell" id="workspaceApp" hidden>', '<main class="app-shell" id="workspaceApp">')
    page = page.replace('<span id="doctorName">未登录</span>', f'<span id="doctorName">当前医生：{doctor_name}</span>')
    page = page.replace('<div class="status-pill" id="visitStatus">待登录</div>', '<div class="status-pill" id="visitStatus">待上传录音</div>')
    return HTMLResponse(page)


@app.post("/api/audio")
async def upload_audio(file: UploadFile = File(...), user: dict[str, str] = Depends(current_user)) -> dict[str, Any]:
    if not file.content_type or not file.content_type.startswith("audio/"):
        raise HTTPException(status_code=400, detail="请上传音频文件")

    audio_id = uuid.uuid4().hex
    suffix = Path(file.filename or "audio").suffix or ".audio"
    stored_name = f"{audio_id}{suffix}"
    target = UPLOAD_DIR / stored_name

    size = 0
    with target.open("wb") as out:
        while chunk := await file.read(1024 * 1024):
            size += len(chunk)
            out.write(chunk)

    with db() as conn:
        conn.execute(
            """
            INSERT INTO audio_files (id, original_name, stored_name, content_type, size_bytes, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (audio_id, file.filename or stored_name, stored_name, file.content_type, size, utc_now()),
        )
        audit(conn, "upload_audio", "audio_file", audio_id, user["username"], {"file_name": file.filename, "size_bytes": size})

    return {
        "audio_id": audio_id,
        "file_name": file.filename,
        "size_bytes": size,
        "status": "uploaded",
    }


@app.post("/api/audio/{audio_id}/transcribe")
def transcribe_audio(audio_id: str, user: dict[str, str] = Depends(current_user)) -> dict[str, Any]:
    row = get_audio(audio_id)
    audio_path = UPLOAD_DIR / row["stored_name"]
    if not audio_path.exists():
        raise HTTPException(status_code=404, detail="音频文件不存在")

    raw_transcript = transcribe_with_provider(audio_path, row["original_name"])
    transcript = raw_transcript
    with db() as conn:
        audit(conn, "transcribe_audio", "audio_file", audio_id, user["username"], {"ai_standardized": False})

    return {
        "audio_id": audio_id,
        "provider": ASR_PROVIDER,
        "transcript": transcript,
        "raw_transcript": raw_transcript,
        "ai_standardized": False,
    }


@app.post("/api/audio-chunks/transcribe")
async def transcribe_audio_chunk(file: UploadFile = File(...), user: dict[str, str] = Depends(current_user)) -> dict[str, Any]:
    if not file.content_type or not (file.content_type.startswith("audio/") or file.content_type == "application/octet-stream"):
        raise HTTPException(status_code=400, detail="请上传音频片段")

    chunk_id = uuid.uuid4().hex
    suffix = guess_audio_suffix(file.filename, file.content_type)
    target = UPLOAD_DIR / f"chunk-{chunk_id}{suffix}"
    size = 0
    with target.open("wb") as out:
        while chunk := await file.read(1024 * 1024):
            size += len(chunk)
            out.write(chunk)

    if size < REALTIME_MIN_CHUNK_BYTES:
        return {
            "chunk_id": chunk_id,
            "provider": ASR_PROVIDER,
            "size_bytes": size,
            "transcript": "",
            "raw_transcript": "",
            "ai_standardized": False,
            "skipped": True,
            "skip_reason": "音频片段过短，已跳过",
        }

    try:
        raw_transcript = transcribe_with_provider(target, file.filename or target.name, realtime=True)
    except HTTPException as exc:
        detail = str(exc.detail)
        if "Invalid data found when processing input" in detail or "moov atom not found" in detail:
            return {
                "chunk_id": chunk_id,
                "provider": ASR_PROVIDER,
                "size_bytes": size,
                "transcript": "",
                "raw_transcript": "",
                "ai_standardized": False,
                "skipped": True,
                "skip_reason": "音频片段不可解码，已跳过",
            }
        raise
    transcript = raw_transcript
    if is_no_speech_transcript(transcript):
        return {
            "chunk_id": chunk_id,
            "provider": ASR_PROVIDER,
            "size_bytes": size,
            "transcript": "",
            "raw_transcript": raw_transcript,
            "ai_standardized": False,
            "skipped": True,
            "skip_reason": "未识别到有效语音，已跳过",
        }
    with db() as conn:
        audit(conn, "transcribe_audio_chunk", "audio_chunk", chunk_id, user["username"], {"size_bytes": size, "ai_standardized": False})

    return {
        "chunk_id": chunk_id,
        "provider": ASR_PROVIDER,
        "size_bytes": size,
        "transcript": transcript,
        "raw_transcript": raw_transcript,
        "ai_standardized": False,
        "skipped": False,
    }


@app.post("/api/emr/generate")
def generate_emr(payload: GenerateEmrRequest, user: dict[str, str] = Depends(current_user)) -> dict[str, Any]:
    started_at = time.perf_counter()
    transcript = payload.transcript.strip()
    if not transcript:
        raise HTTPException(status_code=400, detail="转写文本不能为空")

    result = generate_emr_with_provider(transcript, payload.patient)
    with db() as conn:
        audit(conn, "generate_emr", "transcript", "inline", user["username"], {"provider": result.get("_provider", LLM_PROVIDER)})
    return {
        "provider": result.get("_provider", LLM_PROVIDER),
        "processing_seconds": round(time.perf_counter() - started_at, 2),
        "emr": {
            "chief_complaint": result["chief_complaint"],
            "history_of_present_illness": result["history_of_present_illness"],
            "past_history": result["past_history"],
            "allergy_history": result["allergy_history"],
            "physical_exam": result["physical_exam"],
            "diagnosis": result["diagnosis"],
            "diagnosis_options": result.get("diagnosis_options", []),
            "plan": result["plan"],
        },
        "diagnosis_options": result.get("diagnosis_options", []),
        "missing_items": result["missing_items"],
        "risk_alerts": result["risk_alerts"],
        "evidence": result["evidence"],
    }


@app.post("/api/emr/confirm")
def confirm_emr(payload: ConfirmEmrRequest, user: dict[str, str] = Depends(current_user)) -> dict[str, str]:
    record_id = uuid.uuid4().hex
    emr = simplify_json_value(payload.emr)
    evidence = simplify_json_value(payload.evidence)
    with db() as conn:
        conn.execute(
            """
            INSERT INTO emr_records (id, patient_json, transcript, raw_transcript, emr_json, evidence_json, confirmed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record_id,
                model_json(payload.patient),
                ensure_simplified_chinese(payload.transcript),
                payload.raw_transcript or payload.transcript,
                json.dumps(emr, ensure_ascii=False),
                json.dumps(evidence, ensure_ascii=False),
                utc_now(),
            ),
        )
        audit(conn, "confirm_emr", "emr_record", record_id, user["username"])

    return {"record_id": record_id, "status": "confirmed"}


@app.get("/api/emr/{record_id}/export.txt", response_class=PlainTextResponse)
def export_emr_txt(record_id: str, user: dict[str, str] = Depends(current_user)) -> str:
    row = get_emr_record(record_id)
    patient = json.loads(row["patient_json"])
    emr = json.loads(row["emr_json"])
    with db() as conn:
        audit(conn, "export_emr_txt", "emr_record", record_id, user["username"])
    return build_emr_text(patient, emr, confirmed=True, raw_transcript=row["raw_transcript"] or row["transcript"])


@app.get("/api/emr/{record_id}/export.json")
def export_emr_json(record_id: str, user: dict[str, str] = Depends(current_user)) -> dict[str, Any]:
    row = get_emr_record(record_id)
    with db() as conn:
        audit(conn, "export_emr_json", "emr_record", record_id, user["username"])
    return {
        "record_id": row["id"],
        "patient": json.loads(row["patient_json"]),
        "transcript": row["transcript"],
        "raw_transcript": row["raw_transcript"] or row["transcript"],
        "emr": json.loads(row["emr_json"]),
        "evidence": json.loads(row["evidence_json"]),
        "confirmed_at": row["confirmed_at"],
    }


@app.get("/api/audit-logs")
def list_audit_logs(limit: int = 100, user: dict[str, str] = Depends(current_user)) -> dict[str, Any]:
    limit = max(1, min(limit, 500))
    with db() as conn:
        rows = conn.execute(
            """
            SELECT id, user_id, action, resource_type, resource_id, detail_json, created_at
            FROM audit_logs
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        audit(conn, "view_audit_logs", "audit_logs", "latest", user["username"], {"limit": limit})
    return {"items": [dict(row) for row in rows]}


@app.post("/api/audio/{audio_id}/transcribe-jobs")
def create_transcription_job(audio_id: str, user: dict[str, str] = Depends(current_user)) -> dict[str, str]:
    row = get_audio(audio_id)
    job_id = uuid.uuid4().hex
    JOB_STORE[job_id] = {
        "job_id": job_id,
        "type": "transcription",
        "status": "queued",
        "audio_id": audio_id,
        "created_by": user["username"],
        "created_at": utc_now(),
        "updated_at": utc_now(),
        "result": None,
        "error": None,
    }
    JOB_EXECUTOR.submit(run_transcription_job, job_id, row["stored_name"], row["original_name"])
    with db() as conn:
        audit(conn, "create_transcription_job", "audio_file", audio_id, user["username"], {"job_id": job_id})
    return {"job_id": job_id, "status": "queued"}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str, user: dict[str, str] = Depends(current_user)) -> dict[str, Any]:
    job = JOB_STORE.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在")
    return job


@app.websocket("/ws/jobs/{job_id}")
async def job_status_ws(websocket: WebSocket, job_id: str) -> None:
    await websocket.accept()
    try:
        while True:
            job = JOB_STORE.get(job_id)
            if not job:
                await websocket.send_json({"status": "not_found"})
                return
            await websocket.send_json(job)
            if job["status"] in {"succeeded", "failed"}:
                return
            await asyncio.sleep(1)
    except WebSocketDisconnect:
        return


@app.websocket("/ws/audio/transcribe")
async def realtime_transcribe_ws(websocket: WebSocket) -> None:
    await websocket.accept()
    try:
        while True:
            audio_bytes = await websocket.receive_bytes()
            chunk_id = uuid.uuid4().hex
            target = UPLOAD_DIR / f"ws-{chunk_id}.webm"
            target.write_bytes(audio_bytes)
            raw_transcript = transcribe_with_provider(target, target.name, realtime=True)
            transcript = raw_transcript
            await websocket.send_json({
                "chunk_id": chunk_id,
                "transcript": transcript,
                "raw_transcript": raw_transcript,
                "ai_standardized": False,
                "provider": ASR_PROVIDER,
            })
    except WebSocketDisconnect:
        return


@app.websocket("/ws/asr/stream")
async def ws_asr_stream(websocket: WebSocket) -> None:
    await websocket.accept()
    if STREAM_ASR_PROVIDER in {"sherpa", "sherpa_onnx", "sherpa-onnx"}:
        await ws_sherpa_stream(websocket)
        return
    if STREAM_ASR_PROVIDER != "vosk":
        await websocket.send_json({"type": "error", "message": f"STREAM_ASR_PROVIDER={STREAM_ASR_PROVIDER} 暂不支持"})
        await websocket.close()
        return

    try:
        from vosk import KaldiRecognizer
    except ImportError:
        await websocket.send_json({"type": "error", "message": "未安装 vosk"})
        await websocket.close()
        return

    try:
        recognizer = KaldiRecognizer(get_vosk_model(), VOSK_SAMPLE_RATE)
        recognizer.SetWords(False)
        last_partial = ""
        while True:
            message = await websocket.receive()
            if message.get("bytes") is not None:
                audio_bytes = message["bytes"]
                if recognizer.AcceptWaveform(audio_bytes):
                    payload = json.loads(recognizer.Result() or "{}")
                    text = clean_streaming_asr_text(payload.get("text", ""))
                    if text:
                        last_partial = ""
                        await websocket.send_json({"type": "final", "text": text})
                else:
                    payload = json.loads(recognizer.PartialResult() or "{}")
                    partial = clean_streaming_asr_text(payload.get("partial", ""))
                    if partial and partial != last_partial:
                        last_partial = partial
                        await websocket.send_json({"type": "partial", "text": partial})
            elif message.get("text") == "__stop__":
                payload = json.loads(recognizer.FinalResult() or "{}")
                text = clean_streaming_asr_text(payload.get("text", ""))
                if text:
                    await websocket.send_json({"type": "final", "text": text})
                await websocket.send_json({"type": "done"})
                return
    except WebSocketDisconnect:
        return
    except Exception as exc:
        await websocket.send_json({"type": "error", "message": str(exc)})


async def ws_sherpa_stream(websocket: WebSocket) -> None:
    try:
        import numpy as np
    except ImportError:
        await websocket.send_json({"type": "error", "message": "未安装 numpy"})
        await websocket.close()
        return

    try:
        recognizer = get_sherpa_recognizer()
        stream = recognizer.create_stream()
        last_partial = ""
        while True:
            message = await websocket.receive()
            if message.get("bytes") is not None:
                audio_bytes = message["bytes"]
                if len(audio_bytes) < 2:
                    continue
                if len(audio_bytes) % 2:
                    audio_bytes = audio_bytes[:-1]
                samples = np.frombuffer(audio_bytes, dtype=np.int16).astype(np.float32) / 32768.0
                stream.accept_waveform(SHERPA_SAMPLE_RATE, samples)
                while recognizer.is_ready(stream):
                    recognizer.decode_stream(stream)
                partial = clean_streaming_asr_text(recognizer.get_result(stream))
                if partial and partial != last_partial:
                    last_partial = partial
                    await websocket.send_json({"type": "partial", "text": partial})
            elif message.get("text") == "__stop__":
                stream.input_finished()
                while recognizer.is_ready(stream):
                    recognizer.decode_stream(stream)
                text = clean_streaming_asr_text(recognizer.get_result(stream))
                if text:
                    await websocket.send_json({"type": "final", "text": text})
                await websocket.send_json({"type": "done"})
                return
    except WebSocketDisconnect:
        return
    except Exception as exc:
        await websocket.send_json({"type": "error", "message": str(exc)})


@app.post("/api/files/cleanup")
def cleanup_files(max_age_hours: int = 24, user: dict[str, str] = Depends(current_user)) -> dict[str, Any]:
    cutoff = time.time() - max_age_hours * 3600
    removed = []
    for path in UPLOAD_DIR.glob("chunk-*"):
        if path.is_file() and path.stat().st_mtime < cutoff:
            removed.append(path.name)
            path.unlink()
    for path in UPLOAD_DIR.glob("ws-*"):
        if path.is_file() and path.stat().st_mtime < cutoff:
            removed.append(path.name)
            path.unlink()
    with db() as conn:
        audit(conn, "cleanup_files", "uploads", "chunks", user["username"], {"removed_count": len(removed), "max_age_hours": max_age_hours})
    return {"removed_count": len(removed), "removed_files": removed[:50]}


@app.post("/api/integrations/emr/sync")
def sync_emr(payload: EmrSyncRequest, user: dict[str, str] = Depends(current_user)) -> dict[str, Any]:
    row = get_emr_record(payload.record_id)
    patient = json.loads(row["patient_json"])
    emr = json.loads(row["emr_json"])
    request_payload = {
        "record_id": payload.record_id,
        "target_system": payload.target_system,
        "patient": patient,
        "emr": emr,
    }
    response_payload = {
        "status": "pending_external_integration",
        "message": "HIS/EMR 对接骨架已创建，需配置医院接口后启用真实同步。",
    }
    with db() as conn:
        conn.execute(
            """
            INSERT INTO integration_logs (record_id, target_system, status, request_json, response_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                payload.record_id,
                payload.target_system,
                "pending",
                json.dumps(request_payload, ensure_ascii=False),
                json.dumps(response_payload, ensure_ascii=False),
                utc_now(),
            ),
        )
        audit(conn, "sync_emr_stub", "emr_record", payload.record_id, user["username"], {"target_system": payload.target_system})
    return response_payload


def run_transcription_job(job_id: str, stored_name: str, original_name: str) -> None:
    job = JOB_STORE[job_id]
    job["status"] = "running"
    job["updated_at"] = utc_now()
    try:
        audio_path = UPLOAD_DIR / stored_name
        raw_transcript = transcribe_with_provider(audio_path, original_name)
        transcript = raw_transcript
        job["status"] = "succeeded"
        job["result"] = {
            "transcript": transcript,
            "raw_transcript": raw_transcript,
            "ai_standardized": False,
            "provider": ASR_PROVIDER,
        }
        job["updated_at"] = utc_now()
    except Exception as exc:
        job["status"] = "failed"
        job["error"] = str(exc)
        job["updated_at"] = utc_now()


def get_audio(audio_id: str) -> sqlite3.Row:
    with db() as conn:
        row = conn.execute("SELECT * FROM audio_files WHERE id = ?", (audio_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="音频记录不存在")
    return row


def get_emr_record(record_id: str) -> sqlite3.Row:
    with db() as conn:
        row = conn.execute("SELECT * FROM emr_records WHERE id = ?", (record_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="病历记录不存在")
    return row


def audit(
    conn: sqlite3.Connection,
    action: str,
    resource_type: str,
    resource_id: str,
    user_id: Optional[str] = None,
    detail: Optional[dict[str, Any]] = None,
) -> None:
    conn.execute(
        """
        INSERT INTO audit_logs (user_id, action, resource_type, resource_id, detail_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (user_id, action, resource_type, resource_id, json.dumps(detail or {}, ensure_ascii=False), utc_now()),
    )


def guess_audio_suffix(filename: Optional[str], content_type: Optional[str]) -> str:
    if filename:
        suffix = Path(filename).suffix
        if suffix:
            return suffix
    mapping = {
        "audio/webm": ".webm",
        "audio/ogg": ".ogg",
        "audio/mp4": ".m4a",
        "audio/mpeg": ".mp3",
        "audio/wav": ".wav",
        "audio/x-wav": ".wav",
    }
    return mapping.get(content_type or "", ".webm")


def transcribe_with_provider(audio_path: Path, original_name: str, realtime: bool = False) -> str:
    if ASR_PROVIDER == "mock":
        return build_mock_transcript(original_name)
    if ASR_PROVIDER in {"whisper", "faster-whisper", "faster_whisper"}:
        return transcribe_with_faster_whisper(audio_path, realtime=realtime)

    raise HTTPException(
        status_code=501,
        detail=f"ASR_PROVIDER={ASR_PROVIDER} 尚未配置。请在后端实现 transcribe_with_provider。",
    )


def standardize_asr_transcript(transcript: str) -> str:
    cleaned = normalize_transcript_text(clean_transcript_artifacts(transcript))
    if not cleaned or "未识别到有效语音" in cleaned:
        return ensure_simplified_chinese(cleaned or transcript)
    if not ASR_AI_STANDARDIZE or LLM_PROVIDER != "deepseek" or not DEEPSEEK_API_KEY:
        return ensure_simplified_chinese(cleaned)
    try:
        future = ASR_STANDARDIZER_EXECUTOR.submit(
            standardize_transcript_with_deepseek,
            cleaned,
            ASR_AI_STANDARDIZE_TIMEOUT_SECONDS,
            ASR_AI_STANDARDIZE_MAX_CHARS,
        )
        return ensure_simplified_chinese(future.result(timeout=ASR_AI_STANDARDIZE_TIMEOUT_SECONDS))
    except FutureTimeoutError:
        return ensure_simplified_chinese(cleaned)
    except Exception:
        return ensure_simplified_chinese(cleaned)


def get_whisper_model():
    global WHISPER_MODEL
    if WHISPER_MODEL is not None:
        return WHISPER_MODEL

    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise HTTPException(
            status_code=500,
            detail="未安装 faster-whisper，请执行 pip install -r requirements.txt",
        ) from exc

    try:
        WHISPER_MODEL = WhisperModel(
            WHISPER_MODEL_SIZE,
            device=WHISPER_DEVICE,
            compute_type=WHISPER_COMPUTE_TYPE,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Whisper 模型加载失败：{exc}") from exc

    return WHISPER_MODEL


def get_vosk_model():
    global VOSK_MODEL
    if VOSK_MODEL is not None:
        return VOSK_MODEL
    if not VOSK_MODEL_PATH.exists():
        raise HTTPException(status_code=500, detail=f"Vosk 模型不存在：{VOSK_MODEL_PATH}")
    try:
        from vosk import Model
    except ImportError as exc:
        raise HTTPException(status_code=500, detail="未安装 vosk，请执行 pip install -r requirements.txt") from exc
    VOSK_MODEL = Model(str(VOSK_MODEL_PATH))
    return VOSK_MODEL


def get_sherpa_recognizer():
    global SHERPA_RECOGNIZER
    if SHERPA_RECOGNIZER is not None:
        return SHERPA_RECOGNIZER
    required_files = {
        "tokens": SHERPA_MODEL_PATH / "tokens.txt",
        "encoder": SHERPA_MODEL_PATH / "encoder.int8.onnx",
        "decoder": SHERPA_MODEL_PATH / "decoder.int8.onnx",
    }
    missing = [name for name, path in required_files.items() if not path.exists()]
    if missing:
        raise HTTPException(status_code=500, detail=f"sherpa-onnx 模型文件缺失：{', '.join(missing)}")
    try:
        import sherpa_onnx
    except ImportError as exc:
        raise HTTPException(status_code=500, detail="未安装 sherpa-onnx，请执行 pip install -r requirements.txt") from exc

    SHERPA_RECOGNIZER = sherpa_onnx.OnlineRecognizer.from_paraformer(
        tokens=str(required_files["tokens"]),
        encoder=str(required_files["encoder"]),
        decoder=str(required_files["decoder"]),
        num_threads=SHERPA_NUM_THREADS,
        sample_rate=SHERPA_SAMPLE_RATE,
        feature_dim=80,
        decoding_method="greedy_search",
        provider="cpu",
    )
    return SHERPA_RECOGNIZER


def clean_streaming_asr_text(text: str) -> str:
    cleaned = normalize_transcript_text(clean_transcript_artifacts(text or ""))
    cleaned = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", cleaned)
    if is_bad_transcript_fragment(cleaned) or is_no_speech_transcript(cleaned):
        return ""
    return cleaned


def transcribe_with_faster_whisper(audio_path: Path, realtime: bool = False) -> str:
    model = get_whisper_model()
    try:
        lines = run_whisper_pass(model, audio_path, vad_filter=True)
        transcript = normalize_transcript_text(clean_transcript_artifacts("\n".join(lines))).strip()
        if (not realtime) and WHISPER_RETRY_ON_SHORT and should_retry_transcription(transcript):
            fallback_lines = run_whisper_pass(model, audio_path, vad_filter=False)
            fallback_transcript = normalize_transcript_text(clean_transcript_artifacts("\n".join(fallback_lines))).strip()
            if len(fallback_transcript) > len(transcript):
                transcript = fallback_transcript
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"音频转写失败：{exc}") from exc

    if not transcript:
        transcript = "未识别到有效语音，请确认音频文件包含清晰人声。"

    return transcript


def run_whisper_pass(model: Any, audio_path: Path, vad_filter: bool) -> list[str]:
    kwargs = {
        "language": WHISPER_LANGUAGE or None,
        "beam_size": WHISPER_BEAM_SIZE,
        "temperature": WHISPER_TEMPERATURE,
        "vad_filter": vad_filter,
        "initial_prompt": medical_asr_prompt(),
        "condition_on_previous_text": False,
        "no_speech_threshold": WHISPER_NO_SPEECH_THRESHOLD,
        "compression_ratio_threshold": 2.4,
        "log_prob_threshold": -1.2,
    }
    if vad_filter:
        kwargs["vad_parameters"] = {
            "min_silence_duration_ms": WHISPER_VAD_MIN_SILENCE_MS,
            "speech_pad_ms": WHISPER_VAD_SPEECH_PAD_MS,
        }

    segments, info = model.transcribe(str(audio_path), **kwargs)
    lines = []
    for segment in segments:
        no_speech_prob = float(getattr(segment, "no_speech_prob", 0) or 0)
        avg_logprob = float(getattr(segment, "avg_logprob", 0) or 0)
        if no_speech_prob >= WHISPER_NO_SPEECH_THRESHOLD and avg_logprob < -0.2:
            continue
        text = normalize_transcript_text(segment.text.strip())
        if text and not is_bad_transcript_fragment(text):
            lines.append(text)
    return lines


def should_retry_transcription(transcript: str) -> bool:
    if not transcript or "未识别到有效语音" in transcript:
        return True
    compact = re.sub(r"\s+", "", transcript)
    return len(compact) < WHISPER_FALLBACK_MIN_CHARS


def is_no_speech_transcript(transcript: str) -> bool:
    compact = re.sub(r"\s+", "", transcript or "")
    return not compact or "未识别到有效语音" in compact


def medical_asr_prompt() -> str:
    return (
        "以下是医生和患者的中文门诊问诊对话。"
        "请准确识别医学术语、药物、检查和否定表达。"
        "常见词包括：咳嗽、咳痰、黄痰、发热、低烧、高热、体温、咽痛、鼻塞、流涕、胸痛、胸闷、呼吸困难、气短、腹痛、腹泻、恶心、呕吐、头痛、乏力；"
        "高血压、糖尿病、冠心病、哮喘、慢阻肺、肺炎、支气管炎、胃炎；"
        "青霉素、头孢、阿莫西林、布洛芬、对乙酰氨基酚、奥司他韦、阿奇霉素；"
        "血常规、CRP、C反应蛋白、支原体、流感、抗原、CT、胸片、心电图。"
        "否定表达包括：没有、无、否认、不痛、不喘、不过敏。"
    )


def normalize_transcript_text(text: str) -> str:
    replacements = {
        "布洛芬": "布洛芬",
        "布洛分": "布洛芬",
        "不洛芬": "布洛芬",
        "对乙先氨基酚": "对乙酰氨基酚",
        "对乙酰安基酚": "对乙酰氨基酚",
        "阿莫西林": "阿莫西林",
        "阿莫西灵": "阿莫西林",
        "青霉数": "青霉素",
        "青梅素": "青霉素",
        "头胞": "头孢",
        "克咳": "咳嗽",
        "咳数": "咳嗽",
        "黄谈": "黄痰",
        "胸疼": "胸痛",
        "呼吸困难": "呼吸困难",
        "C反映蛋白": "C反应蛋白",
        "C反应单白": "C反应蛋白",
        "支原体": "支原体",
        "只原体": "支原体",
        "心电图": "心电图",
        "心点图": "心电图",
        "肚屙": "腹泻",
        "肚泻": "腹泻",
        "拉肚": "腹泻",
        "肚痛": "腹痛",
        "肚子痛": "腹痛",
        "心口痛": "胸痛",
        "心口闷": "胸闷",
        "喉咙痛": "咽痛",
        "嗓子痛": "咽痛",
        "鼻水": "流涕",
        "流鼻水": "流涕",
        "喘不过气": "呼吸困难",
        "喘唔过气": "呼吸困难",
        "透不过气": "呼吸困难",
        "冇": "没有",
        "唔": "不",
        "无": "没有",
        "１": "，",
    }
    normalized = re.sub(r"\s+", " ", text).strip()
    normalized = clean_transcript_artifacts(normalized)
    for wrong, right in replacements.items():
        normalized = normalized.replace(wrong, right)
    normalized = normalize_dizziness_context(normalized)
    normalized = compress_repeated_phrases(normalized)
    return ensure_simplified_chinese(normalized)


def ensure_simplified_chinese(text: str) -> str:
    if not text:
        return text
    phrase_map = {
        "醫生": "医生",
        "患者": "患者",
        "門診": "门诊",
        "問診": "问诊",
        "病歷": "病历",
        "轉寫": "转写",
        "頭暈": "头晕",
        "頭痛": "头痛",
        "眩暈": "眩晕",
        "陣發性": "阵发性",
        "持續性": "持续性",
        "胸悶": "胸闷",
        "胸痛": "胸痛",
        "心悸": "心悸",
        "噁心": "恶心",
        "嘔吐": "呕吐",
        "過敏": "过敏",
        "青黴素": "青霉素",
        "頭孢": "头孢",
        "檢查": "检查",
        "診斷": "诊断",
        "治療": "治疗",
        "體溫": "体温",
        "體征": "体征",
        "既往史": "既往史",
        "過敏史": "过敏史",
        "現病史": "现病史",
        "輔助檢查": "辅助检查",
        "神經系統": "神经系统",
        "腦血管": "脑血管",
        "頸部": "颈部",
        "頸源性": "颈源性",
        "電解質": "电解质",
        "血常規": "血常规",
        "心電圖": "心电图",
        "語言": "语言",
        "意識": "意识",
        "發作": "发作",
        "發熱": "发热",
        "發燒": "发烧",
        "咳嗽": "咳嗽",
        "咳痰": "咳痰",
        "氣短": "气短",
        "呼吸困難": "呼吸困难",
        "無": "无",
        "沒有": "没有",
        "否認": "否认",
        "未提及": "未提及",
    }
    char_map = str.maketrans({
        "醫": "医", "門": "门", "問": "问", "診": "诊", "歷": "历", "轉": "转",
        "頭": "头", "暈": "晕", "陣": "阵", "發": "发", "持": "持", "續": "续",
        "性": "性", "胸": "胸", "悶": "闷", "噁": "恶", "嘔": "呕", "過": "过",
        "黴": "霉", "檢": "检", "查": "查", "斷": "断", "療": "疗", "體": "体",
        "溫": "温", "輔": "辅", "助": "助", "神": "神", "經": "经", "系": "系",
        "統": "统", "腦": "脑", "頸": "颈", "電": "电", "質": "质", "常": "常",
        "規": "规", "圖": "图", "語": "语", "識": "识", "燒": "烧", "氣": "气",
        "難": "难", "無": "无", "認": "认", "聽": "听", "視": "视", "覺": "觉",
        "藥": "药", "劑": "剂", "處": "处", "置": "置", "記": "记", "錄": "录",
        "風": "风", "險": "险", "隨": "随", "訪": "访", "複": "复",
    })
    simplified = text
    for traditional, simplified_phrase in phrase_map.items():
        simplified = simplified.replace(traditional, simplified_phrase)
    return simplified.translate(char_map)


def simplify_json_value(value: Any) -> Any:
    if isinstance(value, str):
        return ensure_simplified_chinese(value)
    if isinstance(value, list):
        return [simplify_json_value(item) for item in value]
    if isinstance(value, dict):
        return {key: simplify_json_value(item) for key, item in value.items()}
    return value


def clean_transcript_artifacts(text: str) -> str:
    cleaned = text.replace("\ufffd", "")
    cleaned = re.sub(r"[］\]\)）】》>]{3,}", "", cleaned)
    cleaned = re.sub(r"[［\[\(（【《<]{3,}", "", cleaned)
    cleaned = re.sub(r"([，。！？；：、,.!?;:])\1{2,}", r"\1", cleaned)
    cleaned = re.sub(r"([^\w\s\u4e00-\u9fff])\1{4,}", "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.strip()


def normalize_dizziness_context(text: str) -> str:
    if "头晕" not in text and "晕" not in text:
        return text
    text = text.replace("一阵一阵的家住", "一阵一阵的")
    text = text.replace("一阵的家住", "一阵一阵的")
    text = text.replace("阵的家住", "一阵一阵的")
    text = text.replace("一阵一阵的间断", "一阵一阵的")
    text = text.replace("一阵的间断", "一阵一阵的")
    text = text.replace("持续性的晕", "持续性头晕")
    text = text.replace("一阵一阵的晕", "阵发性头晕")
    return text


def compress_repeated_phrases(text: str) -> str:
    text = compress_repeated_substrings(text)
    separators = "，。！？；、,.!?;:\n"
    parts = re.split(r"([，。！？；、,.!?;:\n])", text)
    rebuilt = []
    previous_content = ""
    repeat_count = 0
    for index in range(0, len(parts), 2):
        content = parts[index].strip()
        sep = parts[index + 1] if index + 1 < len(parts) else ""
        if not content:
            continue
        compact = re.sub(r"\s+", "", content)
        if compact and is_similar_short_phrase(compact, previous_content):
            repeat_count += 1
            if repeat_count >= 1:
                continue
        else:
            repeat_count = 0
            previous_content = compact
        rebuilt.append(content + sep)

    cleaned = "".join(rebuilt).strip()
    cleaned = compress_repeated_substrings(cleaned)
    cleaned = re.sub(r"(.{4,16})(?:\1){2,}", r"\1", cleaned)
    cleaned = re.sub(r"(是一阵一阵的|是一阵的|阵发性头晕|一阵一阵的)(?:[，, ]*\1){1,}", r"\1", cleaned)
    return cleaned.rstrip("，,；;、 ")


def compress_repeated_substrings(text: str) -> str:
    cleaned = text
    # 连续无标点重复，如“试试试试试试”或“尤其是尤其是尤其是”。
    for size in range(2, 11):
        pattern = re.compile(rf"([\u4e00-\u9fff]{{{size}}})(?:\1){{2,}}")
        cleaned = pattern.sub(r"\1", cleaned)

    # 带空格或轻微标点的重复，如“持续什么样 持续什么样 持续什么样”。
    for size in range(2, 13):
        pattern = re.compile(rf"([\u4e00-\u9fff]{{{size}}})(?:[\s，,、。；;：:]+\1){{1,}}")
        cleaned = pattern.sub(r"\1", cleaned)

    # 对特别常见的问诊短句再做定向压缩。
    common_phrases = [
        "持续什么样",
        "还是持续什么样",
        "一阵一阵的",
        "阵发性头晕",
        "尤其是",
        "试试",
    ]
    for phrase in common_phrases:
        escaped = re.escape(phrase)
        cleaned = re.sub(rf"({escaped})(?:[\s，,、。；;：:]*\1)+", r"\1", cleaned)
    return cleaned


def is_similar_short_phrase(current: str, previous: str) -> bool:
    if not current or not previous:
        return False
    if current == previous:
        return True
    if len(current) <= 12 and len(previous) <= 12 and (current in previous or previous in current):
        return True
    return False


def is_bad_transcript_fragment(text: str) -> bool:
    if not text:
        return True
    compact = re.sub(r"\s+", " ", text).strip().lower()
    hallucination_phrases = [
        "thank you for watching",
        "thanks for watching",
        "thank you",
        "字幕",
        "字幕组",
        "subscribe",
        "like and subscribe",
        "本视频",
        "下期再见",
    ]
    if any(phrase in compact for phrase in hallucination_phrases):
        return True
    has_cjk = bool(re.search(r"[\u4e00-\u9fff]", text))
    if not has_cjk and re.fullmatch(r"[a-zA-Z\s.,!?'-]{3,80}", text):
        return True
    if len(text) > 20:
        non_text = re.sub(r"[\w\s\u4e00-\u9fff，。！？；：、,.!?;:]", "", text)
        if len(non_text) / max(len(text), 1) > 0.35:
            return True
    if re.search(r"([］\]\)）】》>])\1{8,}", text):
        return True
    if text.count("\ufffd") >= 2:
        return True
    return False


def build_mock_transcript(file_name: str) -> str:
    return f"""系统：已接收录音文件 {file_name}。
医生：您这次主要哪里不舒服？
患者：我咳嗽三天了，还有点发烧。
医生：最高体温多少？
患者：昨天晚上量到三十八度五。
医生：有没有咳痰、胸痛或者喘不上气？
患者：有一点黄痰，没有胸痛，也没有呼吸困难。
医生：以前有高血压、糖尿病吗？
患者：没有高血压，也没有糖尿病。
医生：有没有药物过敏？
患者：我对青霉素过敏。
医生：这几天自己吃过什么药吗？
患者：吃过一次布洛芬，退烧效果还可以。
系统：当前为 mock 转写。真实部署时请接入医院 ASR、Whisper 或云 ASR。"""


def generate_emr_with_provider(transcript: str, patient: Optional[PatientInfo]) -> dict[str, Any]:
    if LLM_PROVIDER == "openai":
        try:
            return generate_emr_with_openai(transcript, patient)
        except Exception as exc:
            result = extract_clinical_fields(transcript)
            result["_provider"] = "rules_fallback"
            result["risk_alerts"].append(f"大模型生成失败，已回退规则版：{exc}")
            return result
    if LLM_PROVIDER == "deepseek":
        try:
            return generate_emr_with_deepseek(transcript, patient)
        except Exception as exc:
            result = extract_clinical_fields(transcript)
            result["_provider"] = "rules_fallback"
            result["risk_alerts"].append(f"DeepSeek 生成失败，已回退规则版：{exc}")
            return result

    result = extract_clinical_fields(transcript)
    result["_provider"] = "rules"
    return result


def generate_emr_with_openai(transcript: str, patient: Optional[PatientInfo]) -> dict[str, Any]:
    if not os.getenv("OPENAI_API_KEY"):
        raise RuntimeError("未配置 OPENAI_API_KEY")

    try:
        from openai import OpenAI
    except ImportError as exc:
        raise RuntimeError("未安装 openai，请执行 pip install -r requirements.txt") from exc

    patient_info = patient.dict() if patient and hasattr(patient, "dict") else {}
    schema = emr_output_schema()
    prompt = f"""请分析下面的门诊问诊转写，生成门诊通用病历结构化 JSON。

患者信息：
{json.dumps(patient_info, ensure_ascii=False)}

问诊转写：
{transcript}
"""
    client = OpenAI()
    response = client.responses.create(
        model=OPENAI_MODEL,
        instructions=(
            "你是严谨的中文门诊病历结构化助手。"
            "只能使用问诊转写中明确出现的信息。"
            "未提及的信息必须写“未提及”，不得写成“无”。"
            "否认信息必须来自明确否定表达。"
            "诊断只能写为初步考虑或待医生确认。"
            "每个关键字段应尽量给出 evidence 原文依据。"
            "输出必须是符合 JSON Schema 的 JSON。"
        ),
        input=prompt,
        text={
            "format": {
                "type": "json_schema",
                "name": "outpatient_emr",
                "schema": schema,
                "strict": True,
            }
        },
    )

    raw = getattr(response, "output_text", "")
    if not raw:
        raise RuntimeError("OpenAI 返回为空")

    parsed = json.loads(raw)
    result = {
        "chief_complaint": parsed["emr"]["chief_complaint"],
        "history_of_present_illness": parsed["emr"]["history_of_present_illness"],
        "past_history": parsed["emr"]["past_history"],
        "allergy_history": parsed["emr"]["allergy_history"],
        "physical_exam": parsed["emr"]["physical_exam"],
        "diagnosis": parsed["emr"]["diagnosis"],
        "plan": parsed["emr"]["plan"],
        "missing_items": parsed["missing_items"],
        "risk_alerts": parsed["risk_alerts"],
        "evidence": parsed["evidence"],
        "_provider": "openai",
    }
    return result


def generate_emr_with_deepseek(transcript: str, patient: Optional[PatientInfo]) -> dict[str, Any]:
    if not DEEPSEEK_API_KEY:
        raise RuntimeError("未配置 DEEPSEEK_API_KEY")

    try:
        from openai import OpenAI
    except ImportError as exc:
        raise RuntimeError("未安装 openai，请执行 pip install -r requirements.txt") from exc

    patient_info = patient.dict() if patient and hasattr(patient, "dict") else {}
    transcript_for_llm = compact_transcript_for_llm(transcript)
    system_prompt = (
        "你是资深中文门诊病历书写助手，熟悉门诊通用病历规范。"
        "你需要在一次回复中完成：转写纠错、医学语义理解、结构化病历草稿生成。"
        "必须使用简体中文输出，禁止使用繁体中文。"
        "必须输出合法 JSON，不要输出 Markdown，不要输出解释。"
        "只能依据问诊转写和患者信息，不能编造检查结果、生命体征、既往史、过敏史或诊断。"
        "不得把未提及的既往史、基础疾病、用药史、检查结果当作已知事实。"
        "未提及的信息写“未提及”；明确否认的信息才可写“否认/无”。"
        "病历语言必须专业、完整、客观，使用医学术语，如：阵发性/持续性、诱因、伴随症状、缓解因素、加重因素、诊疗建议。"
        "诊断必须写为“初步考虑/待排/待医生确认”，不得给出确定诊断。"
        "可能疾病和鉴别诊断必须由模型根据问诊内容综合判断，并输出 diagnosis_options，不要依赖固定规则或单一关键词。"
        "diagnosis_options 的 basis 必须说明来自问诊原文的依据；如果依据不足，必须写“依据不足，需进一步询问/查体/检查”，不能编造。"
        "处理意见应包含建议完善问诊、体格检查、必要辅助检查、健康宣教和随访/复诊提示。"
    )
    user_prompt = f"""请根据下面的门诊问诊转写生成更专业、更详细的门诊通用病历 JSON。

输出要求：
1. 先在内部完成方言、口音、同音错词、重复句的纠正；不要单独输出纠错说明。
2. 主诉：用“主要症状 + 持续时间”的医学格式，例：阵发性头晕数月。
3. 现病史：至少按“起病时间、症状特点、发作频率/持续时间、诱因、伴随症状、否认症状、就诊前处理”书写；未问到的项目写“未提及”。
4. 既往史、过敏史、体格检查：只写转写中明确提到的内容，未提及则写“未提及”。
5. 初步诊断：使用“初步考虑：……？待医生结合查体及辅助检查确认”的表达。
6. diagnosis_options：列出 3-6 个可能疾病或鉴别诊断选项，每个选项包含 name、basis、suggested_checks。不要写确定诊断，必须写“待排/可能/需鉴别/初步考虑”语气。
   - basis 只能引用问诊转写明确出现的信息，严禁写“有高血压/糖尿病/冠心病/检查异常”等原文未提及事实。
   - suggested_checks 可以提出下一步检查建议，但不能说检查已经完成。
7. 处理意见：专业且可执行，但不得替代医生最终处方；包含查体、鉴别诊断所需检查、风险告知、复诊/急诊提示。
8. missing_items：列出为了形成完整病历仍缺少的问诊/查体/检查信息。
9. risk_alerts：列出需要医生注意的危险信号或鉴别诊断方向。
10. evidence：给出 3-6 条关键原文依据。

JSON 格式必须如下：
{json.dumps(emr_json_example(), ensure_ascii=False, indent=2)}

患者信息：
{json.dumps(patient_info, ensure_ascii=False)}

问诊转写：
{transcript_for_llm}
"""
    client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url=DEEPSEEK_BASE_URL, timeout=DEEPSEEK_TIMEOUT_SECONDS)
    completion = client.chat.completions.create(
        model=DEEPSEEK_MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        response_format={"type": "json_object"},
        temperature=0.1,
        max_tokens=1800,
    )
    content = completion.choices[0].message.content if completion.choices else ""
    if not content:
        raise RuntimeError("DeepSeek 返回为空")

    parsed = json.loads(content)
    result = normalize_emr_result(parsed, "deepseek")
    if transcript_for_llm.strip() != transcript.strip():
        result["evidence"].insert(
            0,
            {
                "label": "输入转写摘要",
                "text": first_meaningful_line(transcript_for_llm),
            },
        )
    return result


def compact_transcript_for_llm(transcript: str) -> str:
    cleaned = clean_transcript_artifacts(transcript)
    cleaned = compress_repeated_phrases(cleaned)
    cleaned = normalize_transcript_text(cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    if len(cleaned) <= EMR_MAX_TRANSCRIPT_CHARS:
        return cleaned
    head = cleaned[: int(EMR_MAX_TRANSCRIPT_CHARS * 0.65)]
    tail = cleaned[-int(EMR_MAX_TRANSCRIPT_CHARS * 0.35) :]
    return f"{head}\n\n……中间过长内容已省略，以下为末段……\n\n{tail}"


def standardize_transcript_with_deepseek(
    transcript: str,
    timeout_seconds: Optional[float] = None,
    max_chars: Optional[int] = None,
) -> str:
    if not transcript.strip():
        return transcript

    try:
        from openai import OpenAI
    except ImportError as exc:
        raise RuntimeError("未安装 openai，请执行 pip install -r requirements.txt") from exc

    source = transcript.strip()
    if max_chars and len(source) > max_chars:
        source = source[:max_chars]

    client = OpenAI(
        api_key=DEEPSEEK_API_KEY,
        base_url=DEEPSEEK_BASE_URL,
        timeout=timeout_seconds or DEEPSEEK_TIMEOUT_SECONDS,
    )
    completion = client.chat.completions.create(
        model=DEEPSEEK_MODEL,
        messages=[
            {
                "role": "system",
                "content": (
                    "你是中文医疗问诊转写标准化助手。"
                    "必须使用简体中文输出，禁止使用繁体中文。"
                    "请把方言、口音、同音错词、错别字、口语表达整理为标准医学普通话。"
                    "重点修正常见同音字、近音字和医学词错字，例如头晕、眩晕、阵发性、持续性、胸闷、心悸、青霉素、头孢等。"
                    "请修正明显重复片段和无意义噪声。"
                    "保留医生/患者角色；如果原文没有角色，可根据语义补为医生或患者。"
                    "不得增加原文没有的信息。"
                    "否定信息必须保留，例如没有胸痛、否认过敏。"
                    "只输出整理后的简体中文问诊文本，不要输出解释。"
                ),
            },
            {
                "role": "user",
                "content": f"请标准化以下问诊转写：\n{source}",
            },
        ],
        temperature=0.1,
        max_tokens=600,
    )
    content = completion.choices[0].message.content if completion.choices else ""
    return content.strip() or transcript


def normalize_emr_result(parsed: dict[str, Any], provider: str) -> dict[str, Any]:
    emr = parsed.get("emr") or {}
    evidence = parsed.get("evidence") or []
    missing_items = parsed.get("missing_items") or []
    risk_alerts = parsed.get("risk_alerts") or []
    diagnosis_options = parsed.get("diagnosis_options") or emr.get("diagnosis_options") or []

    result = {
        "chief_complaint": ensure_simplified_chinese(str(emr.get("chief_complaint") or "未提及")),
        "history_of_present_illness": ensure_simplified_chinese(str(emr.get("history_of_present_illness") or "未提及")),
        "past_history": ensure_simplified_chinese(str(emr.get("past_history") or "未提及")),
        "allergy_history": ensure_simplified_chinese(str(emr.get("allergy_history") or "未提及")),
        "physical_exam": ensure_simplified_chinese(str(emr.get("physical_exam") or "未提及")),
        "diagnosis": ensure_simplified_chinese(str(emr.get("diagnosis") or "待医生确认")),
        "diagnosis_options": normalize_diagnosis_options(diagnosis_options),
        "plan": ensure_simplified_chinese(str(emr.get("plan") or "未提及")),
        "missing_items": [ensure_simplified_chinese(str(item)) for item in missing_items if item],
        "risk_alerts": [ensure_simplified_chinese(str(item)) for item in risk_alerts if item],
        "evidence": [
            {
                "label": ensure_simplified_chinese(str(item.get("label", ""))),
                "text": ensure_simplified_chinese(str(item.get("text", ""))),
            }
            for item in evidence
            if isinstance(item, dict) and item.get("text")
        ],
        "_provider": provider,
    }
    return result


def normalize_diagnosis_options(options: Any) -> list[dict[str, str]]:
    normalized: list[dict[str, str]] = []
    if not isinstance(options, list):
        return normalized
    for item in options[:8]:
        if isinstance(item, str):
            name = item
            basis = ""
            suggested_checks = ""
        elif isinstance(item, dict):
            name = str(item.get("name") or item.get("disease") or item.get("diagnosis") or "")
            basis = str(item.get("basis") or item.get("reason") or item.get("evidence") or "")
            suggested_checks = str(item.get("suggested_checks") or item.get("checks") or item.get("recommendation") or "")
        else:
            continue
        name = ensure_simplified_chinese(name.strip())
        if not name:
            continue
        normalized.append(
            {
                "name": name,
                "basis": ensure_simplified_chinese(basis.strip()),
                "suggested_checks": ensure_simplified_chinese(suggested_checks.strip()),
            }
        )
    return normalized


def emr_json_example() -> dict[str, Any]:
    return {
        "emr": {
            "chief_complaint": "阵发性头晕数月",
            "history_of_present_illness": "患者自诉数月前开始出现头晕，呈阵发性发作，具体每次持续时间、发作频率、诱因及缓解因素未提及。问诊中未提及头痛、恶心呕吐、耳鸣、听力下降、肢体麻木无力、言语不清、胸闷胸痛、心悸等伴随症状。既往类似发作、就诊前用药及检查情况未提及。",
            "past_history": "未提及",
            "allergy_history": "未提及",
            "physical_exam": "未提及",
            "diagnosis": "初步考虑：头晕待查？前庭周围性眩晕、脑血管疾病、颈源性头晕等需鉴别，待医生结合查体及辅助检查确认。",
            "diagnosis_options": [
                {
                    "name": "前庭周围性眩晕可能",
                    "basis": "阵发性头晕，需进一步询问体位诱发、耳鸣、听力下降及眼震情况。",
                    "suggested_checks": "完善眼震、Dix-Hallpike 试验、耳科查体及前庭功能评估。"
                },
                {
                    "name": "脑血管疾病待排",
                    "basis": "头晕病程较长，需排除中枢性眩晕及短暂性脑缺血发作等情况。",
                    "suggested_checks": "完善神经系统查体，必要时行头颅影像学及颈部血管评估。"
                }
            ],
            "plan": "建议完善生命体征、神经系统查体、眼震及平衡功能评估，必要时完善血常规、血糖、电解质、心电图、颈部血管超声或头颅影像学等检查；若出现持续性剧烈头晕、肢体无力、言语不清、意识障碍、胸痛等危险信号，应及时急诊就医。具体诊疗方案由接诊医生确认。",
        },
        "missing_items": ["未记录生命体征及神经系统查体", "未明确头晕性质、持续时间、诱因和伴随症状"],
        "risk_alerts": ["头晕需注意中枢性眩晕、脑血管事件等鉴别诊断"],
        "evidence": [{"label": "主诉", "text": "患者：头晕好几个月了，是一阵一阵的。"}],
    }


def emr_output_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["emr", "missing_items", "risk_alerts", "evidence"],
        "properties": {
            "emr": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "chief_complaint",
                    "history_of_present_illness",
                    "past_history",
                    "allergy_history",
                    "physical_exam",
                    "diagnosis",
                    "diagnosis_options",
                    "plan",
                ],
                "properties": {
                    "chief_complaint": {"type": "string"},
                    "history_of_present_illness": {"type": "string"},
                    "past_history": {"type": "string"},
                    "allergy_history": {"type": "string"},
                    "physical_exam": {"type": "string"},
                    "diagnosis": {"type": "string"},
                    "diagnosis_options": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["name", "basis", "suggested_checks"],
                            "properties": {
                                "name": {"type": "string"},
                                "basis": {"type": "string"},
                                "suggested_checks": {"type": "string"},
                            },
                        },
                    },
                    "plan": {"type": "string"},
                },
            },
            "missing_items": {"type": "array", "items": {"type": "string"}},
            "risk_alerts": {"type": "array", "items": {"type": "string"}},
            "evidence": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["label", "text"],
                    "properties": {
                        "label": {"type": "string"},
                        "text": {"type": "string"},
                    },
                },
            },
        },
    }


def extract_clinical_fields(transcript: str) -> dict[str, Any]:
    normalized = re.sub(r"\s+", "", transcript)
    evidence: list[dict[str, str]] = []

    symptom_patterns = {
        "咳嗽": r"咳嗽|咳了|一直咳|干咳",
        "发热": r"发烧|发热|低烧|高烧|烧到|体温高|三十[七八九]度|3[789](?:\.\d)?度?|38\.?\d?",
        "咳痰": r"咳痰|有痰|黄痰|白痰|浓痰",
        "咽痛": r"咽痛|嗓子疼|喉咙痛|咽喉痛",
        "流涕": r"流鼻涕|鼻涕|流清涕",
        "鼻塞": r"鼻塞|鼻子不通",
        "头晕": r"头晕|眩晕|发晕|晕乎|晕眩|天旋地转",
        "头痛": r"头痛|头疼",
        "乏力": r"乏力|没劲|浑身没力",
        "胸痛": r"胸痛|胸口痛|胸闷",
        "呼吸困难": r"呼吸困难|喘不上气|气短|憋气",
        "腹痛": r"腹痛|肚子疼|肚子痛",
        "腹泻": r"腹泻|拉肚子|稀便",
        "恶心": r"恶心|想吐",
        "呕吐": r"呕吐|吐了",
    }
    symptoms = [name for name, pattern in symptom_patterns.items() if re.search(pattern, normalized)]

    duration = extract_duration(normalized)
    temperature = extract_temperature(normalized)
    has_yellow_sputum = bool(re.search(r"黄痰|黄色痰|浓痰", normalized))
    no_chest_pain = bool(re.search(r"没有胸痛|无胸痛|否认胸痛|胸不痛|没有胸闷", normalized))
    no_dyspnea = bool(re.search(r"没有呼吸困难|无呼吸困难|否认呼吸困难|不喘|没有气短|喘不上气.*没有", normalized))
    no_hypertension = bool(re.search(r"没有高血压|无高血压|否认高血压", normalized))
    no_diabetes = bool(re.search(r"没有糖尿病|无糖尿病|否认糖尿病", normalized))
    penicillin_allergy = bool(re.search(r"青霉素过敏|对青霉素过敏", normalized))
    no_allergy = bool(re.search(r"没有.*过敏|无.*过敏|否认.*过敏|不过敏", normalized))
    ibuprofen = bool(re.search(r"布洛芬|退烧药|退热药|感冒药|阿莫西林|头孢", normalized))

    push_evidence(evidence, "主诉", transcript, list(symptom_patterns.keys()) + ["发烧", "发热", "咳了", "嗓子疼", "肚子疼", "眩晕"])
    push_evidence(evidence, "病程", transcript, ["天", "周", "月", "昨天", "前天", "今天", "小时"])
    push_evidence(evidence, "体温", transcript, ["三十八", "38", "发烧", "发热", "体温"])
    push_evidence(evidence, "伴随症状", transcript, ["黄痰", "胸痛", "呼吸困难", "咽痛", "流鼻涕", "腹泻", "呕吐"])
    push_evidence(evidence, "既往史", transcript, ["高血压", "糖尿病", "既往", "以前"])
    push_evidence(evidence, "过敏史", transcript, ["过敏", "青霉素"])
    push_evidence(evidence, "用药史", transcript, ["布洛芬", "退烧药", "退热药", "感冒药", "头孢", "阿莫西林"])

    if not evidence and transcript.strip():
        evidence.append({"label": "转写原文", "text": first_meaningful_line(transcript)})

    if no_chest_pain and "胸痛" in symptoms:
        symptoms.remove("胸痛")
    if no_dyspnea and "呼吸困难" in symptoms:
        symptoms.remove("呼吸困难")

    missing_items = []
    if not penicillin_allergy and not no_allergy and "过敏" not in normalized:
        missing_items.append("未明确药物或食物过敏史")
    if not re.search(r"体格检查|查体|咽部|肺部|体温", normalized):
        missing_items.append("未记录体格检查")
    if not re.search(r"家族史|家里|父母", normalized):
        missing_items.append("未询问家族史")
    if not re.search(r"吸烟|饮酒", normalized):
        missing_items.append("未询问个人史")
    if "头晕" in symptoms:
        if not re.search(r"头痛|恶心|呕吐|耳鸣|听力|肢体|麻木|无力|言语|视物|胸闷|心悸", normalized):
            missing_items.append("头晕相关伴随症状询问不完整")
        if not re.search(r"血压|神经系统|眼震|步态|平衡|查体", normalized):
            missing_items.append("未记录头晕相关查体及生命体征")

    risk_alerts = []
    if "胸痛" in normalized and not no_chest_pain:
        risk_alerts.append("提及胸痛，请补充部位、性质、持续时间及心电图相关评估")
    if re.search(r"呼吸困难|喘不上气", normalized) and not no_dyspnea:
        risk_alerts.append("提及呼吸困难，请补充血氧、呼吸频率和肺部查体")
    if re.search(r"高热|四十度|40", normalized):
        risk_alerts.append("存在高热表述，请关注感染风险和退热处理")
    if penicillin_allergy:
        risk_alerts.append("青霉素过敏史阳性，后续用药需避开相关药物")
    if "头晕" in symptoms:
        risk_alerts.append("头晕需鉴别前庭周围性眩晕、中枢性眩晕、脑血管事件、心律失常及代谢异常等情况")

    chief_symptoms = [item for item in symptoms if item not in {"咳痰", "鼻塞", "流涕", "乏力"}]
    if not chief_symptoms:
        chief_symptoms = symptoms[:3]
    chief_complaint = f"{'、'.join(chief_symptoms[:3])}{duration}" if chief_symptoms else "未提及"
    diagnosis = infer_diagnosis(symptoms)
    if "头晕" in symptoms:
        intermittent = bool(re.search(r"一阵一阵|阵发|间断|不是一直|发作", normalized))
        hpi = "".join(
            [
                f"患者{'起病时间未明确' if duration == '未明确' else f'约{duration}前'}出现头晕。",
                "症状呈阵发性发作。" if intermittent else "头晕发作形式及持续时间未提及。",
                "具体诱因、每次持续时间、发作频率、缓解或加重因素未提及。",
                "是否伴恶心呕吐、耳鸣、听力下降、头痛、视物旋转、肢体麻木无力、言语不清、胸闷胸痛、心悸等症状未提及。",
                "既往类似发作、就诊前用药及既往相关检查情况未提及。",
            ]
        )
    else:
        hpi = "".join(
            [
                f"患者{'起病时间未明确' if duration == '未明确' else f'约{duration}前'}出现{'、'.join(symptoms) or '不适症状'}。",
                f"{temperature}。" if temperature else "最高体温未提及。",
                "伴少量黄痰。" if has_yellow_sputum else "咳痰情况未提及。",
                "否认胸痛。" if no_chest_pain else "胸痛情况未明确。",
                "否认呼吸困难。" if no_dyspnea else "呼吸困难情况未明确。",
                "曾自行服用布洛芬，退热效果可。" if ibuprofen else "就诊前用药情况未提及。",
            ]
        )

    return {
        "chief_complaint": chief_complaint,
        "diagnosis": diagnosis,
        "diagnosis_options": [],
        "history_of_present_illness": hpi,
        "past_history": "".join(
            [
                "否认高血压病史。" if no_hypertension else "高血压病史未提及。",
                "否认糖尿病病史。" if no_diabetes else "糖尿病病史未提及。",
            ]
        ),
        "allergy_history": "青霉素过敏。" if penicillin_allergy else ("否认明确过敏史。" if no_allergy else "未提及。"),
        "physical_exam": "未提及。",
        "plan": (
            "建议完善生命体征、卧立位血压、神经系统查体、眼震及步态/平衡评估；必要时完善血常规、血糖、电解质、心电图、颈部血管超声或头颅影像学等检查；需鉴别前庭周围性眩晕、中枢性眩晕、脑血管事件、心律失常及代谢异常等。若出现持续性剧烈头晕、肢体无力、言语不清、意识障碍、胸痛等危险信号，应及时急诊就医。具体诊疗方案由接诊医生确认。"
            if "头晕" in symptoms
            else "建议完善体格检查，必要时完善血常规、CRP等检查；结合症状演变评估感染及其他鉴别诊断；诊断及治疗方案由接诊医生结合查体和检查结果确认。"
        ),
        "missing_items": missing_items,
        "risk_alerts": risk_alerts,
        "evidence": evidence,
    }


def extract_duration(text: str) -> str:
    match = re.search(r"([一二两三四五六七八九十\d]+)(天|周|星期|个月|月|小时|年)", text)
    if match:
        return f"{normalize_cn_number(match.group(1))}{match.group(2)}"
    if "昨天" in text:
        return "1天"
    if "前天" in text:
        return "2天"
    if "今天" in text or "早上" in text or "上午" in text or "下午" in text:
        return "半天"
    return "未明确"


def normalize_cn_number(value: str) -> str:
    mapping = {
        "一": "1",
        "二": "2",
        "两": "2",
        "三": "3",
        "四": "4",
        "五": "5",
        "六": "6",
        "七": "7",
        "八": "8",
        "九": "9",
        "十": "10",
    }
    return mapping.get(value, value)


def extract_temperature(text: str) -> str:
    digital = re.search(r"(3[7-9]|4[0-1])(?:[\.点](\d))?度?", text)
    if digital:
        decimal = f".{digital.group(2)}" if digital.group(2) else ""
        return f"最高体温约{digital.group(1)}{decimal}℃"

    cn_temps = {
        "三十七度": "37℃",
        "三十八度": "38℃",
        "三十八度五": "38.5℃",
        "三十九度": "39℃",
        "四十度": "40℃",
    }
    for phrase, value in cn_temps.items():
        if phrase in text:
            return f"最高体温约{value}"
    if re.search(r"发烧|发热|低烧|高烧", text):
        return "有发热，具体体温未提及"
    return ""


def infer_diagnosis(symptoms: list[str]) -> str:
    symptom_set = set(symptoms)
    if "头晕" in symptom_set:
        return "初步考虑：头晕待查？需鉴别前庭周围性眩晕、中枢性眩晕、脑血管事件、心律失常及代谢异常，待医生结合查体及辅助检查确认"
    if {"咳嗽", "发热"}.issubset(symptom_set) or ({"咽痛", "流涕", "鼻塞", "咳痰"} & symptom_set):
        return "初步考虑：急性上呼吸道感染？待医生确认"
    if {"腹痛", "腹泻", "恶心", "呕吐"} & symptom_set:
        return "初步考虑：急性胃肠炎？待医生确认"
    if symptom_set:
        return "初步诊断待医生结合查体及辅助检查确认"
    return "待医生确认"


def first_meaningful_line(transcript: str) -> str:
    lines = [line.strip() for line in transcript.splitlines() if line.strip()]
    if not lines:
        return transcript.strip()[:120]
    return lines[0][:180]


def push_evidence(evidence: list[dict[str, str]], label: str, transcript: str, keywords: list[str]) -> None:
    lines = [line.strip() for line in transcript.splitlines() if line.strip()]
    matched = next((line for line in lines if any(keyword in line for keyword in keywords)), None)
    if matched:
        evidence.append({"label": label, "text": matched})


def build_emr_text(patient: dict[str, Any], emr: dict[str, Any], confirmed: bool, raw_transcript: str = "") -> str:
    body = f"""门诊通用病历

患者姓名：{patient.get("name", "")}
性别：{patient.get("gender", "")}
年龄：{patient.get("age", "")}
科室：{patient.get("department", "")}
就诊号：{patient.get("visit_no", "")}
确认状态：{"医生已确认" if confirmed else "未确认"}

主诉：
{emr.get("chief_complaint", "")}

现病史：
{emr.get("history_of_present_illness", "")}

既往史：
{emr.get("past_history", "")}

过敏史：
{emr.get("allergy_history", "")}

体格检查：
{emr.get("physical_exam", "")}

初步诊断：
{emr.get("diagnosis", "")}

处理意见：
{emr.get("plan", "")}
"""
    body = ensure_simplified_chinese(body)
    if raw_transcript.strip():
        body += f"""

原始问诊转写：
{raw_transcript.strip()}
"""
    return body


static_index = ROOT_DIR / "index.html"
if static_index.exists():
    app.mount("/", StaticFiles(directory=ROOT_DIR, html=True), name="static")


@app.get("/favicon.ico", include_in_schema=False)
def favicon() -> FileResponse:
    raise HTTPException(status_code=404)
