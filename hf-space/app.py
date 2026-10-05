"""OpenAI-compatible TTS server for Kokoro-82M, sized for a free Hugging Face CPU Space.

    POST /v1/audio/speech   OpenAI "Create speech"
    GET  /v1/models         OpenAI "List models"
    GET  /v1/audio/voices   extension: list voices

Voices come from two Apache-2.0 models, loaded on first use:
  hexgrad/Kokoro-82M        54 voices in 9 languages, e.g. af_heart, zf_xiaoxiao
  hexgrad/Kokoro-82M-v1.1-zh 100 extra Chinese voices (zf_001 ... zm_100) plus af_maple, af_sol, bf_vale

Set the API_KEY secret in the Space settings to require `Authorization: Bearer <key>`.
"""

import io
import os
import re
import threading
import time

import numpy as np
import soundfile as sf
import torch
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from kokoro import KModel, KPipeline

SAMPLE_RATE = 24000
MAX_INPUT_CHARS = int(os.environ.get("MAX_INPUT_CHARS", "10000"))
API_KEYS = [k.strip() for k in os.environ.get("API_KEY", "").split(",") if k.strip()]

REPO_V1 = "hexgrad/Kokoro-82M"
REPO_ZH = "hexgrad/Kokoro-82M-v1.1-zh"

VOICES = {
    REPO_V1: """af_alloy af_aoede af_bella af_heart af_jessica af_kore af_nicole af_nova af_river af_sarah af_sky
        am_adam am_echo am_eric am_fenrir am_liam am_michael am_onyx am_puck am_santa
        bf_alice bf_emma bf_isabella bf_lily bm_daniel bm_fable bm_george bm_lewis
        ef_dora em_alex em_santa ff_siwis hf_alpha hf_beta hm_omega hm_psi if_sara im_nicola
        pf_dora pm_alex pm_santa
        zf_xiaobei zf_xiaoni zf_xiaoxiao zf_xiaoyi zm_yunjian zm_yunxi zm_yunxia zm_yunyang""".split(),
    REPO_ZH: ["af_maple", "af_sol", "bf_vale"]
    + [f"zf_{n:03d}" for n in (1, 2, 3, 4, 5, 6, 7, 8, 17, 18, 19, 21, 22, 23, 24, 26, 27, 28, 32, 36, 38, 39, 40, 42, 43, 44, 46, 47, 48, 49, 51, 59, 60, 67, 70, 71, 72, 73, 74, 75, 76, 77, 78, 79, 83, 84, 85, 86, 87, 88, 90, 92, 93, 94, 99)]
    + [f"zm_{n:03d}" for n in (9, 10, 11, 12, 13, 14, 15, 16, 20, 25, 29, 30, 31, 33, 34, 35, 37, 41, 45, 50, 52, 53, 54, 55, 56, 57, 58, 61, 62, 63, 64, 65, 66, 68, 69, 80, 81, 82, 89, 91, 95, 96, 97, 98, 100)],
}
VOICE_REPO = {voice: repo for repo, voices in VOICES.items() for voice in voices}

OPENAI_VOICES = {
    "alloy": "af_alloy",
    "ash": "am_adam",
    "ballad": "bm_george",
    "coral": "af_bella",
    "echo": "am_echo",
    "fable": "bm_fable",
    "onyx": "am_onyx",
    "nova": "af_nova",
    "sage": "af_sarah",
    "shimmer": "af_heart",
    "verse": "am_michael",
    "marin": "af_jessica",
    "cedar": "am_liam",
}

LANGUAGES = {"a": "en-US", "b": "en-GB", "e": "es", "f": "fr", "h": "hi", "i": "it", "p": "pt-BR", "z": "zh-CN"}

# soundfile (libsndfile >= 1.1) encodes everything except AAC, so there is no ffmpeg dependency.
FORMATS = {
    "mp3": ("audio/mpeg", dict(format="MP3")),
    "opus": ("audio/ogg", dict(format="OGG", subtype="OPUS")),
    "flac": ("audio/flac", dict(format="FLAC")),
    "wav": ("audio/wav", dict(format="WAV", subtype="PCM_16")),
    "pcm": ("audio/pcm", None),
}

torch.set_num_threads(os.cpu_count() or 2)
_models: dict[str, KModel] = {}
_pipelines: dict[tuple[str, str], KPipeline] = {}
_load_lock = threading.Lock()
# One synthesis at a time: concurrent requests would only fight over the same CPU cores.
_synth_lock = threading.Lock()


class ApiError(Exception):
    def __init__(self, status, message, param=None, code=None, type_="invalid_request_error"):
        super().__init__(message)
        self.status, self.message, self.param, self.code, self.type = status, message, param, code, type_


def get_pipeline(repo: str, lang: str) -> KPipeline:
    with _load_lock:
        if repo not in _models:
            _models[repo] = KModel(repo_id=repo).eval()
        key = (repo, lang)
        if key not in _pipelines:
            kwargs = {}
            if lang == "z":
                # Read English words inside Chinese text with the English G2P.
                en = KPipeline(lang_code="a", repo_id=repo, model=False)
                kwargs["en_callable"] = lambda text: next(en(text)).phonemes
            _pipelines[key] = KPipeline(lang_code=lang, repo_id=repo, model=_models[repo], **kwargs)
        return _pipelines[key]


def synthesize(text: str, voice: str, speed: float) -> np.ndarray:
    repo = VOICE_REPO[voice]
    pipeline = get_pipeline(repo, voice[0])
    with _synth_lock:
        chunks = [r.audio.numpy() for r in pipeline(text, voice=voice, speed=speed, split_pattern=r"\n+") if r.audio is not None]
    if not chunks:
        raise ApiError(400, "No speech could be generated from 'input'.", "input")
    return np.concatenate(chunks)


def encode(audio: np.ndarray, response_format: str) -> bytes:
    pcm16 = (np.clip(audio, -1.0, 1.0) * 32767).astype(np.int16)
    _, options = FORMATS[response_format]
    if options is None:
        return pcm16.tobytes()
    buf = io.BytesIO()
    sf.write(buf, pcm16, SAMPLE_RATE, **options)
    return buf.getvalue()


app = FastAPI(title="Kokoro OpenAI-compatible TTS", docs_url="/docs")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.exception_handler(ApiError)
async def api_error_handler(_: Request, e: ApiError):
    return JSONResponse({"error": {"message": e.message, "type": e.type, "param": e.param, "code": e.code}}, status_code=e.status)


@app.middleware("http")
async def authenticate(request: Request, call_next):
    if API_KEYS and request.url.path.startswith("/v1/") and request.method != "OPTIONS":
        auth = request.headers.get("authorization", "")
        key = auth[7:].strip() if auth.lower().startswith("bearer ") else request.headers.get("x-api-key", "").strip()
        if key not in API_KEYS:
            return await api_error_handler(request, ApiError(401, "Incorrect API key provided.", code="invalid_api_key"))
    return await call_next(request)


@app.get("/")
def root():
    return {"status": "ok", "endpoints": ["POST /v1/audio/speech", "GET /v1/models", "GET /v1/audio/voices"], "docs": "/docs"}


@app.get("/v1/models")
def list_models():
    return {"object": "list", "data": [{"id": "kokoro", "object": "model", "created": 1735689600, "owned_by": "hexgrad"}]}


@app.get("/v1/audio/voices")
def list_voices(locale: str | None = None):
    data = [{"id": k, "object": "voice", "alias_of": v} for k, v in OPENAI_VOICES.items()] if not locale else []
    for voice, repo in VOICE_REPO.items():
        lang = LANGUAGES[voice[0]]
        if locale and not lang.lower().startswith(locale.lower()):
            continue
        data.append({"id": voice, "object": "voice", "gender": "female" if voice[1] == "f" else "male", "locale": lang, "model": repo})
    return {"object": "list", "data": data}


@app.post("/v1/audio/speech")
def create_speech(body: dict):
    text = body.get("input")
    if not isinstance(text, str) or not text.strip():
        raise ApiError(400, "Missing required parameter: 'input'.", "input", "missing_required_parameter")
    if len(text) > MAX_INPUT_CHARS:
        raise ApiError(400, f"'input' is too long: {len(text)} characters, maximum is {MAX_INPUT_CHARS}.", "input", "string_above_max_length")

    voice = str(body.get("voice") or "af_heart").strip()
    voice = OPENAI_VOICES.get(voice.lower(), voice)
    if voice not in VOICE_REPO:
        raise ApiError(400, f"Invalid value for 'voice': '{voice}'. See GET /v1/audio/voices.", "voice", "invalid_value")

    response_format = body.get("response_format") or "mp3"
    if response_format not in FORMATS:
        raise ApiError(400, f"Invalid value for 'response_format': '{response_format}'. Supported values are: {', '.join(FORMATS)}.", "response_format", "invalid_value")

    try:
        speed = float(body.get("speed", 1.0))
    except (TypeError, ValueError):
        speed = -1
    if not 0.25 <= speed <= 4.0:
        raise ApiError(400, f"Invalid 'speed': {body.get('speed')}. Expected a number between 0.25 and 4.0.", "speed", "invalid_value")

    # "[500]" at the end of a line inserts a 500 ms pause, matching the Edge worker.
    parts, pauses = [], []
    for line in text.strip().split("\n"):
        m = re.search(r"\[(\d+)\]\s*$", line)
        parts.append(line[: m.start()] if m else line)
        pauses.append(min(int(m.group(1)), 20000) if m else 0)

    started = time.time()
    if any(pauses):
        audio = []
        for line, pause in zip(parts, pauses):
            if line.strip():
                audio.append(synthesize(line, voice, speed))
            if pause:
                audio.append(np.zeros(SAMPLE_RATE * pause // 1000, dtype=np.float32))
        audio = np.concatenate(audio) if audio else np.zeros(0, dtype=np.float32)
    else:
        audio = synthesize("\n".join(parts), voice, speed)

    content_type, _ = FORMATS[response_format]
    print(f"synthesized {len(text)} chars -> {len(audio) / SAMPLE_RATE:.1f}s audio in {time.time() - started:.1f}s ({voice})", flush=True)
    return Response(encode(audio, response_format), media_type=content_type)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "7860")))
