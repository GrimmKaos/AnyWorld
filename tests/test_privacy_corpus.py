"""Adversarial literal corpus with explicit semantic-guard limitations."""

import pytest

from core.schemas import RoundResolution
from logic.llm.errors import LLMResolutionError
from logic.llm.validation import check_public_output


@pytest.mark.parametrize(
    "guidance,public",
    [
        ("Password: XY", "The password is XY."),
        ("PIN: 7", "Enter PIN 7 to unlock it."),
        ("Secret code is red", "The code is RED."),
        ("Salainen tunnus on XY", "Tunnus on XY."),
        ("秘密口令是青", "口令是青"),
        ("秘密の合言葉は赤", "合言葉は赤"),
    ],
)
def test_named_short_secrets_and_multilingual_literals(guidance, public):
    with pytest.raises(LLMResolutionError):
        check_public_output(
            RoundResolution(global_narrative=public, player_resolutions={}), {}, guidance
        )


def test_arbitrary_secret_paraphrases_remain_an_explicit_limitation():
    # This guard cannot prove that arbitrary rewritten hidden facts are safe.
    # Keeping this case documents the limitation; it is not a privacy success claim.
    check_public_output(
        RoundResolution(
            global_narrative="Your ally is secretly working for the enemy.", player_resolutions={}
        ),
        {},
        "Mira has betrayed the party and serves the king.",
    )
