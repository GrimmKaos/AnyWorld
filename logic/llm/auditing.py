"""Independent LLM audits using the manager's measured parser."""

import logging
from typing import Any, Protocol

from pydantic import BaseModel

from core.schemas import (
    AuditVerdict,
    ChanceEventResult,
    ContextSummary,
    DicePlan,
    RoundResolution,
    SummaryAudit,
)
from . import prompts
from .errors import LLMResolutionError, SummaryRejectedError

logger = logging.getLogger(__name__)


class Parse(Protocol):
    """A bounded, metered inference call; audits never own conversation state."""

    async def __call__(
        self,
        messages: list[dict[str, str]],
        schema: type[BaseModel],
        kind: str,
        repair_attempt: int = 0,
    ) -> Any:
        """Parse one bounded request without changing the manager's conversation state."""
        ...


async def classify_hidden_checks(
    parse: Parse, plan: DicePlan, planning_input: dict[str, Any], *, repair_attempt: int
) -> DicePlan:
    """Classify cited secrets independently; rejection keeps the action roll public."""
    hidden = []
    for name in plan.hidden_rolls:
        messages = prompts.classify_audit_prompt(
            name, plan.hidden_roll_sources[name], planning_input
        )
        verdict = await parse(messages, AuditVerdict, "dice_audit", repair_attempt=repair_attempt)
        if verdict.preserved:
            hidden.append(name)
        else:
            logger.info("Rejected private source for %s; retaining public action check", name)
    return plan.model_copy(
        update={
            "hidden_rolls": hidden,
            "hidden_roll_sources": {name: plan.hidden_roll_sources[name] for name in hidden},
        }
    )


async def audit_chance_outcomes(
    parse: Parse,
    result: RoundResolution,
    events: list[ChanceEventResult],
    *,
    repair_attempt: int,
) -> None:
    """Reject omitted event effects before publishing or remembering a round."""
    audit_messages = prompts.chance_outcomes_audit_prompt(result, events)
    audit = await parse(audit_messages, AuditVerdict, "event_audit", repair_attempt=repair_attempt)
    if not audit.preserved:
        raise LLMResolutionError(
            "Private chance event consequences were omitted or contradicted. "
            "Rewrite the round honoring the same event results: " + "; ".join(audit.corrections)
        )
    logger.info("Private chance event narrative audit passed")


async def audit_summary(
    parse: Parse, messages: list[dict[str, str]], summary: ContextSummary
) -> None:
    """Reject memory that drops, changes or invents durable facts."""
    audit = await parse(
        prompts.summary_audit_prompt(messages, summary), SummaryAudit, "summary_audit"
    )
    if not audit.preserved or audit.corrections:
        raise SummaryRejectedError(audit.corrections)
