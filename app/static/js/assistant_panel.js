(function () {
  var REQUEST_TIMEOUT_MS = 35000;
  var RESPONSE_PLACEHOLDER = "Ask a question or use a quick prompt to get started.";
  var RESPONSE_BUSY = "Working...";
  var RESPONSE_EMPTY_QUESTION = "Enter a question to continue.";
  var RESPONSE_UNAVAILABLE = "Assistant is temporarily unavailable.";
  var RESPONSE_TIMEOUT = "Assistant is taking longer than expected. Please try again.";

  function tenantPathPrefix() {
    const root = document.documentElement;
    return root ? String(root.getAttribute("data-tenant-path-prefix") || "").trim() : "";
  }

  function prefixTenantPath(path) {
    const prefix = tenantPathPrefix();
    const value = String(path || "").trim();
    if (!prefix || !value || !value.startsWith("/") || value.startsWith("//")) {
      return value;
    }
    if (value === prefix || value.startsWith(prefix + "/")) {
      return value;
    }
    return prefix + value;
  }

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

  function appendResultLinks(container, links) {
    if (!container || !Array.isArray(links) || !links.length) {
      return;
    }
    const linksEl = document.createElement("div");
    linksEl.className = "assistant-panel__result-links";
    links.forEach(function (link) {
      const href = prefixTenantPath((link && link.href) || "");
      const label = String((link && link.label) || "").trim();
      if (!href || !label) {
        return;
      }
      const anchor = document.createElement("a");
      anchor.className = "assistant-panel__result-related";
      anchor.href = href;
      anchor.textContent = label;
      linksEl.appendChild(anchor);
    });
    if (linksEl.childElementCount) {
      container.appendChild(linksEl);
    }
  }

  function appendStructuredResults(responseEl, items) {
    if (!responseEl || !Array.isArray(items) || !items.length) {
      return;
    }
    const list = document.createElement("div");
    list.className = "assistant-panel__result-list";
    items.forEach(function (item) {
      const href = prefixTenantPath((item && item.href) || "");
      const title = String((item && item.title) || "").trim();
      if (!href || !title) {
        return;
      }
      const article = document.createElement("article");
      article.className = "assistant-panel__result-item";
      article.setAttribute("data-assistant-result-item", String((item && item.record_type) || "").trim());

      const titleLink = document.createElement("a");
      titleLink.className = "assistant-panel__result-link";
      titleLink.href = href;
      titleLink.textContent = title;
      article.appendChild(titleLink);

      const meta = String((item && item.meta) || "").trim();
      if (meta) {
        const metaEl = document.createElement("p");
        metaEl.className = "assistant-panel__result-meta";
        metaEl.textContent = meta;
        article.appendChild(metaEl);
      }

      appendResultLinks(article, item && item.links);
      list.appendChild(article);
    });
    if (list.childElementCount) {
      responseEl.appendChild(list);
    }
  }

  function setResponseContent(responseEl, message, isPlaceholder, items) {
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
    if (!isPlaceholder) {
      appendStructuredResults(responseEl, items);
    }
  }

  async function readErrorMessage(response) {
    try {
      const payload = await response.json();
      const detail = String((payload || {}).detail || "").trim();
      if (detail) {
        return detail;
      }
    } catch (_error) {
      // Keep the default fallback if the response body is not JSON.
    }
    return RESPONSE_UNAVAILABLE;
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
      setResponseContent(responseEl, RESPONSE_EMPTY_QUESTION, true);
      return;
    }

    setBusy(submitButton, statusEl, true);
    setResponseContent(responseEl, RESPONSE_BUSY, true);
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
        throw new Error(await readErrorMessage(response));
      }
      const payload = await response.json();
      const answer = String((payload || {}).answer || "").trim();
      const items = Array.isArray((payload || {}).items) ? payload.items : [];
      setResponseContent(responseEl, answer || RESPONSE_UNAVAILABLE, false, items);
    } catch (error) {
      const message = error && error.name === "AbortError"
        ? RESPONSE_TIMEOUT
        : String((error && error.message) || "").trim() || RESPONSE_UNAVAILABLE;
      setResponseContent(responseEl, message, true);
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
    if (!panel || !openButton || !form || !input) {
      return;
    }
    if (responseEl) {
      setResponseContent(responseEl, RESPONSE_PLACEHOLDER, true);
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
