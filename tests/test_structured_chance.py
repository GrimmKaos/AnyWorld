"""Host-selected cadence, eligibility and scope are independent contracts."""

import asyncio

import pytest
from pydantic import ValidationError

from core.schemas import StructuredChanceRule, ChanceTriggerPlan, DicePlan
from logic.dice import structured_rule_text, validate_legacy_rule
from logic.llm_manager import LLMContextManager
from support import FakeClient


def rule(**changes):
    return StructuredChanceRule(
        chance_percent=40, cadence="per_round", effect="A bell rings", scope="per_player", **changes
    )


@pytest.mark.parametrize(
    "eligibility,eligible",
    [("", ["Alice", "Bob"]), ("Inside the tower", ["Alice"]), ("Inside the tower", [])],
)
def test_per_round_scope_respects_optional_eligibility(eligibility, eligible):
    async def run():
        spec = rule(eligibility=eligibility)
        calls = []

        def response(kwargs):
            calls.append(kwargs["response_format"])
            if kwargs["response_format"] is ChanceTriggerPlan:
                return ChanceTriggerPlan(occurrences=eligible)
            return DicePlan(rolls={"Alice": False, "Bob": False}, hidden_rolls=[])

        manager = LLMContextManager(FakeClient(response))
        manager.set_genesis("A tower", structured_rule_text(spec))
        manager.configure_chance_rule(spec)
        plan = await manager.plan_dice(
            {"Alice": "Wait", "Bob": "Wait"}, "Alice is inside the tower."
        )
        assert len(plan.chance_events) == len(eligible)
        assert all(event.trigger == "per_round" for event in plan.chance_events)
        assert (ChanceTriggerPlan in calls) == bool(eligibility)
        await manager.close()

    asyncio.run(run())


def test_explicit_trigger_and_percent_boundaries():
    with pytest.raises(ValidationError, match="explicit occurrence"):
        StructuredChanceRule(
            chance_percent=0, cadence="condition", effect="A bell rings", scope="shared"
        )
    with pytest.raises(ValidationError, match="extra percentages"):
        rule(eligibility="Another 20% rule")
    for text in ("A 40% chance a bell rings", "40% per round when someone enters"):
        with pytest.raises(ValueError, match="ambiguous"):
            validate_legacy_rule(text)
    validate_legacy_rule("40% per round a bell rings")
