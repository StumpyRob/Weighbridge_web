(function () {
  var REQUEST_TIMEOUT_MS = 25000;

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

  async function submitAssistantQuestion(form, question) {
    const endpoint = form.getAttribute("data-endpoint") || "";
    const responseEl = document.querySelector("[data-assistant-response]");
    const submitButton = form.querySelector("[data-assistant-submit]");
    const statusEl = form.querySelector("[data-assistant-status]");
    if (!endpoint || !responseEl) {
      return;
    }

    const trimmedQuestion = String(question || "").trim();
    if (!trimmedQuestion) {
      responseEl.textContent = "Enter a question to continue.";
      return;
    }

    setBusy(submitButton, statusEl, true);
    responseEl.textContent = "Working...";
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
      responseEl.textContent = answer || "Assistant is temporarily unavailable.";
    } catch (_error) {
      responseEl.textContent = "Assistant is temporarily unavailable.";
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
    if (!panel || !openButton || !form || !input) {
      return;
    }

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
      submitAssistantQuestion(form, input.value);
    });

    promptButtons.forEach(function (button) {
      button.addEventListener("click", function () {
        const question = button.getAttribute("data-assistant-prompt") || "";
        input.value = question;
        openPanel();
        submitAssistantQuestion(form, question);
      });
    });
  }

  document.addEventListener("DOMContentLoaded", initializeAssistantPanel);
})();
