"""Independent LLM audits using the manager's measured parser."""

import logging
from typing import Any, Protocol

from pydantic import BaseModel

from core.schemas import (
    AuditVerdict,
    ChanceEventResult,
    ConditionalCheckAudit,
    ContextSummary,
    DicePlan,
    RoundResolution,
    SummaryAudit,
)
from logic.dice import conditional_chance_rule_ids
from . import prompts
from .errors import LLMResolutionError

logger = logging.getLogger(__name__)


class Parse(Protocol):
    """A bounded, metered inference call; audits never own conversation state."""

    async def __call__(
        self,
        messages: list[dict[str, str]],
        schema: type[BaseModel],
        kind: str,
        repair_attempt: int = 0,
    ) -> Any: ...


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


async def audit_planned_checks(
    parse: Parse,
    messages: list[dict[str, str]],
    guidance: str,
    plan: DicePlan,
    planning_input: dict[str, Any],
    *,
    repair_attempt: int,
) -> None:
    """Audit missed conditions and privacy classification before rolling dice."""
    conditional_ids = {
        key
        for key in conditional_chance_rule_ids(guidance)
        if plan.chance_rule_decisions[key].trigger == "condition"
    }
    if not conditional_ids:
        return
    audit_plan = {key: plan.chance_rule_decisions[key].occurrences for key in conditional_ids}
    audit_messages = prompts.planned_checks_audit_prompt(messages, planning_input, audit_plan)
    audit = await parse(
        audit_messages, ConditionalCheckAudit, "dice_audit", repair_attempt=repair_attempt
    )
    if audit.missing_occurrences or audit.invalid_occurrences:
        raise LLMResolutionError(
            "Conditional occurrence mismatch; change only chance_rule_decisions. "
            "Preserve action rolls and their privacy classification. " + audit.model_dump_json()
        )
    logger.info("Private dice planning audit passed")


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
        raise LLMResolutionError("Summary changed durable facts; original memory retained.")
