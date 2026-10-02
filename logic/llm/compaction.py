"""Transactional bounded conversation compaction."""

import logging
from time import perf_counter
from pydantic import BaseModel
from core.config import settings
from core.schemas import ContextSummary, SummaryAudit
from . import auditing, prompts
from .errors import LLMResolutionError, SummaryRejectedError
from .response_schemas import participant_schema
from .validation import check_semantics

logger = logging.getLogger("logic.llm_manager")


async def compact(self, prompt: dict[str, str], schema: type[BaseModel], kind: str) -> None:
    """Merge old pairs into durable memory transactionally; never FIFO-forget."""
    original_memory, original_history = self.memory, self.history
    started = perf_counter()
    passes = 0
    compacted = False
    original_fits = False
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
            if passes == 0:
                original_fits = fits
            history_limit = settings.llm.history_round_limit
            checkpoint_due = history_limit is not None and len(self.history) > 2 * history_limit
            target_fits = (
                count + self.budget.request_output_limit(kind) + settings.llm.token_safety_margin
                <= self.context_window_size * settings.llm.compaction_target_fraction
            )
            if fits and not checkpoint_due and (not compacted or target_fits or not self.history):
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
            # Local estimates choose a likely prefix; backend verification adjusts
            # in whole pairs instead of sending every overlapping prefix.
            blank = ContextSummary(
                world_state="", player_states={}, important_npcs="", unresolved_threads=[]
            )
            likely = 2
            for length in range(2, prefix_limit + 1, 2):
                candidate = [
                    *self._fixed_messages(),
                    *self.history[:length],
                    prompts.summary_prompt(),
                ]
                estimate = self.budget.context_size(candidate)
                if not self.budget.fits(
                    estimate + self.budget.output_limit("summary"), "summary_audit"
                ):
                    break
                likely = length
            for length in _prefixes(likely):
                candidate = [
                    *self._fixed_messages(),
                    *self.history[:length],
                    prompts.summary_prompt(),
                ]
                summary_count = await self.budget.input_tokens(candidate, summary_schema, "summary")
                audit_count = await self.budget.input_tokens(
                    prompts.summary_audit_prompt(candidate, blank), SummaryAudit, "summary_audit"
                )
                if self.budget.fits(summary_count, "summary") and self.budget.fits(
                    audit_count + self.budget.output_limit("summary"), "summary_audit"
                ):
                    selected, compact_messages = length, candidate
                    break
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
            try:
                memory = await _checkpoint(
                    self, compact_messages, summary_schema, selected, kind, passes
                )
            except LLMResolutionError:
                if fits:
                    logger.info("Context compaction deferred kind=%s reason=optional_failure", kind)
                    return
                smaller = 2 * (selected // 4)
                if smaller < 2:
                    raise
                selected = smaller
                compact_messages = [
                    *self._fixed_messages(),
                    *self.history[:selected],
                    prompts.summary_prompt(),
                ]
                memory = await _checkpoint(
                    self, compact_messages, summary_schema, selected, kind, passes, repair=False
                )
            before = self.budget.context_size(
                [*([self.memory] if self.memory else []), *self.history[:selected]]
            )
            prefix = self._fixed_messages(kind)
            if self.memory is not None:
                prefix = prefix[:-1]
            candidate_count = await self.budget.input_tokens(
                [*prefix, memory, *self.history[selected:], prompt], schema, kind
            )
            if candidate_count >= count:
                raise LLMResolutionError(
                    "Summary did not reduce the formatted request; facts retained."
                )
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
        if isinstance(exc, LLMResolutionError) and original_fits:
            logger.info("Context compaction deferred kind=%s reason=optional_failure", kind)
            return
        raise


def _prefixes(length: int):
    """Bound expensive probes by halving complete history pairs."""
    while length >= 2:
        yield length
        length = 2 * (length // 4)


async def _checkpoint(self, messages, schema, selected, kind, passes, *, repair=True):
    original = list(messages)
    before = self.budget.context_size(
        [*([self.memory] if self.memory else []), *self.history[:selected]]
    )
    for attempt in range(2 if repair else 1):
        try:
            summary = await self._parse(messages, schema, "summary", repair_attempt=attempt)
            check_semantics(summary, tuple(self._known_player_names) or None)
            if not summary.world_state.strip():
                raise LLMResolutionError("Summary has no world state; original memory retained.")
            memory = {
                "role": "user",
                "content": "Durable historical memory:\n" + summary.model_dump_json(),
            }
            if self.budget.context_size([memory]) >= before:
                raise LLMResolutionError(
                    "Summary did not reduce context; original memory retained."
                )
            audit_messages = prompts.summary_audit_prompt(original, summary)
            audit_count = await self.budget.input_tokens(
                audit_messages, SummaryAudit, "summary_audit"
            )
            if not self.budget.fits(audit_count, "summary_audit"):
                raise LLMResolutionError("Summary audit does not fit; original memory retained.")
            logger.info("Context compaction audit started kind=%s pass=%d", kind, passes)
            await auditing.audit_summary(self._parse, original, summary)
            # The caller measures the complete upcoming request again before inference.
            return memory
        except LLMResolutionError as exc:
            if attempt or not repair:
                raise
            corrections = exc.corrections if isinstance(exc, SummaryRejectedError) else [str(exc)]
            messages = [
                *original,
                {
                    "role": "user",
                    "content": "Repair the memory checkpoint without changing facts: "
                    + "; ".join(corrections),
                },
            ]
