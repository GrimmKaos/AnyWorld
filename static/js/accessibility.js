"use strict";

let focusedModal = null;
let previousFocus = null;
function modalTargets(modal) {
    return [...modal.querySelectorAll("input, textarea, button, select, [tabindex]")]
        .filter((element) => !element.disabled && !element.hidden && element.getClientRects().length);
}
function syncModalFocus() {
    const modal = [elements.historyModal, elements.hostModal, elements.loginModal]
        .find((element) => !element.hidden) || null;
    if (modal === focusedModal) return;
    if (!focusedModal && modal) previousFocus = document.activeElement;
    focusedModal = modal;
    if (modal) modalTargets(modal)[0]?.focus();
    else previousFocus?.focus();
}
document.addEventListener("keydown", (event) => {
    if (!focusedModal || event.key !== "Tab") return;
    const targets = modalTargets(focusedModal);
    if (!targets.length) { event.preventDefault(); return; }
    const first = targets[0];
    const last = targets.at(-1);
    if (event.shiftKey && (document.activeElement === first || !focusedModal.contains(document.activeElement))) {
        event.preventDefault(); last.focus();
    } else if (!event.shiftKey && (document.activeElement === last || !focusedModal.contains(document.activeElement))) {
        event.preventDefault(); first.focus();
    }
});
new MutationObserver(syncModalFocus).observe(document.body, {
    subtree: true, attributes: true, attributeFilter: ["hidden"],
});
syncModalFocus();
