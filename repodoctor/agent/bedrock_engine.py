"""BedrockEngine: a ReasoningEngine backed by Amazon Bedrock via the Strands Agents SDK.

Design:
  * Investigation is a tool-using Strands agent with three read-only tools; it answers with a
    structured Hypothesis.
  * Patching is ONE tool-less call: the model sees the real file and returns a small edit
    (old text -> new text). WE build the unified diff, so the model never writes diff syntax.
  * Costs are bounded: per-call max_tokens, a cap on model calls per phase, a token budget per
    session, clipped tool results, and a hard stop for runaway loops (a Strands hook).
  * Strands/boto3 are imported lazily, so the rest of RepoDoctor works without them installed.
"""

from __future__ import annotations

import logging
import os
import time
import weakref
from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Mapping

from pydantic import BaseModel, Field

from repodoctor.tools.edits import EditError, make_edit_diff
from repodoctor.tools.paths import (
    InvalidPathError,
    is_protected_path,
    validate_repo_path,
)

from .prompts import (
    INVESTIGATE_SYSTEM,
    PATCH_SYSTEM,
    investigation_prompt,
    patch_prompt,
)
from .reasoning import (
    AgentTools,
    InvestigationFailed,
    ModelUnavailableError,
    PatchGenerationError,
    ReasoningError,
)
from .schemas import Hypothesis, InvestigationContext, PatchContext, PatchProposal

logger = logging.getLogger("repodoctor.bedrock")

AgentFactory = Callable[..., Any]  # (*, system_prompt, tools, hooks) -> strands.Agent


# ================================================================== settings & usage
@dataclass(frozen=True)
class BedrockSettings:
    model_id: str
    region: str = "us-east-1"
    max_tokens: int = 2048  # per model response
    max_model_calls_investigate: int = 12
    max_model_calls_patch: int = 6
    max_session_tokens: int = 200_000  # input + output, across the whole session
    max_edit_tries: int = (
        2  # tries to get an edit that applies to the real file, per patch attempt
    )
    max_tool_result_chars: int = 12_000
    price_input_per_mtok: float | None = None  # optional, for cost display only
    price_output_per_mtok: float | None = None

    def __post_init__(self) -> None:
        if not self.model_id.strip():
            raise ValueError("model_id must not be empty")
        for name in (
            "max_tokens",
            "max_model_calls_investigate",
            "max_model_calls_patch",
            "max_session_tokens",
            "max_edit_tries",
            "max_tool_result_chars",
        ):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} must be >= 1")

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "BedrockSettings":
        source = os.environ if env is None else env
        model_id = source.get("BEDROCK_MODEL_ID", "").strip()
        if not model_id:
            raise ValueError(
                "BEDROCK_MODEL_ID is not set. Set it to an inference profile id your account can call, "
                "for example in PowerShell: $env:BEDROCK_MODEL_ID = 'global.anthropic.claude-haiku-4-5-20251001-v1:0'"
            )
        values: dict[str, Any] = {"model_id": model_id}
        region = source.get("AWS_REGION", "").strip()
        if region:
            values["region"] = region
        for var, name, cast in (
            ("REPODOCTOR_MODEL_MAX_TOKENS", "max_tokens", int),
            ("REPODOCTOR_MAX_SESSION_TOKENS", "max_session_tokens", int),
            ("REPODOCTOR_PRICE_INPUT_PER_MTOK", "price_input_per_mtok", float),
            ("REPODOCTOR_PRICE_OUTPUT_PER_MTOK", "price_output_per_mtok", float),
        ):
            raw = source.get(var, "").strip()
            if raw:
                try:
                    values[name] = cast(raw)
                except ValueError as exc:
                    raise ValueError(f"{var} has an invalid value: {raw!r}") from exc
        return cls(**values)


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    model_calls: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def cost_usd(self, settings: BedrockSettings) -> float | None:
        if (
            settings.price_input_per_mtok is None
            or settings.price_output_per_mtok is None
        ):
            return None
        return (
            self.input_tokens * settings.price_input_per_mtok
            + self.output_tokens * settings.price_output_per_mtok
        ) / 1_000_000


# ================================================================== structured outputs
class HypothesisOutput(BaseModel):
    """Root cause of the failing test."""

    file: str = Field(
        description="Repo-relative path of the SOURCE file that must change (never a test file)"
    )
    line: int = Field(
        default=0, ge=0, description="Line number of the faulty code; 0 if unknown"
    )
    description: str = Field(
        min_length=1,
        max_length=1500,
        description="1-2 sentences explaining the root cause",
    )
    evidence: list[str] = Field(
        default_factory=list, description="Up to 3 short code snippets you actually saw"
    )
    confidence: Literal["low", "medium", "high"] = "medium"


class EditProposal(BaseModel):
    """One small edit to one file."""

    file: str = Field(description="Repo-relative path of the file to edit")
    old_text: str = Field(
        min_length=1,
        max_length=6000,
        description="Exact text to replace, copied verbatim from the file; must appear exactly once",
    )
    new_text: str = Field(max_length=12000, description="Replacement text")
    summary: str = Field(
        default="", max_length=300, description="One sentence describing the fix"
    )


# ================================================================== tools shown to the model
def _clip(text: str, limit: int) -> str:
    return (
        text
        if len(text) <= limit
        else text[:limit] + f"\n...[truncated {len(text) - limit} characters]"
    )


def _error(out: Any) -> str:
    hint = (
        " Stop using tools and give your structured answer now."
        if out.error_code == "tool_budget_exceeded"
        else ""
    )
    return f"ERROR [{out.error_code}]: {out.error}.{hint}"


def build_investigation_tools(
    tools: AgentTools, settings: BedrockSettings
) -> list[Any]:
    from strands import tool  # lazy: Strands is only needed for real model runs

    limit = settings.max_tool_result_chars

    @tool
    def list_files(path: str = ".") -> str:
        """List the files in the repository under a directory.

        Args:
            path: Repo-relative directory. Use "." for the repository root.
        """
        out = tools.list_files({"path": path, "max_files": 150})
        if not out.ok:
            return _error(out)
        body = "\n".join(out.files) or "(no files)"
        return _clip(body + ("\n[list truncated]" if out.truncated else ""), limit)

    @tool
    def read_file(path: str, start_line: int = 1, end_line: int = 0) -> str:
        """Read lines of a text file. Returns numbered lines.

        Args:
            path: Repo-relative file path.
            start_line: First line to read (1-based).
            end_line: Last line to read; 0 means about 200 lines from start_line.
        """
        end = end_line if end_line >= start_line else start_line + 199
        out = tools.read_file({"path": path, "start_line": start_line, "end_line": end})
        if not out.ok:
            return _error(out)
        header = (
            f"{out.path} (lines {out.start_line}-{out.end_line} of {out.total_lines})"
        )
        footer = "\n[more lines follow]" if out.has_more else ""
        return _clip(f"{header}\n{out.numbered()}{footer}", limit)

    @tool
    def search_code(pattern: str, path: str = ".", regex: bool = False) -> str:
        """Search the repository for text. Returns 'path:line: text' matches.

        Args:
            pattern: Text to find (case-insensitive).
            path: Repo-relative directory or file to search; "." for everything.
            regex: Treat the pattern as a regular expression instead of plain text.
        """
        out = tools.search_code(
            {"pattern": pattern, "path": path, "literal": not regex, "max_results": 30}
        )
        if not out.ok:
            return _error(out)
        rows = [f"{m.path}:{m.line}: {m.text}" for m in out.matches] or ["(no matches)"]
        return _clip(
            "\n".join(rows) + ("\n[more matches not shown]" if out.truncated else ""),
            limit,
        )

    return [list_files, read_file, search_code]


# ================================================================== runaway-loop guard
class ModelCallLimitExceeded(ReasoningError):
    pass


class _CallLimiter:
    """Strands hook provider: abort when the model has been called too many times."""

    def __init__(self, limit: int) -> None:
        self.limit, self.count, self.exceeded = limit, 0, False

    def register_hooks(self, registry: Any, **kwargs: Any) -> None:
        from strands.hooks import BeforeModelCallEvent

        registry.add_callback(BeforeModelCallEvent, self._on_call)

    def _on_call(self, event: Any) -> None:
        self.count += 1
        if self.count > self.limit:
            self.exceeded = True
            raise ModelCallLimitExceeded(f"more than {self.limit} model calls")


# ================================================================== the engine
def _default_agent_factory(settings: BedrockSettings) -> AgentFactory:
    def make(*, system_prompt: str, tools: list[Any], hooks: list[Any]) -> Any:
        from strands import Agent
        from strands.models import BedrockModel

        # Auth comes from the environment: AWS_BEARER_TOKEN_BEDROCK (Bedrock API key) or normal AWS credentials.
        model = BedrockModel(
            model_id=settings.model_id,
            region_name=settings.region,
            max_tokens=settings.max_tokens,
        )
        return Agent(
            model=model,
            tools=tools,
            system_prompt=system_prompt,
            callback_handler=None,
            hooks=hooks,
        )

    return make


def _causes(exc: BaseException) -> list[BaseException]:
    chain, seen, cur = [], set(), exc
    while cur is not None and id(cur) not in seen:
        chain.append(cur)
        seen.add(id(cur))
        cur = (
            getattr(cur, "original_exception", None) or cur.__cause__ or cur.__context__
        )
    return chain


class BedrockEngine:
    def __init__(
        self, settings: BedrockSettings, *, agent_factory: AgentFactory | None = None
    ) -> None:
        self.settings = settings
        self._factory = agent_factory or _default_agent_factory(settings)
        self._usage: dict[str, Usage] = {}
        # keyed by the agent OBJECT (weakly): id() values are reused after garbage collection
        self._seen: weakref.WeakKeyDictionary[Any, tuple[int, int]] = (
            weakref.WeakKeyDictionary()
        )

    # ---- usage ----
    def usage_for(self, session_id: str) -> Usage:
        return self._usage.setdefault(session_id, Usage())

    def _record(
        self,
        session_id: str,
        kind: str,
        agent: Any,
        started: float,
        *,
        failed: bool = False,
    ) -> None:
        try:
            acc = agent.event_loop_metrics.accumulated_usage
            now_in, now_out = (
                int(acc.get("inputTokens", 0)),
                int(acc.get("outputTokens", 0)),
            )
        except Exception:  # noqa: BLE001 - usage is best-effort telemetry
            return
        prev_in, prev_out = self._seen.get(agent, (0, 0))
        self._seen[agent] = (now_in, now_out)
        if failed and (now_in, now_out) == (prev_in, prev_out):
            return  # the call was rejected before using any tokens (e.g. access denied)
        usage = self.usage_for(session_id)
        usage.input_tokens += now_in - prev_in
        usage.output_tokens += now_out - prev_out
        usage.model_calls += 1
        logger.info(
            "model_call session=%s kind=%s input_tokens=%d output_tokens=%d duration=%.2fs session_total=%d",
            session_id,
            kind,
            now_in - prev_in,
            now_out - prev_out,
            time.monotonic() - started,
            usage.total_tokens,
        )

    # ---- error mapping ----
    @staticmethod
    def _map_error(
        exc: BaseException, limiter: _CallLimiter | None, fail: type[ReasoningError]
    ) -> BaseException:
        from strands.types.exceptions import (
            ContextWindowOverflowException,
            MaxTokensReachedException,
            ModelThrottledException,
            StructuredOutputException,
        )

        chain = _causes(exc)
        if limiter is not None and limiter.exceeded:
            return fail(
                f"The model needed more than {limiter.limit} calls; stopping to control cost."
            )
        try:
            from botocore.exceptions import BotoCoreError, ClientError

            service_errors: tuple[type[BaseException], ...] = (
                ClientError,
                BotoCoreError,
            )
        except ImportError:  # pragma: no cover
            service_errors = ()
        for item in chain:
            if isinstance(item, ModelThrottledException) or (
                service_errors and isinstance(item, service_errors)
            ):
                code = (
                    getattr(item, "response", {}).get("Error", {}).get("Code")
                    if hasattr(item, "response")
                    else None
                )
                logger.warning(
                    "model_unavailable type=%s code=%s", type(item).__name__, code
                )
                return ModelUnavailableError(
                    f"{type(item).__name__}{f' ({code})' if code else ''}"
                )
        for item in chain:
            if isinstance(
                item,
                (
                    StructuredOutputException,
                    MaxTokensReachedException,
                    ContextWindowOverflowException,
                ),
            ):
                return fail(
                    f"The model did not produce a usable answer ({type(item).__name__})."
                )
        return exc

    # ---- one model invocation ----
    def _run(
        self,
        session_id: str,
        kind: str,
        agent: Any,
        prompt: str,
        output_model: type[BaseModel],
        limiter: _CallLimiter,
        fail: type[ReasoningError],
    ) -> BaseModel:
        usage = self.usage_for(session_id)
        if usage.total_tokens >= self.settings.max_session_tokens:
            raise fail(
                f"Token budget for this session is used up ({usage.total_tokens} tokens)."
            )
        started = time.monotonic()
        try:
            result = agent(prompt, structured_output_model=output_model)
        except Exception as exc:  # noqa: BLE001
            self._record(session_id, kind, agent, started, failed=True)
            mapped = self._map_error(exc, limiter, fail)
            if mapped is exc:
                raise
            raise mapped from exc
        self._record(session_id, kind, agent, started)
        out = getattr(result, "structured_output", None)
        if not isinstance(out, output_model):
            raise fail(
                "The model did not return the structured answer that was requested."
            )
        return out

    # ---- ReasoningEngine ----
    def investigate(
        self, context: InvestigationContext, tools: AgentTools
    ) -> Hypothesis:
        limiter = _CallLimiter(self.settings.max_model_calls_investigate)
        agent = self._factory(
            system_prompt=INVESTIGATE_SYSTEM,
            tools=build_investigation_tools(tools, self.settings),
            hooks=[limiter],
        )
        out = self._run(
            context.session_id,
            "investigate",
            agent,
            investigation_prompt(context),
            HypothesisOutput,
            limiter,
            InvestigationFailed,
        )
        assert isinstance(out, HypothesisOutput)
        try:
            path = validate_repo_path(out.file)
        except InvalidPathError as exc:
            raise InvestigationFailed(
                f"The model named an invalid path: {exc}"
            ) from exc
        if is_protected_path(path):
            raise InvestigationFailed(
                f"The model blamed a test or test-configuration file ({path}). RepoDoctor does not modify tests."
            )
        return Hypothesis(
            file=path,
            line=out.line or None,
            description=out.description,
            evidence=out.evidence[:3],
            confidence=out.confidence,
        )

    def propose_patch(self, context: PatchContext, tools: AgentTools) -> PatchProposal:
        hypothesis_path = context.hypothesis.file
        files: dict[str, str] = {}

        def load(path: str) -> str:
            if path not in files:
                read = tools.read_file_complete({"path": path})
                if not read.ok:
                    raise PatchGenerationError(f"Could not read {path}: {read.error}")
                files[path] = read.content + (
                    "\n" if read.ends_with_newline and read.content else ""
                )
            return files[path]

        content = load(hypothesis_path)
        limiter = _CallLimiter(self.settings.max_model_calls_patch)
        agent = self._factory(system_prompt=PATCH_SYSTEM, tools=[], hooks=[limiter])
        prompt = patch_prompt(context, hypothesis_path, content)
        problem = "no edit was produced"
        for _ in range(self.settings.max_edit_tries):
            edit = self._run(
                context.session_id,
                "patch",
                agent,
                prompt,
                EditProposal,
                limiter,
                PatchGenerationError,
            )
            assert isinstance(edit, EditProposal)
            try:
                path = validate_repo_path(edit.file)
                if is_protected_path(path):
                    raise EditError(
                        "protected_path",
                        "Tests and test configuration must not be edited. Fix the source code.",
                    )
                diff = make_edit_diff(path, load(path), edit.old_text, edit.new_text)
                return PatchProposal(diff=diff, summary=edit.summary or f"Edit {path}")
            except InvalidPathError as exc:
                problem = f"invalid path: {exc}"
            except EditError as exc:
                problem = str(exc)
            prompt = (
                f"Your edit could not be applied: {problem} Return a corrected edit."
            )
        raise PatchGenerationError(f"The model's edit could not be applied: {problem}")
