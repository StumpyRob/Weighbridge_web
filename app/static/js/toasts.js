(function () {
  const toastsRoot = document.getElementById("flash-toasts");
  if (!toastsRoot) {
    return;
  }

  const HIDE_DELAY_MS = 4000;
  const HIDE_TRANSITION_MS = 250;
  const SUCCESS_QUERY_FLAGS = ["saved", "created", "completed", "paid", "voided"];

  function removeToast(toast) {
    if (toast && toast.parentNode) {
      toast.remove();
    }
  }

  function hideToast(toast) {
    if (!toast || toast.dataset.hiding === "1") {
      return;
    }
    toast.dataset.hiding = "1";
    toast.classList.add("is-hiding");
    toast.addEventListener(
      "transitionend",
      function () {
        removeToast(toast);
      },
      { once: true }
    );
    window.setTimeout(function () {
      removeToast(toast);
    }, HIDE_TRANSITION_MS + 50);
  }

  toastsRoot.addEventListener("click", function (event) {
    const target = event.target;
    if (!(target instanceof HTMLElement)) {
      return;
    }
    const closeButton = target.closest("[data-toast-close]");
    if (!closeButton) {
      return;
    }
    const toast = closeButton.closest(".flash-toast");
    if (!(toast instanceof HTMLElement)) {
      return;
    }
    hideToast(toast);
  });

  const successToasts = Array.from(
    toastsRoot.querySelectorAll(".flash-toast[data-flash-success='1']")
  );

  if (successToasts.length > 0 && window.history && window.history.replaceState) {
    const url = new URL(window.location.href);
    let shouldReplace = false;
    SUCCESS_QUERY_FLAGS.forEach(function (flag) {
      if (url.searchParams.get(flag) === "1") {
        url.searchParams.delete(flag);
        shouldReplace = true;
      }
    });
    if (shouldReplace) {
      const nextQuery = url.searchParams.toString();
      const nextUrl = `${url.pathname}${nextQuery ? `?${nextQuery}` : ""}${url.hash}`;
      window.history.replaceState({}, "", nextUrl);
    }
  }

  successToasts.forEach(function (toast) {
    window.setTimeout(function () {
      hideToast(toast);
    }, HIDE_DELAY_MS);
  });
})();
