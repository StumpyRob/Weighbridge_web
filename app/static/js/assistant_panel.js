(function () {
  var REQUEST_TIMEOUT_MS = 25000;
  var RESPONSE_PLACEHOLDER = "Ask a question or use a quick prompt to get started.";
  var RESPONSE_BUSY = "Working...";
  var RESPONSE_EMPTY_QUESTION = "Enter a question to continue.";
  var RESPONSE_UNAVAILABLE = "Assistant is temporarily unavailable.";

  function csrfToken() {
    const meta = document.querySelector("meta[name='csrf-token']");
    return meta ? meta.getAttribute("content") || "" : "";
  }

  function setBusy(submitButton, statusEl, isBusy) {
    if (!submitButton) {
      return;
    }
    submitButton.disabled = !!isBusy;
    submitButton.textContent = isBusy ? "Thinking..." : "Submit";
    if (statusEl) {
      statusEl.hidden = !isBusy;
    }
  }

  function setResponseContent(responseEl, message, isPlaceholder) {
    if (!responseEl) {
      return;
    }
    responseEl.textContent = "";
    const paragraph = document.createElement("p");
    paragraph.className = isPlaceholder
      ? "assistant-panel__response-placeholder"
      : "assistant-panel__response-text";
    paragraph.textContent = String(message || "").trim();
    responseEl.appendChild(paragraph);
  }

  function setFollowupsVisible(followupsEl, isVisible) {
    if (!followupsEl) {
      return;
    }
    followupsEl.hidden = !isVisible;
  }

  async function submitAssistantQuestion(form, question, followupsEl) {
    const endpoint = form.getAttribute("data-endpoint") || "";
    const responseEl = document.querySelector("[data-assistant-response]");
    const submitButton = form.querySelector("[data-assistant-submit]");
    const statusEl = form.querySelector("[data-assistant-status]");
    if (!endpoint || !responseEl) {
      return;
    }

    const trimmedQuestion = String(question || "").trim();
    if (!trimmedQuestion) {
      setResponseContent(responseEl, RESPONSE_EMPTY_QUESTION, true);
      setFollowupsVisible(followupsEl, false);
      return;
    }

    setBusy(submitButton, statusEl, true);
    setResponseContent(responseEl, RESPONSE_BUSY, true);
    setFollowupsVisible(followupsEl, false);
    const abortController = typeof AbortController === "function" ? new AbortController() : null;
    const timeoutId = window.setTimeout(function () {
      if (abortController) {
        abortController.abort();
      }
    }, REQUEST_TIMEOUT_MS);
    try {
      const response = await fetch(endpoint, {
        method: "POST",
        credentials: "same-origin",
        signal: abortController ? abortController.signal : undefined,
        headers: {
          "Accept": "application/json",
          "Content-Type": "application/json",
          "X-CSRF-Token": csrfToken(),
        },
        body: JSON.stringify({ question: trimmedQuestion }),
      });
      if (!response.ok) {
        throw new Error("assistant-unavailable");
      }
      const payload = await response.json();
      const answer = String((payload || {}).answer || "").trim();
      setResponseContent(responseEl, answer || RESPONSE_UNAVAILABLE, false);
      setFollowupsVisible(followupsEl, true);
    } catch (_error) {
      setResponseContent(responseEl, RESPONSE_UNAVAILABLE, true);
      setFollowupsVisible(followupsEl, true);
    } finally {
      window.clearTimeout(timeoutId);
      setBusy(submitButton, statusEl, false);
    }
  }

  function initializeAssistantPanel() {
    const panel = document.querySelector("[data-assistant-panel]");
    const openButton = document.querySelector("[data-assistant-open]");
    const form = document.querySelector("[data-assistant-form]");
    const input = document.querySelector("[data-assistant-input]");
    const responseEl = document.querySelector("[data-assistant-response]");
    const followupsEl = document.querySelector("[data-assistant-followups]");
    if (!panel || !openButton || !form || !input) {
      return;
    }
    if (responseEl) {
      setResponseContent(responseEl, RESPONSE_PLACEHOLDER, true);
    }
    setFollowupsVisible(followupsEl, false);

    var closeTimer = null;
    const closeButtons = panel.querySelectorAll("[data-assistant-close]");
    const promptButtons = panel.querySelectorAll("[data-assistant-prompt]");

    function openPanel() {
      if (closeTimer) {
        window.clearTimeout(closeTimer);
        closeTimer = null;
      }
      panel.hidden = false;
      document.body.classList.add("assistant-panel-open");
      openButton.setAttribute("aria-expanded", "true");
      window.requestAnimationFrame(function () {
        panel.classList.add("is-open");
        input.focus();
      });
    }

    function closePanel() {
      panel.classList.remove("is-open");
      document.body.classList.remove("assistant-panel-open");
      openButton.setAttribute("aria-expanded", "false");
      closeTimer = window.setTimeout(function () {
        panel.hidden = true;
      }, 220);
    }

    openButton.addEventListener("click", openPanel);
    closeButtons.forEach(function (button) {
      button.addEventListener("click", closePanel);
    });
    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape" && !panel.hidden) {
        closePanel();
      }
    });

    form.addEventListener("submit", function (event) {
      event.preventDefault();
      submitAssistantQuestion(form, input.value, followupsEl);
    });

    promptButtons.forEach(function (button) {
      button.addEventListener("click", function () {
        const question = button.getAttribute("data-assistant-prompt") || "";
        input.value = question;
        openPanel();
        submitAssistantQuestion(form, question, followupsEl);
      });
    });
  }

  document.addEventListener("DOMContentLoaded", initializeAssistantPanel);
})();
