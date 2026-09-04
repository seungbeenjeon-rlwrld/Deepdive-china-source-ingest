"""Claude Code CLI as an LLM provider.

Why this exists: the pipeline needs a model to execute the two research
prompts, and every Chinese-model option turned out to cost something the user
does not have — Tencent Hunyuan needs a mainland Chinese ID, and Zhipu's free
tier proved unreliable and mislabelled evidence (measured: 58 records claimed
VERBATIM_PARTIAL_TEXT for 32-161 char snippets). Shelling out to an already
installed and authenticated `claude` removes the API-key step entirely and
produces better-structured output.

The trade-off, stated plainly: this consumes the same subscription allowance as
interactive Claude Code use. A batch over many companies can slow down or block
the operator's own session. For heavy scheduled runs, a metered API provider is
the better fit.

Text generation only. The pipeline still supplies its own retrieval
(`research.retrieval_injection`) so the evidence handed to the model stays
logged and auditable, rather than depending on whatever the CLI searched for
on its own.
"""

from __future__ import annotations

import shutil
import subprocess
from typing import Any, Optional

from .config import ClaudeCliSettings
from .models import ResearchResponse
from .provider import (
    EmptyResponseError,
    ProviderError,
    ResearchProvider,
    TimeoutError_,
)
from .utils import get_logger

# The installer puts the binary here and warns that it may not be on PATH, so
# look for it rather than failing on a fresh install.
FALLBACK_PATHS = (
    "~/.local/bin/claude",
    "~/.claude/local/claude",
    "/usr/local/bin/claude",
    "/opt/homebrew/bin/claude",
)

# The CLI reports some failures on stdout with **exit code 0** — measured:
# "Not logged in · Please run /login" exits 0. Checking the return code alone
# would store that string as research output, so the text is screened too.
# Only applied to short outputs; real stage output runs to thousands of chars.
_FAILURE_MARKERS = (
    "not logged in",
    "please run /login",
    "invalid api key",
    "credit balance is too low",
    "usage limit reached",
    "rate limit",
    "authentication_error",
)
_FAILURE_SCAN_LIMIT = 600

INSTALL_HINT = (
    "Install it with:  curl -fsSL https://claude.ai/install.sh | bash\n"
    "  Then check it is on PATH and authenticated:  claude --version"
)


class ClaudeCliProvider(ResearchProvider):
    """Runs a prompt through `claude -p`, reading the prompt from stdin."""

    name = "claude-cli"

    def __init__(self, settings: ClaudeCliSettings) -> None:
        self.settings = settings
        self.log = get_logger()

        self.binary = self._resolve(settings.binary or "claude")
        self.log.debug("claude cli: %s", self.binary)

    @staticmethod
    def _resolve(binary: str) -> str:
        """Find the CLI on PATH, then at the installer's known locations."""
        import os

        found = shutil.which(binary)
        if found:
            return found
        if os.sep in binary:  # an explicit path was given and does not exist
            raise ProviderError(
                f"the Claude CLI was not found at {binary!r}.", hint=INSTALL_HINT
            )
        for candidate in FALLBACK_PATHS:
            path = os.path.expanduser(candidate)
            if os.access(path, os.X_OK):
                return path
        raise ProviderError(
            f"the Claude CLI ({binary!r}) was not found on PATH or at "
            f"{', '.join(FALLBACK_PATHS)}.",
            hint=INSTALL_HINT + "\n  Or set provider to zhipu / mock instead.",
        )

    @staticmethod
    def _failure_in_output(text: str) -> Optional[str]:
        """Detect a CLI error reported on stdout with a zero exit code."""
        if len(text) > _FAILURE_SCAN_LIMIT:
            return None
        lowered = text.lower()
        return next((m for m in _FAILURE_MARKERS if m in lowered), None)

    @property
    def supports_search(self) -> bool:
        # Search-capable in principle, but the pipeline deliberately supplies
        # its own retrieval so the evidence trail stays auditable. Pair this
        # with `search_sweep.provider: serpapi`.
        return False

    def _argv(self) -> list[str]:
        argv = [self.binary, "-p"]
        if self.settings.model:
            argv += ["--model", self.settings.model]
        # Prompts are self-contained and retrieval is injected, so the CLI's own
        # tools are not needed. Restricting them keeps runs reproducible and
        # avoids unlogged searches leaking into the evidence.
        if self.settings.disallowed_tools:
            argv += ["--disallowedTools", self.settings.disallowed_tools]
        if self.settings.extra_args:
            argv += list(self.settings.extra_args)
        return argv

    def run_research(self, prompt: str, *, label: str = "") -> ResearchResponse:
        argv = self._argv()
        self.log.info("claude cli: %s (prompt %d chars)", " ".join(argv[1:]), len(prompt))

        try:
            completed = subprocess.run(
                argv,
                input=prompt,
                capture_output=True,
                text=True,
                timeout=self.settings.timeout_seconds,
                cwd=self.settings.cwd or None,
            )
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError_(
                f"claude cli timed out after {self.settings.timeout_seconds}s for "
                f"{label or 'request'}."
            ) from exc
        except OSError as exc:
            raise ProviderError(f"could not run {self.binary}: {exc}", hint=INSTALL_HINT) from exc

        stderr = (completed.stderr or "").strip()
        text = (completed.stdout or "").strip()

        if completed.returncode != 0:
            detail = stderr or text or f"exit code {completed.returncode}"
            hint = ""
            lowered = detail.lower()
            if "login" in lowered or "auth" in lowered or "unauthor" in lowered:
                hint = "Run `claude` once interactively to authenticate, then retry."
            elif "limit" in lowered or "quota" in lowered or "usage" in lowered:
                hint = (
                    "Subscription allowance reached. This provider shares your "
                    "interactive Claude Code limit — wait, or switch to "
                    "provider: zhipu for this run."
                )
            raise ProviderError(
                f"claude cli failed for {label or 'request'}: {detail[:400]}", hint=hint
            )

        # Exit code 0 is not proof of success for this CLI.
        marker = self._failure_in_output(text)
        if marker:
            hint = "Run `claude` once interactively and complete /login, then retry."
            if "limit" in marker:
                hint = (
                    "Subscription allowance reached. This provider shares your "
                    "interactive Claude Code limit — wait, or switch to "
                    "provider: zhipu for this run."
                )
            raise ProviderError(
                f"claude cli reported a failure on stdout for {label or 'request'} "
                f"(exit 0, matched {marker!r}): {text[:200]}",
                hint=hint,
            )

        if not text:
            raise EmptyResponseError(
                f"claude cli returned no output for {label or 'request'}"
                + (f" (stderr: {stderr[:200]})" if stderr else "")
            )

        warnings: list[str] = []
        if stderr:
            warnings.append(f"claude cli wrote to stderr: {stderr[:300]}")

        return ResearchResponse(
            text=text,
            # There is no structured API envelope; keep what we can reconstruct.
            raw={
                "provider": self.name,
                "argv": argv[1:],
                "returncode": completed.returncode,
                "stdout_chars": len(text),
                "stderr": stderr or None,
                "note": (
                    "claude cli returns plain text, so there is no API response "
                    "envelope to preserve. The verbatim output is in 'text'."
                ),
            },
            provider=self.name,
            model=self.settings.model,
            search_results=[],
            usage=None,
            request_id=None,
            finish_reason="stop",
            warnings=warnings,
        )

    def search(self, query, *, count=20, site=None, industry=None, freshness=None, mode=2):
        return {
            "query": query, "pages": [], "raw": None, "supported": False,
            "note": (
                "claude-cli is used for text generation only; point "
                "search_sweep.provider at serpapi so retrieval stays auditable."
            ),
        }

    def describe(self) -> dict[str, Any]:
        return {
            "provider": self.name,
            "model": self.settings.model,
            "endpoints": {"inference": f"{self.binary} -p", "search": None},
            "note": (
                "Uses the local Claude Code CLI and its existing authentication, so no "
                "API key is needed. Shares the interactive subscription allowance — a "
                "large batch can slow the operator's own session."
            ),
        }
