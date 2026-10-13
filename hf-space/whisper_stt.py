"""OpenAI-compatible speech-to-text with Whisper (faster-whisper, int8 on CPU).

    POST /v1/audio/transcriptions   OpenAI "Create transcription" (multipart: file, model, language, prompt, response_format)
    POST /v1/audio/translations     OpenAI "Create translation" (to English)

Other modules can call transcribe(path) directly; the podcast module uses it for episode transcripts.

Environment:
    WHISPER_MODEL    faster-whisper model: tiny, base, small (default), medium, large-v3-turbo, ...
    WHISPER_THREADS  CPU threads (default: all cores)
    WHISPER_BEAM     beam size; 1 (default) is greedy decoding, fastest on CPU
    MAX_UPLOAD_MB    largest accepted upload (default 200)
    API_KEY          require `Authorization: Bearer <key>` (comma-separate several keys)
"""

import os
import tempfile
import threading
import time

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse

WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "small")
WHISPER_THREADS = int(os.environ.get("WHISPER_THREADS", "0")) or (os.cpu_count() or 2)
WHISPER_BEAM = int(os.environ.get("WHISPER_BEAM", "1"))
MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_MB", "200")) * 1024 * 1024
API_KEYS = [k.strip() for k in os.environ.get("API_KEY", "").split(",") if k.strip()]

# Paths this module serves when mounted at the root next to other modules (see app.py).
PATHS = ["/v1/audio/transcriptions", "/v1/audio/translations"]
RESPONSE_FORMATS = ("json", "text", "srt", "verbose_json", "vtt")
# Whisper often writes Mandarin in traditional characters; a simplified prompt steers it.
DEFAULT_PROMPTS = {"zh": "以下是普通话的句子。"}

_model = None
_load_lock = threading.Lock()
# One transcription at a time: parallel runs would only share the same CPU cores.
_run_lock = threading.Lock()


class ApiError(Exception):
    def __init__(self, status, message, param=None, code=None, type_="invalid_request_error"):
        super().__init__(message)
        self.status, self.message, self.param, self.code, self.type = status, message, param, code, type_


def get_model():
    global _model
    with _load_lock:
        if _model is None:
            from faster_whisper import WhisperModel

            started = time.time()
            _model = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8", cpu_threads=WHISPER_THREADS)
            print(f"whisper {WHISPER_MODEL} loaded in {time.time() - started:.0f}s", flush=True)
        return _model


def transcribe(path: str, language: str | None = None, prompt: str | None = None, task: str = "transcribe", temperature: float = 0.0) -> dict:
    """Transcribe an audio file. Returns {language, duration, text, segments: [{id, start, end, text}]}."""
    from faster_whisper import decode_audio

    model = get_model()
    audio = decode_audio(path)
    with _run_lock:
        started = time.time()
        if not language and not prompt:
            language = model.detect_language(audio)[0]
        segments, info = model.transcribe(
            audio,
            language=language or None,
            task=task,
            initial_prompt=prompt or DEFAULT_PROMPTS.get(language or ""),
            beam_size=WHISPER_BEAM,
            temperature=temperature or 0.0,
            vad_filter=True,
        )
        segs = [{"id": i, "start": round(s.start, 2), "end": round(s.end, 2), "text": s.text.strip()} for i, s in enumerate(segments)]
    print(f"transcribed {info.duration:.0f}s of {info.language} audio in {time.time() - started:.0f}s", flush=True)
    joiner = "" if info.language in ("zh", "ja", "yue") else " "
    return {"language": info.language, "duration": round(info.duration, 2), "text": joiner.join(s["text"] for s in segs), "segments": segs}


def _timestamp(seconds: float, sep: str) -> str:
    ms = int(round(seconds * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{ms:03d}"


def to_srt(result: dict) -> str:
    return "".join(f"{i}\n{_timestamp(s['start'], ',')} --> {_timestamp(s['end'], ',')}\n{s['text']}\n\n" for i, s in enumerate(result["segments"], 1))


def to_vtt(result: dict) -> str:
    return "WEBVTT\n\n" + "".join(f"{_timestamp(s['start'], '.')} --> {_timestamp(s['end'], '.')}\n{s['text']}\n\n" for s in result["segments"])


# ---------------------------------------------------------------- HTTP API

app = FastAPI(title="Whisper OpenAI-compatible STT", docs_url=None, redoc_url=None)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.exception_handler(ApiError)
async def api_error_handler(_: Request, e: ApiError):
    return JSONResponse({"error": {"message": e.message, "type": e.type, "param": e.param, "code": e.code}}, status_code=e.status)


@app.middleware("http")
async def authenticate(request: Request, call_next):
    if API_KEYS and request.method != "OPTIONS":
        auth = request.headers.get("authorization", "")
        key = auth[7:].strip() if auth.lower().startswith("bearer ") else request.headers.get("x-api-key", "").strip()
        if key not in API_KEYS:
            return await api_error_handler(request, ApiError(401, "Incorrect API key provided.", code="invalid_api_key"))
    return await call_next(request)


def _handle(file: UploadFile, task: str, language, prompt, response_format, temperature):
    if response_format not in RESPONSE_FORMATS:
        raise ApiError(400, f"Invalid value for 'response_format': '{response_format}'. Supported values are: {', '.join(RESPONSE_FORMATS)}.", "response_format", "invalid_value")
    suffix = os.path.splitext(file.filename or "")[1] or ".audio"
    with tempfile.NamedTemporaryFile(suffix=suffix) as tmp:
        size = 0
        while chunk := file.file.read(1 << 20):
            size += len(chunk)
            if size > MAX_UPLOAD_BYTES:
                raise ApiError(413, f"File is larger than {MAX_UPLOAD_BYTES >> 20} MB.", "file", "file_too_large")
            tmp.write(chunk)
        tmp.flush()
        if size == 0:
            raise ApiError(400, "Missing required parameter: 'file'.", "file", "missing_required_parameter")
        try:
            result = transcribe(tmp.name, language=language, prompt=prompt, task=task, temperature=temperature)
        except Exception as e:
            if "decod" in str(e).lower() or "invalid data" in str(e).lower() or "av." in type(e).__module__:
                raise ApiError(400, "Could not decode the file. Upload audio or video (mp3, m4a, wav, webm, mp4, ...).", "file", "invalid_file")
            raise

    if response_format == "text":
        return PlainTextResponse(result["text"])
    if response_format == "srt":
        return PlainTextResponse(to_srt(result), media_type="application/x-subrip")
    if response_format == "vtt":
        return PlainTextResponse(to_vtt(result), media_type="text/vtt")
    if response_format == "verbose_json":
        return {"task": task, **result}
    return {"text": result["text"]}


@app.post("/v1/audio/transcriptions")
def create_transcription(
    file: UploadFile = File(...),
    model: str = Form("whisper-1"),
    language: str | None = Form(None),
    prompt: str | None = Form(None),
    response_format: str = Form("json"),
    temperature: float = Form(0.0),
):
    return _handle(file, "transcribe", language, prompt, response_format, temperature)


@app.post("/v1/audio/translations")
def create_translation(
    file: UploadFile = File(...),
    model: str = Form("whisper-1"),
    prompt: str | None = Form(None),
    response_format: str = Form("json"),
    temperature: float = Form(0.0),
):
    return _handle(file, "translate", None, prompt, response_format, temperature)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "7860")))
