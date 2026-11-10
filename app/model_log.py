"""Best-effort, bounded diagnostics; callers supply only allowlisted metadata."""
import json
import os
from pathlib import Path
import threading

MAX_BYTES = 2 * 1024 * 1024
_LOCK = threading.Lock()


def write_diagnostic(path, record):
    try:
        payload = (json.dumps(record, ensure_ascii=False, allow_nan=False) + '\n').encode('utf-8')
        if len(payload) > 16384:
            return
        path = Path(path)
        with _LOCK:
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists() and path.stat().st_size + len(payload) > MAX_BYTES:
                os.replace(path, path.with_name(path.name + '.1'))
            with path.open('ab') as handle:
                handle.write(payload)
    except Exception:
        # Diagnostics must never break or repeat a model request.
        pass
