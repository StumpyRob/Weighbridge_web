(function () {
  const BUTTON_SELECTOR = "[data-qz-print-button]";
  const STATUS_SELECTOR = "[data-qz-print-status]";
  const QZ_LIBRARY_SRC = "/static/vendor/qz-tray.js";
  let qzLoadPromise = null;

  function findStatusElement(button) {
    const root = button.closest(".print-actions");
    if (!root) {
      return null;
    }
    return root.querySelector(STATUS_SELECTOR);
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

  function configureQzSecurity(qz) {
    if (!qz || qz.__weighbridgeQzConfigured) {
      return;
    }
    if (qz.security && typeof qz.security.setCertificatePromise === "function") {
      qz.security.setCertificatePromise(function (resolve) {
        resolve("");
      });
    }
    if (qz.security && typeof qz.security.setSignatureAlgorithm === "function") {
      qz.security.setSignatureAlgorithm("SHA512");
    }
    if (qz.security && typeof qz.security.setSignaturePromise === "function") {
      qz.security.setSignaturePromise(function () {
        return function (resolve) {
          resolve("");
        };
      });
    }
    qz.__weighbridgeQzConfigured = true;
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

  function buildSuccessUrl(baseUrl, printerName) {
    if (!baseUrl) {
      return "";
    }
    const url = new URL(baseUrl, window.location.origin);
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

  function describeError(error, preferredPrinterName) {
    const message = String(error && error.message ? error.message : error || "").trim();
    const normalized = message.toLowerCase();
    if (!message) {
      return "Direct print failed.";
    }
    if (
      normalized.includes("failed to load qz tray support") ||
      normalized.includes("javascript library did not load")
    ) {
      return "QZ Tray support is unavailable in this browser session.";
    }
    if (
      normalized.includes("websocket") ||
      normalized.includes("connect") ||
      normalized.includes("refused") ||
      normalized.includes("closed before") ||
      normalized.includes("failed to establish a connection")
    ) {
      return "QZ Tray is not running or could not be reached on this workstation.";
    }
    if (
      normalized.includes("signature") ||
      normalized.includes("certificate") ||
      normalized.includes("sign")
    ) {
      return "QZ Tray rejected the request because certificate/signing is not fully configured yet.";
    }
    if (normalized.includes("not found") && normalized.includes("printer")) {
      if (preferredPrinterName) {
        return `Printer "${preferredPrinterName}" was not found in QZ Tray.`;
      }
      return "The selected printer was not found in QZ Tray.";
    }
    if (normalized.includes("no default printer")) {
      return "No default printer is available for QZ Tray.";
    }
    return message;
  }

  async function handleQzPrint(button) {
    const statusElement = findStatusElement(button);
    const pdfUrl = String(button.getAttribute("data-qz-pdf-url") || "").trim();
    const preferredPrinterName = String(
      button.getAttribute("data-qz-printer-name") || ""
    ).trim();
    const documentLabel =
      String(button.getAttribute("data-qz-document-label") || "").trim() || "Ticket";
    const successBaseUrl = String(
      button.getAttribute("data-qz-success-base-url") || ""
    ).trim();
    const originalLabel = button.textContent;
    const originalDisabled = button.disabled;

    try {
      button.disabled = true;
      button.setAttribute("aria-busy", "true");
      button.textContent = "Printing...";

      if (!pdfUrl) {
        throw new Error("Ticket PDF URL is missing.");
      }

      setStatus(statusElement, "Preparing direct print...", null);
      const qz = await ensureQzLibrary();
      configureQzSecurity(qz);

      setStatus(statusElement, "Connecting to QZ Tray...", null);
      await ensureQzConnection(qz);

      const printerName = await resolvePrinter(qz, preferredPrinterName);
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
      const successUrl = buildSuccessUrl(successBaseUrl, printerName);
      if (successUrl) {
        window.location.assign(successUrl);
      }
    } catch (error) {
      const message = describeError(error, preferredPrinterName);
      setStatus(
        statusElement,
        `${message} Start QZ Tray and retry, or use Preview/Download PDF.`,
        "error"
      );
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
