"use strict";
function handleMessage(message) {
    // Snapshots rebuild the current view after reconnect; live events then update
    // only the affected bounded log, roster, status or input-control state.
    const { type, payload } = message;
    if (!clientSession.authenticated && type !== "auth_ok" && type !== "error") {
        return;
    }
    if (type === "auth_ok") {
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
    } else if (type === "turn_directive") {
        startRound(payload.round_number);
        syncActions(payload.round_number, payload.submitted_actions);
        applyTurn(payload.active_player_id, payload.active_player_name);
    } else if (type === "round_start") {
        startRound(payload.round_number);
    } else if (type === "action_echo") {
        showAction(
            payload.round_number,
            payload.player_name,
            payload.action,
            payload.player_color_index,
        );
    } else if (type === "state_update") {
        setThinking(false);
        setPlayerOrder(payload.player_order);
        if (payload.scenario_title) {
            elements.title.textContent = displayGameTitle(payload.scenario_title);
        }
        startRound(payload.round_number);
        syncActions(payload.round_number, payload.submitted_actions);
        appendState(payload.global_narrative, payload.round_number);
        Object.entries(payload.dice_results || {}).forEach(([player, roll]) => {
            appendText(
                elements.log,
                `🎲 ${player} rolled ${roll}/100`,
                `dice-entry current-round${playerColorClass(player)}`,
            );
        });
        Object.entries(payload.player_resolutions).forEach(([player, resolution]) => {
            appendText(
                elements.log,
                `[${player}] ${resolution}`,
                `resolution-entry current-round${playerColorClass(player)}`,
            );
        });
        markRoundComplete();
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
            `Failed round attempts: ${payload.round_failures || 0}`,
            `Request time: ${(round.latency_seconds || 0).toFixed(2)}s`,
            `Round work time: ${(payload.round_work_seconds || 0).toFixed(2)}s (includes budgeting and retries)`,
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
    if (action && !elements.actionInput.disabled && send("action", { action })) {
        elements.actionInput.value = "";
        elements.actionInput.disabled = true;
    }
});


connectSocket();
