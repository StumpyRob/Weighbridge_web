(function () {
  const storageKey = "sidebar_collapsed";
  const root = document.documentElement;
  const shell = document.querySelector("[data-shell-root]");
  const toggle = document.querySelector("[data-shell-toggle]");
  const sidebar = document.querySelector("[data-shell-sidebar]");
  const backdrop = document.querySelector("[data-shell-backdrop]");

  if (!shell || !toggle || !sidebar || !backdrop) {
    return;
  }

  const mobileQuery = window.matchMedia("(max-width: 1099px)");
  const navLinks = sidebar.querySelectorAll("a");
  let desktopHidden = root.classList.contains("shell-sidebar-hidden");

  function isMobile() {
    return mobileQuery.matches;
  }

  function persistCollapsedState() {
    try {
      window.localStorage.setItem(storageKey, desktopHidden ? "1" : "0");
    } catch (error) {
      return;
    }
  }

  function syncToggleState(isDrawerOpen) {
    if (isMobile()) {
      const drawerExpanded = Boolean(isDrawerOpen);
      toggle.setAttribute("aria-expanded", drawerExpanded ? "true" : "false");
      toggle.setAttribute(
        "aria-label",
        drawerExpanded ? "Close navigation menu" : "Open navigation menu"
      );
      return;
    }

    const sidebarVisible = !desktopHidden;
    toggle.setAttribute("aria-expanded", sidebarVisible ? "true" : "false");
    toggle.setAttribute(
      "aria-label",
      sidebarVisible ? "Hide navigation sidebar" : "Show navigation sidebar"
    );
  }

  function applyDesktopVisibilityState() {
    root.classList.toggle("shell-sidebar-hidden", desktopHidden);
    shell.classList.toggle("app-shell--sidebar-hidden", desktopHidden);
  }

  function setDesktopHidden(nextHidden) {
    desktopHidden = Boolean(nextHidden);
    applyDesktopVisibilityState();
    persistCollapsedState();
    syncToggleState(document.body.classList.contains("is-shell-nav-open"));
  }

  function syncShellState(open) {
    const isOpen = Boolean(open) && isMobile();
    document.body.classList.toggle("is-shell-nav-open", isOpen);
    backdrop.hidden = !isOpen;

    if (isMobile()) {
      if (isOpen) {
        sidebar.removeAttribute("inert");
      } else {
        sidebar.setAttribute("inert", "");
      }
      syncToggleState(isOpen);
      return;
    }

    sidebar.removeAttribute("inert");
    backdrop.hidden = true;
    document.body.classList.remove("is-shell-nav-open");
    syncToggleState(false);
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
    if (isMobile()) {
      const isOpen = document.body.classList.contains("is-shell-nav-open");
      if (isOpen) {
        closeShell();
        return;
      }
      openShell();
      return;
    }

    setDesktopHidden(!desktopHidden);
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

  navLinks.forEach(function (link) {
    link.addEventListener("click", function () {
      if (isMobile()) {
        closeShell();
      }
    });
  });

  const handleMediaChange = function () {
    syncShellState(false);
    applyDesktopVisibilityState();
  };

  if (typeof mobileQuery.addEventListener === "function") {
    mobileQuery.addEventListener("change", handleMediaChange);
  } else if (typeof mobileQuery.addListener === "function") {
    mobileQuery.addListener(handleMediaChange);
  }

  applyDesktopVisibilityState();
  syncShellState(false);
})();
