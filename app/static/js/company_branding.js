(() => {
  function normalizeHex(rawValue) {
    let value = String(rawValue || "").trim().toUpperCase();
    if (!value) {
      return null;
    }
    if (value.startsWith("#")) {
      value = value.slice(1);
    }
    if (/^[0-9A-F]{3}$/.test(value)) {
      value = value
        .split("")
        .map((char) => char + char)
        .join("");
    }
    if (!/^[0-9A-F]{6}$/.test(value)) {
      return null;
    }
    return `#${value}`;
  }

  function errorNodeFor(inputId) {
    return document.querySelector(`[data-color-error-for="${inputId}"]`);
  }

  function setInvalidState(inputId, isInvalid) {
    const errorNode = errorNodeFor(inputId);
    if (!errorNode) {
      return;
    }
    errorNode.hidden = !isInvalid;
    if (isInvalid) {
      errorNode.classList.add("is-visible");
    } else {
      errorNode.classList.remove("is-visible");
    }
  }

  function syncPair(pickerInput) {
    const targetId = String(pickerInput.dataset.syncTarget || "").trim();
    if (!targetId) {
      return null;
    }
    const hexInput = document.getElementById(targetId);
    if (!hexInput) {
      return null;
    }
    return { pickerInput, hexInput };
  }

  function applyValidValue(pair, value) {
    pair.hexInput.value = value;
    pair.pickerInput.value = value;
    setInvalidState(pair.hexInput.id, false);
  }

  function bindPair(pair) {
    const startingHex = normalizeHex(pair.hexInput.value);
    const startingPicker = normalizeHex(pair.pickerInput.value);
    if (startingHex) {
      applyValidValue(pair, startingHex);
    } else if (startingPicker) {
      applyValidValue(pair, startingPicker);
    } else {
      applyValidValue(pair, "#000000");
    }

    pair.pickerInput.addEventListener("input", () => {
      const normalized = normalizeHex(pair.pickerInput.value);
      if (!normalized) {
        return;
      }
      applyValidValue(pair, normalized);
    });

    pair.hexInput.addEventListener("input", () => {
      const normalized = normalizeHex(pair.hexInput.value);
      if (!pair.hexInput.value.trim()) {
        setInvalidState(pair.hexInput.id, false);
        return;
      }
      if (!normalized) {
        setInvalidState(pair.hexInput.id, true);
        return;
      }
      applyValidValue(pair, normalized);
    });

    pair.hexInput.addEventListener("blur", () => {
      const normalized = normalizeHex(pair.hexInput.value);
      if (!pair.hexInput.value.trim()) {
        setInvalidState(pair.hexInput.id, false);
        return;
      }
      if (!normalized) {
        setInvalidState(pair.hexInput.id, true);
        return;
      }
      applyValidValue(pair, normalized);
    });
  }

  function bindSubmitValidation(form, pairs) {
    form.addEventListener("submit", (event) => {
      let hasInvalid = false;
      for (const pair of pairs) {
        const normalized = normalizeHex(pair.hexInput.value);
        if (!pair.hexInput.value.trim()) {
          setInvalidState(pair.hexInput.id, false);
          continue;
        }
        if (!normalized) {
          setInvalidState(pair.hexInput.id, true);
          hasInvalid = true;
          continue;
        }
        applyValidValue(pair, normalized);
      }
      if (hasInvalid) {
        event.preventDefault();
      }
    });
  }

  document.addEventListener("DOMContentLoaded", () => {
    const pickers = Array.from(document.querySelectorAll("input[type='color'][data-sync-target]"));
    if (!pickers.length) {
      return;
    }
    const pairs = pickers.map(syncPair).filter(Boolean);
    if (!pairs.length) {
      return;
    }
    pairs.forEach(bindPair);
    const form = pairs[0].hexInput.closest("form");
    if (form) {
      bindSubmitValidation(form, pairs);
    }
  });
})();
