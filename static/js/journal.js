"use strict";

const journalRequests = new Map();
const journalTimers = new Map();

function finishJournalReplay() {
    clientSession.replaying = false;
    applySnapshot(clientSession.replaySnapshot);
    const live = clientSession.liveEvents.splice(0);
    live.forEach((event) => handleMessage(event));
}

function clearJournalRequest(mode) {
    clearTimeout(journalTimers.get(mode));
    journalTimers.delete(mode);
    journalRequests.delete(mode);
}

function failJournalRequest(mode) {
    clearJournalRequest(mode);
    if (mode === "replay" && clientSession.replaying) finishJournalReplay();
    if (mode === "export") {
        clientSession.exportEvents = [];
        clientSession.exportUntil = null;
        elements.historyExport.disabled = false;
    }
    if (mode === "history") elements.historyNext.disabled = false;
}

function resetJournalRequests() {
    [...journalRequests.keys()].forEach(failJournalRequest);
}

function requestJournal(mode, after = 0, search = "") {
    clearJournalRequest(mode);
    const request = { mode, after, limit: 100, search };
    journalRequests.set(mode, request);
    if (!send("journal_request", request)) {
        failJournalRequest(mode);
        return;
    }
    journalTimers.set(mode, window.setTimeout(() => {
        failJournalRequest(mode);
        showError("History request timed out. Please try again.");
    }, 30000));
}

function handleJournalError(payload) {
    if (!["journal_rate_limited", "journal_request_failed"].includes(payload.code)) return false;
    const request = payload.request;
    const pending = journalRequests.get(request?.mode);
    if (!pending || ["mode", "after", "limit", "search"].some(
        (key) => pending[key] !== request[key])) return true;
    if (payload.code === "journal_request_failed") {
        failJournalRequest(request.mode);
        showError(payload.msg || "History request failed. Please try again.");
        return true;
    }
    clearTimeout(journalTimers.get(request.mode));
    const socket = clientSession.ws;
    journalTimers.set(request.mode, window.setTimeout(() => {
        if (clientSession.ws === socket && clientSession.authenticated) {
            requestJournal(request.mode, request.after, request.search);
        } else failJournalRequest(request.mode);
    }, Math.max(100, Number(payload.retry_after_seconds) * 1000 + 50)));
    return true;
}

function handleJournalPage(payload) {
    clearJournalRequest(payload.mode);
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
            finishJournalReplay();
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
