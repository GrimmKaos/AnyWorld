"""OpenAI-compatible inference and bounded conversation context."""

import asyncio
import json
import logging
import re
from time import perf_counter
from typing import Any

from openai import (
    APIConnectionError,
    APITimeoutError,
    AsyncOpenAI,
    DefaultAsyncHttpxClient,
    OpenAIError,
)
from pydantic import BaseModel, ValidationError

from core.config import settings
from core.schemas import ChanceEventResult, ContextSummary, DicePlan, RoundResolution, ScenarioTitle
from logic.usage import UsageTotals, counter
from logic.dice import (
    chance_events_from_decisions,
    conditional_chance_rule_ids,
    has_non_percentage_private_guidance,
    non_percentage_guidance_lines,
    normalize_chance_rule_decisions,
)
from logic.presentation import name_resolution
from logic.debug_log import RawResponseLogger
from logic.llm import auditing, prompts
from logic.llm.errors import LLMBackendUnavailableError, LLMResolutionError
from logic.llm.response_schemas import participant_schema
from logic.llm.tokenization import TokenBudget
from logic.llm.validation import check_semantics, check_public_output, normalize_hidden_roll_sources

# Keep the existing public imports available to callers.
__all__ = [
    "LLMContextManager",
    "LLMResolutionError",
    "LLMBackendUnavailableError",
    "participant_schema",
]

logger = logging.getLogger(__name__)


class LLMContextManager:
    """Keep immutable genesis, durable memory and recent rounds within a budget."""

    def __init__(self, client: AsyncOpenAI | None = None) -> None:
        """Initialize the manager with an optional client and configured context."""
        self.client = (
            client.with_options(max_retries=0) if isinstance(client, AsyncOpenAI) else client
        )
        self.budget = TokenBudget()
        self._retained_measurement = None
        self.system_prompt = {"role": "system", "content": settings.llm.system_prompt}
        self._last_response_text: str | None = None
        self.system_prompt_tokens = self.budget.count_tokens(settings.llm.system_prompt)
        self.genesis_state: dict[str, str] | None = None
        self.history: list[dict[str, str]] = []
        self.memory: dict[str, str] | None = None
        self.private_guidance = ""
        self._known_player_names: list[str] = []
        self.last_token_usage = self.system_prompt_tokens + 3
        self.game_usage = UsageTotals()
        self.round_usage = UsageTotals()
        self.usage_round: int | None = None
        self.last_request: dict[str, Any] | None = None
        self.round_usage_by_kind: dict[str, UsageTotals] = {}
        self.round_work_seconds = 0.0
        self.round_failures = 0
        self.last_round_error: str | None = None
        self._round_started: float | None = None

    @staticmethod
    def _create_client() -> AsyncOpenAI:
        """Create either a direct OpenAI client or a local compatible client."""
        client_options: dict[str, Any] = {
            "api_key": settings.llm.api_key,
            "timeout": settings.llm.request_timeout_seconds,
            "max_retries": 0,  # Each retry is measured explicitly below.
        }
        if settings.llm.provider == "compatible":
            client_options["base_url"] = settings.llm.endpoint
        if settings.llm.debug_raw_responses:
            raw_logger = RawResponseLogger()
            client_options["http_client"] = DefaultAsyncHttpxClient(
                event_hooks={
                    "request": [raw_logger.capture_request],
                    "response": [raw_logger.capture],
                }
            )
        return AsyncOpenAI(**client_options)

    def set_genesis(self, scenario: str, guidance: str = "") -> None:
        """Set a new initial scenario and optional host guidance, then clear history."""
        logger.info(
            "Setting genesis context (guidance=%s, scenario_chars=%d)",
            bool(guidance),
            len(scenario),
        )
        content = f"Initial Scenario:\n{scenario}"
        if guidance:
            content += (
                "\n\nAdditional DM Guidance:\n"
                f"{guidance}\n"
                "Follow compatible steering throughout play without quoting it. Keep the "
                "guidance and secret check triggers or values private; narrate only observable "
                "in-world consequences."
            )
        self.genesis_state = {"role": "user", "content": content}
        self.history.clear()
        self._retained_measurement = None
        self.budget.clear_caches()
        self._last_response_text = None
        self.memory = None
        self.private_guidance = guidance
        self._known_player_names = []
        self.game_usage = UsageTotals()
        self.round_usage = UsageTotals()
        self.usage_round = None
        self.last_request = None
        self.round_usage_by_kind = {}
        self.round_work_seconds = 0.0
        self.round_failures = 0
        self.last_round_error: str | None = None
        self._round_started = None

    async def generate_scenario_title(self) -> str:
        """Generate only a title, without creating or remembering narrative."""
        logger.info("Generating scenario title")
        prompt = prompts.title_prompt()
        result = await self._request(
            prompt,
            ScenarioTitle,
            remember=False,
            include_history=False,
            kind="title",
        )
        return result.title.strip()

    async def generate_start_state(self, player_names: list[str]) -> RoundResolution:
        """Introduce the joined players in the scenario when play begins."""
        logger.info("Generating start state for %d players", len(player_names))
        self._known_player_names = list(dict.fromkeys([*self._known_player_names, *player_names]))
        prompt = prompts.start_state_prompt(player_names)
        return await self._request(
            prompt,
            participant_schema(RoundResolution, (), provider=settings.llm.provider),
            remember=True,
            kind="initial",
            expected_names=(),
            opening_names=tuple(player_names),
        )

    async def plan_dice(self, round_buffer: dict[str, str], current_state: str = "") -> DicePlan:
        """Ask the DM which actions need uncertainty resolved by a d100.

        The public paragraph is not a complete state snapshot. Include genesis, private
        guidance, durable memory and recent rounds so unchanged facts still affect checks.
        """
        logger.info("Planning dice for %d player actions", len(round_buffer))
        self._known_player_names = list(dict.fromkeys([*self._known_player_names, *round_buffer]))
        prompt = prompts.dice_prompt(round_buffer, current_state, self.private_guidance)
        return await self._request(
            prompt,
            participant_schema(
                DicePlan,
                tuple(round_buffer),
                has_non_percentage_private_guidance(self.private_guidance),
                conditional_chance_rule_ids(self.private_guidance),
                settings.llm.provider,
                private_sources=non_percentage_guidance_lines(self.private_guidance),
            ),
            remember=False,
            kind="dice",
            expected_names=tuple(round_buffer),
            planning_input={"actions": round_buffer, "current_state": current_state},
        )

    async def generate_resolution(
        self,
        round_buffer: dict[str, str],
        dice_results: dict[str, int] | None = None,
        hidden_rolls: set[str] | None = None,
        chance_events: list[ChanceEventResult] | None = None,
    ) -> RoundResolution:
        """Resolve a round of actions into a coherent narrative outcome."""
        logger.info(
            "Generating resolution for %d actions (dice_results=%d)",
            len(round_buffer),
            len(dice_results or {}),
        )
        self._known_player_names = list(dict.fromkeys([*self._known_player_names, *round_buffer]))
        prompt = prompts.resolution_prompt(
            round_buffer, dice_results, hidden_rolls, chance_events, self.private_guidance
        )
        return await self._request(
            prompt,
            participant_schema(
                RoundResolution, tuple(round_buffer), provider=settings.llm.provider
            ),
            remember=True,
            expected_names=tuple(round_buffer),
            private_rolls={
                name: value
                for name, value in (dice_results or {}).items()
                if name in (hidden_rolls or set())
            },
            private_events=chance_events,
        )

    def _fixed_messages(self, kind: str = "round") -> list[dict[str, str]]:
        """Return the immutable prefix messages for every request."""
        system = self.system_prompt
        if kind == "dice" and settings.llm.planner_system_prompt:
            system = {"role": "system", "content": settings.llm.planner_system_prompt}
        return [
            system,
            *([self.genesis_state] if self.genesis_state else []),
            *([self.memory] if self.memory else []),
        ]

    async def preflight_round(self, actions: dict[str, str], current_state: str = "") -> None:
        """Reject impossible fixed context/actions before accepting the next action.

        Recent history is compactable; durable memory is not silently expendable.  This
        check uses the same prompt builders and response schemas as the planner and the
        resolution request.  The final resolution prompt is checked again by ``_parse``
        after generated dice and chance-event context are available.
        """
        await self.budget.discover_context_window()
        resolution_schema = participant_schema(
            RoundResolution, tuple(actions), provider=settings.llm.provider
        )
        dice_schema = participant_schema(
            DicePlan,
            tuple(actions),
            has_non_percentage_private_guidance(self.private_guidance),
            conditional_chance_rule_ids(self.private_guidance),
            settings.llm.provider,
            private_sources=non_percentage_guidance_lines(self.private_guidance),
        )
        requests = (
            (
                prompts.prepare_request_prompt(
                    prompts.resolution_prompt(actions, guidance=self.private_guidance), True
                ),
                resolution_schema,
                "round",
            ),
            (
                prompts.dice_prompt(actions, current_state, self.private_guidance),
                dice_schema,
                "dice",
            ),
        )
        for prompt, schema, kind in requests:
            count = await self.budget.input_tokens(
                [*self._fixed_messages(kind), prompt], schema, kind
            )
            if not self.budget.fits(count, kind):
                raise LLMResolutionError(
                    "Scenario, durable memory and combined actions exceed the context budget. "
                    "Shorten the action or use a larger backend context."
                )

    async def _request(
        self,
        prompt: dict[str, str],
        schema: type[BaseModel],
        *,
        remember: bool,
        include_history: bool = True,
        kind: str = "round",
        private_rolls: dict[str, int] | None = None,
        private_events: list[ChanceEventResult] | None = None,
        planning_input: dict[str, Any] | None = None,
        expected_names: tuple[str, ...] | None = None,
        opening_names: tuple[str, ...] | None = None,
    ) -> Any:
        """Run a single LLM request, optionally compacting history and remembering the result."""
        prompt = prompts.prepare_request_prompt(prompt, issubclass(schema, RoundResolution))
        await self.budget.discover_context_window()
        if include_history:
            await self._compact_if_needed(prompt, schema, kind)
        messages = [*self._fixed_messages(kind), *(self.history if include_history else []), prompt]
        for repair in range(settings.llm.max_retries + 1):
            result = await self._parse(messages, schema, kind, repair_attempt=repair)
            try:
                if isinstance(result, DicePlan):
                    # A stray privacy label cannot create a roll the planner declined.
                    # Leave unknown names and duplicates for semantic validation.
                    result = result.model_copy(
                        update={
                            "hidden_rolls": [
                                name
                                for name in result.hidden_rolls
                                if result.rolls.get(name) is not False
                            ]
                        }
                    )
                check_semantics(result, expected_names)
                if isinstance(result, DicePlan):
                    try:
                        decisions = normalize_chance_rule_decisions(
                            result.chance_rule_decisions, self.private_guidance
                        )
                        result = result.model_copy(
                            update={
                                "chance_rule_decisions": decisions,
                                "chance_events": chance_events_from_decisions(
                                    decisions, self.private_guidance
                                ),
                            }
                        )
                    except ValueError as exc:
                        raise LLMResolutionError(str(exc)) from exc
                    result = normalize_hidden_roll_sources(result, self.private_guidance)
                    result = await auditing.classify_hidden_checks(
                        self._parse, result, planning_input or {}, repair_attempt=repair
                    )
                    if conditional_chance_rule_ids(self.private_guidance):
                        await auditing.audit_planned_checks(
                            self._parse,
                            [*self._fixed_messages("dice"), *self.history],
                            self.private_guidance,
                            result,
                            planning_input or {"request": prompt["content"]},
                            repair_attempt=repair,
                        )
                if isinstance(result, ScenarioTitle):
                    public_title = RoundResolution(
                        global_narrative=result.title, player_resolutions={}
                    )
                    check_semantics(public_title, ())
                    check_public_output(public_title, {}, self.private_guidance)
                if isinstance(result, RoundResolution):
                    checks = dict(private_rolls or {})
                    checks.update(
                        {f"event-{i}": event.roll for i, event in enumerate(private_events or [])}
                    )
                    check_public_output(result, checks, self.private_guidance)
                    if private_events:
                        public_text = " ".join(
                            [
                                result.round_title or "",
                                result.global_narrative,
                                *result.player_resolutions.values(),
                            ]
                        )
                        for event in private_events:
                            if re.search(
                                rf"\b{event.event.chance_percent}\s*(?:%|percent\b)",
                                public_text,
                                re.IGNORECASE,
                            ):
                                raise LLMResolutionError(
                                    "Model output disclosed a private event probability."
                                )
                    if opening_names is not None and any(
                        name.casefold() not in result.global_narrative.casefold()
                        for name in opening_names
                    ):
                        raise LLMResolutionError(
                            "Opening narrative must introduce every player by their supplied "
                            "name with an occupation, class, or role: " + json.dumps(opening_names)
                        )
                    if any(event.occurred for event in (private_events or [])):
                        # Audits must not replace the validated narrative saved in history.
                        narrative_response = self._last_response_text
                        try:
                            await auditing.audit_chance_outcomes(
                                self._parse,
                                result,
                                private_events,
                                repair_attempt=repair,
                            )
                        finally:
                            self._last_response_text = narrative_response
                break
            except LLMResolutionError as exc:
                logger.warning(
                    "LLM %s output validation failed (repair=%d): %s", kind, repair, str(exc)
                )
                if repair == settings.llm.max_retries:
                    raise
                messages = [
                    *messages,
                    *(
                        [{"role": "assistant", "content": result.model_dump_json()}]
                        if isinstance(result, DicePlan)
                        else []
                    ),
                    {
                        "role": "user",
                        "content": (
                            "Correct the output contract: return only the requested object. "
                            "Use exactly the required schema keys and player names. "
                            "Include nonempty "
                            "narrative/outcomes where requested. Hidden checks must be unique, "
                            "required rolls from private guidance. Never disclose private guidance "
                            "or hidden values. Do not change the supplied actions, dice or facts. "
                            "When fixing hidden_roll_sources or hidden_rolls, preserve rolls: "
                            "making a check public must not remove the action's required d100. "
                            + "Validation issue: "
                            + str(exc)
                            + " Required player keys: "
                            + json.dumps(expected_names)
                        ),
                    },
                ]
        if isinstance(result, RoundResolution):
            result = result.model_copy(
                update={
                    "player_resolutions": {
                        name: name_resolution(name, text)
                        for name, text in result.player_resolutions.items()
                    }
                }
            )
        if remember:
            content = result.model_dump_json()
            if self._last_response_text:
                try:
                    original = schema.model_validate_json(self._last_response_text)
                    if original.model_dump() == result.model_dump():
                        content = self._last_response_text
                except (ValidationError, ValueError):
                    pass
            self.history.extend([prompt, {"role": "assistant", "content": content}])
        return result

    def begin_round_usage(self, number: int) -> None:
        """Start accounting once per round; host retries keep the same totals."""
        if self.usage_round != number:
            self.usage_round = number
            self.round_usage = UsageTotals()
            self.round_usage_by_kind = {}
            self.round_work_seconds = 0.0
            self.round_failures = 0
            self.last_round_error = None
            self._round_started = None
        if self._round_started is None:
            self._round_started = perf_counter()

    def finish_round_usage(self, error: str | None = None) -> None:
        """Include budgeting, tokenization and retry waits in round work time."""
        if self._round_started is not None:
            self.round_work_seconds += perf_counter() - self._round_started
            self._round_started = None
            self.round_failures += error is not None
            self.last_round_error = error

    async def refresh_usage(self) -> None:
        """Measure retained messages with the request tokenizer, without inference."""
        messages = [*self._fixed_messages(), *self.history]
        count = await self.budget.input_tokens(messages, None)
        self._retained_measurement = (messages, count, self.token_count_method)

    def usage_snapshot(self) -> dict[str, Any]:
        """Separate estimated retained messages from billed request consumption."""
        messages = [*self._fixed_messages(), *self.history]
        measured = self._retained_measurement
        if measured is not None and measured[0] == messages:
            retained, method = measured[1:]
        else:
            retained = self.budget.context_size(messages)
            method = (
                "local tokenizer estimate"
                if self.budget.encoding
                else "conservative UTF-8 estimate"
            )
        return {
            "round_number": self.usage_round,
            "round_failures": self.round_failures,
            "last_round_error": self.last_round_error,
            "round_work_seconds": self.round_work_seconds
            + (perf_counter() - self._round_started if self._round_started is not None else 0),
            "round": self.round_usage.snapshot(),
            "game": self.game_usage.snapshot(),
            "round_by_kind": {
                kind: usage.snapshot() for kind, usage in self.round_usage_by_kind.items()
            },
            "last_request": self.last_request,
            "retained_context_tokens": retained,
            "context_window_size": self.context_window_size,
            "context_window_source": self.context_window_source,
            "counting_method": f"Retained messages: {method}; excludes next input/schema/output",
        }

    async def _parse(
        self,
        messages: list[dict[str, str]],
        schema: type[BaseModel],
        kind: str,
        repair_attempt: int = 0,
    ) -> Any:
        """Measure each attempt, including SDK-compatible transient retries."""
        count = await self.budget.input_tokens(messages, schema, kind)
        if not self.budget.fits(count, kind):
            raise LLMResolutionError(
                "Request exceeds the context budget; history and durable memory were preserved."
            )
        # A repair is already the next logical request for the same output. Restarting the
        # full transient-retry sequence for every repair would multiply provider calls
        # (max_retries + 1)^2. The initial request may use the configured retry budget; a
        # repair gets one provider attempt and either succeeds or advances to the next repair.
        attempt_limit = settings.llm.max_retries + 1 if repair_attempt == 0 else 1
        retry_backoff = sum(min(0.5 * 2**attempt, 8.0) for attempt in range(attempt_limit - 1))
        overall_timeout = settings.llm.request_timeout_seconds * attempt_limit + retry_backoff
        try:
            async with asyncio.timeout(overall_timeout):
                for attempt in range(attempt_limit):
                    try:
                        return await self._parse_attempt(
                            messages, schema, kind, count, attempt, repair_attempt
                        )
                    except LLMResolutionError as exc:
                        cause = exc.__cause__
                        status = getattr(cause, "status_code", None)
                        transient = isinstance(cause, OpenAIError) and (
                            status in (408, 409, 429)
                            or (status is not None and status >= 500)
                            or type(cause).__name__ in ("APIConnectionError", "APITimeoutError")
                        )
                        if not transient or attempt == attempt_limit - 1:
                            raise
                        logger.info("Retrying LLM request kind=%s attempt=%d", kind, attempt + 2)
                        await asyncio.sleep(min(0.5 * 2**attempt, 8.0))
        except TimeoutError as exc:
            raise LLMResolutionError("The model request failed: deadline exceeded.") from exc

    async def _parse_attempt(
        self,
        messages: list[dict[str, str]],
        schema: type[BaseModel],
        kind: str,
        count: int,
        attempt: int,
        repair_attempt: int = 0,
    ) -> Any:
        """Send and account for one provider attempt."""
        if self.client is None:
            self.client = self._create_client()
        started = perf_counter()
        response = None
        error = None
        cap_key = "max_tokens" if settings.llm.provider == "compatible" else "max_completion_tokens"
        effort = self.budget.reasoning_effort(kind)
        template_options = self.budget.template_options(kind)
        try:
            response = await self.client.beta.chat.completions.parse(
                model=settings.llm.model_name,
                messages=messages,
                response_format=schema,
                reasoning_effort=effort,
                **({"extra_body": template_options} if template_options else {}),
                **{cap_key: self.budget.request_output_limit(kind)},
            )
            choice = response.choices[0]
            if getattr(choice, "finish_reason", None) == "length":
                raise LLMResolutionError(
                    "Model output reached its token limit; no result committed."
                )
            parsed = choice.message.parsed
            if parsed is None:
                raise LLMResolutionError("Model returned no validated result.")
            result = schema.model_validate(
                parsed.model_dump() if isinstance(parsed, BaseModel) else parsed
            )
            self._last_response_text = getattr(choice.message, "content", None)
        except asyncio.CancelledError:
            error = "CancelledError"
            raise
        except LLMResolutionError:
            error = "LLMResolutionError"
            raise
        except (
            OpenAIError,
            ValidationError,
            IndexError,
            AttributeError,
            TypeError,
            ValueError,
            TimeoutError,
        ) as exc:
            # Provider exception bodies may contain private prompts. Keep them out of
            # public errors and logs; retain only the exception class for diagnosis.
            error = type(exc).__name__
            response = getattr(exc, "completion", response)
            logger.warning("LLM %s failed: %s", kind, error)
            if isinstance(exc, APIConnectionError) and not isinstance(exc, APITimeoutError):
                raise LLMBackendUnavailableError(
                    "Could not connect to the LLM backend; no result committed."
                ) from exc
            raise LLMResolutionError(
                "The model request failed or was truncated; no result committed."
            ) from exc
        finally:
            usage = getattr(response, "usage", None)
            timings = getattr(response, "timings", None)
            record = {
                "kind": kind,
                "round_number": self.usage_round,
                "retry": attempt > 0 or repair_attempt > 0,
                "repair_attempt": repair_attempt,
                "attempt": attempt + repair_attempt + 1,
                "estimated_input_tokens": count,
                "configured_output_tokens": self.budget.output_limit(kind),
                "thinking_output_tokens": self.budget.thinking_output_limit(kind),
                "request_output_tokens": self.budget.request_output_limit(kind),
                "counting_method": self.token_count_method,
                "input_tokens": counter(usage, "prompt_tokens"),
                "completion_tokens": counter(usage, "completion_tokens"),
                "total_tokens": counter(usage, "total_tokens"),
                "cached_tokens": counter(
                    getattr(usage, "prompt_tokens_details", None), "cached_tokens"
                ),
                "processed_prompt_tokens": counter(timings, "prompt_n"),
                "reused_prompt_tokens": counter(timings, "cache_n"),
                "latency_seconds": perf_counter() - started,
                "error": error,
            }
            self.last_request = record
            self.game_usage.add(record)
            if self.usage_round is not None:
                self.round_usage.add(record)
                self.round_usage_by_kind.setdefault(kind, UsageTotals()).add(record)
            self.last_token_usage = record["total_tokens"] or count
            logger.info("LLM usage %s", json.dumps(record, sort_keys=True))
        return result

    async def _compact_if_needed(
        self, prompt: dict[str, str], schema: type[BaseModel], kind: str
    ) -> None:
        """Merge old pairs into durable memory transactionally; never FIFO-forget."""
        original_memory, original_history = self.memory, self.history
        started = perf_counter()
        passes = 0
        compacted = False
        summary_schema = (
            participant_schema(
                ContextSummary, tuple(self._known_player_names), provider=settings.llm.provider
            )
            if self._known_player_names
            else ContextSummary
        )
        try:
            while True:
                messages = [*self._fixed_messages(kind), *self.history, prompt]
                count = await self.budget.input_tokens(messages, schema, kind)
                fits = self.budget.fits(count, kind)
                history_limit = settings.llm.history_round_limit
                checkpoint_due = history_limit is not None and len(self.history) > 2 * history_limit
                target_fits = (
                    count
                    + self.budget.request_output_limit(kind)
                    + settings.llm.token_safety_margin
                    <= self.context_window_size * settings.llm.compaction_target_fraction
                )
                if (
                    fits
                    and not checkpoint_due
                    and (not compacted or target_fits or not self.history)
                ):
                    if compacted:
                        logger.info(
                            "Context compaction complete kind=%s passes=%d messages=%d->%d "
                            "input_tokens=%d counting_method=%s elapsed_seconds=%.3f",
                            kind,
                            passes,
                            len(original_history),
                            len(self.history),
                            count,
                            self.token_count_method,
                            perf_counter() - started,
                        )
                    return
                if not self.history:
                    raise LLMResolutionError("Durable memory and current input do not fit.")
                # Largest prefix that fits the summary request; retained memory is
                # included exactly once so later summaries replace obsolete facts.
                selected = 0
                compact_messages = []
                prefix_limit = len(self.history)
                if checkpoint_due and fits:
                    # Freeze the ledger between checkpoints and retain the newest half of rounds.
                    prefix_limit -= 2 * max(1, history_limit // 2)
                for length in range(2, prefix_limit + 1, 2):
                    candidate = [
                        *self._fixed_messages(),
                        *self.history[:length],
                        prompts.summary_prompt(),
                    ]
                    if not self.budget.fits(
                        await self.budget.input_tokens(candidate, summary_schema, "summary")
                        + self.budget.output_limit("summary")
                        + 512,
                        "summary",
                    ):
                        break
                    selected, compact_messages = length, candidate
                if not selected and fits:
                    logger.info(
                        "Context compaction deferred kind=%s reason=no_summary_fits "
                        "retained_messages=%d passes=%d",
                        kind,
                        len(self.history),
                        passes,
                    )
                    return
                if not selected:
                    raise LLMResolutionError("History cannot be safely summarized within budget.")
                passes += 1
                logger.info(
                    "Context compaction started kind=%s pass=%d reason=%s "
                    "selected_messages=%d input_tokens=%d context_window=%d counting_method=%s",
                    kind,
                    passes,
                    "history_limit" if checkpoint_due and fits else "token_budget",
                    selected,
                    count,
                    self.context_window_size,
                    self.token_count_method,
                )
                summary = await self._parse(compact_messages, summary_schema, "summary")
                check_semantics(summary, tuple(self._known_player_names) or None)
                if not summary.world_state.strip():
                    raise LLMResolutionError(
                        "Summary has no world state; original memory retained."
                    )
                memory = {
                    "role": "user",
                    "content": "Durable historical memory:\n" + summary.model_dump_json(),
                }
                before = self.budget.context_size(
                    [*([self.memory] if self.memory else []), *self.history[:selected]]
                )
                if self.budget.context_size([memory]) >= before:
                    raise LLMResolutionError(
                        "Summary did not reduce context; original memory retained."
                    )
                logger.info("Context compaction audit started kind=%s pass=%d", kind, passes)
                await auditing.audit_summary(self._parse, compact_messages, summary)
                self.memory = memory
                self.history = self.history[selected:]
                compacted = True
                logger.info(
                    "Context compaction pass accepted kind=%s pass=%d "
                    "estimated_memory_tokens=%d->%d",
                    kind,
                    passes,
                    before,
                    self.budget.context_size([memory]),
                )
        except BaseException as exc:
            # Cancellation must not leave a half-compacted conversation either.
            self.memory, self.history = original_memory, original_history
            logger.warning(
                "Context compaction rolled back kind=%s error=%s passes=%d "
                "restored_messages=%d elapsed_seconds=%.3f",
                kind,
                type(exc).__name__,
                passes,
                len(original_history),
                perf_counter() - started,
            )
            raise

    async def close(self) -> None:
        """Close the OpenAI and HTTP clients."""
        if self.client is not None:
            await self.client.close()
            self.client = None
        await self.budget.close()

    @property
    def context_window_size(self) -> int:
        """The discovered per-slot context limit, or configured fallback."""
        return self.budget.context_window_size

    @context_window_size.setter
    def context_window_size(self, value: int) -> None:
        self.budget.context_window_size = value

    @property
    def context_window_source(self) -> str:
        return self.budget.context_window_source

    @property
    def token_count_method(self) -> str:
        return self.budget.token_count_method

    async def discover_context_window(self) -> None:
        """Discover the backend context window size when available."""
        await self.budget.discover_context_window()
