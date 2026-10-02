"use strict";
function handleMessage(message, replayed = false) {
    // Snapshots rebuild the current view after reconnect; live events then update
    // only the affected bounded log, roster, status or input-control state.
    const { type, payload } = message;
    if (!clientSession.authenticated && type !== "auth_ok" && type !== "error") {
        return;
    }
    if (type !== "auth_ok" && payload.session_id && payload.session_id !== clientSession.sessionId) return;
    if (type === "journal_page") { handleJournalPage(payload); return; }
    if (clientSession.replaying && !replayed &&
        !["auth_ok", "error", "chat_echo", "token_usage", "action_accepted"].includes(type)) {
        clientSession.liveEvents.push(message);
        return;
    }
    if (Number.isInteger(payload.event_id)) {
        if (payload.event_id <= clientSession.cursor) return;
        clientSession.cursor = payload.event_id;
    }
    if (type === "auth_ok") {
        if (payload.session_id && payload.session_id !== clientSession.sessionId) {
            clientSession.cursor = 0;
            clientSession.renderedRounds.clear();
            renderedActions.clear();
            clientSession.lastStartedRound = 0;
            const banner = document.getElementById("game-banner");
            elements.log.replaceChildren(banner);
        }
        clientSession.replaying = Boolean(payload.session_id && payload.latest_event_id > clientSession.cursor);
        clientSession.replaySnapshot = payload;
        clientSession.reconnectAttempts = 0;
        clientSession.replaced = false;
        elements.reclaimButton.hidden = true;
        clientSession.sessionId = payload.session_id || clientSession.sessionId;
        clientSession.roundNumber = payload.round_number || null;
        (payload.accepted_actions || []).forEach(acceptAction);
        rememberAuth({ ...clientSession.savedAuth, name: payload.name, reconnect_token: payload.reconnect_token });
        // Persist only the identity proof, never the password or password digest.
        // A reopened tab still asks for credentials before it can reclaim this player.
        writeStored("localStorage", identityKey(payload.name), JSON.stringify({
            clientId: clientSession.clientId,
            reconnectToken: payload.reconnect_token,
        }));
        clientSession.authenticated = true;
        elements.chatInput.disabled = false;
        elements.connectionStatus.textContent = "Connected";
        elements.loginModal.hidden = true;
        elements.grid.hidden = false;
        elements.loginError.textContent = "";
        applySnapshot(payload);
        if (clientSession.replaying) requestJournal("replay", clientSession.cursor);
        const pending = clientSession.pendingAction;
        // Reuse the original ID only in its original session and round.
        if (pending && pending.session_id === clientSession.sessionId &&
            pending.round_number === clientSession.roundNumber && payload.state === "ACTIVE_TURN" &&
            payload.active_player_id === clientSession.clientId) {
            send("action", pending);
            elements.actionInput.disabled = true;
        }
    } else if (type === "action_accepted") {
        acceptAction(payload);
    } else if (type === "turn_directive") {
        clientSession.roundNumber = payload.round_number;
        startRound(payload.round_number);
        syncActions(payload.round_number, payload.submitted_actions);
        applyTurn(payload.active_player_id, payload.active_player_name);
    } else if (type === "round_start") {
        clientSession.roundNumber = payload.round_number;
        startRound(payload.round_number);
    } else if (type === "action_echo") {
        showAction(
            payload.round_number,
            payload.player_name,
            payload.action,
            payload.player_color_index,
        );
    } else if (type === "state_update") {
        if (!replayed) setThinking(false);
        setPlayerOrder(payload.player_order);
        if (payload.scenario_title) {
            elements.title.textContent = displayGameTitle(payload.scenario_title);
        }
        renderResolution(payload);
    } else if (type === "player_roster") {
        renderPlayers(payload.players);
    } else if (type === "dm_thinking") {
        setThinking(Boolean(payload.active));
        if (payload.active) {
            elements.retryRoundButton.hidden = true;
            elements.endGameButton.hidden = !clientSession.isHost;
            if (!clientSession.scenarioSubmitting) {
                elements.hostModal.hidden = true;
            }
        }
    } else if (type === "chat_echo") {
        appendText(
            elements.chatMessages,
            `${payload.name}: ${payload.chat}`,
            "chat-entry",
            MAX_CHAT_ENTRIES,
        );
    } else if (type === "system_msg") {
        appendText(
            elements.chatMessages,
            `System: ${payload.msg}`,
            "chat-entry",
            MAX_CHAT_ENTRIES,
        );
        if (payload.msg === "The game has started.") {
            elements.hostModal.hidden = true;
        }
    } else if (type === "token_usage") {
        elements.tokenUsage.hidden = false;
        elements.tokenUsage.title = payload.counting_method || "Estimated token usage";
        const used = Math.max(0, Number(payload.retained_context_tokens ?? payload.approximate_tokens) || 0);
        const limit = Math.max(1, Number(payload.context_window_size) || used || 1);
        const ratio = Math.min(1, used / limit);
        elements.tokenChart.style.background =
            `conic-gradient(var(--accent) ${ratio * 360}deg, var(--border) ${ratio * 360}deg)`;
        elements.tokenChart.setAttribute(
            "aria-label",
            `Estimated retained context: ${used.toLocaleString()} of ${limit.toLocaleString()}`,
        );
        const formatTotal = (value) => value == null ? "unknown" : value.toLocaleString();
        const round = payload.round || {};
        const game = payload.game || {};
        elements.tokenCount.textContent = `≈ ${used.toLocaleString()} / ${limit.toLocaleString()}`;
        document.getElementById("token-details").textContent = [
            payload.counting_method || "Retained context is estimated; next input, schema and output are excluded.",
            `Context limit source: ${payload.context_window_source || "configured"}`,
            `Round tokens: ${formatTotal(round.total_tokens)} (input ${formatTotal(round.input_tokens)}, output ${formatTotal(round.completion_tokens)})`,
            `Game tokens: ${formatTotal(game.total_tokens)}`,
            `Round cache reads: ${formatTotal(round.cached_tokens)}`,
            `llama.cpp processed / reused: ${formatTotal(round.processed_prompt_tokens)} / ${formatTotal(round.reused_prompt_tokens)}`,
            `Requests: ${round.attempts || 0} · Errors: ${round.errors || 0} · Retries: ${round.retries || 0}`,
            ...(clientSession.isHost ? [`Failed round attempts: ${payload.round_failures || 0}`] : []),
            ...(clientSession.isHost ? [`Request time: ${(round.latency_seconds || 0).toFixed(2)}s`] : []),
            ...(clientSession.isHost ? [`Round work time: ${(payload.round_work_seconds || 0).toFixed(2)}s (includes budgeting and retries)`] : []),
            "Unknown means the provider did not report every counter; tokens are not a currency cost.",
        ].join("\n");
    } else if (type === "game_ended") {
        setThinking(false);
        elements.actionInput.disabled = true;
        elements.endGameButton.hidden = true;
        elements.retryRoundButton.hidden = true;
        appendText(elements.chatMessages, `System: ${payload.msg}`, "chat-entry", MAX_CHAT_ENTRIES);
    } else if (type === "scenario_ready") {
        clientSession.scenarioSubmitting = false;
        appendScenario(payload.original_scenario, true);
        elements.hostModal.hidden = false;
        elements.endGameButton.hidden = true;
        elements.title.textContent = payload.title;
        elements.hostStatus.classList.remove("error");
        elements.hostStatus.textContent = `“${payload.title}” is ready.`;
        elements.scenarioStep.hidden = true;
        elements.lobbyStep.hidden = false;
        elements.scenarioForm.querySelector("button").disabled = false;
    } else if (type === "error") {
        const message = payload.msg || "Unknown server error.";
        if (clientSession.scenarioSubmitting) {
            clientSession.scenarioSubmitting = false;
            showScenarioError(message);
        } else {
            showError(message);
        }
        elements.scenarioForm.querySelector("button").disabled = false;
        elements.startButton.disabled = false;
        if (payload.state) {
            showHostStep(payload.state);
            elements.hostStatus.textContent = payload.msg;
            elements.endGameButton.hidden = !clientSession.isHost ||
                !["ACTIVE_TURN", "AWAITING_LLM"].includes(payload.state);
        }
        if (payload.round_paused) {
            setThinking(false);
            elements.retryRoundButton.hidden = !clientSession.isHost;
            elements.actionInput.disabled = true;
        }
    }
}

elements.loginForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    elements.loginError.textContent = "";
    try {
        const name = elements.name.value.trim();
        const identity = rememberedIdentity(name);
        const targetId = identity?.clientId || clientSession.clientId;
        const auth = {
            name,
            password_digest: await passwordDigest(elements.password.value, targetId),
            reconnect_token: identity?.reconnectToken || (
                clientSession.savedAuth?.name?.toLowerCase() === name.toLowerCase()
                    ? clientSession.savedAuth.reconnect_token : undefined
            ),
        };
        rememberAuth(auth);
        elements.password.value = "";
        if (targetId !== clientSession.clientId || clientSession.ws.readyState !== WebSocket.OPEN) {
            clientSession.clientId = targetId;
            writeStored("sessionStorage", "artificialDungeonClientId", clientSession.clientId);
            connectSocket();
        } else {
            send("auth", auth);
        }
    } catch (error) {
        showError(error.message);
    }
});

elements.scenarioForm.addEventListener("submit", (event) => {
    event.preventDefault();
    if (
        send("scenario_init", {
            scenario: elements.scenario.value.trim(),
            chance_event: elements.chanceEvent.value.trim(),
            guidance: elements.guidance.value.trim(),
        })
    ) {
        clientSession.scenarioSubmitting = true;
        elements.scenarioForm.querySelector("button").disabled = true;
        elements.hostStatus.classList.remove("error");
        elements.hostStatus.textContent = "Generating the scenario...";
    }
});

elements.endGameButton.addEventListener("click", () => {
    if (window.confirm("End this game for every player?")) {
        send("end_game", {});
    }
});

elements.retryRoundButton.addEventListener("click", () => {
    send("retry_round", {});
});

elements.startButton.addEventListener("click", () => {
    if (send("start_game", {})) {
        elements.startButton.disabled = true;
        elements.hostStatus.textContent = "Starting game...";
    }
});

elements.chatForm.addEventListener("submit", (event) => {
    event.preventDefault();
    const message = elements.chatInput.value.trim();
    if (message && send("chat", { message })) {
        elements.chatInput.value = "";
    }
});

elements.actionForm.addEventListener("submit", (event) => {
    event.preventDefault();
    const action = elements.actionInput.value.trim();
    if (action && !elements.actionInput.disabled) {
        const previous = clientSession.pendingAction;
        clientSession.pendingAction = previous && previous.action === action &&
            previous.session_id === clientSession.sessionId && previous.round_number === clientSession.roundNumber
            ? previous : { action, action_id: createClientId(), session_id: clientSession.sessionId,
                round_number: clientSession.roundNumber };
        saveDraft();
        if (send("action", clientSession.pendingAction)) elements.actionInput.disabled = true;
    }
});

elements.actionInput.addEventListener("input", saveDraft);
elements.reclaimButton.addEventListener("click", () => {
    clientSession.replaced = false;
    elements.reclaimButton.hidden = true;
    connectSocket();
});


connectSocket();
