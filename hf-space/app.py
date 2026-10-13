"""One Space, several independent services, each a module with its own FastAPI app.

    tts      kokoro_tts.py   OpenAI-compatible Kokoro TTS     /v1/audio/speech, /v1/models, /v1/audio/voices
    stt      whisper_stt.py  OpenAI-compatible Whisper STT    /v1/audio/transcriptions, /v1/audio/translations
    podcast  podcast.py      YouTube channels as podcasts     /podcast/  (feeds at /podcast/feed/<channel>)

MODULES picks which ones load (default "tts,stt,podcast"). A module that fails to import is
skipped and reported at /, so one broken service doesn't take the others down.

Modules either get their own path prefix (podcast -> /podcast) or, to keep OpenAI-style URLs,
share the root and claim the paths listed in their PATHS; each request goes to the module with
the longest matching path.

The podcast module uses ACCESS_KEY for its ?key= check; if that is unset it reuses API_KEY,
so the Space's existing secret protects every service.
"""

import importlib
import os
import traceback

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse

# name -> (python module, mount prefix or None to share the root, what to show at /)
AVAILABLE = {
    "tts": ("kokoro_tts", None, "/v1/audio/speech"),
    "stt": ("whisper_stt", None, "/v1/audio/transcriptions"),
    "podcast": ("podcast", "/podcast", "/podcast/"),
}
ENABLED = [m.strip() for m in os.environ.get("MODULES", "tts,stt,podcast").split(",") if m.strip()]

if os.environ.get("API_KEY") and not os.environ.get("ACCESS_KEY"):
    os.environ["ACCESS_KEY"] = os.environ["API_KEY"]


class PathDispatcher:
    """ASGI app sending each request to the sub-app with the longest matching path prefix."""

    def __init__(self, default):
        self.default = default
        self.routes: list[tuple[str, object]] = []

    def add(self, prefix: str, sub_app):
        self.routes.append((prefix.rstrip("/"), sub_app))
        self.routes.sort(key=lambda r: -len(r[0]))

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            path = scope["path"]
            for prefix, sub_app in self.routes:
                if path == prefix or path.startswith(prefix + "/"):
                    return await sub_app(scope, receive, send)
        await self.default(scope, receive, send)


root = FastAPI(title="nxdev services", docs_url=None, redoc_url=None)
app = PathDispatcher(root)
loaded: dict[str, str] = {}
failed: dict[str, str] = {}


@root.get("/")
def index():
    return {"status": "ok", "modules": loaded, "failed": failed}


def redirect_to(target: str):
    def handler(request: Request):
        return RedirectResponse(target + (f"?{request.url.query}" if request.url.query else ""))

    return handler


for name in ENABLED:
    if name not in AVAILABLE:
        failed[name] = "unknown module"
        continue
    module_name, prefix, entry = AVAILABLE[name]
    try:
        module = importlib.import_module(module_name)
        if prefix:
            # A mount only matches "/podcast/...", so send the bare "/podcast" to its home page.
            root.add_api_route(prefix, redirect_to(prefix + "/"), include_in_schema=False)
            root.mount(prefix, module.app)
        else:
            for path in module.PATHS:
                app.add(path, module.app)
        loaded[name] = entry
    except Exception as e:
        failed[name] = f"{type(e).__name__}: {e}"
        traceback.print_exc()

print(f"modules loaded: {loaded}; failed: {failed}", flush=True)

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "7860")), proxy_headers=True, forwarded_allow_ips="*")
