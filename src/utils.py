"""Small helpers: slugs, timestamps, logging, .env loading."""

from __future__ import annotations

import hashlib
import logging
import os
import re
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

LOGGER_NAME = "china_research"

# Ranges we consider safe *and* meaningful to keep verbatim in a path.
# Keeping CJK makes directories readable for a downstream Claude process;
# they are valid UTF-8 filenames on macOS/Linux and on modern Windows.
_CJK = (
    "㐀-䶿"  # CJK ext A
    "一-鿿"  # CJK unified
    "豈-﫿"  # compatibility ideographs
    "぀-ヿ"  # kana (Japanese company names)
    "가-힯"  # hangul syllables
)
_KEEP = re.compile(rf"[^a-z0-9{_CJK}]+")


def slugify(name: str) -> str:
    """Deterministic, filesystem-safe slug.

    ``AgiBot`` -> ``agibot``
    ``Unitree Robotics`` -> ``unitree-robotics``
    ``宇树科技`` -> ``宇树科技`` (preserved; already path-safe)

    If normalisation had to drop characters we append a short digest of the
    original so two different inputs can never collide on one directory.
    """
    original = name.strip()
    if not original:
        raise ValueError("company name must not be empty")

    folded = unicodedata.normalize("NFKC", original).casefold()
    slug = _KEEP.sub("-", folded).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)

    # Did we lose anything other than separators/case?
    lossy = _KEEP.sub("", folded) != _KEEP.sub("", slug.replace("-", ""))
    if not slug:
        return "company-" + _digest(original)
    if lossy or len(slug) > 80:
        return slug[:80].strip("-") + "-" + _digest(original)
    return slug


def _digest(value: str, length: int = 8) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()[:length]


def run_timestamp(now: datetime | None = None) -> str:
    """Local-time run directory name, e.g. ``2026-09-02_174500``."""
    return (now or datetime.now()).strftime("%Y-%m-%d_%H%M%S")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_dotenv(path: str | Path = ".env") -> int:
    """Minimal .env loader — no dependency, does not overwrite real env vars."""
    p = Path(path)
    if not p.is_file():
        return 0
    loaded = 0
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value
            loaded += 1
    return loaded


_SECRET_HINTS = ("SECRET", "KEY", "TOKEN", "PASSWORD")


def redact(value: str | None) -> str:
    """Never print credentials; show only enough to identify a typo."""
    if not value:
        return "<unset>"
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}{'*' * 8}{value[-2:]}"


def scrub_env_mapping(mapping: dict[str, str]) -> dict[str, str]:
    return {
        k: (redact(v) if any(h in k.upper() for h in _SECRET_HINTS) else v)
        for k, v in mapping.items()
    }


def setup_logging(log_file: Path | None = None, verbose: bool = False) -> logging.Logger:
    """Terminal gets high-level progress; the file gets full detail."""
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()
    logger.propagate = False

    stream = logging.StreamHandler(sys.stderr)
    # Non-verbose: only real errors reach the terminal. Warnings still go
    # to logs/run.log, so progress output stays readable.
    stream.setLevel(logging.DEBUG if verbose else logging.ERROR)
    stream.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    logger.addHandler(stream)

    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-8s %(name)s %(message)s")
        )
        logger.addHandler(file_handler)

    return logger


def get_logger() -> logging.Logger:
    return logging.getLogger(LOGGER_NAME)


def say(message: str = "") -> None:
    """User-facing terminal output (stdout, unbuffered-ish)."""
    print(message, flush=True)


def add_file_handler(log_file: Path) -> None:
    """Attach a detailed file handler to the already-configured logger."""
    logger = get_logger()
    log_file.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(log_file, encoding="utf-8")
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-8s %(name)s %(message)s")
    )
    logger.addHandler(handler)
