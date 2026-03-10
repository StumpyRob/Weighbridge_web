(function () {
  function setOpen(dialog, open) {
    if (open) {
      if (typeof dialog.showModal === "function") {
        dialog.showModal();
        return;
      }
      dialog.setAttribute("open", "open");
      return;
    }
    if (typeof dialog.close === "function") {
      dialog.close();
      return;
    }
    dialog.removeAttribute("open");
  }

  function bindResetDialog(root) {
    const openButton = root.querySelector("[data-demo-reset-open]");
    const cancelButton = root.querySelector("[data-demo-reset-cancel]");
    const dialog = root.querySelector("[data-demo-reset-dialog]");
    const input = root.querySelector("[data-demo-reset-input]");
    const submitButton = root.querySelector("[data-demo-reset-submit]");
    if (!openButton || !cancelButton || !dialog || !input || !submitButton) {
      return;
    }

    function resetState() {
      input.value = "";
      submitButton.disabled = true;
    }

    function syncSubmitState() {
      submitButton.disabled = input.value.trim() !== "DEMO";
    }

    openButton.addEventListener("click", function () {
      resetState();
      setOpen(dialog, true);
      window.setTimeout(function () {
        input.focus();
      }, 0);
    });

    cancelButton.addEventListener("click", function () {
      resetState();
      setOpen(dialog, false);
    });

    dialog.addEventListener("cancel", function (event) {
      event.preventDefault();
      resetState();
      setOpen(dialog, false);
    });

    dialog.addEventListener("click", function (event) {
      if (event.target !== dialog) {
        return;
      }
      resetState();
      setOpen(dialog, false);
    });

    dialog.addEventListener("close", resetState);
    input.addEventListener("input", syncSubmitState);
  }

  document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll("[data-demo-reset]").forEach(bindResetDialog);
  });
})();
