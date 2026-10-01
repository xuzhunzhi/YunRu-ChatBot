from __future__ import annotations

import csv
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path


def _float_env(name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def _int_env(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


@dataclass(frozen=True, slots=True)
class MemorySettings:
    enabled: bool = True
    data_dir: Path = Path(__file__).resolve().parents[2] / "data" / "memory"
    interval_seconds: float = 600.0
    retention_days: int = 7
    model_timeout: float = 45.0
    max_batches: int = 8

    @classmethod
    def from_environment(cls):
        base = Path(os.environ.get("QQBOT_DATA_DIR", str(Path(__file__).resolve().parents[2] / "data")))
        return cls(
            enabled=os.environ.get("QQBOT_MEMORY_ENABLED", "1").lower() not in {"0", "false", "off"},
            data_dir=base.resolve() / "memory",
            interval_seconds=_float_env("QQBOT_MEMORY_INTERVAL_SECONDS", 600.0, 60.0, 86400.0),
            retention_days=_int_env("QQBOT_MEMORY_RETENTION_DAYS", 7, 1, 30),
        )


def protect_directory(path: Path):
    """Restrict the dedicated memory directory, never a user/workspace root."""
    path = path.resolve()
    if path.name != "memory" or path == path.parent:
        raise ValueError("memory_directory_required")
    path.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        flags = subprocess.CREATE_NO_WINDOW
        identity = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"], capture_output=True,
                                  text=True, check=True, timeout=5, creationflags=flags)
        sid = next(csv.reader(identity.stdout.splitlines()))[1].strip()
        if not sid.startswith("S-1-") or not all(c.isdigit() or c in "S-" for c in sid):
            raise ValueError("invalid_windows_identity")
        subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r", f"*{sid}:(OI)(CI)F"],
                       capture_output=True, check=True, timeout=5, creationflags=flags)
    else:
        path.chmod(0o700)
