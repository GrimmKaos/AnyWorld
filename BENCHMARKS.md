# Local backend benchmarks

Measured 2026-09-28 against the already-running local llama.cpp server. Backend profile: build
`b11194-9f70b2cec`, Gemma 4 26B A4B Instruct IQ3_XS, one slot, `n_ctx=244736` from `/props`.
Reasoning effort was `none`; the round-usage runner also explicitly disabled thinking. These are
single-run observations on this machine, not general performance guarantees.

The runner names below identify historical local tools; those scripts and raw reports are not
included in this checkout. These measurements were not rerun for the 2026-10-04 documentation
and commit.

## Adjudication

`tests/benchmark_adjudication.py` completed all four probes with no request errors. Three matched
their expected roll decisions; the hidden-hazard probe did not:

| Case | Expected action roll | Expected hidden | Result | Reference match |
| --- | --- | --- | --- | --- |
| Safe unlocked door | No | No | No roll | Yes |
| Hidden needle hazard | Yes | Yes | No roll | **No** |
| Injured player crossing an icy beam | Yes | No | Public roll | Yes |
| Two players contesting a key | Yes | No | Both received public rolls | Yes |

The hidden-hazard scenario described a spring-loaded needle behind the latch and explicitly said
opening without disarming it requires a hidden dexterity check. The planner returned `false` with no
hidden roll. This is a concrete false negative for this fixture; it does not estimate the general
miss rate.

## Memory retention

`tests/benchmark_memory.py` replayed 12 synthetic rounds with the reference facts only in round
one. Both runs had zero request errors and passed all seven substring checks for the injury, key,
coin count, consumed vial, Mira relationship, dusk promise, and locked gate.

| History-round limit | Wall time | Inference attempts | Total tokens | Retained pairs | Fact checks |
| ---: | ---: | ---: | ---: | ---: | ---: |
| None | 13.03 s | 12 | 36,756 | 12 | 7/7 |
| 4 | 18.22 s | 18 | 33,301 | 3 | 7/7 |

The limit-4 run made six additional summary/audit calls, reduced retained history from 12 pairs to
3, and used 3,455 fewer total tokens for this fixture. The checks are simple substring tests over
retained memory and history, not proof that arbitrary facts survive summarization.

## Prefix and tokenizer cache

`tests/benchmark_prefix.py` preserved equivalent JSON values and stored the original response text.
The rendered raw and canonical assistant messages were 953 and 935 tokens, with an 836-token
common prefix. Backend tokenization measured the same next-request input at 1,469 tokens both
times; the first count took 4.35 ms and the cached count 0.011 ms. The generation reused 835 prompt
tokens and processed 5. This measures text/tokenizer prefix overlap, not guaranteed KV-cache
savings.

## Round usage and prompt reuse

`tests/benchmark_round_usage.py` used a fixed opening, two or six players, short or long actions,
and a 20-second delay before each repeat. The first dice request in each pair used
`cache_prompt=false`; this bypasses reuse for that planner request, but does not clear the
server-wide cache. Each resolution can reuse the dice request's prefix, including in a nominally
cold pair. The table therefore reports actual processed/reused counters rather than treating cold
as a fully empty cache.

| Players | Actions | First wall time | Repeat wall time | First processed / reused tokens | Repeat processed / reused tokens | Errors |
| ---: | --- | ---: | ---: | ---: | ---: | ---: |
| 2 | Short | 3.29 s | 2.48 s | 1,412 / 436 | 10 / 1,838 | 0 |
| 2 | Long | 3.56 s | 3.00 s | 4,036 / 436 | 10 / 4,462 | 0 |
| 6 | Short | 3.76 s | 3.36 s | 1,525 / 445 | 10 / 1,960 | 0 |
| 6 | Long | 5.23 s | 4.08 s | 9,297 / 470 | 10 / 9,757 | 0 |

All eight matrix runs completed without errors or retries. The short/long inputs and warm repeats
are synthetic; observed wall times depend on the loaded model, backend load, and cache state.
