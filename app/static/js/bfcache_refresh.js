(function () {
  function shouldReloadForBfcacheRestore() {
    return !!document.querySelector('input[name="row_version"]');
  }

  window.addEventListener("pageshow", function (event) {
    if (!shouldReloadForBfcacheRestore()) {
      return;
    }

    const navigationEntries =
      typeof window.performance?.getEntriesByType === "function"
        ? window.performance.getEntriesByType("navigation")
        : [];
    const restoredFromHistory =
      !!event.persisted ||
      navigationEntries.some(function (entry) {
        return entry && entry.type === "back_forward";
      });

    if (!restoredFromHistory) {
      return;
    }

    window.location.reload();
  });
})();
