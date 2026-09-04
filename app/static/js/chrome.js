/**
 * Dual chrome: Material You 3 Expressive (android) ↔ Fluent 2 (desktop).
 * Preference key: localStorage `hlops-chrome` = auto | android | desktop | ios
 */
(function () {
  const KEY = "hlops-chrome";
  const MQ = window.matchMedia("(min-width: 1024px)");

  function resolve(pref) {
    const p = pref || localStorage.getItem(KEY) || "auto";
    if (p === "android" || p === "desktop" || p === "ios") return p;
    return MQ.matches ? "desktop" : "android";
  }

  function apply() {
    const chrome = resolve();
    document.documentElement.dataset.chrome = chrome;
    const meta = document.querySelector('meta[name="theme-color"]:not([media]), meta[name="theme-color"]');
    // Keep both media metas; update default for install UI
    document.documentElement.style.colorScheme =
      window.matchMedia("(prefers-color-scheme: light)").matches ? "light dark" : "dark light";
    return chrome;
  }

  apply();
  MQ.addEventListener("change", () => {
    if ((localStorage.getItem(KEY) || "auto") === "auto") apply();
  });

  window.HomelabChrome = {
    get: () => document.documentElement.dataset.chrome,
    set(pref) {
      localStorage.setItem(KEY, pref);
      apply();
    },
    apply,
  };
})();
