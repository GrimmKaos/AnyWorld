"use strict";

function requestJournal(mode, after = 0, search = "") {
    send("journal_request", { mode, after, limit: 100, search });
}

function handleJournalPage(payload) {
    if (payload.incomplete) elements.connectionStatus.textContent = "Connected; some archived history is unavailable.";
    if (payload.mode === "replay") {
        payload.events.forEach((event) => {
            // The snapshot already supplies the opening separately.
            if (event.type !== "state_update" || event.payload.round_number) handleMessage(event, true);
            else clientSession.cursor = Math.max(clientSession.cursor, event.payload.event_id);
        });
        if (payload.has_more) requestJournal("replay", payload.cursor);
        else {
            clientSession.cursor = Math.max(clientSession.cursor, payload.cursor);
            clientSession.replaying = false;
            applySnapshot(clientSession.replaySnapshot);
            const live = clientSession.liveEvents.splice(0);
            live.forEach((event) => handleMessage(event));
        }
    } else if (payload.mode === "export") {
        if (!clientSession.exportUntil) clientSession.exportUntil = payload.latest_event_id;
        clientSession.exportEvents.push(...payload.events.filter(
            (event) => event.payload.event_id <= clientSession.exportUntil));
        if (payload.has_more && payload.cursor < clientSession.exportUntil) requestJournal("export", payload.cursor);
        else {
            const text = clientSession.exportEvents.map((event) => JSON.stringify(event)).join("\n") + "\n";
            const link = document.createElement("a");
            link.href = URL.createObjectURL(new Blob([text], { type: "application/x-ndjson" }));
            link.download = `anyworld-public-${clientSession.sessionId}.jsonl`;
            link.click();
            URL.revokeObjectURL(link.href);
            clientSession.exportEvents = [];
            elements.historyExport.disabled = false;
        }
    } else {
        elements.historyEntries.replaceChildren();
        payload.events.forEach((event) => {
            const entry = document.createElement("pre");
            entry.textContent = JSON.stringify(event, null, 2);
            elements.historyEntries.appendChild(entry);
        });
        clientSession.historyCursor = payload.cursor;
        elements.historyNext.disabled = !payload.has_more;
    }
}

elements.historyButton.addEventListener("click", () => {
    elements.historyModal.hidden = false;
    requestJournal("history", 0, elements.historySearch.value.trim());
    syncModalFocus();
});
elements.historyClose.addEventListener("click", () => {
    elements.historyModal.hidden = true;
    syncModalFocus();
});
elements.historyForm.addEventListener("submit", (event) => {
    event.preventDefault();
    requestJournal("history", 0, elements.historySearch.value.trim());
});
elements.historyNext.addEventListener("click", () => {
    requestJournal("history", clientSession.historyCursor, elements.historySearch.value.trim());
});
elements.historyExport.addEventListener("click", () => {
    elements.historyExport.disabled = true;
    clientSession.exportUntil = clientSession.cursor;
    clientSession.exportEvents = [];
    requestJournal("export");
});
