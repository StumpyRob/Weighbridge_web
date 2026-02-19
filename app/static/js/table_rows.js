(() => {
  function isInteractiveElement(target) {
    return Boolean(
      target.closest(
        "a, button, input, select, textarea, label, summary, form, [data-row-link-ignore]"
      )
    );
  }

  const rows = document.querySelectorAll("tr[data-row-link]");
  rows.forEach((row) => {
    const href = String(row.getAttribute("data-row-link") || "").trim();
    if (!href) {
      return;
    }
    row.classList.add("data-row-link");
    row.tabIndex = 0;
    row.setAttribute("role", "link");

    row.addEventListener("click", (event) => {
      if (isInteractiveElement(event.target)) {
        return;
      }
      window.location.href = href;
    });

    row.addEventListener("keydown", (event) => {
      if (event.key !== "Enter" && event.key !== " ") {
        return;
      }
      event.preventDefault();
      window.location.href = href;
    });
  });
})();
