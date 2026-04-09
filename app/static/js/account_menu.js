(function () {
  const menus = Array.from(document.querySelectorAll("[data-account-menu]"));
  if (!menus.length) {
    return;
  }

  function closeMenu(menu, restoreFocus) {
    if (!menu || !menu.open) {
      return;
    }
    menu.removeAttribute("open");
    if (restoreFocus) {
      const summary = menu.querySelector("summary");
      if (summary && typeof summary.focus === "function") {
        summary.focus();
      }
    }
  }

  document.addEventListener("click", function (event) {
    menus.forEach(function (menu) {
      if (menu.open && !menu.contains(event.target)) {
        closeMenu(menu, false);
      }
    });
  });

  document.addEventListener("keydown", function (event) {
    if (event.key !== "Escape") {
      return;
    }
    menus.forEach(function (menu) {
      closeMenu(menu, true);
    });
  });

  menus.forEach(function (menu) {
    menu.querySelectorAll("a").forEach(function (link) {
      link.addEventListener("click", function () {
        closeMenu(menu, false);
      });
    });
  });
})();
