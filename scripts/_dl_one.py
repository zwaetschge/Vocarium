"""Download a single HuggingFace repo into the cache mounted at /cache.

Invoked from download-models.sh; not intended for direct use. The repo id
is passed as argv[1]; HF_HOME=/cache is set by the caller so all snapshot
files end up under /cache/hub/.
"""

import sys

from huggingface_hub import snapshot_download

if len(sys.argv) != 2:
    print("usage: _dl_one.py <repo_id>", file=sys.stderr)
    sys.exit(2)

repo_id = sys.argv[1]
print(f"Pulling {repo_id} ...", flush=True)
path = snapshot_download(
    repo_id=repo_id,
    cache_dir="/cache/hub",
    local_dir_use_symlinks=False,
)
print(f"OK: {path}", flush=True)
