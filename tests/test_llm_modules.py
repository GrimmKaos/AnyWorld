"""Contracts across extracted LLM components, without a backend."""

import asyncio
import json

import pytest

from core.config import settings
from core.schemas import AuditVerdict, ChanceEvent, ChanceEventResult, DicePlan
from logic.llm.auditing import classify_hidden_checks
from logic.llm.errors import LLMResolutionError
from logic.llm_manager import LLMContextManager
from test_priority_one_llm import FakeClient


def test_injected_classification_preserves_rolls_and_original_plan():
    """Rejecting privacy changes neither required dice nor the caller's plan."""

    async def run():
        plan = DicePlan(
            rolls={"Alice": True},
            hidden_rolls=["Alice"],
            hidden_roll_sources={"Alice": "Keep the story moving quickly."},
        )
        original = plan.model_dump()
        calls = []

        async def parse(messages, schema, kind, repair_attempt=0):
            calls.append((messages, schema, kind, repair_attempt))
            return AuditVerdict(preserved=False, corrections=[])

        result = await classify_hidden_checks(
            parse, plan, {"actions": {"Alice": "Open the gate."}}, repair_attempt=1
        )
        assert result.rolls == {"Alice": True}
        assert result.hidden_rolls == [] and result.hidden_roll_sources == {}
        assert plan.model_dump() == original
        assert len(calls) == 1
        assert calls[0][1:] == (AuditVerdict, "dice_audit", 1)
        assert "Open the gate." in calls[0][0][-1]["content"]

    asyncio.run(run())


@pytest.mark.parametrize("audit_passes", [True, False])
def test_chance_audit_restores_raw_narrative_before_commit_or_failure(audit_passes):
    """An audit response must never replace the narrative's exact JSON prefix."""

    async def run():
        settings.llm.context_window_size = 32_768
        settings.llm.max_retries = 0
        client = FakeClient()
        original_parse = client.parse
        narrative = []

        async def parse(**kwargs):
            response = await original_parse(**kwargs)
            message = response.choices[0].message
            if kwargs["response_format"] is AuditVerdict:
                message.parsed = AuditVerdict(preserved=audit_passes, corrections=[])
            message.content = json.dumps(message.parsed.model_dump(), indent=2)
            if kwargs["response_format"] is not AuditVerdict:
                narrative.append(message.content)
            return response

        client.beta.chat.completions.parse = parse
        manager = LLMContextManager(client)
        check = ChanceEventResult(
            event=ChanceEvent(
                source_rule="A breeze rises every round.",
                trigger="per_round",
                occurrence="round",
                chance_percent=50,
            ),
            roll=20,
            occurred=True,
        )
        try:
            if audit_passes:
                await manager.generate_resolution({"Alice": "Wait."}, chance_events=[check])
                assert manager.history[-1]["content"] == narrative[0]
            else:
                with pytest.raises(LLMResolutionError, match="consequences were omitted"):
                    await manager.generate_resolution({"Alice": "Wait."}, chance_events=[check])
                assert manager.history == []
            assert manager._last_response_text == narrative[0]
            assert len(client.calls) == manager.game_usage.attempts == 2
        finally:
            await manager.close()

    asyncio.run(run())
