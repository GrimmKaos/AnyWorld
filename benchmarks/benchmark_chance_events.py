"""Opt-in live benchmark for conditional and per-round chance-event recognition.

Run this script against the configured llama.cpp/OpenAI-compatible backend.  It intentionally
uses a deterministic 100% chance so the benchmark isolates recognition from probability noise.
Conditional cases are run in a fresh context twice: once with the action that should trigger the
rule and once with an action that should not. Per-round cases use two ordinary actions and should
produce the event for both. The console output and optional JSON report include complete
structured resolutions for local review. Usage and token diagnostics are not part of this
benchmark.
"""

import argparse
import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from logic.dice import roll_chance
from logic.llm_manager import LLMContextManager


@dataclass(frozen=True)
class ChanceCase:
    """One chance rule and paired actions for the live benchmark."""

    name: str
    event_description: str
    scenario: str
    triggering_actions: dict[str, str]
    non_triggering_actions: dict[str, str]
    event_type: str = "conditional"


CASES = (
    ChanceCase(
        "fireball_speech",
        (
            "100% chance that if a player casts a fireball, they lose the ability to speak and "
            "understand language for a short duration."
        ),
        "Arxs stands in a torchlit stone chamber facing a wooden training target.",
        {"Arxs": "Cast a fireball at the wooden training target."},
        {"Arxs": "Search the torchlit chamber for a hidden latch."},
    ),
    ChanceCase(
        "cursed_chest_shadow",
        "100% chance that when a player opens the cursed iron chest, a shadow begins "
        "following them.",
        "Arxs finds a locked-looking cursed iron chest on a dusty pedestal.",
        {"Arxs": "Open the cursed iron chest."},
        {"Arxs": "Examine the cursed iron chest without opening it."},
    ),
    ChanceCase(
        "tower_transparency",
        "100% chance that upon entering the abandoned tower, the floor becomes transparent "
        "for a short time.",
        "Arxs stands outside the open doorway of an abandoned stone tower.",
        {"Arxs": "Enter the abandoned tower."},
        {"Arxs": "Inspect the abandoned tower doorway from outside."},
    ),
    ChanceCase(
        "silver_fountain_vision",
        "100% chance that if a player drinks from the silver fountain, they see a brief "
        "vision of the future.",
        "Arxs kneels beside a silver fountain whose water glows in the dark grotto.",
        {"Arxs": "Drink from the silver fountain."},
        {"Arxs": "Study the silver fountain and its carvings without drinking."},
    ),
    ChanceCase(
        "ancient_bell_ghost",
        "100% chance that whenever a player rings the ancient bell, a ghost appears nearby.",
        "Arxs is alone in a ruined chapel with an ancient bell hanging above the altar.",
        {"Arxs": "Ring the ancient bell."},
        {"Arxs": "Walk around the ancient bell and inspect its cracked surface."},
    ),
    ChanceCase(
        "moonlit_sword_glow",
        "100% chance that when a player draws the sword in the moonlit shrine, its blade "
        "begins glowing.",
        "Arxs kneels in a moonlit shrine before a sheathed ceremonial sword.",
        {"Arxs": "Draw the ceremonial sword from its sheath."},
        {"Arxs": "Pray beside the sheathed ceremonial sword without drawing it."},
    ),
    ChanceCase(
        "runes_true_name",
        "100% chance that if a player reads the runes aloud, they hear a voice speak their "
        "true name.",
        "Arxs studies a ring of glowing runes carved into the cavern wall.",
        {"Arxs": "Read the glowing runes aloud."},
        {"Arxs": "Copy the glowing runes into a notebook without reading them aloud."},
    ),
    ChanceCase(
        "black_mirror_reflection",
        "100% chance that after a player touches the black mirror, their reflection moves "
        "independently for a short time.",
        "Arxs stands before a tall black mirror in an otherwise empty gallery.",
        {"Arxs": "Touch the surface of the black mirror."},
        {"Arxs": "Look at the black mirror from several steps away without touching it."},
    ),
    ChanceCase(
        "red_seal_time_pause",
        "100% chance that once a player breaks the red wax seal, time pauses briefly in the "
        "chamber.",
        "Arxs finds a red wax seal closing a stone door in a silent archive.",
        {"Arxs": "Break the red wax seal on the stone door."},
        {"Arxs": "Read the inscription beside the intact red wax seal."},
    ),
    ChanceCase(
        "blue_lantern_dream",
        "100% chance that when a player sleeps beside the blue lantern, they receive a clue "
        "in a vivid dream.",
        "Arxs has made camp beside a blue lantern in a sheltered cave.",
        {"Arxs": "Sleep beside the blue lantern."},
        {"Arxs": "Keep watch beside the blue lantern instead of sleeping."},
    ),
    ChanceCase(
        "statue_coin_answer",
        "100% chance that if a player gives a coin to the silent statue, the statue answers "
        "one question.",
        "Arxs faces a silent stone statue with an open palm in the crossroads plaza.",
        {"Arxs": "Place a gold coin in the silent statue's open palm."},
        {"Arxs": "Ask the silent statue a question without giving it a coin."},
    ),
    ChanceCase(
        "ice_bridge_frost_mark",
        "100% chance that whenever a player steps onto the ice bridge, a frost mark appears "
        "on their hand.",
        "Arxs stands at the near end of a fragile ice bridge over a dark ravine.",
        {"Arxs": "Step onto the ice bridge."},
        {"Arxs": "Examine the ice bridge from the near side without stepping onto it."},
    ),
    ChanceCase(
        "secret_name_door",
        "100% chance that if a player speaks the secret name to the sealed door, the door "
        "opens silently.",
        "Arxs stands before a sealed obsidian door covered in tiny keyhole-shaped symbols.",
        {"Arxs": "Speak the secret name to the sealed obsidian door."},
        {"Arxs": "Press an ear to the sealed obsidian door and listen without speaking."},
    ),
    ChanceCase(
        "golden_mushroom_paths",
        "100% chance that after a player eats the golden mushroom, invisible paths become "
        "visible to them.",
        "Arxs discovers a single golden mushroom growing beside a fork in the cavern passage.",
        {"Arxs": "Eat the golden mushroom."},
        {"Arxs": "Inspect the golden mushroom and leave it uneaten."},
    ),
    ChanceCase(
        "skull_crown_whisper",
        "100% chance that when a player places the crown on the stone skull, the crown begins "
        "whispering.",
        "Arxs holds an old iron crown above a stone skull on a raised dais.",
        {"Arxs": "Place the iron crown on the stone skull."},
        {"Arxs": "Search the dais around the stone skull without placing the crown."},
    ),
    ChanceCase(
        "altar_blood_chamber",
        "100% chance that upon spilling blood on the black altar, the chamber rearranges "
        "itself.",
        "Arxs stands in a black-stone chamber before an altar covered in dry red stains.",
        {"Arxs": "Spill a drop of blood on the black altar."},
        {"Arxs": "Examine the black altar and its dry stains without spilling blood."},
    ),
    ChanceCase(
        "round_distant_bell",
        "100% chance per round that a distant bell tolls once, regardless of what the players do.",
        "Arxs is in a quiet underground gallery with several sealed passageways.",
        {"Arxs": "Search the gallery floor for concealed writing."},
        {"Arxs": "Walk toward the eastern sealed passageway."},
        "per_round",
    ),
    ChanceCase(
        "round_blue_campfire",
        "100% chance each round that the party's campfire burns blue for a moment.",
        "Arxs rests beside a small campfire in a sheltered forest clearing.",
        {"Arxs": "Check the bedrolls and organize the camp supplies."},
        {"Arxs": "Watch the tree line for movement."},
        "per_round",
    ),
    ChanceCase(
        "round_north_whisper",
        "100% chance every round that a brief whisper comes from the north tunnel.",
        "Arxs stands at a three-way fork deep inside a silent limestone cave.",
        {"Arxs": "Examine the tracks near the western tunnel."},
        {"Arxs": "Mark the southern tunnel entrance with chalk."},
        "per_round",
    ),
    ChanceCase(
        "round_shadow_exit",
        "100% chance per turn that each player's shadow points toward the nearest exit "
        "for a moment.",
        "Arxs stands alone in a circular room with three visible exits.",
        {"Arxs": "Inspect the symbols carved around the circular room."},
        {"Arxs": "Study the three exits and compare their airflow."},
        "per_round",
    ),
)


def _dice_results(plan: Any) -> dict[str, int]:
    """Supply stable action-check values for any ordinary rolls the planner requests."""
    return {player: 50 for player, required in plan.rolls.items() if required}


def _response_text(response: dict[str, Any] | None) -> str:
    """Flatten the structured narrative for the concise human-readable console output."""
    if response is None:
        return ""
    parts = [response["global_narrative"]]
    parts.extend(
        f"{player}: {narrative}" for player, narrative in response["player_resolutions"].items()
    )
    return " ".join(parts)


def _score(items: list[dict[str, Any]]) -> dict[str, Any]:
    """Return a compact model-comparison score for a group of action trials."""
    passed = sum(item["passed"] for item in items)
    total = len(items)
    return {
        "passed": passed,
        "total": total,
        "failed": total - passed,
        "success_rate_percent": round(100 * passed / total, 1) if total else 0.0,
    }


async def run_action(case: ChanceCase, label: str, actions: dict[str, str]) -> dict[str, Any]:
    """Run one independent action and retain only feature-relevant benchmark data."""
    manager = LLMContextManager()
    expected_event = case.event_type == "per_round" or label == "triggering"
    result: dict[str, Any] = {
        "case": case.name,
        "event_type": case.event_type,
        "event_description": case.event_description,
        "action_label": label,
        "actions": actions,
        "expected_event": expected_event,
        "event_detected": False,
        "event_occurred": False,
        "response": None,
    }
    error: str | None = None
    try:
        manager.set_genesis(case.scenario, case.event_description)
        manager.begin_round_usage(1)
        plan = await manager.plan_dice(actions, case.scenario)
        result["event_detected"] = bool(plan.chance_events)
        chance_results = [roll_chance(event) for event in plan.chance_events]
        result["event_occurred"] = any(item.occurred for item in chance_results)
        resolution = await manager.generate_resolution(
            actions,
            _dice_results(plan),
            set(plan.hidden_rolls),
            chance_results,
        )
        result["response"] = resolution.model_dump()
    except Exception as exc:  # Keep the wider suite running when one live call fails.
        error = f"{type(exc).__name__}: {exc}"
    finally:
        manager.finish_round_usage(error)
        await manager.close()
    result["passed"] = error is None and result["event_occurred"] == expected_event
    if error is not None:
        result["error"] = error
    return result


async def run(output: Path | None = None) -> list[dict[str, Any]]:
    """Run all paired live cases and print concise feature results."""
    report: list[dict[str, Any]] = []
    total_actions = len(CASES) * 2
    print(f"chance_events benchmark: {len(CASES)} cases, {total_actions} actions", flush=True)
    for index, case in enumerate(CASES, start=1):
        action_pairs = (
            (
                ("triggering", case.triggering_actions),
                ("non_triggering", case.non_triggering_actions),
            )
            if case.event_type == "conditional"
            else (
                ("per_round_action_one", case.triggering_actions),
                ("per_round_action_two", case.non_triggering_actions),
            )
        )
        for label, actions in action_pairs:
            result = await run_action(case, label, actions)
            report.append(result)
            status = "PASS" if result["passed"] else "FAIL"
            print(
                f"[{index}/{len(CASES)}] {status} {case.event_type} {case.name} " f"({label})",
                flush=True,
            )
            print(f"  Event: {case.event_description}", flush=True)
            print(f"  Action: {json.dumps(actions, ensure_ascii=False)}", flush=True)
            print(
                f"  Detected: {result['event_detected']} | "
                f"Occurred: {result['event_occurred']} | "
                f"Expected: {result['expected_event']}",
                flush=True,
            )
            if "error" in result:
                print(f"  Error: {result['error']}", flush=True)
            else:
                print(f"  Response: {_response_text(result['response'])}", flush=True)
    conditional = [item for item in report if item["event_type"] == "conditional"]
    per_round = [item for item in report if item["event_type"] == "per_round"]
    summary = {
        "overall": _score(report),
        "conditional": _score(conditional),
        "per_round": _score(per_round),
        "failures": [
            {
                "case": item["case"],
                "action_label": item["action_label"],
                "expected_event": item["expected_event"],
                "event_detected": item["event_detected"],
                "event_occurred": item["event_occurred"],
                **({"error": item["error"]} if "error" in item else {}),
            }
            for item in report
            if not item["passed"]
        ],
    }
    print(
        "SUMMARY "
        f"overall={summary['overall']['passed']}/{summary['overall']['total']} "
        f"({summary['overall']['success_rate_percent']:.1f}%) | "
        f"conditional={summary['conditional']['passed']}/{summary['conditional']['total']} "
        f"({summary['conditional']['success_rate_percent']:.1f}%) | "
        f"per_round={summary['per_round']['passed']}/{summary['per_round']['total']} "
        f"({summary['per_round']['success_rate_percent']:.1f}%)",
        flush=True,
    )
    if output is not None:
        output.write_text(
            json.dumps({"summary": summary, "results": report}, indent=2, ensure_ascii=False)
            + "\n",
            encoding="utf-8",
        )
    return report


def parse_args() -> argparse.Namespace:
    """Parse the optional JSON report destination."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        help="also save the JSON report to this path",
    )
    return parser.parse_args()


if __name__ == "__main__":
    asyncio.run(run(parse_args().output))
