"""Contracts across extracted LLM components, without a backend."""

import asyncio
import json

import pytest

from core.config import DEFAULT_CONFIG_PATH, Settings, settings
from core.schemas import AuditVerdict, ChanceEvent, ChanceEventResult, DicePlan, RoundResolution
from logic.llm.auditing import classify_hidden_checks
from logic.llm.errors import LLMResolutionError
from logic.llm import prompts
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


def test_story_prompt_prioritizes_plot_progress_and_rare_rolls():
    """Narrative instructions require concrete progress without over-rolling."""
    resolution = prompts.resolution_prompt({"Alice": "Inspect the signal."})["content"]
    dice = prompts.dice_prompt({"Alice": "Inspect the signal."})["content"]

    assert "every round as a story beat" in resolution
    assert "atmosphere cannot be its only content" in resolution
    assert "conservative roll policy from the planner system instructions" in dice
    assert "Rolls are rare exceptions" not in dice


def test_connection_annotations_explain_server_lifecycle():
    """Connection metadata is identified clearly in both narrative prompts."""
    configured = Settings.load(DEFAULT_CONFIG_PATH).llm.system_prompt
    resolution = prompts.prepare_request_prompt(
        prompts.resolution_prompt({"Alice": "Wait."}), is_resolution=True
    )["content"]

    for prompt in (configured, resolution):
        normalized = " ".join(prompt.split())
        assert "a departure means" in normalized and "client disconnected" in normalized
        assert "a return means" in normalized and "reconnected" in normalized
        assert "server metadata" in normalized or "server connection metadata" in normalized
        assert "Never mention the server" in prompt


def test_round_resolution_has_no_unused_title_field():
    """Round output contains only the narrative and participant outcomes."""
    assert set(RoundResolution.model_fields) == {"global_narrative", "player_resolutions"}


def test_dice_planner_uses_central_system_prompt():
    """Dice planning uses the dedicated prompt, separate from narrative generation."""
    manager = LLMContextManager(FakeClient())
    assert manager._fixed_messages("dice")[0]["content"] == prompts.DICE_PLANNER_SYSTEM_PROMPT
    assert manager._fixed_messages("round")[0]["content"] == settings.llm.system_prompt


def test_resolved_rounds_retain_compact_input_records():
    """Repeated adjudication instructions are not replayed as round history."""

    async def run():
        manager = LLMContextManager(FakeClient())
        await manager.generate_resolution({"Alice": "Inspect the signal."})

        assert len(manager.history) == 2
        assert manager.history[0]["content"].startswith("Round action record")
        assert "Inspect the signal." in manager.history[0]["content"]
        assert "Resolve all supplied actions" not in manager.history[0]["content"]

    asyncio.run(run())


@pytest.mark.parametrize("preserved,corrections", [(True, []), (False, ["The event is missing."])])
def test_chance_audit_restores_raw_narrative_before_commit_or_failure(preserved, corrections):
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
                message.parsed = AuditVerdict(preserved=preserved, corrections=corrections)
                audit_data = json.loads(kwargs["messages"][-1]["content"])
                assert audit_data["authoritative_event_results"] == [check.model_dump()]
                assert audit_data["proposed_round"] == json.loads(narrative[0])
                assert [entry["role"] for entry in kwargs["messages"]] == ["system", "user"]
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
            if preserved and not corrections:
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
