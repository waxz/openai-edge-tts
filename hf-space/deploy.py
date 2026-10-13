"""Upload this Space: the files in hf-space/ plus podcast.py from ../youtube-podcast/.

    HF_TOKEN=... python deploy.py <user>/<space>

Files on the Space that are not part of this set are deleted (they stay in its git history).
"""

import sys
from pathlib import Path

from huggingface_hub import CommitOperationAdd, CommitOperationDelete, HfApi

HERE = Path(__file__).parent
FILES = {
    "README.md": HERE / "README.md",
    "Dockerfile": HERE / "Dockerfile",
    "requirements.txt": HERE / "requirements.txt",
    "app.py": HERE / "app.py",
    "kokoro_tts.py": HERE / "kokoro_tts.py",
    "whisper_stt.py": HERE / "whisper_stt.py",
    "podcast.py": HERE.parent / "youtube-podcast" / "podcast.py",
}

repo = sys.argv[1] if len(sys.argv) > 1 else "nxdev-org/tts"
api = HfApi()
stale = [f for f in api.list_repo_files(repo, repo_type="space") if f not in FILES and f != ".gitattributes"]
ops = [CommitOperationAdd(path_in_repo=name, path_or_fileobj=str(path)) for name, path in FILES.items()]
ops += [CommitOperationDelete(path_in_repo=f) for f in stale]
info = api.create_commit(repo, ops, repo_type="space", commit_message="Deploy TTS + podcast modules")
print(info.commit_url)
