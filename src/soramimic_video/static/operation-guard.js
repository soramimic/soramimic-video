/* Short foreground work blocks new actions without interrupting existing work. */
(() => {
  "use strict";
  const leases = new Set();
  const marked = new Map();
  const continuing = new WeakSet();
  const inputs = new Map();
  const actions = new Map();
  const selector = "button, input, select, textarea, a[download], [role=button], [data-operation-input]";
  const continuations = new Set([
    "cancel", "builder-cancel", "title-candidates-close",
    "metadata-change-keep", "metadata-change-update", "metadata-change-hide-warning",
  ]);
  const editableText = element => element.matches("textarea, input:not([type]), input[type=text], input[type=number], input[type=password], input[type=search], input[type=url]");
  function allowed(element) {
    return continuations.has(element.id) || continuing.has(element)
      || [...leases].some(lease => lease.allow.includes(element)) || element.matches(".info-btn")
      || !!element.closest("#video-save-help")
      || (element.id === "song-record-button" && element.getAttribute("aria-pressed") === "true");
  }
  function blocked(element) {
    return leases.size > 0 && !allowed(element);
  }
  function remember(element) {
    if (element.matches("input, select, textarea")) inputs.set(element, {
      value: element.value, checked: element.checked, files: element.files,
    });
  }
  function sync() {
    for (const element of document.querySelectorAll(selector)) {
      if (leases.size && !inputs.has(element)) remember(element);
      if (blocked(element)) {
        if (!marked.has(element)) marked.set(element, element.getAttribute("aria-disabled"));
        element.setAttribute("data-operation-blocked", "");
        if (!element.matches("[data-operation-input]:not(button)")) element.setAttribute("aria-disabled", "true");
      } else if (marked.has(element)) restore(element);
    }
    for (const element of marked.keys()) if (!element.isConnected) restore(element);
  }
  function restore(element) {
    const previous = marked.get(element);
    element.removeAttribute("data-operation-blocked");
    if (element.getAttribute("aria-disabled") === "true") {
      if (previous === null) element.removeAttribute("aria-disabled");
      else element.setAttribute("aria-disabled", previous);
    }
    marked.delete(element);
  }
  function intercept(event) {
    if (!leases.size || !(event.target instanceof Element)) return;
    // Native media controls and reading/copying text remain available.
    if (event.target.closest("audio, video")) return;
    let element = event.target.closest(selector);
    if (!element) {
      const label = event.target.closest("label");
      element = label?.control || document.getElementById(label?.getAttribute("for"));
    }
    if (event.type === "submit") {
      element = event.target;
      // A dialog's allowed completion button must also be allowed to submit it.
      const submitter = event.submitter;
      if (element.getAttribute("method")?.toLowerCase() === "dialog"
          && submitter?.form === element && allowed(submitter)) return;
    }
    if (!element || !blocked(element)) return;
    if (["input", "change"].includes(event.type)) {
      if (!event.isTrusted) { remember(element); return; }
      // A file picker or select opened before the lock can finish during it.
      const previous = inputs.get(element);
      if (previous) {
        if (element.matches("input[type=file]")) element.files = previous.files;
        else element.value = previous.value;
        if (previous.checked !== undefined) element.checked = previous.checked;
      }
    }
    if (event.type === "keydown") {
      if (["Tab", "Escape"].includes(event.key)) return;
      if ((event.ctrlKey || event.metaKey) && ["a", "c"].includes(event.key.toLowerCase())) return;
      if (editableText(element) && ["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
      if (editableText(element) && !element.matches("input[type=number]")
          && ["ArrowUp", "ArrowDown", "PageUp", "PageDown"].includes(event.key)) return;
    }
    if (["click", "pointerdown"].includes(event.type) && editableText(element)) return;
    event.preventDefault();
    event.stopImmediatePropagation();
  }
  for (const type of ["click", "pointerdown", "keydown", "beforeinput", "paste", "cut", "drop", "dragover", "submit", "input", "change"]) {
    document.addEventListener(type, intercept, true);
  }
  // Programmatic input/change events apply the operation's own result.
  const observer = new MutationObserver(() => {
    for (const [control, action] of actions) if (!control.isConnected) action.cancel();
    if (leases.size) sync();
  });
  observer.observe(document.documentElement, { childList: true, subtree: true,
    attributes: true, attributeFilter: ["aria-pressed"] });
  window.addEventListener("pagehide", () => { for (const action of actions.values()) action.cancel(); });
  globalThis.VideoOperations = {
    isBusy() { return leases.size > 0; },
    hasAction(control) { return actions.has(control); },
    beginAction(control, { onCancel = () => {}, onFinish = () => {}, cancelLabel = "処理をキャンセル", allow = [] } = {}) {
      if (actions.has(control)) return actions.get(control);
      const cancel = document.createElement("button");
      cancel.type = "button";
      cancel.className = "btn-sm operation-cancel";
      cancel.textContent = "キャンセル";
      cancel.setAttribute("aria-label", cancelLabel);
      const anchor = control.matches("summary") ? control.parentElement : control;
      anchor.after(cancel);
      const lease = this.begin({ allow: [control, cancel, ...allow] });
      const action = {
        canceled: false,
        setCancelable(enabled) { cancel.disabled = !enabled; },
        finish() {
          if (actions.get(control) !== action) return;
          actions.delete(control);
          cancel.remove();
          lease.end();
          onFinish();
        },
        cancel() {
          if (cancel.disabled || action.canceled || actions.get(control) !== action) return;
          action.canceled = true;
          cancel.disabled = true;
          try {
            const pending = onCancel();
            if (pending && typeof pending.then === "function") {
              Promise.resolve(pending).then(() => action.finish(), () => action.finish());
            } else action.finish();
          } catch (error) { action.finish(); throw error; }
        },
      };
      actions.set(control, action);
      cancel.addEventListener("click", () => action.cancel());
      return action;
    },
    cancelAction(control) { actions.get(control)?.cancel(); },
    allowContinuation(element) { if (element) { continuing.add(element); sync(); } },
    clearContinuation(element) { if (element) { continuing.delete(element); sync(); } },
    begin({ allow = [] } = {}) {
      const lease = { allow };
      leases.add(lease);
      sync();
      return {
        isAlone() { return leases.size === 1 && leases.has(lease); },
        end() { leases.delete(lease); sync(); if (!leases.size) inputs.clear(); },
      };
    },
  };
})();
