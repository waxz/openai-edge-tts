"""One Space, several independent services, each a module with its own FastAPI app.

    tts      kokoro_tts.py  OpenAI-compatible Kokoro TTS     /v1/audio/speech, /v1/models, /v1/audio/voices
    podcast  podcast.py     YouTube channels as podcasts     /podcast/  (feeds at /podcast/feed/<channel>)

MODULES picks which ones load (default "tts,podcast"). A module that fails to import is skipped
and reported at /, so one broken service doesn't take the others down.

The podcast module uses ACCESS_KEY for its ?key= check; if that is unset it reuses API_KEY,
so the Space's existing secret protects both services.
"""

import importlib
import os
import traceback

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse

# name -> (module, mount path, what to show at /)
AVAILABLE = {
    "podcast": ("podcast", "/podcast", "/podcast/"),
    "tts": ("kokoro_tts", "/", "/v1/audio/speech"),
}
ENABLED = [m.strip() for m in os.environ.get("MODULES", "tts,podcast").split(",") if m.strip()]

if os.environ.get("API_KEY") and not os.environ.get("ACCESS_KEY"):
    os.environ["ACCESS_KEY"] = os.environ["API_KEY"]

app = FastAPI(title="nxdev services", docs_url=None, redoc_url=None)
loaded: dict[str, str] = {}
failed: dict[str, str] = {}


@app.get("/")
def index():
    return {"status": "ok", "modules": loaded, "failed": failed}


def redirect_to(target: str):
    def handler(request: Request):
        return RedirectResponse(target + (f"?{request.url.query}" if request.url.query else ""))

    return handler


# Mount longer prefixes first; the "/" mount catches everything else, so it must come last.
for name in sorted(ENABLED, key=lambda n: -len(AVAILABLE.get(n, ("", "", ""))[1])):
    if name not in AVAILABLE:
        failed[name] = "unknown module"
        continue
    module_name, mount_path, entry = AVAILABLE[name]
    try:
        module = importlib.import_module(module_name)
        if mount_path != "/":
            # A mount only matches "/podcast/...", so send the bare "/podcast" to its home page.
            app.add_api_route(mount_path, redirect_to(mount_path + "/"), include_in_schema=False)
        app.mount(mount_path, module.app)
        loaded[name] = entry
    except Exception as e:
        failed[name] = f"{type(e).__name__}: {e}"
        traceback.print_exc()

print(f"modules loaded: {loaded}; failed: {failed}", flush=True)

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "7860")), proxy_headers=True, forwarded_allow_ips="*")
