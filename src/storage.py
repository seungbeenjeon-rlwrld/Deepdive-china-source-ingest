"""Storage backends.

Only :class:`LocalStorageBackend` is implemented. Google Drive / Notion / S3
would be new subclasses of :class:`StorageBackend`; nothing in the pipeline
needs to change to add them.

Everything is written UTF-8 with ``ensure_ascii=False`` so Chinese characters
survive round-tripping, and no run ever overwrites another: run directories are
timestamped and a collision gets a numeric suffix.
"""

from __future__ import annotations

import json
import os
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Optional

from .models import RunMetadata, SourceRecord
from .utils import get_logger, run_timestamp, slugify

METADATA_FILE = "metadata.json"
STAGE1_MD = "01_entity_discovery.md"
STAGE1_JSON = "01_entity_discovery.json"
STAGE2_MD = "02_sources.md"
STAGE2_JSON = "02_sources.json"
SWEEP_JSON = "03_search_sweep.json"
SWEEP_MD = "03_search_sweep.md"
RAW_STAGE1 = "raw_stage1_response.json"
RAW_STAGE2 = "raw_stage2_response.json"
RAW_SWEEP = "raw_search_sweep_responses.json"
RAW_SOURCES_DIR = "raw_sources"


class StorageBackend(ABC):
    """Destination for one research run's artefacts."""

    @abstractmethod
    def create_run(self, company: str, *, timestamp: Optional[str] = None) -> Path:
        """Allocate and return a fresh, non-colliding run location."""

    @abstractmethod
    def save(self, relative_path: str, content: str) -> Path:
        """Persist text content at ``relative_path`` inside the run."""

    @abstractmethod
    def save_json(self, relative_path: str, payload: Any) -> Path:
        """Persist a JSON-serialisable payload inside the run."""

    @abstractmethod
    def read(self, relative_path: str) -> str:
        """Read back text previously saved in the run (used by --resume)."""

    @abstractmethod
    def exists(self, relative_path: str) -> bool: ...

    @abstractmethod
    def write_metadata(self, metadata: RunMetadata) -> Path: ...


class LocalStorageBackend(StorageBackend):
    def __init__(self, root_dir: Path, run_dir: Optional[Path] = None) -> None:
        self.root_dir = Path(root_dir)
        self.run_dir: Optional[Path] = Path(run_dir) if run_dir else None
        self.log = get_logger()

    # -- run lifecycle ----------------------------------------------------
    def create_run(self, company: str, *, timestamp: Optional[str] = None) -> Path:
        slug = slugify(company)
        stamp = timestamp or run_timestamp()
        base = self.root_dir / slug
        candidate = base / stamp
        suffix = 1
        # Never clobber an existing run, even within the same second.
        while candidate.exists():
            suffix += 1
            candidate = base / f"{stamp}_{suffix}"
        candidate.mkdir(parents=True)
        (candidate / RAW_SOURCES_DIR).mkdir(exist_ok=True)
        (candidate / "logs").mkdir(exist_ok=True)
        self.run_dir = candidate
        self.log.debug("created run dir %s", candidate)
        return candidate

    def attach_run(self, run_dir: Path) -> Path:
        """Point the backend at an existing run directory (resume path)."""
        run_dir = Path(run_dir).expanduser().resolve()
        if not run_dir.is_dir():
            raise FileNotFoundError(f"run directory does not exist: {run_dir}")
        self.run_dir = run_dir
        (run_dir / RAW_SOURCES_DIR).mkdir(exist_ok=True)
        (run_dir / "logs").mkdir(exist_ok=True)
        return run_dir

    def _path(self, relative_path: str) -> Path:
        if self.run_dir is None:
            raise RuntimeError("no active run — call create_run() or attach_run() first")
        target = (self.run_dir / relative_path).resolve()
        # Defensive: keep everything inside the run directory.
        if not str(target).startswith(str(self.run_dir.resolve())):
            raise ValueError(f"refusing to write outside the run directory: {relative_path}")
        target.parent.mkdir(parents=True, exist_ok=True)
        return target

    # -- writes -----------------------------------------------------------
    def save(self, relative_path: str, content: str) -> Path:
        target = self._path(relative_path)
        self._atomic_write(target, content)
        self.log.debug("wrote %s (%d chars)", relative_path, len(content))
        return target

    def save_json(self, relative_path: str, payload: Any) -> Path:
        text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
        return self.save(relative_path, text + "\n")

    def save_source(self, record: SourceRecord, *, index: int) -> dict[str, str]:
        """Write one preserved source as both Markdown (with front matter) and JSON."""
        stem = f"{RAW_SOURCES_DIR}/source_{index:03d}"
        md = self.save(f"{stem}.md", record.to_markdown())
        js = self.save_json(f"{stem}.json", record.to_dict())
        return {"markdown": str(md), "json": str(js)}

    def write_metadata(self, metadata: RunMetadata) -> Path:
        return self.save_json(METADATA_FILE, metadata.to_dict())

    @staticmethod
    def _atomic_write(target: Path, content: str) -> None:
        """Write via a temp file + rename so a crash cannot leave a half file."""
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, target)

    # -- reads ------------------------------------------------------------
    def read(self, relative_path: str) -> str:
        return self._path(relative_path).read_text(encoding="utf-8")

    def exists(self, relative_path: str) -> bool:
        if self.run_dir is None:
            return False
        return (self.run_dir / relative_path).exists()

    def read_json(self, relative_path: str) -> Any:
        return json.loads(self.read(relative_path))

    def load_stage1(self) -> tuple[str, dict[str, Any] | None]:
        """Recover the full stage 1 text for resume.

        Prefers the raw API response (most faithful), then the stage 1 JSON,
        then the Markdown file. Raises if none is usable.
        """
        errors: list[str] = []

        if self.exists(STAGE1_JSON):
            try:
                data = self.read_json(STAGE1_JSON)
                text = (data or {}).get("text")
                if text:
                    return text, data
                errors.append(f"{STAGE1_JSON}: no 'text' field")
            except (ValueError, OSError) as exc:
                errors.append(f"{STAGE1_JSON}: {exc}")

        if self.exists(RAW_STAGE1):
            try:
                raw = self.read_json(RAW_STAGE1)
                choices = (raw.get("Response") or {}).get("Choices") or []
                if choices:
                    text = ((choices[0] or {}).get("Message") or {}).get("Content")
                    if text:
                        return text, {"text": text, "raw": raw, "recovered_from": RAW_STAGE1}
                errors.append(f"{RAW_STAGE1}: no Choices[0].Message.Content")
            except (ValueError, OSError, AttributeError, IndexError) as exc:
                errors.append(f"{RAW_STAGE1}: {exc}")

        if self.exists(STAGE1_MD):
            try:
                text = _strip_md_header(self.read(STAGE1_MD))
                if text.strip():
                    return text, {"text": text, "recovered_from": STAGE1_MD}
                errors.append(f"{STAGE1_MD}: file is empty")
            except OSError as exc:
                errors.append(f"{STAGE1_MD}: {exc}")

        raise FileNotFoundError(
            "could not recover a stage 1 result from "
            f"{self.run_dir}. Tried: " + "; ".join(errors or ["no stage 1 files present"])
        )

    def load_metadata(self) -> dict[str, Any] | None:
        if not self.exists(METADATA_FILE):
            return None
        try:
            return self.read_json(METADATA_FILE)
        except (ValueError, OSError) as exc:
            self.log.warning("could not read %s: %s", METADATA_FILE, exc)
            return None


_MD_HEADER_SENTINEL = "<!-- BEGIN PROVIDER OUTPUT -->"


def md_document(title: str, header_lines: list[str], body: str) -> str:
    """Wrap provider output in a small header without touching the body.

    The sentinel lets :func:`_strip_md_header` recover the exact original text
    on resume, so a Markdown file is a lossless carrier of stage 1 output.
    """
    lines = [f"# {title}", ""]
    lines.extend(header_lines)
    lines.extend(["", _MD_HEADER_SENTINEL, ""])
    return "\n".join(lines) + body.rstrip() + "\n"


def _strip_md_header(text: str) -> str:
    if _MD_HEADER_SENTINEL in text:
        return text.split(_MD_HEADER_SENTINEL, 1)[1].lstrip("\n")
    return text
