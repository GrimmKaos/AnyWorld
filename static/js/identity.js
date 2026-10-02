"use strict";
// Storage can be unavailable in privacy modes. Keep the active tab usable in memory.
function readStored(storageName, key) {
    try {
        return window[storageName].getItem(key);
    } catch {
        return null;
    }
}

function writeStored(storageName, key, value) {
    try {
        window[storageName].setItem(key, value);
    } catch {
        // Automatic socket reconnects can still use this tab's in-memory credentials.
    }
}

function readStoredObject(storageName, key) {
    try {
        const value = JSON.parse(readStored(storageName, key));
        return value && typeof value === "object" && !Array.isArray(value) ? value : null;
    } catch {
        return null;
    }
}

const storedId = readStored("sessionStorage", "artificialDungeonClientId");
clientSession.clientId = storedId || createClientId();
clientSession.savedAuth = readStoredObject("sessionStorage", "artificialDungeonAuth");
const savedDraft = readStoredObject("sessionStorage", "artificialDungeonDraft");
elements.actionInput.value = typeof savedDraft?.text === "string" ? savedDraft.text : "";
clientSession.pendingAction = savedDraft?.pending || null;

function saveDraft() {
    writeStored("sessionStorage", "artificialDungeonDraft", JSON.stringify({
        text: elements.actionInput.value, pending: clientSession.pendingAction,
    }));
}

function acceptAction(payload) {
    const pending = clientSession.pendingAction;
    if (pending && payload.session_id === pending.session_id &&
        payload.round_number === pending.round_number && payload.action_id === pending.action_id) {
        if (elements.actionInput.value.trim() === pending.action) elements.actionInput.value = "";
        clientSession.pendingAction = null;
        saveDraft();
    }
}

// crypto.randomUUID() is unavailable on non-secure HTTP origins except localhost.
// Use a standards-compatible fallback so remote HTTP clients do not fail before
// the WebSocket is even created.
function createClientId() {
    if (window.crypto && typeof window.crypto.randomUUID === "function") {
        return window.crypto.randomUUID();
    }
    if (window.crypto && typeof window.crypto.getRandomValues === "function") {
        const bytes = new Uint8Array(16);
        window.crypto.getRandomValues(bytes);
        bytes[6] = (bytes[6] & 0x0f) | 0x40;
        bytes[8] = (bytes[8] & 0x3f) | 0x80;
        const hex = Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0"));
        return `${hex.slice(0, 4).join("")}-${hex.slice(4, 6).join("")}-${hex.slice(6, 8).join("")}-${hex.slice(8, 10).join("")}-${hex.slice(10).join("")}`;
    }
    const randomHex = () => Math.floor(Math.random() * 16).toString(16);
    return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, (character) => {
        const value = character === "x" ? Number.parseInt(randomHex(), 16) :
            (Number.parseInt(randomHex(), 16) & 0x3) | 0x8;
        return value.toString(16);
    });
}

writeStored("sessionStorage", "artificialDungeonClientId", clientSession.clientId);

function identityKey(name) {
    return `artificialDungeonIdentity:${name.trim().toLowerCase()}`;
}

function rememberAuth(auth) {
    clientSession.savedAuth = auth;
    writeStored("sessionStorage", "artificialDungeonAuth", JSON.stringify(auth));
}

function rememberedIdentity(name) {
    const identity = readStoredObject("localStorage", identityKey(name));
    if (typeof identity?.clientId !== "string" ||
        !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(identity.clientId) ||
        typeof identity.reconnectToken !== "string" || !identity.reconnectToken) {
        return null;
    }
    return identity;
}

function fallbackSha256(value) {
    const rightRotate = (word, amount) => (word >>> amount) | (word << (32 - amount));
    const maxWord = 2 ** 32;
    const words = [];
    const hash = [];
    const constants = [];
    const composite = {};
    let primeCounter = 0;
    for (let candidate = 2; primeCounter < 64; candidate += 1) {
        if (!composite[candidate]) {
            for (let multiple = candidate * candidate; multiple < 313; multiple += candidate) {
                composite[multiple] = true;
            }
            if (primeCounter < 8) hash[primeCounter] = (candidate ** 0.5 * maxWord) | 0;
            constants[primeCounter] = (candidate ** (1 / 3) * maxWord) | 0;
            primeCounter += 1;
        }
    }
    const encoded = unescape(encodeURIComponent(value));
    for (let index = 0; index < encoded.length; index += 1) {
        words[index >> 2] |= encoded.charCodeAt(index) << (3 - (index % 4)) * 8;
    }
    words[encoded.length >> 2] |= 0x80 << (3 - (encoded.length % 4)) * 8;
    words[((encoded.length + 8) >> 6) * 16 + 15] = encoded.length * 8;
    for (let block = 0; block < words.length; block += 16) {
        const schedule = words.slice(block, block + 16);
        const oldHash = hash.slice();
        for (let index = 0; index < 64; index += 1) {
            const w15 = schedule[index - 15];
            const w2 = schedule[index - 2];
            const a = hash[0];
            const e = hash[4];
            const temp1 = hash[7] + (rightRotate(e, 6) ^ rightRotate(e, 11) ^ rightRotate(e, 25))
                + ((e & hash[5]) ^ (~e & hash[6])) + constants[index]
                + (schedule[index] = index < 16 ? (schedule[index] || 0) :
                    (schedule[index - 16] + (rightRotate(w15, 7) ^ rightRotate(w15, 18) ^ (w15 >>> 3))
                    + schedule[index - 7] + (rightRotate(w2, 17) ^ rightRotate(w2, 19) ^ (w2 >>> 10))) | 0);
            const temp2 = (rightRotate(a, 2) ^ rightRotate(a, 13) ^ rightRotate(a, 22))
                + ((a & hash[1]) ^ (a & hash[2]) ^ (hash[1] & hash[2]));
            hash.pop();
            hash.unshift((temp1 + temp2) | 0);
            hash[4] = (hash[4] + temp1) | 0;
        }
        hash.forEach((valuePart, index) => { hash[index] = (valuePart + oldHash[index]) | 0; });
    }
    return hash.map((word) => (word >>> 0).toString(16).padStart(8, "0")).join("");
}

async function passwordDigest(password, identity = clientSession.clientId) {
    const value = password + identity;
    // Some mobile browsers expose crypto.subtle but reject it on an insecure HTTP
    // origin. Fall back if the digest operation itself is unavailable or rejected.
    if (window.crypto?.subtle && window.TextEncoder) {
        try {
            const bytes = new TextEncoder().encode(value);
            const digest = await window.crypto.subtle.digest("SHA-256", bytes);
            return Array.from(new Uint8Array(digest), (byte) =>
                byte.toString(16).padStart(2, "0"),
            ).join("");
        } catch (error) {
            console.warn("Web Crypto SHA-256 unavailable; using fallback", error);
        }
    }
    return fallbackSha256(value);
}
