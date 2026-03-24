(function () {
  const toggle = document.querySelector("[data-shell-toggle]");
  const sidebar = document.querySelector("[data-shell-sidebar]");
  const backdrop = document.querySelector("[data-shell-backdrop]");

  if (!toggle || !sidebar || !backdrop) {
    return;
  }

  const mobileQuery = window.matchMedia("(max-width: 1099px)");

  function isMobile() {
    return mobileQuery.matches;
  }

  function syncShellState(open) {
    const isOpen = Boolean(open) && isMobile();
    document.body.classList.toggle("is-shell-nav-open", isOpen);
    toggle.setAttribute("aria-expanded", isOpen ? "true" : "false");
    backdrop.hidden = !isOpen;

    if (isMobile()) {
      if (isOpen) {
        sidebar.removeAttribute("inert");
      } else {
        sidebar.setAttribute("inert", "");
      }
      return;
    }

    sidebar.removeAttribute("inert");
    backdrop.hidden = true;
    document.body.classList.remove("is-shell-nav-open");
  }

  function focusToggle() {
    if (typeof toggle.focus === "function") {
      toggle.focus();
    }
  }

  function openShell() {
    if (!isMobile()) {
      return;
    }
    syncShellState(true);
    const firstLink = sidebar.querySelector("a");
    if (firstLink && typeof firstLink.focus === "function") {
      firstLink.focus();
    }
  }

  function closeShell(options) {
    const restoreFocus = Boolean(options && options.restoreFocus);
    syncShellState(false);
    if (restoreFocus) {
      focusToggle();
    }
  }

  toggle.addEventListener("click", function () {
    if (!isMobile()) {
      return;
    }
    const isOpen = document.body.classList.contains("is-shell-nav-open");
    if (isOpen) {
      closeShell();
      return;
    }
    openShell();
  });

  backdrop.addEventListener("click", function () {
    closeShell({ restoreFocus: true });
  });

  document.addEventListener("keydown", function (event) {
    if (event.key !== "Escape") {
      return;
    }
    if (!document.body.classList.contains("is-shell-nav-open")) {
      return;
    }
    closeShell({ restoreFocus: true });
  });

  sidebar.querySelectorAll("a").forEach(function (link) {
    link.addEventListener("click", function () {
      if (isMobile()) {
        closeShell();
      }
    });
  });

  const handleMediaChange = function () {
    syncShellState(false);
  };

  if (typeof mobileQuery.addEventListener === "function") {
    mobileQuery.addEventListener("change", handleMediaChange);
  } else if (typeof mobileQuery.addListener === "function") {
    mobileQuery.addListener(handleMediaChange);
  }

  syncShellState(false);
})();
