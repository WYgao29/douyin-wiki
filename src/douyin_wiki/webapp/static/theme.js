"use strict";

(function initializeTheme() {
  const STORAGE_KEY = "douyin-wiki.theme";
  const MODES = new Set(["light", "dark", "system"]);
  const media = window.matchMedia("(prefers-color-scheme: dark)");

  function readMode() {
    try {
      const value = window.localStorage.getItem(STORAGE_KEY) || "system";
      return MODES.has(value) ? value : "system";
    } catch (_error) {
      return "system";
    }
  }

  function resolveMode(mode) {
    return mode === "system" ? (media.matches ? "dark" : "light") : mode;
  }

  function apply(mode = readMode()) {
    const safeMode = MODES.has(mode) ? mode : "system";
    const resolved = resolveMode(safeMode);
    document.documentElement.dataset.theme = resolved;
    document.documentElement.dataset.themeMode = safeMode;
    document.documentElement.style.colorScheme = resolved;
    document.querySelectorAll("[data-theme-select]").forEach((select) => {
      select.value = safeMode;
    });
    document.dispatchEvent(new CustomEvent("douku:theme", {
      detail: {mode: safeMode, resolved},
    }));
    return safeMode;
  }

  function set(mode) {
    const safeMode = MODES.has(mode) ? mode : "system";
    try {
      window.localStorage.setItem(STORAGE_KEY, safeMode);
    } catch (_error) {
      // Local storage may be unavailable in hardened browser profiles.
    }
    return apply(safeMode);
  }

  media.addEventListener("change", () => {
    if (readMode() === "system") apply("system");
  });

  window.DoukuTheme = {apply, readMode, set};
  apply();
})();
