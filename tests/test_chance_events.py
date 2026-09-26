"""Exact event probabilities, private planning and retry-safe round integration."""

import asyncio
import json

import pytest
from pydantic import ValidationError

from core.config import settings
from core.schemas import (
    ChanceEvent,
    ChanceEventResult,
    ChanceRuleDecision,
    ConditionalCheckAudit,
    DicePlan,
    RoundResolution,
    SummaryAudit,
)
from logic import dice
from logic.llm_manager import LLMContextManager, LLMResolutionError, participant_schema
from test_engine import payload
from test_priority_one_lifecycle import setup, submit_round
from test_priority_one_llm import FakeClient

RULE = "Add a 20% chance every time a building is entered that it collapses on the player."


def event(**changes):
    """One conditional occurrence, grounded in a private rule."""
    return ChanceEvent.model_validate(
        {
            "source_rule": RULE,
            "trigger": "condition",
            "occurrence": "Host enters the <tower>",
            "chance_percent": 20,
            **changes,
        }
    )


@pytest.mark.parametrize("percentage", [0, 2, 20, 99, 100])
def test_exact_probability_over_all_possible_draws(monkeypatch, percentage):
    """Exactly p of the 100 equiprobable draws trigger a p-percent event."""
    draws = iter(range(100))

    def draw(upper):
        assert upper == 100
        return next(draws)

    monkeypatch.setattr(dice.secrets, "randbelow", draw)
    results = [dice.roll_chance(event(chance_percent=percentage)) for _ in range(100)]
    assert sum(result.occurred for result in results) == percentage
    assert [result.roll for result in results] == list(range(1, 101))


@pytest.mark.parametrize("percentage", [-1, 101, 2.5, "20", True])
def test_invalid_probability_rejected(percentage):
    with pytest.raises(ValidationError):
        event(chance_percent=percentage)


@pytest.mark.parametrize(
    "guidance,chance_event,match",
    [
        ("Keep the tone eerie.", "Add a 40% chance every round that a bell rings.", None),
        ("Add a 20% chance every round that a bell rings.", "", "Freeform"),
        ("Add a forty percent chance that a bell rings.", "", "Freeform"),
        ("There is a 40 per cent chance a bell rings.", "", "Freeform"),
        ("Use the phrase percentage chance in the narration.", "", "Freeform"),
        (
            "Keep the tone eerie.",
            "Add a 20% chance and a 30% chance that a bell rings.",
            "exactly one",
        ),
        (
            "Keep the tone eerie.",
            "Add a 20.5% chance that a bell rings.",
            "whole-number",
        ),
        ("Keep the tone eerie.", "Add a 20% chance\nthat a bell rings.", "single line"),
    ],
)
def test_scenario_guidance_accepts_at_most_one_dedicated_chance_event(
    guidance, chance_event, match
):
    if match:
        with pytest.raises(ValueError, match=match):
            dice.combine_private_guidance(guidance, chance_event)
    else:
        assert dice.combine_private_guidance(guidance, chance_event) == (
            guidance + "\n" + chance_event
        )


@pytest.mark.parametrize(
    "events,guidance",
    [
        ([event()], ""),
        ([event(chance_percent=21)], RULE),
        ([event(), event()], RULE),
        ([event(source_rule="20%")], "No percentages in private guidance."),
        ([event(occurrence="   ")], RULE),
        ([event(source_rule="20.5% chance", chance_percent=20)], "20.5% chance"),
        ([event(source_rule="20% or 30% chance")], "20% or 30% chance"),
    ],
)
def test_invalid_event_plan_rejected(events, guidance):
    with pytest.raises(ValueError):
        dice.validate_chance_events(events, guidance)


def test_distinct_occurrences_and_every_round_rule_are_allowed():
    daily = event(
        source_rule="Add a 2% chance every round that a stranger helps the party.",
        trigger="per_round",
        occurrence="round",
        chance_percent=2,
    )
    dice.validate_chance_events(
        [daily, event(), event(occurrence="Player enters the barn")],
        daily.source_rule + "\n" + RULE,
    )
    schema = participant_schema(DicePlan, ("Host",), False).model_json_schema()
    assert schema["properties"]["chance_events"]["maxItems"] == 0


@pytest.mark.parametrize(
    "reference",
    [
        "rule-1",
        "Each round, one player's clothes turn into a clown costume (50% chance).",
        "One player's clothes turn into a clown costume",
    ],
)
def test_reported_guidance_accepts_ids_and_paraphrases_without_exact_round_label(reference):
    rule = (
        "Include a 50% chance one of the players have their clothes inexplicably "
        "turned into a clown costume."
    )
    guidance = rule + "\nIntroduce new NPCs when the story looks like it's stagnating."
    planned = event(
        source_rule=reference, chance_percent=50, trigger="per_round", occurrence="every round"
    )
    resolved = dice.validate_chance_events([planned], guidance)
    assert dice.private_chance_rules(guidance) == {"rule-1": (rule, 50)}
    assert resolved[0].source_rule == rule
    assert resolved[0].occurrence == "round"
    assert resolved[0].chance_percent == 50
    assert planned.source_rule == reference


def test_equal_percentages_use_rule_ids_and_reject_ambiguous_paraphrases():
    other = "Add a 20% chance every round that a helpful stranger appears."
    guidance = RULE + "\n" + other
    resolved = dice.validate_chance_events([event(source_rule="rule-2")], guidance)
    assert resolved[0].source_rule == other
    with pytest.raises(ValueError, match="rule ID"):
        dice.validate_chance_events([event(source_rule="Something happens")], guidance)
    with pytest.raises(ValueError, match="repeat"):
        dice.validate_chance_events([event(source_rule="rule-1"), event()], guidance)


def test_planner_catalog_resolves_rule_reference_before_returning():
    async def run():
        settings.llm.context_window_size = 32_768
        client = FakeClient(
            DicePlan(
                rolls={"Host": False},
                hidden_rolls=[],
                chance_rule_decisions={
                    "rule-1": ChanceRuleDecision(
                        trigger="per_round", occurrences=[event().occurrence], reason="Host enters."
                    )
                },
            )
        )
        manager = LLMContextManager(client)
        manager.set_genesis("A tower", RULE + "\nIntroduce NPCs if the story stalls.")
        plan = await manager.plan_dice({"Host": "Enter the tower"})
        assert plan.chance_events == [event()]
        prompt = client.calls[0]["messages"][-1]["content"]
        assert '"source_rule": "rule-1"' in prompt
        assert RULE in prompt
        assert len(client.calls) == 2
        assert client.calls[1]["response_format"] is ConditionalCheckAudit
        await manager.close()

    asyncio.run(run())


def test_planner_validates_private_source_and_resolution_receives_authoritative_result():
    async def run():
        settings.llm.context_window_size = 32_768
        client = FakeClient(
            DicePlan(
                rolls={"Host": False},
                hidden_rolls=[],
                chance_rule_decisions={
                    "rule-1": ChanceRuleDecision(
                        trigger="condition", occurrences=[event().occurrence], reason="Host enters."
                    )
                },
            )
        )
        manager = LLMContextManager(client)
        manager.set_genesis("A tower", RULE)
        plan = await manager.plan_dice({"Host": "Enter the tower"})
        assert plan.chance_events == [event()]
        planner_prompt = client.calls[0]["messages"][-1]["content"]
        assert "entering is not remaining inside" in planner_prompt
        result = ChanceEventResult(event=event(), roll=21, occurred=False)
        client.result = RoundResolution(
            global_narrative="The tower stands intact.",
            player_resolutions={"Host": "Host enters the tower safely."},
        )
        await manager.generate_resolution({"Host": "Enter the tower"}, chance_events=[result])
        prompt = client.calls[-1]["messages"][-1]["content"]
        assert '"occurred": false' in prompt
        assert "not action-quality dice" in prompt
        assert "trigger actually occurs" in prompt
        assert len(client.calls) == 3
        await manager.close()

    asyncio.run(run())


def test_planner_cannot_invent_probability_from_player_action():
    async def run():
        settings.llm.context_window_size = 32_768
        settings.llm.max_retries = 0
        client = FakeClient(
            DicePlan(
                rolls={"Host": False},
                hidden_rolls=[],
                chance_rule_decisions={
                    "rule-1": ChanceRuleDecision(
                        trigger="condition", occurrences=["Host enters"], reason="Entered"
                    )
                },
            )
        )
        manager = LLMContextManager(client)
        manager.set_genesis("A tower", "Keep the surroundings peaceful.")
        with pytest.raises(LLMResolutionError, match="every private rule"):
            await manager.plan_dice({"Host": RULE})
        assert manager.history == []
        await manager.close()

    asyncio.run(run())


@pytest.mark.parametrize("leak", ["The hidden check rolled 19.", "The collapse had a 20% chance."])
def test_private_event_mechanics_rejected_before_remembering(leak):
    async def run():
        settings.llm.context_window_size = 32_768
        settings.llm.max_retries = 0
        client = FakeClient(
            RoundResolution(global_narrative=leak, player_resolutions={"Host": "Host enters."})
        )
        manager = LLMContextManager(client)
        manager.set_genesis("A tower", RULE)
        with pytest.raises(LLMResolutionError, match="private"):
            await manager.generate_resolution(
                {"Host": "Enter"},
                chance_events=[ChanceEventResult(event=event(), roll=19, occurred=True)],
            )
        assert manager.history == []
        await manager.close()

    asyncio.run(run())


def test_planner_repairs_omitted_rule_and_preserves_multiple_occurrences():
    async def run():
        settings.llm.context_window_size = 32_768
        other = "Add a 20% chance every round that a stranger appears."
        checks = [
            event(source_rule="rule-1"),
            event(source_rule="rule-1", occurrence="Player enters the barn"),
            event(source_rule="rule-2", trigger="per_round", occurrence="round"),
        ]
        calls = []

        def response(kwargs):
            if kwargs["response_format"] is ConditionalCheckAudit:
                return ConditionalCheckAudit(missing_occurrences=[], invalid_occurrences=[])
            calls.append(kwargs)
            ids = ["rule-2"] if len(calls) == 1 else ["rule-1", "rule-2"]
            return DicePlan(
                rolls={"Host": False, "Player": False},
                hidden_rolls=[],
                chance_rule_decisions={
                    rule_id: ChanceRuleDecision(
                        trigger="per_round" if rule_id == "rule-2" else "condition",
                        occurrences=[
                            check.occurrence for check in checks if check.source_rule == rule_id
                        ],
                        reason="Current round applies.",
                    )
                    for rule_id in ids
                },
            )

        manager = LLMContextManager(FakeClient(response))
        manager.set_genesis("Two buildings", RULE + "\n" + other)
        plan = await manager.plan_dice({"Host": "Enter the tower", "Player": "Enter the barn"})
        assert len(calls) == 2
        assert [item.source_rule for item in plan.chance_events] == [RULE, RULE, other]
        schema = calls[0]["response_format"].model_json_schema()
        coverage = schema["properties"]["chance_rule_decisions"]
        assert coverage["required"] == ["rule-1"]
        assert coverage["additionalProperties"] is False
        assert "every private rule" in calls[1]["messages"][-1]["content"]
        await manager.close()

    asyncio.run(run())


@pytest.mark.parametrize("casts_spell", [False, True])
def test_reported_clothing_and_sprite_rules_are_evaluated_independently(casts_spell):
    async def run():
        settings.llm.context_window_size = 32_768
        clothing = (
            "Include an 80% chance one of the players have their clothes turn into a clown "
            "uniform every round."
        )
        sprite = "Include a 50% chance that a helpful sprite appears when a player casts a spell."
        guidance = (
            clothing + " \n" + sprite + "\r\nIntroduce new NPCs when the plot seems to stagnate."
        )
        events = [
            event(source_rule="rule-1", chance_percent=80, trigger="per_round", occurrence="round")
        ]
        if casts_spell:
            events.append(
                event(source_rule="rule-2", chance_percent=50, occurrence="Host casts a spell")
            )
        client = FakeClient(
            DicePlan(
                rolls={"Host": False},
                hidden_rolls=[],
                chance_rule_decisions={
                    "rule-1": ChanceRuleDecision(
                        trigger="per_round", occurrences=[], reason="Every round."
                    ),
                    "rule-2": ChanceRuleDecision(
                        trigger="condition",
                        occurrences=["Host casts a spell"] if casts_spell else [],
                        reason="Host casts a spell." if casts_spell else "Nobody casts a spell.",
                    ),
                },
            )
        )
        manager = LLMContextManager(client)
        manager.set_genesis("A magical village", guidance)
        plan = await manager.plan_dice({"Host": "Cast a spell" if casts_spell else "Look around"})
        assert [check.chance_percent for check in plan.chance_events] == (
            [80, 50] if casts_spell else [80]
        )
        assert set(plan.chance_rule_decisions) == {"rule-1", "rule-2"}
        assert plan.chance_events[0].source_rule == clothing
        if casts_spell:
            assert plan.chance_events[1].source_rule == sprite
        await manager.close()

    asyncio.run(run())


def test_occurrences_are_the_single_source_of_chance_rolls():
    decisions = {
        "rule-1": ChanceRuleDecision(
            trigger="condition", occurrences=[event().occurrence], reason="Building entered."
        )
    }
    assert dice.chance_events_from_decisions(decisions, RULE) == [event()]
    decisions["rule-1"].occurrences = []
    assert dice.chance_events_from_decisions(decisions, RULE) == []


def test_per_round_chance_events_do_not_require_a_model_decision():
    guidance = "Add a 40% chance every round that a Banana Split appears from nowhere."
    assert dice.chance_events_from_decisions({}, guidance) == [
        ChanceEvent(
            source_rule=guidance,
            trigger="per_round",
            occurrence="round",
            chance_percent=40,
        )
    ]


def test_unspecified_trigger_per_round_decision_keeps_absurd_action_roll():
    async def run():
        rule = (
            "Add a 40% chance one of the players suddenly gets handed a Banana Split from nowhere"
        )
        client = FakeClient(
            DicePlan(
                rolls={"Arxs": True},
                hidden_rolls=[],
                chance_rule_decisions={
                    "rule-1": ChanceRuleDecision(
                        trigger="per_round", occurrences=["round"], reason="No trigger specified."
                    )
                },
            )
        )
        manager = LLMContextManager(client)
        manager.set_genesis("A modern city.", rule)
        plan = await manager.plan_dice({"Arxs": "Manifest a company of Ultramarines."})
        assert plan.rolls == {"Arxs": True} and plan.hidden_rolls == []
        assert len(plan.chance_events) == 1
        assert plan.chance_events[0].trigger == "per_round"
        assert len(client.calls) == 1
        await manager.close()

    asyncio.run(run())


def test_conditional_chance_trigger_cannot_be_reclassified_as_per_round():
    decision = ChanceRuleDecision(
        trigger="per_round",
        occurrences=[event().occurrence],
        reason="The building was entered.",
    )

    normalized = dice.normalize_chance_rule_decisions({"rule-1": decision}, RULE)
    events = dice.chance_events_from_decisions(normalized, RULE)

    assert normalized["rule-1"].trigger == "condition"
    assert events == [event()]


def test_chance_rule_without_explicit_trigger_defaults_to_per_round():
    rule = "Add a 40% chance one of the players suddenly gets handed a Banana Split from nowhere."
    decisions = {
        "rule-1": ChanceRuleDecision(
            trigger="condition", occurrences=[], reason="No trigger occurred."
        )
    }

    events = dice.chance_events_from_decisions(decisions, rule)

    assert events == [
        ChanceEvent(
            source_rule=rule,
            trigger="per_round",
            occurrence="round",
            chance_percent=40,
        )
    ]


def test_per_round_plan_needs_no_audit_and_invalid_hidden_source_stays_public():
    async def run():
        guidance = "Introduce strange visitors when the story stalls."
        chance = "Add a 40% chance per round that a Banana Split appears."
        drafts = []

        def response(kwargs):
            assert kwargs["response_format"] is not SummaryAudit
            drafts.append(kwargs)
            return DicePlan(
                rolls={"Arxs": True},
                hidden_rolls=["Arxs"] if len(drafts) == 1 else [],
                hidden_roll_sources={"Arxs": chance} if len(drafts) == 1 else {},
            )

        manager = LLMContextManager(FakeClient(response))
        manager.set_genesis("A modern city with reality warps.", guidance + "\n" + chance)
        plan = await manager.plan_dice({"Arxs": "Use a Vox-Caster to contact the Inquisition."})
        assert plan.rolls == {"Arxs": True} and plan.hidden_rolls == []
        assert len(plan.chance_events) == 1
        assert plan.chance_events[0].chance_percent == 40
        assert len(drafts) == 1
        schema = drafts[0]["response_format"].model_json_schema()
        sources = schema["properties"]["hidden_roll_sources"]["properties"]["Arxs"]["enum"]
        assert sources == ["", guidance]
        await manager.close()

    asyncio.run(run())


def test_explicit_per_turn_rules_are_authoritative_when_model_omits_occurrences():
    """A model cannot suppress unconditional percentage events by misclassifying them."""
    guidance = "\n".join(
        [
            "Add a 40% chance per turn that dwarves intervene.",
            "Add a 60% chance per turn that a voice narrates the scene.",
            "Add a 50% chance per turn that one player's clothing changes.",
        ]
    )
    decisions = {
        f"rule-{index}": ChanceRuleDecision(
            trigger="condition", occurrences=[], reason="No conditional trigger occurred."
        )
        for index in range(1, 4)
    }

    events = dice.chance_events_from_decisions(decisions, guidance)

    assert [(event.trigger, event.occurrence, event.chance_percent) for event in events] == [
        ("per_round", "round", 40),
        ("per_round", "round", 60),
        ("per_round", "round", 50),
    ]


def test_percentage_only_guidance_cannot_enable_hidden_action_rolls():
    assert not dice.has_non_percentage_private_guidance(RULE)
    assert dice.has_non_percentage_private_guidance(RULE + "\nThe gate has an invisible alarm.")


def test_planner_keeps_freeform_steering_general_and_absurd_checks_public():
    async def run():
        client = FakeClient(DicePlan(rolls={"Host": True}, hidden_rolls=[]))
        manager = LLMContextManager(client)
        manager.set_genesis(
            "A modern city.",
            "Keep the world coherent while introducing surprising but compatible details.",
        )
        await manager.plan_dice({"Host": "Find the nearest base of the Human Imperium."})
        prompt = client.calls[0]["messages"][-1]["content"]
        assert "setting-conflicting attempt" in prompt
        assert "freeform steering or a percentage event" in prompt
        assert "style, tone, diction" not in prompt
        await manager.close()

    asyncio.run(run())


def test_plan_audit_repairs_skipped_spell_triggers_alongside_every_round_event():
    """Audit runs even when a conditional rule incorrectly claims no occurrences."""

    async def run():
        clothing = "Include an 80% chance clothes become a clown uniform every round."
        sprite = "Include a 50% chance a helpful sprite appears when a player casts a spell."
        drafts = []
        audits = []

        def response(kwargs):
            if kwargs["response_format"] is ConditionalCheckAudit:
                audits.append(kwargs)
                passed = len(audits) > 1
                return ConditionalCheckAudit(
                    invalid_occurrences=[],
                    missing_occurrences=(
                        []
                        if passed
                        else ["Host conjuring fire and Player casting ice each trigger rule-2."]
                    ),
                )
            drafts.append(kwargs)
            return DicePlan(
                rolls={"Host": False, "Player": False},
                hidden_rolls=[],
                chance_rule_decisions={
                    "rule-1": ChanceRuleDecision(
                        trigger="per_round", occurrences=[], reason="Every round."
                    ),
                    "rule-2": ChanceRuleDecision(
                        trigger="condition",
                        occurrences=(
                            [] if len(drafts) == 1 else ["Host conjures fire", "Player casts ice"]
                        ),
                        reason="No spells." if len(drafts) == 1 else "Both players cast spells.",
                    ),
                },
            )

        manager = LLMContextManager(FakeClient(response))
        manager.set_genesis("Two wizards in a clearing.", clothing + "\n" + sprite)
        plan = await manager.plan_dice({"Host": "Conjure fire", "Player": "Cast an ice spell"})
        assert len(drafts) == len(audits) == 2
        assert [check.chance_percent for check in plan.chance_events] == [80, 50, 50]
        assert [check.occurrence for check in plan.chance_events] == [
            "round",
            "Host conjures fire",
            "Player casts ice",
        ]
        assert manager.game_usage.attempts == 4
        assert manager.history == []
        audit_input = audits[0]["messages"][-1]["content"]
        assert "Conjure fire" in audit_input and "Cast an ice spell" in audit_input
        await manager.close()

    asyncio.run(run())


def test_unrelated_private_guidance_cannot_hide_public_action_roll(tmp_path, monkeypatch):
    """Correct the plan before rolls are classified in public events and HTML."""

    async def run():
        engine, sender, resolver = await setup(tmp_path)
        guidance = "Introduce new NPCs when the plot seems to stagnate."
        engine.private_guidance = guidance
        drafts = []

        def response(kwargs):
            if kwargs["response_format"] is SummaryAudit:
                return SummaryAudit(
                    preserved=False,
                    corrections=["NPC pacing guidance does not make Host's ordinary roll private."],
                )
            drafts.append(kwargs)
            return DicePlan(
                rolls={"Host": True, "Player": False},
                hidden_rolls=["Host"] if len(drafts) == 1 else [],
                hidden_roll_sources={"Host": guidance} if len(drafts) == 1 else {},
            )

        manager = LLMContextManager(FakeClient(response))
        manager.set_genesis("A locked gate", guidance)
        monkeypatch.setattr(resolver, "plan_dice", manager.plan_dice)
        monkeypatch.setattr("logic.engine.roll_d100", lambda: 79)
        await submit_round(engine)
        await engine.wait_for_inference()
        assert not engine.round_paused and engine.round_counter == 1
        assert len(drafts) == 1
        assert sender.events_of_type("state_update")[-1].payload["dice_results"] == {"Host": 79}
        archive = engine.transcript.path.read_text(encoding="utf-8")
        assert "Host: 79/100 (resounding success)" in archive
        assert "Private checks from DM guidance" not in archive
        await manager.close()
        await engine.shutdown()

    asyncio.run(run())


def test_confused_hidden_audit_cannot_replan_public_rolls_or_per_round_events():
    async def run():
        guidance = 'Reply in caveman-style language only. Example: "Dwarf hit player."'
        chance = "Add a 40% chance per round that a Banana Split appears."
        audits = []

        def response(kwargs):
            if kwargs["response_format"] is SummaryAudit:
                audits.append(kwargs)
                return SummaryAudit(
                    preserved=False,
                    corrections=["Missing Banana Split check; make psychic powers hidden."],
                )
            return DicePlan(
                rolls={"Arxs": True},
                hidden_rolls=["Arxs"],
                hidden_roll_sources={"Arxs": guidance},
            )

        client = FakeClient(response)
        manager = LLMContextManager(client)
        manager.set_genesis("A modern city.", guidance + "\n" + chance)
        plan = await manager.plan_dice({"Arxs": "Manifest a world using psychic powers."})
        assert plan.rolls == {"Arxs": True}
        assert plan.hidden_rolls == [] and plan.hidden_roll_sources == {}
        assert len(plan.chance_events) == 1 and plan.chance_events[0].chance_percent == 40
        assert len(client.calls) == 2 and len(audits) == 1
        assert "Banana Split" not in str(audits[0]["messages"])
        await manager.close()

    asyncio.run(run())


@pytest.mark.parametrize("source", ["", RULE, "Invented hidden source that is not guidance."])
def test_unsupported_hidden_sources_keep_action_roll_public(source):
    manager = LLMContextManager(FakeClient())
    guidance = "The locked gate conceals a private alarm."
    manager.set_genesis("A gate", guidance + "\n" + RULE)
    plan = manager._normalize_hidden_roll_sources(
        DicePlan(rolls={"Host": True}, hidden_rolls=["Host"], hidden_roll_sources={"Host": source})
    )
    assert plan.rolls == {"Host": True}
    assert plan.hidden_rolls == [] and plan.hidden_roll_sources == {}


def test_valid_private_source_keeps_its_check_hidden():
    manager = LLMContextManager(FakeClient())
    guidance = "The locked gate conceals a private alarm."
    manager.set_genesis("A gate", guidance)
    plan = manager._normalize_hidden_roll_sources(
        DicePlan(
            rolls={"Host": True}, hidden_rolls=["Host"], hidden_roll_sources={"Host": guidance}
        )
    )
    assert plan.hidden_rolls == ["Host"]


@pytest.mark.parametrize("failed_attempts", [1, 2])
def test_multiple_rules_and_occurrences_roll_independently_and_survive_retry(
    tmp_path, monkeypatch, failed_attempts
):
    async def run():
        engine, sender, resolver = await setup(tmp_path)
        other = "Add a 20% chance every round that a stranger appears."
        engine.private_guidance = RULE + "\n" + other
        checks = [
            event(),
            event(occurrence="Player enters the barn"),
            event(source_rule=other, trigger="per_round", occurrence="round"),
        ]
        values = iter([0, 99, 19])
        draws = []
        received = []

        def draw(upper):
            draws.append(upper)
            return next(values)

        async def plan(actions, current_state=""):
            return DicePlan(
                rolls={name: False for name in actions}, hidden_rolls=[], chance_events=checks
            )

        async def resolve(actions, dice_results=None, hidden_rolls=None, chance_events=None):
            received.append(chance_events)
            if len(received) <= failed_attempts:
                raise LLMResolutionError("Retry needed")
            return RoundResolution(
                global_narrative="The tower collapses; a stranger approaches the intact barn.",
                player_resolutions={name: f"{name} watches the stranger." for name in actions},
            )

        monkeypatch.setattr(dice.secrets, "randbelow", draw)
        monkeypatch.setattr(resolver, "plan_dice", plan)
        monkeypatch.setattr(resolver, "generate_resolution", resolve)
        await submit_round(engine)
        await engine.wait_for_inference()
        assert not engine.round_paused
        assert not sender.events_of_type("error")
        assert engine.round_counter == 1
        assert draws == [100, 100, 100]
        assert len(received) == failed_attempts + 1
        assert all(item == received[0] for item in received)
        assert received[0] == received[1]
        assert [result.roll for result in received[1]] == [1, 100, 20]
        assert [result.occurred for result in received[1]] == [True, False, True]
        archive = engine.transcript.path.read_text(encoding="utf-8")
        for roll in (1, 100, 20):
            assert f"roll {roll}/100" in archive
        assert sender.events_of_type("state_update")[-1].payload["dice_results"] == {}
        await engine.shutdown()

    asyncio.run(run())


@pytest.mark.parametrize("repair_succeeds", [False, True])
def test_successful_event_omission_is_repaired_or_paused_before_remembering(repair_succeeds):
    """A successful roll cannot be committed solely because the round JSON is valid."""

    async def run():
        settings.llm.context_window_size = 32_768
        settings.llm.max_retries = 1
        rule = "Include a 50% chance one player's clothes turn into a clown costume every round."
        check = ChanceEventResult(
            event=event(
                source_rule=rule, chance_percent=50, trigger="per_round", occurrence="round"
            ),
            roll=30,
            occurred=True,
        )
        drafts = []
        audits = []

        def response(kwargs):
            if kwargs["response_format"] is SummaryAudit:
                audits.append(kwargs)
                return SummaryAudit(
                    preserved=repair_succeeds and len(audits) == 2,
                    corrections=["Host's clothes must visibly become a clown costume."],
                )
            text = (
                "Host's clothes become a clown costume."
                if repair_succeeds and drafts
                else "Host watches the gate."
            )
            draft = RoundResolution(
                global_narrative="The gate remains locked.", player_resolutions={"Host": text}
            )
            drafts.append(draft)
            return draft

        client = FakeClient(response)
        manager = LLMContextManager(client)
        manager.set_genesis("A gate", rule)
        if repair_succeeds:
            result = await manager.generate_resolution({"Host": "Watch"}, chance_events=[check])
            assert "clown costume" in result.player_resolutions["Host"]
            assert result.global_narrative == "The gate remains locked."
            assert json.loads(manager.history[-1]["content"]) == result.model_dump()
            assert len(manager.history) == 2
            assert "Host watches the gate." not in manager.history[-1]["content"]
        else:
            with pytest.raises(LLMResolutionError, match="consequences were omitted"):
                await manager.generate_resolution({"Host": "Watch"}, chance_events=[check])
            assert manager.history == []
        assert len(drafts) == len(audits) == 2
        assert manager.game_usage.attempts == 4
        for call in client.calls:
            text = " ".join(message["content"] for message in call["messages"])
            assert '"roll": 30' in text and '"occurred": true' in text
        assert client.calls[1]["max_completion_tokens"] == settings.llm.summary_output_tokens
        assert "same event results" in client.calls[2]["messages"][-1]["content"]
        await manager.close()

    asyncio.run(run())


def test_nonblocking_event_prompt_preserves_the_original_action():
    async def run():
        settings.llm.context_window_size = 32_768
        rule = "Include a 50% chance one player's clothes change every round."
        check = ChanceEventResult(
            event=event(
                source_rule=rule, chance_percent=50, trigger="per_round", occurrence="round"
            ),
            roll=1,
            occurred=True,
        )
        resolution = RoundResolution(
            global_narrative="The window reveals a moonlit courtyard.",
            player_resolutions={
                "Host": (
                    "Host's clothing changes into a velvet robe, and Host looks out the "
                    "window to see the courtyard."
                )
            },
        )

        def response(kwargs):
            return (
                SummaryAudit(preserved=True, corrections=[])
                if kwargs["response_format"] is SummaryAudit
                else resolution
            )

        client = FakeClient(response)
        manager = LLMContextManager(client)
        manager.set_genesis("A tower", rule)
        await manager.generate_resolution(
            {"Host": "Look outside the window"}, chance_events=[check]
        )
        prompt = client.calls[0]["messages"][-1]["content"]
        assert "Chance effects are modifiers, not replacements for player actions" in prompt
        assert "does not stop them from looking out a window" in prompt
        await manager.close()

    asyncio.run(run())


@pytest.mark.parametrize("per_round", [False, True])
def test_round_retries_reuse_events_and_only_private_archives_expose_them(
    tmp_path, monkeypatch, caplog, per_round
):
    caplog.set_level("INFO", logger="logic.engine")

    async def run():
        engine, sender, resolver = await setup(tmp_path)
        check = (
            event(
                source_rule="Add a 20% chance every round that a stranger appears.",
                trigger="per_round",
                occurrence="round",
            )
            if per_round
            else event()
        )
        engine.private_guidance = check.source_rule
        plans = []
        received = []
        draws = []

        async def plan(actions, current_state=""):
            plans.append(actions)
            # A second round with no new entry has no conditional check.
            return DicePlan(
                rolls={name: False for name in actions},
                hidden_rolls=[],
                chance_events=[check] if per_round or len(plans) == 1 else [],
            )

        async def resolve(actions, dice_results=None, hidden_rolls=None, chance_events=None):
            received.append(chance_events)
            if len(received) <= 3:
                raise LLMResolutionError("Temporary failure")
            return RoundResolution(
                global_narrative="The tower falls.",
                player_resolutions={name: f"{name} sees falling stones." for name in actions},
            )

        def draw(upper):
            draws.append(upper)
            return 19

        monkeypatch.setattr(resolver, "plan_dice", plan)
        monkeypatch.setattr(resolver, "generate_resolution", resolve)
        monkeypatch.setattr(dice.secrets, "randbelow", draw)
        await submit_round(engine)
        await engine.wait_for_inference()
        assert engine.round_paused
        await engine.process_payload("host", payload("retry_round"))
        await engine.wait_for_inference()
        assert engine.round_counter == 1
        assert received[0] == received[1]
        assert received[1][0].occurred
        assert len(plans) == 1 and draws == [100]
        logs = [
            record.getMessage()
            for record in caplog.records
            if "Private chance events" in record.getMessage()
        ]
        assert len(logs) == 4
        assert all(item == received[0] for item in received)
        assert "reused=False" in logs[0] and "reused=True" in logs[1]
        for line in logs:
            assert check.source_rule in line
            assert '"chance_percent": 20' in line
            assert '"roll": 20' in line and '"occurred": true' in line
        public = json.dumps([message.model_dump() for _, message in sender.events])
        assert "chance_events" not in public and check.source_rule not in public
        assert "chance_percent" not in public and "source_rule" not in public
        archive = engine.transcript.path.read_text(encoding="utf-8")
        assert "Private percentage events" in archive
        if not per_round:
            assert "Host enters the &lt;tower&gt;" in archive
        assert "20% chance; roll 20/100; triggered" in archive
        await submit_round(engine)
        await engine.wait_for_inference()
        assert engine.round_counter == 2 and len(plans) == 2
        if per_round:
            assert received[-1][0].event == check and draws == [100, 100]
        else:
            assert received[-1] is None and draws == [100]
        await engine.shutdown()

    asyncio.run(run())
