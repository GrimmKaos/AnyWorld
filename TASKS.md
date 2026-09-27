# Tasks

Static review: 2026-09-14, including pre-existing working-tree edits. Implementation was read
only; venv and .venv were excluded. No tests, app startup or live inference were performed.
P1 = correctness/security or substantial waste; P2 = optimization/reliability; P3 = optional.
Active items remain open until their acceptance checks pass. Implementation of the 2026-09-27
review findings was authorized on 2026-09-27; other backlog items remain recommendations.

Current review policy: offline tests may run in read-only reviews when temporary files are
acceptable. Per-test working directories and their artifacts are deleted on teardown, including
after failures; forced termination can prevent cleanup. Use `python -B -m pytest -p no:cacheprovider`
with `PYTHONDONTWRITEBYTECODE=1` to avoid bytecode/cache artifacts, including in child processes.
In-process ASGI tests are allowed; live server startup and backend benchmarks remain outside this
workflow. The no-tests statement above records the historical review, not a current restriction.

Historical benchmark observations below came from local runs. Their reports and runner files
are not part of the published repository; these observations are not reproducible evidence
provided by this checkout and do not establish general performance guarantees.

## Active

- [ ] **P1 - Preserve committed rounds when delivery or usage reporting times out** - logic/engine.py.
  - Offline reproduction: a round commits and clears pending_resolution, then usage reporting reaches the job deadline. The failure handler pauses the game with no pending round to retry.
  - Keep optional token measurement outside the effects lock and inference deadline, bound its duration, and prevent post-commit failures from reverting committed state. Publish usage once after final accounting.
  - Acceptance: delayed/failed telemetry cannot pause a completed round; turn delivery and End remain responsive; genuine inference failures still retain actions/dice for retry.

- [ ] **P2 - Preserve player names during OpenAI schema cleanup** - logic/llm_manager.py:participant_schema.
  - Recursive keyword removal currently deletes property names such as pattern, minimum and format while leaving them required. Traverse schema nodes while preserving property/definition names.
  - Acceptance: all accepted player names remain in generated schemas; unsupported constraint keywords are still removed and runtime validation remains strict.

- [ ] **P2 - Separate reusable instructions from retained round data** - logic/llm_manager.py:generate_resolution, _request.
  - Offline example: a 12-character action retained a 2,449-character request before its response. Each later planner/resolver request retransmits the accumulated generic instructions.
  - Keep shared policy in stable request context and retain compact actions, authoritative dice, presence notes and outcomes. Preserve privacy, causal facts and stable serialization.
  - Acceptance: reduced retained/request tokens, with deployed-backend comparisons covering adjudication, possessions, injuries and unresolved facts; no savings percentage assumed.

- [ ] **P2 - Reuse message tokenization across output schemas** - logic/llm_manager.py:_input_tokens, preflight_round.
  - Mocked trace confirms two identical apply-template/tokenize pairs per preflight when planner and resolver use the same system prompt. Cache message counts separately from schema allowances and cache schema serialization.
  - Acceptance: one backend tokenization pair for identical messages across schemas; template/model changes invalidate counts; unavailable tokenization remains retryable and estimates remain labelled.

- [ ] **P2 - Reduce serial compaction-prefix probes** - logic/llm_manager.py:_compact_if_needed.
  - Growing-prefix scans repeatedly tokenize overlapping history, producing quadratic cumulative input volume when many prefixes fit.
  - Select likely prefixes using local estimates, then verify backend budgets and adjust conservatively. Preserve audit reserves, rollback and durable facts.
  - Acceptance: fewer backend probes for long histories with no over-budget summary/audit requests or lost memory.

- [ ] **P3 - Simplify the resolver boundary and outcome presentation** - logic/models.py; logic/engine.py; logic/llm_manager.py.
  - Make resolver fakes implement the required protocol, remove optional-method compatibility paths, and move shared exceptions out of the concrete backend.
  - Remove unreachable UUID/missing-outcome fallbacks after exact-name validation; establish one normalization boundary while keeping remembered and displayed outcomes consistent.
  - Acceptance: no unchecked resolution fallback, identical valid outcomes, and existing fake-resolver/lifecycle coverage passes.

- [ ] **P3 - Batch frontend log rendering** - static/js/app.js.
  - Append round content as a batch and scroll once per event instead of reading layout after every insertion.
  - Acceptance: action deduplication, DOM limits, round styling and reconnect rendering remain correct; long rounds avoid repeated forced layouts.

- [ ] **P2 - Use a compact fact ledger plus recent rounds for narrative memory** - core/schemas.py; logic/engine.py; logic/llm_manager.py.
  - Maintain authoritative players, world, NPCs, resources and unresolved threads, applying validated changes from the existing resolution where feasible. Keep rich prose in transcripts/UI instead of retransmitting it indefinitely.
  - Separate immutable premise from changing facts; replace superseded facts and retrieve archived details when relevant. Freeze checkpoints between compactions when practical to preserve prefix reuse. Avoid adding mandatory summarization every round.
  - Acceptance: long-session comparison against current history uses fewer total tokens without regressions in identity, possessions, injuries, causal consistency or unresolved quests.
  - Experiment (2026-09-15): periodic checkpoints reduced tokens in a synthetic replay but lost a consumed item and changed a deadline. Kept disabled; a new summary audit rejected a faulty live summary and preserved history. A lossless compact ledger remains unfinished.

- [ ] **P2 - Reduce planner and output tokens without degrading adjudication** - logic/llm_manager.py:plan_dice, generate_resolution; core/schemas.py; config.yaml.
  - Planner receives the narrative system prompt by default; `planner_system_prompt` can override it. Evaluate a concise planner prompt with necessary facts, bounded short outcomes and nonduplicative public state; avoid generating new titles the engine discards.
  - Compare two-call behavior against safe deterministic handling of routine actions. A one-call experiment could supply server-generated candidate rolls and let the model select checks, but must evaluate selection bias; never let the LLM invent authoritative rolls.
  - Acceptance: compare total tokens, latency and coherent/fair outcomes. A routine-looking action must still account for contextual hazards.
  - Experiment (2026-09-15): shorter prompts did not consistently lower total cost and a candidate missed hidden-roll classification. Full planner remains default. Configured Gemma thinking is disabled to reserve bounded output for structured answers. Safe-action overrolling remains a live-model limitation; no one-call or deterministic bypass was adopted.

- [ ] **P2 - Bound slow-socket backpressure** - api/server.py:ConnectionManager._send_text, broadcast_global.
  - Broadcasts already serialize once; per-socket send locks and a five-second timeout close failing sockets (close timeout: two seconds). Broadcasts still await all sends.
  - Verify slow-client isolation and prompt player-disconnect handling under load; consider bounded delivery queues if needed. Acceptance: stalled receivers cannot hold up healthy clients or remain active turn participants indefinitely.
  - Review (2026-09-27): sends awaited under effects_lock can delay turn delivery and End by the five-second send plus two-second close timeouts. Use bounded ordered per-client queues with prompt presence updates on overflow/failure.

- [ ] **P2 - Recover missed rounds and preserve access to full history** - static/js/app.js:applySnapshot, trimContainer; logic/lobby.py:\_snapshot_locked.
  - Existing DOM prevents snapshot state replacement after disconnect. Reload only receives current state; the 500-entry cap deletes early history without a retrieval path.
  - Add public event sequence/cursor replay and paginated/virtualized history. Acceptance: reconnect restores missed events once and users can reach the opening without unbounded DOM growth or private-memory exposure.
  - First step: track the last rendered completed round and include its full public result (outcomes and public dice) in snapshots. Never use private HTML transcripts as public replay data.

- [ ] **P2 - Verify dependency bounds and clean installation** - INSTALL.md; pyproject.toml; api/tls_bootstrap.py.
  - Runtime documentation now covers HTTPS, certificate lifetime and address coverage, private transcript contents, reconnects, and context counting. Minimum-version compatibility still needs verification.
  - Verify minimum dependencies support APIs used, including beta.chat.completions.parse; constrain a tested set. Acceptance: clean installation/documented startup works and counting limitations are explicit.

## Waiting On

- [ ] **Measure representative session length and player idle time** - Backend profile received and live runs completed on 2026-09-15: llama.cpp b10964/b29c606e2, Gemma 4 26B A4B Q4_K_XXL, canonical template, one 128000-token slot and q8_0 KV. A synthetic OpenAI run on 2026-09-27 completed 16 rounds without added inter-round waits; it stopped at round 17 after three dice-planning failures and repeated compaction rollbacks. Typical human delay/session length and idle-time effects remain unmeasured.

## Someday

- [ ] **P3 - Revisit OpenAI cache controls if deployed usage justifies tuning** - core/config.py; logic/llm_manager.py.
  - Keep reusable prefixes stable. Use routing/retention options only when supported by the selected model and Chat Completions endpoint; never pad prompts or transfer llama.cpp flags to OpenAI. Cached input still consumes context. [OpenAI prompt caching guidance](https://developers.openai.com/api/docs/guides/prompt-caching).
  - Measurement (2026-09-27): synthetic two-player run on GPT-5.6 Luna using default caching completed 16 rounds with no added idle waits. Across setup and the failed 17th round: 250,560 input tokens, 117,808 cached reads (47%), 131,154 cache-write tokens and 7,900 output tokens; estimated cost $0.0449 versus $0.0596 without cache reads/writes (about 25% lower). Resolution-call cache reads rose to about 95% by round 16; dice-planning calls had no cache hits. Round 17 stopped after three dice-planning failures, with context compaction rolling back each time.
  - Priority conclusion: demoted from P2 to P3. Default caching already yields substantial reuse and estimated savings, so no immediate cache-control change is justified. Revisit if real player idle intervals, cold/expired-cache behavior, or production cost data show a gap. The round-17 compaction failure is a separate memory/reliability concern, not evidence that cache controls need tuning.

- [ ] **P3 - Support multiple sessions and host reset** - Isolate engines, resolvers, credentials, transcripts and cancellation before adding workers/reset. Retains earlier repository backlog intent.
- [ ] **P3 - Evaluate multilingual play** - Retains earlier translation backlog intent; assess coherence and token budgets rather than assuming a model class is required.
- [ ] **P3 - Improve transcript resilience and colors** - logic/transcript.py ignores player_colors; positional CSS changes colors when participants are omitted and sorted dice can disagree. Writes/finalization are already serialized and the palette now matches the game. Apply the supplied stable color mapping to actions, outcomes and dice; add bounded filenames and exclusive creation; test retry after write failure. The unused previous_state transcript parameter and its call arguments have been removed.
- [ ] **P3 - Version static assets reproducibly** - Replace manual ?v= values with content/build hashes and suitable cache headers so unchanged assets stay cached and edits invalidate reliably.

## Done

- [x] ~~Normalize unsupported hidden-roll labels to public checks~~ (2026-09-27)
  - Missing, percentage-based, or unrelated private sources no longer fail dice planning. The
    action's planned d100 remains and is treated as public; valid cited secret sources stay hidden.

- [x] ~~Retry failed rounds automatically before pausing~~ (2026-09-27)
  - Up to two round-level retries reuse pending actions and dice, keeping the thinking indicator
    active without intermediate error messages. The existing overall deadline, cancellation and
    stale-generation guards remain effective. Exhaustion preserves manual Retry/End recovery.

- [x] ~~Narrow conditional audits and clarify unspecified chance triggers~~ (2026-09-26)
  - Conditional audits return structured occurrence differences instead of unrestricted critiques
    of public rolls or missing random results. Model-classified per-round rules bypass this audit.
    The planner defaults unspecified triggers to per-round and preserves action rolls during
    privacy repairs. Trigger interpretation still depends on model accuracy without explicit cadence.

- [x] ~~Prevent planning audits from demanding unrolled chance results~~ (2026-09-26)
  - Per-round-only public plans bypass the planning audit. Remaining audits check conditional
    occurrences and hidden-check classification before rolls, excluding per-round event data.
    Source choices are constrained to non-percentage guidance lines; repairs retain the rejected
    plan as context. Offline validation: 212 Python tests passed; live behavior remains unverified.

- [x] ~~Separate the single percentage event from freeform DM guidance~~ (2026-09-26)
  - Scenario setup now has an optional single-line `chance_event` field accepting exactly one
    whole-number percentage rule per game. Percentage rules in freeform guidance are rejected;
    the remaining guidance is non-probabilistic steering. README.md, INSTALL.md, AGENTS.md and
    the host-form documentation describe the same contract. Per-round checks are generated by
    Python, conditional triggers remain model-classified, and setting-conflicting attempts can
    receive public low-plausibility checks instead of being rejected as impossible.

- [x] ~~Separate title preparation from the generated opening~~ (2026-09-17)
  - Scenario submission generates only a title. Joining players see the host-typed prompt; Start Game generates the opening with all joined names. The prompt requests setting, goal, roles, paragraphs, and consistent physical consequences. These instructions do not guarantee model coherence.
- [x] ~~Update transcript appearance and distinguish scenario versions~~ (2026-09-17)
  - Transcripts match the game's green palette and retain both Original scenario prompt and Opening scenario. Private guidance and hidden checks are archived for the server operator, not broadcast to players.
- [x] ~~Correct retained-context display and add operational logging~~ (2026-09-17)
  - Retained context uses backend tokenization when available, with labelled fallback estimates and limit source. Logs cover compaction, inference lifecycle, retries, and private-guidance dice checks.
- [x] ~~Clean up test artifacts and refresh the documentation~~ (2026-09-17)
  - Per-test temporary directories are deleted on teardown, including after failures. Read-only reviews may run offline tests when temporary files are allowed. README is simplified; INSTALL.md holds setup and diagnostics. Latest code validation: 157 Python and 15 JavaScript tests passed; formatting/lint checks passed, with an existing Starlette/httpx deprecation warning.

- [x] ~~P2 - Instrument total round cost and cache reuse before tuning~~ (2026-09-15)
  - Per-attempt, round and game totals now include summaries/audits, retries, failures, cancellations, unknown counters and work time. The expandable usage panel separates retained context from consumption. Final deployed llama.cpp matrix: 8/8 successful cases, 2/6 players, short/long actions, cold first calls and warm repeats after 20 seconds of artificial idle. Compaction accounting is also covered offline and by a live summary/audit pair.

- [x] ~~P2 - Benchmark llama.cpp cache interference and stable prefixes~~ (2026-09-15)
  - Measured processed/reused prompt counters and latency on the deployed one-slot server, including explicit slot 0. No routing change was justified; separate auxiliary capacity remains unmeasured. Preserve original validated assistant text when normalization makes no change, avoiding unnecessary token-prefix changes. Cold first calls use cache_prompt=false because cache erase returned HTTP 501.

- [x] ~~P2 - Cache message token counts and tune compaction cadence~~ (2026-09-15)
  - Added bounded content/tokenizer and request/template/schema count caches; failed tokenizer fallbacks remain retryable. Necessary compaction targets a lower watermark with transactional validation and audit. Optional periodic checkpoints remain disabled after live retention failures. Repeated live request counts agreed (1354 tokens); counting took 4.23 ms initially and 0.31 ms cached. No general percentage saving is claimed.

- [x] ~~P2 - Validate semantics before remembering or committing LLM output~~ (2026-09-15)
  - Exact participant schemas and semantic checks reject missing/extra names, empty required content and invalid hidden-roll membership before memory/state commit. Bounded repair retains authoritative actions and dice; failed rounds pause. Normalize displayed/remembered outcomes consistently. Summary auditing rejects detected durable-fact loss transactionally; model-assisted validation cannot prove arbitrary semantic correctness.

- [x] ~~P2 - Fix stale tests and cover context/cache/lifecycle behavior~~ (2026-09-15)
  - At that milestone, 88 offline tests passed, with fake clients and isolated settings/transcripts. Added semantic, accounting, cache, template-option and discovery-recovery regressions plus separate opt-in live runners. Black, Flake8 and JavaScript syntax checks pass. Corrected a stale dice-description assertion without changing the dice distribution. One existing Starlette/httpx deprecation warning remains.


The older completed items below preserve the original problem statements; their Implementation
lines describe the fixes. Historical P1 implementation was validated with fake-model and ASGI regression tests.
The 2026-09-15 P2 work adds deployed llama.cpp measurements above; these do not establish
general long-session coherence or OpenAI performance.

- [x] ~~P1 - Enforce complete token budgets on every call~~ (2026-09-14) - logic/llm_manager.py:\_request, \_compact_if_needed, \_bounded_messages, \_count_tokens; core/config.py.
  - The reserved 2,048 tokens (or quarter-context) are never passed as an output cap. Schema/chat-template overhead is absent, cl100k_base can mismatch the local model, and len/4 can underestimate input. Summary requests bypass bounding.
  - Add provider-supported generation limits per request type, model-appropriate counting and a safety margin. Preflight aggregate actions/scenario before accepting an impossible round. Handle truncated output explicitly.
  - Acceptance: input + output allowance fits effective context for dice, opening, rounds and summaries; cover large parties, guidance and Unicode. Label fallback counts as estimates.
  - Implementation: Implemented per-kind output caps, complete request/schema/margin admission, backend template/tokenizer counting, conservative fallback, aggregate action preflight and truncation/deadline handling.

- [x] ~~P1 - Protect durable memory from FIFO eviction~~ (2026-09-14) - logic/llm_manager.py:\_compact_if_needed, \_bounded_messages; core/schemas.py:ContextSummary.
  - Compaction ignores the upcoming prompt size. Large actions can force unsummarized deletion; the historical summary itself becomes the oldest disposable pair. Failed compaction silently falls back to forgetting.
  - Separate durable memory from recent dialogue, compact against the complete upcoming request, validate bounded summaries and require useful size reduction. Retain old memory until success; pause/reject instead of dropping essential facts.
  - Acceptance: inventory, injuries, locations, NPC relationships and unresolved promises survive repeated compaction, long turns and summary failures without resurrecting obsolete facts.
  - Implementation: Replaced FIFO eviction with separate durable memory and transactional, budgeted merging; failure, cancellation, empty or expanding summaries preserve original memory.

- [x] ~~P1 - Supply authoritative facts and private triggers to dice planning~~ (2026-09-14) - logic/llm_manager.py:plan_dice; logic/engine.py:\_resolve_round; config.yaml.
  - Planning asks which rolls originate from private guidance but excludes that guidance. It only sees the latest paragraph, while the system prompt discourages repeating unchanged state; prior obstacles/capabilities can disappear.
  - Give the planner compact relevant facts/rules and private triggers, distinct from public narrative. Test that generated public prose does not disclose secret checks.
  - Acceptance: persistent injuries, locked doors and hidden hazards affect planning across quiet rounds and compaction; ordinary rolls remain public and private rolls remain private.
  - Implementation: Planner now receives genesis/private rules, durable memory and recent facts. Hidden checks stay out of public dice broadcasts (current server-side transcripts include them), and explicit output disclosures are rejected before history commit.

- [x] ~~P1 - Authenticate sockets before subscriptions or replacement~~ (2026-09-14) - api/server.py:connect, broadcast_global, websocket_endpoint; logic/lobby.py.
  - All accepted UUID sockets receive game/chat broadcasts before authentication. Browser filtering is not authorization. Reusing a UUID closes the old socket before credentials are checked; engine handlers identify callers by UUID rather than authenticated socket ownership.
  - Separate pending/authenticated connections, bind actions to authenticated sockets, and replace sessions only after successful authentication. Validate passwords in ASGI lifespan as well as CLI; bound pending connections/auth attempts.
  - Acceptance: raw unauthenticated sockets cannot receive game data, displace players or act under existing IDs; cover duplicate UUIDs and direct ASGI startup.
  - Implementation: Pending sockets are isolated; successful authentication atomically promotes sockets. Reconnects require private per-player tokens, commands check socket ownership, and lifespan enforces credentials/admission limits.

- [x] ~~P1 - Make inference independent of the receive loop and keep ENDED terminal~~ (2026-09-14) - api/server.py:websocket_endpoint; logic/engine.py:\_resolve_round; logic/lobby.py setup/start/end.
  - Inline inference blocks the triggering player's chat and host controls. Another socket can end play, but setup/start success and round-error paths overwrite ENDED. Success checking and committing are separate, and finalization can race transcript writes/events.
  - Use owned inference tasks, session/round generation identifiers, cancellation/deadlines and bounded retries. Check validity atomically with commit; serialize transcript writes/finalization and close tasks/clients in lifespan.
  - Acceptance: end during every inference phase/error never revives play or writes after finalization; all connected players retain responsive chat.
  - Implementation: Inference uses owned tasks and generation guards; end/commit events and transcript writes are ordered. Errors pause with actions/dice retained and a host retry control. Shutdown cancels work and closes clients.

- [x] ~~P1 - Resume turns after everyone disconnects~~ (2026-09-14) - logic/engine.py:\_next_turn_locked; logic/lobby.py:\_authenticate.
  - If inference finishes with nobody connected, active_player_id becomes None. Reauthentication never invokes turn selection, leaving play stuck. Older resolution commits can also clear newer departure/return flags.
  - Resume under the lock, broadcast the directive, and version connection transitions. Acceptance: reconnect after total disconnect during input/inference resumes exactly one correct turn and narrates transitions once.
  - Implementation: Reauthentication resumes stalled turns; new rounds reset to join order. Versioned connection transitions are not cleared by stale outcomes.

- [x] ~~Review working-tree code and update AGENTS.md with current architecture and known discrepancies.~~ (2026-09-14)
- [x] ~~Create prioritized token, coherence, cache and reliability recommendations.~~ (2026-09-14)
- [x] ~~Make token usage visible to all players.~~ (2026-09-01)
- [x] ~~Game transcripts should use the same player colors as the player round actions pane, to give it a more lively look.~~ (2026-09-01)
- [x] ~~Add the game name in front of the scenario name: Anyworld - (scenario name)~~ (2026-09-01)
- [x] ~~Add more logging.~~ (2026-09-01)
  - ~~logic/llm_manager.py logs set_genesis, generate_initial_state, generate_start_state, plan_dice, generate_resolution, context compaction, and backend token statistics.~~
  - ~~api/tls_bootstrap.py logs when certificates need to be renewed.~~
- [x] ~~class \_NonSuccessOnly(logging.Filter) in app.py does not work. All "200 OK" events are logged to the console.~~ (2026-09-01)
- [x] ~~After the host has entered the scenario, the AI fails to generate a descriptive name for the session, instead telling the host that “Untitled Session” is ready. Only after the game is started is the name generated.~~ (2026-09-01)
- [x] ~~Restrict dice rolls in system prompt to truly difficult/risky/absurd attempts only~~ (2026-08-28)
- [x] ~~Use brighter player colors for the current turn and dim past rounds~~ (2026-08-28)
- [x] ~~Fix "weaving the outcome" loading text scroll jump~~ (2026-08-28)
- [x] ~~Convert logged games to readable HTML~~ (2026-08-28)
- [x] ~~Investigate Safari login page issue~~ (2026-08-28)
- [x] ~~Show token usage approximation to host~~ (2026-08-28)
- [x] ~~Complete the test suite~~ (2026-08-28)
- [x] ~~Validate client_id format on the server~~ (2026-08-28)
- [x] ~~Hash passwords in transit~~ (2026-08-28)
- [x] ~~Cap DOM growth in log and state panes~~ (2026-08-28)
- [x] ~~Reconnect the WebSocket instead of reloading the page~~ (2026-08-28)
- [x] ~~Enforce input length limits client-side~~ (2026-08-28)
- [x] ~~Add a host end-game control~~ (2026-08-28)
- [x] ~~Dim previous-round entries for readability~~ (2026-08-28)
- [x] ~~Cache system prompt token count~~ (2026-08-28)
- [x] ~~Deduplicate prompt sources~~ (2026-08-28)
- [x] ~~Hoist payload handler map~~ (2026-08-28)
- [x] ~~Move dm-thinking indicator out of log pane~~ (2026-08-28)
- [x] ~~Compact frontend state growth~~ (2026-08-28)
- [x] ~~Add DM dice-roll function~~ (2026-08-28)
- [x] ~~Drop redundant roster broadcast after auth~~ (2026-08-28)
