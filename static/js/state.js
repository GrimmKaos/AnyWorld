"use strict";
// One tab session; identity, connection and rendering modules share this record.
const clientSession = {
 clientId: null, savedAuth: null, ws: null, connectionTimer: null,
 reconnectAttempts: 0, reconnectTimer: null, authenticated: false, isHost: false,
 scenarioSubmitting: false, lastStartedRound: 0, sessionId: null, cursor: 0,
 pendingAction: null, replaced: false, replaying: false,
 seenEvents: new Set(), renderedRounds: new Set(),
};
const elements = {
    grid: document.getElementById("grid-container"),
    loginModal: document.getElementById("login-modal"),
    loginForm: document.getElementById("login-form"),
    loginError: document.getElementById("login-error"),
    name: document.getElementById("name-input"),
    password: document.getElementById("password-input"),
    hostModal: document.getElementById("host-modal"),
    scenarioStep: document.getElementById("scenario-step"),
    scenarioForm: document.getElementById("scenario-form"),
    scenario: document.getElementById("scenario-input"),
    chanceEvent: document.getElementById("chance-event-input"),
    guidance: document.getElementById("guidance-input"),
    lobbyStep: document.getElementById("lobby-step"),
    startButton: document.getElementById("start-button"),
    hostStatus: document.getElementById("host-status"),
    title: document.getElementById("scenario-title"),
    identity: document.getElementById("player-identity"),
    log: document.getElementById("log-pane"),
    playerList: document.getElementById("player-list"),
    lobbyPlayerList: document.getElementById("lobby-player-list"),
    lobbyPlayerCount: document.getElementById("lobby-player-count"),
    chatMessages: document.getElementById("chat-messages"),
    chatForm: document.getElementById("chat-form"),
    chatInput: document.getElementById("chat-input"),
    actionForm: document.getElementById("input-pane"),
    actionInput: document.getElementById("action-input"),
    dmThinking: document.getElementById("dm-thinking"),
    connectionStatus: document.getElementById("connection-status"),
    tokenUsage: document.getElementById("token-usage"),
    tokenChart: document.getElementById("token-chart"),
    tokenCount: document.getElementById("token-count"),
    endGameButton: document.getElementById("end-game-button"),
    retryRoundButton: document.getElementById("retry-round-button"),
};

const renderedActions = new Set();
const playerColors = new Map();
const MAX_LOG_ENTRIES = 500;
const MAX_CHAT_ENTRIES = 300;
