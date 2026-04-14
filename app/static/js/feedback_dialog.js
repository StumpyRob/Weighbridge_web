(function () {
  function setOpen(dialog, open) {
    if (!dialog) {
      return;
    }
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

  document.addEventListener("DOMContentLoaded", function () {
    const dialog = document.querySelector("[data-feedback-dialog]");
    const openButtons = Array.from(document.querySelectorAll("[data-feedback-open]"));
    if (!dialog || openButtons.length === 0) {
      return;
    }

    const closeButtons = dialog.querySelectorAll("[data-feedback-close]");
    const pageUrlField = dialog.querySelector("[data-feedback-page-url]");
    const messageField = dialog.querySelector("[data-feedback-message]");

    function syncPageUrl() {
      if (pageUrlField) {
        pageUrlField.value = window.location.href || pageUrlField.value || "";
      }
    }

    openButtons.forEach(function (button) {
      button.addEventListener("click", function () {
        syncPageUrl();
        setOpen(dialog, true);
        window.setTimeout(function () {
          if (messageField) {
            messageField.focus();
          }
        }, 0);
      });
    });

    closeButtons.forEach(function (button) {
      button.addEventListener("click", function () {
        setOpen(dialog, false);
      });
    });

    dialog.addEventListener("cancel", function (event) {
      event.preventDefault();
      setOpen(dialog, false);
    });

    dialog.addEventListener("click", function (event) {
      if (event.target !== dialog) {
        return;
      }
      setOpen(dialog, false);
    });

    syncPageUrl();
  });
})();
