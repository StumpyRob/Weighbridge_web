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
    const openButton = document.querySelector("[data-feedback-open]");
    if (!dialog || !openButton) {
      return;
    }

    const closeButtons = dialog.querySelectorAll("[data-feedback-close]");
    const sourceTitle = dialog.querySelector("[data-feedback-source-title]");
    const messageField = dialog.querySelector("[data-feedback-message]");

    function syncSourceTitle() {
      if (sourceTitle) {
        sourceTitle.value = document.title || "";
      }
    }

    openButton.addEventListener("click", function () {
      syncSourceTitle();
      setOpen(dialog, true);
      window.setTimeout(function () {
        if (messageField) {
          messageField.focus();
        }
      }, 0);
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

    syncSourceTitle();
  });
})();
