"""Contracts across extracted LLM components, without a backend."""

import asyncio
import json
import os

import httpx
import pytest

from core.config import DEFAULT_CONFIG_PATH, Settings, settings
from core.schemas import AuditVerdict, ChanceEvent, ChanceEventResult, DicePlan, RoundResolution
from logic.llm.auditing import classify_hidden_checks
from logic.llm.errors import LLMResolutionError
from logic.llm import prompts
from logic.llm_manager import LLMContextManager, participant_schema
from logic import llm_manager
from test_priority_one_llm import FakeClient


@pytest.mark.parametrize("provider", ["openai", "compatible"])
def test_client_ignores_inherited_organization_and_project(monkeypatch, provider):
    """Ambient SDK scope must not override the game's configured API key."""
    monkeypatch.setenv("OPENAI_ORG_ID", "org-other-application")
    monkeypatch.setenv("OPENAI_PROJECT_ID", "proj-other-application")
    settings.llm.provider = provider
    requests = []

    def send(request):
        requests.append(request)
        return httpx.Response(200, json={"object": "list", "data": []}, request=request)

    sdk_client = llm_manager.AsyncOpenAI

    class ClientWithTransport(sdk_client):
        def __init__(self, **options):
            options["http_client"] = httpx.AsyncClient(transport=httpx.MockTransport(send))
            super().__init__(**options)

    monkeypatch.setattr(llm_manager, "AsyncOpenAI", ClientWithTransport)

    async def run():
        manager = LLMContextManager()
        manager.client = manager._create_client()
        try:
            await manager.client.models.list()
        finally:
            await manager.close()

    asyncio.run(run())

    assert len(requests) == 1
    assert requests[0].headers["authorization"] == f"Bearer {settings.llm.api_key}"
    assert "openai-organization" not in requests[0].headers
    assert "openai-project" not in requests[0].headers
    assert os.environ["OPENAI_ORG_ID"] == "org-other-application"
    assert os.environ["OPENAI_PROJECT_ID"] == "proj-other-application"


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
    assert list(RoundResolution.model_fields) == ["player_resolutions", "global_narrative"]


def test_round_resolution_generates_player_outcomes_before_shared_narrative():
    """The shared narrative is generated after all player outcomes are available."""
    names = ("Alice", "Bob")
    schema = participant_schema(RoundResolution, names)
    assert list(schema.model_fields) == ["player_resolutions", "global_narrative"]
    assert list(schema.model_json_schema()["properties"]) == [
        "player_resolutions",
        "global_narrative",
    ]

    prompt = prompts.resolution_prompt({name: "Take an action." for name in names})["content"]
    assert "Generate player_resolutions first" in prompt
    assert "synthesize global_narrative from those outcomes" in prompt


def test_dice_planner_uses_central_system_prompt():
    """Dice planning uses the dedicated prompt, separate from narrative generation."""
    manager = LLMContextManager(FakeClient())
    assert manager._fixed_messages("dice")[0]["content"] == prompts.DICE_PLANNER_SYSTEM_PROMPT
    assert manager._fixed_messages("round")[0]["content"] == settings.llm.system_prompt


def test_active_guidance_is_near_current_input_and_keeps_percentage_authority():
    """Narration refreshes steering without turning it into mandatory random events."""
    guidance = "Occasionally show a distant visitor."
    prompt = prompts.prepare_request_prompt(
        prompts.resolution_prompt({"Alice": "Walk south."}, guidance=guidance),
        True,
        guidance,
    )["content"]
    assert prompt.index(guidance) > prompt.index("Current round actions:")
    assert "allow gaps and variation" in prompt
    assert "never apply failed or untriggered percentage events" in prompt
    planning = prompts.dice_prompt({"Alice": "Walk south."})
    assert prompts.prepare_request_prompt(planning, False, guidance) == planning


def test_resolved_rounds_retain_compact_input_records():
    """Repeated adjudication instructions are not replayed as round history."""

    async def run():
        settings.llm.context_window_size = 32_768
        manager = LLMContextManager(FakeClient())
        await manager.generate_resolution({"Alice": "Inspect the signal."})

        assert len(manager.history) == 2
        assert manager.history[0]["content"].startswith("Round action record")
        assert "Inspect the signal." in manager.history[0]["content"]
        assert "Resolve all supplied actions" not in manager.history[0]["content"]

    asyncio.run(run())


def test_failed_request_does_not_change_compaction_participants(monkeypatch):
    """Pending participants never enter durable names, even before compaction."""

    async def run():
        settings.llm.context_window_size = 32_768
        settings.llm.max_retries = 0
        manager = LLMContextManager(FakeClient())
        manager._known_player_names = ["Alice"]
        seen = []

        async def compact(*args):
            seen.append(list(manager._known_player_names))

        async def fail(*args, **kwargs):
            raise LLMResolutionError("Injected failure")

        monkeypatch.setattr(manager, "_compact_if_needed", compact)
        monkeypatch.setattr(manager, "_parse", fail)
        with pytest.raises(LLMResolutionError, match="Injected failure"):
            await manager.generate_resolution({"Bob": "Wait."})
        assert seen == [["Alice"]]
        assert manager._known_player_names == ["Alice"]
        assert manager.history == []

    asyncio.run(run())


def test_normalized_narrative_is_privacy_checked_before_commit(monkeypatch):
    """A presentation mutation cannot bypass the public-output guard."""

    async def run():
        settings.llm.context_window_size = 32_768
        settings.llm.max_retries = 0
        manager = LLMContextManager(FakeClient())
        manager.set_genesis("A gate.", "The sealed gate hides a secret alarm.")
        monkeypatch.setattr(
            "logic.llm_manager.name_resolution",
            lambda name, text: "The sealed gate hides a secret alarm.",
        )
        with pytest.raises(LLMResolutionError, match="private guidance"):
            await manager.generate_resolution({"Alice": "Wait."})
        assert manager.history == []
        assert manager._known_player_names == []

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
            assert not hasattr(manager, "_last_response_text")
            assert len(client.calls) == manager.game_usage.attempts == 2
        finally:
            await manager.close()

    asyncio.run(run())
