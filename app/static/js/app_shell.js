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
  let desktopCollapsed = root.classList.contains("shell-sidebar-collapsed");

  function isMobile() {
    return mobileQuery.matches;
  }

  function persistCollapsedState() {
    try {
      window.localStorage.setItem(storageKey, desktopCollapsed ? "1" : "0");
    } catch (error) {
      return;
    }
  }

  function syncNavTitles() {
    const shouldShowTitles = desktopCollapsed && !isMobile();
    navLinks.forEach(function (link) {
      const label = link.getAttribute("data-nav-label") || "";
      if (shouldShowTitles && label) {
        link.setAttribute("title", label);
      } else {
        link.removeAttribute("title");
      }
    });
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

    const sidebarExpanded = !desktopCollapsed;
    toggle.setAttribute("aria-expanded", sidebarExpanded ? "true" : "false");
    toggle.setAttribute(
      "aria-label",
      sidebarExpanded ? "Collapse sidebar" : "Expand sidebar"
    );
  }

  function applyDesktopCollapsedState() {
    root.classList.toggle("shell-sidebar-collapsed", desktopCollapsed);
    shell.classList.toggle("app-shell--collapsed", desktopCollapsed);
    syncNavTitles();
  }

  function setDesktopCollapsed(nextCollapsed) {
    desktopCollapsed = Boolean(nextCollapsed);
    applyDesktopCollapsedState();
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

    setDesktopCollapsed(!desktopCollapsed);
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
    applyDesktopCollapsedState();
  };

  if (typeof mobileQuery.addEventListener === "function") {
    mobileQuery.addEventListener("change", handleMediaChange);
  } else if (typeof mobileQuery.addListener === "function") {
    mobileQuery.addListener(handleMediaChange);
  }

  applyDesktopCollapsedState();
  syncShellState(false);
})();
