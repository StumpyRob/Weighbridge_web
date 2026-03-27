(function () {
  const BUTTON_SELECTOR = "[data-qz-print-button]";
  const STATUS_SELECTOR = "[data-qz-print-status]";
  const WORKSTATION_NOTE_SELECTOR = "[data-qz-workstation-note]";
  const QZ_LIBRARY_SRC = "/static/vendor/qz-tray.js";
  const TOAST_ROOT_ID = "flash-toasts";
  const TOAST_HIDE_TRANSITION_MS = 250;
  const WORKSTATION_ID_STORAGE_KEY = "qz_workstation_id";
  const WORKSTATION_LABEL_STORAGE_KEY = "qz_workstation_label";
  let qzLoadPromise = null;

  function storageGet(key) {
    try {
      return String(window.localStorage.getItem(key) || "").trim();
    } catch (_error) {
      return "";
    }
  }

  function storageSet(key, value) {
    try {
      if (!value) {
        window.localStorage.removeItem(key);
        return;
      }
      window.localStorage.setItem(key, String(value));
    } catch (_error) {
      return;
    }
  }

  function workstationId() {
    let current = storageGet(WORKSTATION_ID_STORAGE_KEY);
    if (current) {
      return current;
    }
    if (window.crypto && typeof window.crypto.randomUUID === "function") {
      current = window.crypto.randomUUID();
    } else {
      current = "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, function (char) {
        const random = Math.floor(Math.random() * 16);
        const value = char === "x" ? random : (random & 0x3) | 0x8;
        return value.toString(16);
      });
    }
    storageSet(WORKSTATION_ID_STORAGE_KEY, current);
    return current;
  }

  function preferredWorkstationLabel() {
    return storageGet(WORKSTATION_LABEL_STORAGE_KEY);
  }

  function findStatusElement(button) {
    const root = button.closest(".print-actions");
    if (!root) {
      return null;
    }
    return root.querySelector(STATUS_SELECTOR);
  }

  function findWorkstationNoteElement(button) {
    const root = button.closest(".print-actions");
    if (!root) {
      return null;
    }
    return root.querySelector(WORKSTATION_NOTE_SELECTOR);
  }

  function clearElement(element) {
    while (element && element.firstChild) {
      element.removeChild(element.firstChild);
    }
  }

  function setStatus(statusElement, message, tone) {
    if (!(statusElement instanceof HTMLElement)) {
      return;
    }
    statusElement.hidden = !message;
    statusElement.textContent = message || "";
    statusElement.classList.remove("is-error", "is-success");
    if (tone === "error") {
      statusElement.classList.add("is-error");
    } else if (tone === "success") {
      statusElement.classList.add("is-success");
    }
  }

  function hideWorkstationNote(noteElement) {
    if (!(noteElement instanceof HTMLElement)) {
      return;
    }
    clearElement(noteElement);
    noteElement.hidden = true;
  }

  function setWorkstationNote(noteElement, options) {
    if (!(noteElement instanceof HTMLElement)) {
      return;
    }
    const message = String((options && options.message) || "").trim();
    if (!message) {
      hideWorkstationNote(noteElement);
      return;
    }

    clearElement(noteElement);
    noteElement.hidden = false;

    const text = document.createElement("span");
    text.textContent = message;
    noteElement.appendChild(text);

    if (options && typeof options.onAction === "function") {
      const spacer = document.createTextNode(" ");
      noteElement.appendChild(spacer);

      const action = document.createElement("button");
      action.type = "button";
      action.className = "btn btn--ghost btn--sm";
      action.textContent = String(options.actionLabel || "Name this workstation?");
      action.addEventListener("click", options.onAction);
      noteElement.appendChild(action);
    }
  }

  function applyWorkstationUiState(state) {
    document.querySelectorAll(BUTTON_SELECTOR).forEach(function (button) {
      if (!(button instanceof HTMLElement)) {
        return;
      }
      const noteElement = findWorkstationNoteElement(button);
      if (!(noteElement instanceof HTMLElement)) {
        return;
      }
      if (!state || !state.needsWorkstationName) {
        hideWorkstationNote(noteElement);
        return;
      }
      setWorkstationNote(noteElement, {
        message: "This workstation is not named yet.",
        actionLabel: "Name this workstation?",
        onAction: function () {
          promptForWorkstationName(button);
        },
      });
    });
  }

  function removeToast(toast) {
    if (toast && toast.parentNode) {
      toast.remove();
    }
  }

  function hideToast(toast) {
    if (!toast || toast.dataset.hiding === "1") {
      return;
    }
    toast.dataset.hiding = "1";
    toast.classList.add("is-hiding");
    toast.addEventListener(
      "transitionend",
      function () {
        removeToast(toast);
      },
      { once: true }
    );
    window.setTimeout(function () {
      removeToast(toast);
    }, TOAST_HIDE_TRANSITION_MS + 50);
  }

  function ensureToastRoot() {
    let root = document.getElementById(TOAST_ROOT_ID);
    if (!(root instanceof HTMLElement)) {
      root = document.createElement("div");
      root.id = TOAST_ROOT_ID;
      root.className = "flash-toasts";
      root.setAttribute("aria-live", "polite");
      document.body.appendChild(root);
    }
    if (root.dataset.qzToastBound !== "1") {
      root.dataset.qzToastBound = "1";
      root.addEventListener("click", function (event) {
        const target = event.target;
        if (!(target instanceof HTMLElement)) {
          return;
        }
        const closeButton = target.closest("[data-toast-close]");
        if (!closeButton) {
          return;
        }
        const toast = closeButton.closest(".flash-toast");
        if (!(toast instanceof HTMLElement)) {
          return;
        }
        hideToast(toast);
      });
    }
    return root;
  }

  function dismissQzToasts() {
    const root = document.getElementById(TOAST_ROOT_ID);
    if (!(root instanceof HTMLElement)) {
      return;
    }
    root.querySelectorAll(".flash-toast[data-qz-toast='1']").forEach(function (toast) {
      removeToast(toast);
    });
  }

  function showErrorToast(message) {
    const root = ensureToastRoot();
    dismissQzToasts();

    const toast = document.createElement("div");
    toast.className = "flash-toast flash-toast--error";
    toast.setAttribute("data-flash-toast", "");
    toast.setAttribute("data-qz-toast", "1");

    const content = document.createElement("div");
    content.className = "flash-toast__content";
    content.textContent = message;

    const closeButton = document.createElement("button");
    closeButton.type = "button";
    closeButton.className = "flash-toast__close";
    closeButton.setAttribute("aria-label", "Dismiss notification");
    closeButton.setAttribute("data-toast-close", "");
    closeButton.textContent = "x";

    toast.appendChild(content);
    toast.appendChild(closeButton);
    root.appendChild(toast);
  }

  function loadScript(src) {
    return new Promise(function (resolve, reject) {
      const existing = document.querySelector(`script[src="${src}"]`);
      if (existing) {
        existing.addEventListener("load", function () {
          resolve();
        });
        existing.addEventListener("error", function () {
          reject(new Error("Failed to load QZ Tray support."));
        });
        if (window.qz) {
          resolve();
        }
        return;
      }

      const script = document.createElement("script");
      script.src = src;
      script.async = true;
      script.onload = function () {
        resolve();
      };
      script.onerror = function () {
        reject(new Error("Failed to load QZ Tray support."));
      };
      document.head.appendChild(script);
    });
  }

  async function ensureQzLibrary() {
    if (window.qz) {
      return window.qz;
    }
    if (!qzLoadPromise) {
      qzLoadPromise = loadScript(QZ_LIBRARY_SRC)
        .then(function () {
          if (!window.qz) {
            throw new Error("QZ Tray JavaScript library did not load.");
          }
          return window.qz;
        })
        .catch(function (error) {
          qzLoadPromise = null;
          throw error;
        });
    }
    return qzLoadPromise;
  }

  function csrfToken() {
    const meta = document.querySelector("meta[name='csrf-token']");
    return meta ? String(meta.getAttribute("content") || "").trim() : "";
  }

  function fetchText(url, options) {
    return fetch(url, options).then(async function (response) {
      const text = String((await response.text()) || "").trim();
      if (!response.ok) {
        throw new Error(text || "QZ signing request failed.");
      }
      return text;
    });
  }

  async function postJson(url, payload) {
    const response = await fetch(url, {
      method: "POST",
      cache: "no-store",
      credentials: "same-origin",
      headers: {
        Accept: "application/json",
        "Content-Type": "application/json",
        "X-CSRF-Token": csrfToken(),
      },
      body: JSON.stringify(payload || {}),
    });
    const text = String((await response.text()) || "").trim();
    let parsed = null;
    if (text) {
      try {
        parsed = JSON.parse(text);
      } catch (_error) {
        parsed = null;
      }
    }
    if (!response.ok) {
      const detail =
        parsed && typeof parsed === "object" && parsed !== null
          ? String(parsed.detail || "").trim()
          : "";
      throw new Error(detail || text || "Request failed.");
    }
    if (!text) {
      return {};
    }
    return parsed && typeof parsed === "object" ? parsed : {};
  }

  function configureQzSecurity(qz, signingConfig) {
    const certificateUrl = String(signingConfig.certificateUrl || "").trim();
    const signUrl = String(signingConfig.signUrl || "").trim();
    const signingKey = `${certificateUrl}|${signUrl}`;

    if (!qz || qz.__weighbridgeQzConfigured === signingKey) {
      return;
    }
    if (!certificateUrl || !signUrl) {
      throw new Error("QZ signing routes are unavailable on this page.");
    }
    if (qz.security && typeof qz.security.setCertificatePromise === "function") {
      qz.security.setCertificatePromise(
        function (resolve, reject) {
          fetchText(certificateUrl, {
            cache: "no-store",
            credentials: "same-origin",
            headers: {
              Accept: "text/plain",
            },
          }).then(resolve, reject);
        },
        { rejectOnFailure: true }
      );
    }
    if (qz.security && typeof qz.security.setSignatureAlgorithm === "function") {
      qz.security.setSignatureAlgorithm("SHA512");
    }
    if (qz.security && typeof qz.security.setSignaturePromise === "function") {
      qz.security.setSignaturePromise(function (toSign) {
        return function (resolve, reject) {
          fetchText(signUrl, {
            method: "POST",
            cache: "no-store",
            credentials: "same-origin",
            headers: {
              Accept: "text/plain",
              "Content-Type": "application/json",
              "X-CSRF-Token": csrfToken(),
            },
            body: JSON.stringify({
              request: toSign,
            }),
          }).then(resolve, reject);
        };
      });
    }
    qz.__weighbridgeQzConfigured = signingKey;
  }

  async function ensureQzConnection(qz) {
    if (
      qz.websocket &&
      typeof qz.websocket.isActive === "function" &&
      qz.websocket.isActive()
    ) {
      return;
    }
    await qz.websocket.connect({
      retries: 0,
      delay: 0,
    });
  }

  function arrayBufferToBase64(buffer) {
    const bytes = new Uint8Array(buffer);
    const chunkSize = 0x8000;
    let binary = "";
    for (let index = 0; index < bytes.length; index += chunkSize) {
      const chunk = bytes.subarray(index, index + chunkSize);
      binary += String.fromCharCode.apply(null, chunk);
    }
    return window.btoa(binary);
  }

  async function fetchPdfAsBase64(url) {
    const response = await fetch(url, {
      credentials: "same-origin",
      headers: {
        Accept: "application/pdf",
      },
    });
    if (!response.ok) {
      const errorText = (await response.text()).trim();
      throw new Error(errorText || "Ticket PDF download failed.");
    }
    const buffer = await response.arrayBuffer();
    if (!buffer.byteLength) {
      throw new Error("Ticket PDF was empty.");
    }
    return arrayBufferToBase64(buffer);
  }

  async function resolvePrinter(qz, preferredName) {
    if (preferredName) {
      try {
        return await qz.printers.find(preferredName);
      } catch (_error) {
        throw new Error(`Printer "${preferredName}" was not found.`);
      }
    }
    const defaultPrinter = await qz.printers.getDefault();
    if (!defaultPrinter) {
      throw new Error("No default printer is available.");
    }
    return defaultPrinter;
  }

  function buildSuccessUrl(baseUrl, printerName, successKind) {
    if (!baseUrl) {
      return "";
    }
    const url = new URL(baseUrl, window.location.origin);
    const normalizedKind = String(successKind || "ticket").trim().toLowerCase();
    if (normalizedKind === "invoice") {
      url.searchParams.set("invoice_print_sent", "1");
      url.searchParams.delete("invoice_print_job_id");
      url.searchParams.delete("print_failed");
      url.searchParams.delete("print_error");
      url.searchParams.delete("print_error_detail");
      url.searchParams.delete("print_job_id");
      return url.toString();
    }
    if (normalizedKind === "wtn") {
      url.searchParams.set("wtn_sent", "1");
      url.searchParams.delete("wtn_failed");
      url.searchParams.delete("wtn_error_detail");
      url.searchParams.delete("wtn_job_id");
      return url.toString();
    }
    url.searchParams.set("printed", "1");
    url.searchParams.set("print_sent", "1");
    url.searchParams.set("print_status", "1");
    url.searchParams.set("printed_to", printerName || "QZ Tray");
    url.searchParams.set("print_destination", printerName || "QZ Tray");
    url.searchParams.delete("print_failed");
    url.searchParams.delete("print_error");
    url.searchParams.delete("print_error_detail");
    url.searchParams.delete("print_job_id");
    return url.toString();
  }

  function classifyError(error, preferredPrinterName) {
    const message = String(error && error.message ? error.message : error || "").trim();
    const normalized = message.toLowerCase();
    if (!message) {
      return {
        message: "Direct print failed.",
        recovery: "Use Preview or Download PDF.",
      };
    }
    if (
      normalized.includes("failed to load qz tray support") ||
      normalized.includes("javascript library did not load")
    ) {
      return {
        message: "QZ Tray support is unavailable in this browser session.",
        recovery: "Reload the page or use Preview or Download PDF.",
      };
    }
    if (
      normalized.includes("websocket") ||
      normalized.includes("connect") ||
      normalized.includes("refused") ||
      normalized.includes("closed before") ||
      normalized.includes("failed to establish a connection")
    ) {
      return {
        message: "QZ Tray is not running or could not be reached on this workstation.",
        recovery: "Start QZ Tray and retry, or use Preview or Download PDF.",
      };
    }
    if (
      normalized.includes("direct workstation printing is disabled") ||
      normalized.includes("direct workstation printing is not available") ||
      normalized.includes("qz signing") ||
      normalized.includes("signing route") ||
      normalized.includes("csrf validation failed")
    ) {
      return {
        message: "Direct workstation printing is not set up for this workspace.",
        recovery: "Use Preview or Download PDF, or contact your administrator.",
      };
    }
    if (
      normalized.includes("signature") ||
      normalized.includes("certificate") ||
      normalized.includes("sign")
    ) {
      return {
        message: "Direct workstation printing is not set up for this workspace.",
        recovery: "Use Preview or Download PDF, or contact your administrator.",
      };
    }
    if (normalized.includes("not found") && normalized.includes("printer")) {
      if (preferredPrinterName) {
        return {
          message: `Printer "${preferredPrinterName}" was not found in QZ Tray.`,
          recovery: "Use a different workstation printer or use Preview or Download PDF.",
        };
      }
      return {
        message: "The selected printer was not found in QZ Tray.",
        recovery: "Use a different workstation printer or use Preview or Download PDF.",
      };
    }
    if (normalized.includes("no default printer")) {
      return {
        message: "No default printer is available on this workstation.",
        recovery: "Choose a printer name or set a workstation default printer.",
      };
    }
    return {
      message: message,
      recovery: "Use Preview or Download PDF if the issue continues.",
    };
  }

  async function registerWorkstation(button) {
    const registerUrl = String(
      button.getAttribute("data-qz-workstation-register-url") || ""
    ).trim();
    if (!registerUrl) {
      return {
        workstation: {
          key: workstationId(),
          label: preferredWorkstationLabel(),
          named: !!preferredWorkstationLabel(),
        },
        needsWorkstationName: !preferredWorkstationLabel(),
      };
    }

    const response = await postJson(registerUrl, {
      workstation_key: workstationId(),
      workstation_label: preferredWorkstationLabel() || null,
    });
    const workstation = response.workstation || {};
    const label = String(workstation.label || "").trim();
    if (label) {
      storageSet(WORKSTATION_LABEL_STORAGE_KEY, label);
    }
    const state = {
      workstation: {
        key: String(workstation.key || workstationId()).trim(),
        label: label,
        named: !!label,
      },
      needsWorkstationName: Boolean(response.needs_workstation_name || !label),
    };
    applyWorkstationUiState(state);
    return state;
  }

  async function promptForWorkstationName(button) {
    const labelUrl = String(
      button.getAttribute("data-qz-workstation-label-url") || ""
    ).trim();
    if (!labelUrl) {
      return;
    }
    const currentLabel = preferredWorkstationLabel();
    const value = window.prompt("Name this workstation", currentLabel || "");
    if (value === null) {
      return;
    }
    const nextLabel = String(value || "").trim();
    if (!nextLabel) {
      showErrorToast("Workstation name required. Enter a short label such as Front Desk PC.");
      return;
    }
    try {
      const response = await postJson(labelUrl, {
        workstation_key: workstationId(),
        workstation_label: nextLabel,
      });
      const workstation = response.workstation || {};
      const savedLabel = String(workstation.label || nextLabel).trim();
      storageSet(WORKSTATION_LABEL_STORAGE_KEY, savedLabel);
      applyWorkstationUiState({
        workstation: {
          key: String(workstation.key || workstationId()).trim(),
          label: savedLabel,
          named: true,
        },
        needsWorkstationName: false,
      });
    } catch (error) {
      const message = String(error && error.message ? error.message : error || "").trim();
      showErrorToast(message || "Workstation name could not be saved.");
    }
  }

  async function resolvePrintTarget(button) {
    const resolveUrl = String(button.getAttribute("data-qz-resolve-url") || "").trim();
    const documentType = String(button.getAttribute("data-qz-document-type") || "").trim();
    const fallbackPrinterName = String(
      button.getAttribute("data-qz-printer-name") || ""
    ).trim();

    if (!resolveUrl || !documentType) {
      const displayName = fallbackPrinterName || "Default Printer";
      return {
        printerName: fallbackPrinterName,
        printerDisplayName: displayName,
        hint: `Printing via QZ -> ${displayName}`,
      };
    }

    const response = await postJson(resolveUrl, {
      workstation_key: workstationId(),
      document_type: documentType,
    });
    const workstation = response.workstation || {};
    const label = String(workstation.label || "").trim();
    if (label) {
      storageSet(WORKSTATION_LABEL_STORAGE_KEY, label);
    }
    applyWorkstationUiState({
      workstation: {
        key: String(workstation.key || workstationId()).trim(),
        label: label,
        named: !!label,
      },
      needsWorkstationName: Boolean(response.needs_workstation_name || !label),
    });

    const printer = response.printer || {};
    const printerName = String(printer.name || "").trim();
    const displayName = String(printer.display_name || "").trim() || "Default Printer";
    return {
      printerName: printerName,
      printerDisplayName: displayName,
      hint: String(response.hint || "").trim() || `Printing via QZ -> ${displayName}`,
    };
  }

  async function initializeQzButtons() {
    const buttons = Array.from(document.querySelectorAll(BUTTON_SELECTOR));
    if (!buttons.length) {
      return;
    }
    try {
      await registerWorkstation(buttons[0]);
    } catch (_error) {
      return;
    }
  }

  async function handleQzPrint(button) {
    const statusElement = findStatusElement(button);
    const pdfUrl = String(button.getAttribute("data-qz-pdf-url") || "").trim();
    const certificateUrl = String(
      button.getAttribute("data-qz-certificate-url") || ""
    ).trim();
    const signUrl = String(button.getAttribute("data-qz-sign-url") || "").trim();
    const documentLabel =
      String(button.getAttribute("data-qz-document-label") || "").trim() || "Ticket";
    const successBaseUrl = String(
      button.getAttribute("data-qz-success-base-url") || ""
    ).trim();
    const successKind = String(
      button.getAttribute("data-qz-success-kind") || "ticket"
    ).trim();
    const originalLabel = button.textContent || "";
    const originalDisabled = button.disabled;

    try {
      dismissQzToasts();
      button.disabled = true;
      button.setAttribute("aria-busy", "true");
      button.textContent = "Printing...";

      if (!pdfUrl) {
        throw new Error("Ticket PDF URL is missing.");
      }

      setStatus(statusElement, "Preparing direct print...", null);
      await registerWorkstation(button);
      const printTarget = await resolvePrintTarget(button);
      setStatus(statusElement, printTarget.hint, null);

      const qz = await ensureQzLibrary();
      configureQzSecurity(qz, {
        certificateUrl: certificateUrl,
        signUrl: signUrl,
      });

      setStatus(statusElement, "Connecting to QZ Tray...", null);
      await ensureQzConnection(qz);

      const printerName = await resolvePrinter(qz, printTarget.printerName);
      setStatus(statusElement, `Sending ${documentLabel} to ${printerName}...`, null);

      const pdfBase64 = await fetchPdfAsBase64(pdfUrl);
      const config = qz.configs.create(printerName, {
        jobName: documentLabel,
      });
      await qz.print(config, [
        {
          type: "pixel",
          format: "pdf",
          flavor: "base64",
          data: pdfBase64,
        },
      ]);

      setStatus(statusElement, `Printed to ${printerName}.`, "success");
      const successUrl = buildSuccessUrl(successBaseUrl, printerName, successKind);
      if (successUrl) {
        window.location.assign(successUrl);
      }
    } catch (error) {
      const errorState = classifyError(
        error,
        String(button.getAttribute("data-qz-printer-name") || "").trim()
      );
      setStatus(statusElement, "", null);
      showErrorToast(`Direct print unavailable. ${errorState.message} ${errorState.recovery}`);
    } finally {
      button.disabled = originalDisabled;
      button.removeAttribute("aria-busy");
      button.textContent = originalLabel;
    }
  }

  document.addEventListener("submit", function (event) {
    const submitter = event.submitter;
    if (!(submitter instanceof HTMLElement) || !submitter.matches(BUTTON_SELECTOR)) {
      return;
    }
    event.preventDefault();
    handleQzPrint(submitter);
  });

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () {
      initializeQzButtons();
    });
  } else {
    initializeQzButtons();
  }

  window.addEventListener("pagehide", function () {
    if (
      !window.qz ||
      !window.qz.websocket ||
      typeof window.qz.websocket.isActive !== "function" ||
      !window.qz.websocket.isActive() ||
      typeof window.qz.websocket.disconnect !== "function"
    ) {
      return;
    }
    window.qz.websocket.disconnect().catch(function () {
      return null;
    });
  });
})();
