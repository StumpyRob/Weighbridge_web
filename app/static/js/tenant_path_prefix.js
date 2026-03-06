(function () {
  function tenantPrefix() {
    const root = document.documentElement;
    return root ? String(root.getAttribute("data-tenant-path-prefix") || "").trim() : "";
  }

  function shouldPrefix(path, prefix) {
    if (!prefix || typeof path !== "string") {
      return false;
    }
    if (!path.startsWith("/") || path.startsWith("//")) {
      return false;
    }
    if (path === prefix || path.startsWith(prefix + "/")) {
      return false;
    }
    if (path.startsWith("/static/uploads/company/")) {
      return true;
    }
    if (path === "/platform" || path.startsWith("/platform/")) {
      return false;
    }
    if (path === "/static" || path.startsWith("/static/")) {
      return false;
    }
    if (path === "/media" || path.startsWith("/media/")) {
      return false;
    }
    return true;
  }

  function prefixPath(value, prefix) {
    if (!shouldPrefix(value, prefix)) {
      return value;
    }
    return prefix + value;
  }

  function applyPrefix(root) {
    const prefix = tenantPrefix();
    if (!prefix) {
      return;
    }

    const scope = root || document;
    const selector = [
      "[href]",
      "[src]",
      "[action]",
      "[hx-get]",
      "[hx-post]",
      "[hx-put]",
      "[hx-patch]",
      "[hx-delete]",
      "[data-row-link]",
    ].join(",");
    const elements = scope.querySelectorAll(selector);

    elements.forEach(function (element) {
      [
        "href",
        "src",
        "action",
        "hx-get",
        "hx-post",
        "hx-put",
        "hx-patch",
        "hx-delete",
        "data-row-link",
      ].forEach(function (attributeName) {
        const value = element.getAttribute(attributeName);
        if (!value) {
          return;
        }
        const scopedValue = prefixPath(value, prefix);
        if (scopedValue !== value) {
          element.setAttribute(attributeName, scopedValue);
        }
      });
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    applyPrefix(document);
  });

  document.body.addEventListener("htmx:afterSwap", function (event) {
    applyPrefix(event.target);
  });
})();
