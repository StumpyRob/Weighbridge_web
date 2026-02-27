(function () {
  function onReady(callback) {
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", callback, { once: true });
      return;
    }
    callback();
  }

  function closestHelpIcon(target) {
    if (!(target instanceof Element)) {
      return null;
    }
    return target.closest(".help-icon");
  }

  onReady(function () {
    if (!(document.body instanceof HTMLBodyElement)) {
      return;
    }

    const initialIcon = document.querySelector(".help-icon");
    if (!(initialIcon instanceof HTMLElement)) {
      return;
    }

    document.body.classList.add("js-help-tooltips");

    const layer = document.createElement("div");
    layer.className = "help-tooltip-layer";
    layer.setAttribute("role", "tooltip");
    document.body.appendChild(layer);

    let activeIcon = null;
    let rafHandle = 0;

    function helpTextFor(icon) {
      const wrapper = icon.closest(".help-inline");
      if (!(wrapper instanceof HTMLElement)) {
        return "";
      }
      const inlineTip = wrapper.querySelector(".help-tooltip");
      if (!(inlineTip instanceof HTMLElement)) {
        return "";
      }
      return String(inlineTip.textContent || "").trim();
    }

    function positionLayer(icon) {
      const gap = 8;
      const margin = 8;
      const rect = icon.getBoundingClientRect();

      layer.style.left = "0px";
      layer.style.top = "0px";

      const tipRect = layer.getBoundingClientRect();
      let left = rect.left + rect.width / 2 - tipRect.width / 2;
      left = Math.max(margin, Math.min(left, window.innerWidth - tipRect.width - margin));

      let top = rect.top - tipRect.height - gap;
      if (top < margin) {
        top = rect.bottom + gap;
      }
      const maxTop = window.innerHeight - tipRect.height - margin;
      top = Math.max(margin, Math.min(top, maxTop));

      layer.style.left = Math.round(left) + "px";
      layer.style.top = Math.round(top) + "px";
    }

    function show(icon) {
      const text = helpTextFor(icon);
      if (!text) {
        hide();
        return;
      }
      activeIcon = icon;
      layer.textContent = text;
      layer.classList.add("is-visible");
      positionLayer(icon);
    }

    function hide() {
      activeIcon = null;
      layer.classList.remove("is-visible");
    }

    function scheduleReposition() {
      if (!(activeIcon instanceof HTMLElement)) {
        return;
      }
      if (rafHandle) {
        cancelAnimationFrame(rafHandle);
      }
      rafHandle = requestAnimationFrame(function () {
        if (activeIcon instanceof HTMLElement) {
          positionLayer(activeIcon);
        }
      });
    }

    document.addEventListener("mouseover", function (event) {
      const icon = closestHelpIcon(event.target);
      if (!(icon instanceof HTMLElement)) {
        return;
      }
      if (activeIcon === icon) {
        return;
      }
      show(icon);
    });

    document.addEventListener("mouseout", function (event) {
      if (!(activeIcon instanceof HTMLElement)) {
        return;
      }
      const sourceIcon = closestHelpIcon(event.target);
      if (sourceIcon !== activeIcon) {
        return;
      }
      const nextIcon = closestHelpIcon(event.relatedTarget);
      if (nextIcon === activeIcon) {
        return;
      }
      hide();
    });

    document.addEventListener("focusin", function (event) {
      const icon = closestHelpIcon(event.target);
      if (!(icon instanceof HTMLElement)) {
        return;
      }
      show(icon);
    });

    document.addEventListener("focusout", function (event) {
      const icon = closestHelpIcon(event.target);
      if (!(icon instanceof HTMLElement)) {
        return;
      }
      if (activeIcon === icon) {
        hide();
      }
    });

    document.addEventListener("click", function (event) {
      const icon = closestHelpIcon(event.target);
      if (icon instanceof HTMLButtonElement) {
        event.preventDefault();
        if (activeIcon === icon) {
          hide();
        } else {
          show(icon);
        }
        return;
      }
      if (activeIcon && event.target instanceof Node && !activeIcon.contains(event.target)) {
        hide();
      }
    });

    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape") {
        hide();
      }
    });

    window.addEventListener("scroll", scheduleReposition, true);
    window.addEventListener("resize", scheduleReposition);
  });
})();
