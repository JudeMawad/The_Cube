#!/bin/sh
# Discover pip-installed CUDA libraries before starting the server's Python process.
set -eu

repo_root=$(CDPATH= cd -P "$(dirname "$0")/.." && pwd)
cd "$repo_root"
python="$repo_root/ai_node/.venv/bin/python"

if [ ! -x "$python" ]; then
    echo "AI-node Python not found: $python. Create ai_node/.venv and install ai_node/requirements.txt (see ai_node/README.md)." >&2
    exit 1
fi

cuda_library_path=$("$python" - <<'PY'
from importlib.util import find_spec
from pathlib import Path
import sys

directories = []
for package, library in (
    ("nvidia.cublas.lib", "libcublas.so.12"),
    ("nvidia.cudnn.lib", "libcudnn.so.9"),
):
    try:
        spec = find_spec(package)
    except ImportError:
        spec = None
    locations = spec.submodule_search_locations if spec is not None else None
    directory = next(
        (Path(location).resolve() for location in locations or ()
         if (Path(location) / library).is_file()),
        None,
    )
    if directory is None:
        sys.exit(
            f"AI-node CUDA runtime: cannot find {library} in {package}. "
            "Install or repair the runtime with "
            "ai_node/.venv/bin/python -m pip install --force-reinstall "
            "-r ai_node/requirements.txt (see ai_node/README.md)."
        )
    directories.append(str(directory))
print(":".join(directories))
PY
)

LD_LIBRARY_PATH="$cuda_library_path${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export LD_LIBRARY_PATH
exec "$python" -m uvicorn ai_node.app:app --host 0.0.0.0 --port 8766 --workers 1
