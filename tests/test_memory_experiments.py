"""Deterministic comparison cases; no deployed-backend evaluation or activation."""

import asyncio
import json

import pytest

from core.schemas import ContextSummary, DicePlan, AuditVerdict
from logic.llm.experiments import (
    FactDelta,
    FactLedger,
    classify_hidden_checks_batch,
    without_duplicate_state,
)


def test_versioned_ledger_preserves_superseded_evidence_and_validates_transactions():
    ledger = FactLedger()
    with pytest.raises(RuntimeError, match="disabled"):
        ledger.stage([])
    ledger = FactLedger(enabled=True)
    cases = [
        ("identity", "Alice the ranger"),
        ("inventory", "vial consumed"),
        ("injury", "broken wrist"),
        ("deadline", "before dusk"),
        ("thread", "missing map"),
    ]
    changes = [
        FactDelta(
            key=kind,
            category=kind,
            value=value,
            source_round=1,
            evidence=f"Accepted round 1: {value}",
            expected_version=0,
        )
        for kind, value in cases
    ]
    pending = ledger.stage(changes)
    assert not ledger.current
    ledger.commit(pending)
    with pytest.raises(RuntimeError, match="stale"):
        ledger.commit(pending)
    summary = ContextSummary(
        world_state="The vial consumed; find the missing map before dusk.",
        player_states={"Alice": "Alice the ranger has a broken wrist."},
        important_npcs="",
        unresolved_threads=["missing map"],
    )
    assert all(ledger.compare_summary(summary).values())
    faulty = summary.model_copy(
        update={"world_state": "Find the map before dawn.", "unresolved_threads": []}
    )
    coverage = ledger.compare_summary(faulty)
    assert not coverage["inventory"] and not coverage["deadline"] and not coverage["thread"]
    replacement = changes[1].model_copy(
        update={"value": "vial replaced", "source_round": 2, "expected_version": 1}
    )
    ledger.commit(ledger.stage([replacement]))
    assert ledger.current["inventory"].version == 2
    assert ledger.evidence[1].delta.value == "vial consumed"
    with pytest.raises(ValueError, match="superseded"):
        ledger.stage([changes[1]])


def test_batch_audits_are_disabled_by_default_and_keep_separate_verdicts():
    async def run():
        calls = []
        plan = DicePlan(
            rolls={"Alice": True, "_Bob": True},
            hidden_rolls=["Alice", "_Bob"],
            hidden_roll_sources={"Alice": "A hidden trap", "_Bob": "Keep the tone eerie"},
        )

        async def parse(messages, schema, kind):
            calls.append((messages, kind))
            return schema(
                verdicts={
                    "Alice": AuditVerdict(preserved=True),
                    "_Bob": AuditVerdict(preserved=False),
                }
            )

        with pytest.raises(RuntimeError, match="disabled"):
            await classify_hidden_checks_batch(parse, plan, {})
        result = await classify_hidden_checks_batch(
            parse, plan, {"world_state": "The trap remains armed"}, enabled=True
        )
        assert len(calls) == 1 and calls[0][1] == "dice_audit"
        assert result.hidden_rolls == ["Alice"]
        assert result.rolls == plan.rolls

    asyncio.run(run())


def test_redundant_context_requires_exact_latest_authoritative_evidence():
    prompt = {"role": "user", "content": "Current game state:\nGate locked\n\nActions: Wait"}
    history = [{"role": "assistant", "content": json.dumps({"global_narrative": "Gate locked"})}]
    with pytest.raises(RuntimeError, match="disabled"):
        without_duplicate_state(prompt, history, "Gate locked")
    assert (
        without_duplicate_state(prompt, history, "Gate locked", enabled=True)["content"]
        == "Actions: Wait"
    )
    assert without_duplicate_state(prompt, history, "Gate opened", enabled=True) == prompt
    assert without_duplicate_state(prompt, [], "Gate locked", enabled=True) == prompt
