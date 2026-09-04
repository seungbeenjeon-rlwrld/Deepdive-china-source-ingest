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

        self.binary = settings.binary or "claude"
        resolved = shutil.which(self.binary)
        if resolved is None:
            raise ProviderError(
                f"the Claude CLI ({self.binary!r}) was not found on PATH.",
                hint=INSTALL_HINT + "\n  Or set provider to zhipu / mock instead.",
            )
        self.binary = resolved
        self.log.debug("claude cli: %s", self.binary)

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
