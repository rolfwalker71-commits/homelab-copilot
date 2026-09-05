/**
 * Dual chrome + appearance theme.
 * Chrome: localStorage `hlops-chrome` = auto | android | desktop | ios
 * Theme:  localStorage `hlops-theme`  = system | light | dark
 * Mobile companion (`/mobile`): localStorage `hlops-mobile-theme`.
 *   1) hlops-mobile-theme if set
 *   2) else inherit hlops-theme (read-only, never overwritten from /mobile)
 *   3) else dark
 * data-theme is always resolved light|dark; System follows prefers-color-scheme.
 */
(function () {
  const CHROME_KEY = "hlops-chrome";
  const THEME_KEY = "hlops-theme";
  const MOBILE_THEME_KEY = "hlops-mobile-theme";
  const MQ_LG = window.matchMedia("(min-width: 1024px)");
  const MQ_LIGHT = window.matchMedia("(prefers-color-scheme: light)");
  const THEME_LABELS = { light: "Hell", dark: "Dunkel", system: "System" };
  const isMobileApp = () => location.pathname.startsWith("/mobile");
  const THEME_COLORS = {
    dark: { android: "#15221f", desktop: "#161c1b", ios: "#15221f" },
    light: { android: "#d4ebe5", desktop: "#e8eeec", ios: "#d4ebe5" },
  };

  function resolveChrome(pref) {
    if (isMobileApp()) return "android";
    const p = pref || localStorage.getItem(CHROME_KEY) || "auto";
    if (p === "android" || p === "desktop" || p === "ios") return p;
    return MQ_LG.matches ? "desktop" : "android";
  }

  function normalizeThemePref(pref, fallback) {
    if (pref === "light" || pref === "dark" || pref === "system") return pref;
    return fallback || "system";
  }

  function readThemePref() {
    if (isMobileApp()) {
      const mobile = localStorage.getItem(MOBILE_THEME_KEY);
      if (mobile === "light" || mobile === "dark" || mobile === "system") return mobile;
      const desk = localStorage.getItem(THEME_KEY);
      if (desk === "light" || desk === "dark" || desk === "system") return desk;
      return "dark";
    }
    return normalizeThemePref(localStorage.getItem(THEME_KEY), "system");
  }

  function persistThemePref(pref) {
    const n = normalizeThemePref(pref, isMobileApp() ? "dark" : "system");
    if (isMobileApp()) localStorage.setItem(MOBILE_THEME_KEY, n);
    else localStorage.setItem(THEME_KEY, n);
    return n;
  }

  function resolveTheme(pref) {
    const p = normalizeThemePref(pref || readThemePref(), isMobileApp() ? "dark" : "system");
    if (p === "light" || p === "dark") return p;
    return MQ_LIGHT.matches ? "light" : "dark";
  }

  function applyThemeColor(theme, chrome) {
    const meta =
      document.querySelector('meta[name="theme-color"]:not([media])') ||
      document.querySelector('meta[name="theme-color"]');
    if (!meta) return;
    const row = THEME_COLORS[theme] || THEME_COLORS.dark;
    meta.setAttribute("content", row[chrome] || row.android);
  }

  function syncThemeControls(themePref) {
    document.querySelectorAll("[data-theme-set]").forEach((el) => {
      const on = el.getAttribute("data-theme-set") === themePref;
      el.classList.toggle("is-active", on);
      if (el.getAttribute("role") === "radio" || el.getAttribute("role") === "menuitemradio") {
        el.setAttribute("aria-checked", on ? "true" : "false");
      }
    });
    const btn = document.getElementById("btn-theme");
    if (btn) {
      btn.setAttribute("aria-label", "Darstellung: " + (THEME_LABELS[themePref] || themePref));
      btn.dataset.themePref = themePref;
    }
    document.querySelectorAll("[data-theme-icon]").forEach((el) => {
      el.hidden = el.getAttribute("data-theme-icon") !== themePref;
    });
  }

  function apply() {
    const chromePref = localStorage.getItem(CHROME_KEY) || "auto";
    const themePref = readThemePref();
    const chrome = resolveChrome(chromePref);
    const theme = resolveTheme(themePref);
    const root = document.documentElement;
    root.dataset.chrome = chrome;
    root.dataset.theme = theme;
    root.dataset.themePref = themePref;
    root.style.colorScheme = theme;
    applyThemeColor(theme, chrome);
    syncThemeControls(themePref);
    return { chrome, theme, themePref };
  }

  function closeThemeMenu() {
    const menu = document.getElementById("theme-menu");
    const btn = document.getElementById("btn-theme");
    if (menu) menu.hidden = true;
    if (btn) btn.setAttribute("aria-expanded", "false");
  }

  apply();

  MQ_LG.addEventListener("change", () => {
    if ((localStorage.getItem(CHROME_KEY) || "auto") === "auto") apply();
  });
  MQ_LIGHT.addEventListener("change", () => {
    if (readThemePref() === "system") apply();
  });

  document.addEventListener("click", (e) => {
    const setBtn = e.target.closest("[data-theme-set]");
    if (setBtn) {
      persistThemePref(setBtn.getAttribute("data-theme-set"));
      apply();
      closeThemeMenu();
      return;
    }
    const toggle = e.target.closest("#btn-theme");
    if (toggle) {
      const menu = document.getElementById("theme-menu");
      if (!menu) return;
      const open = menu.hidden;
      menu.hidden = !open;
      toggle.setAttribute("aria-expanded", open ? "true" : "false");
      return;
    }
    if (!e.target.closest(".theme-control")) closeThemeMenu();
  });

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closeThemeMenu();
  });

  window.HomelabChrome = {
    get: () => document.documentElement.dataset.chrome,
    set(pref) {
      localStorage.setItem(CHROME_KEY, pref);
      apply();
    },
    apply,
  };

  window.HomelabTheme = {
    getPref: () => readThemePref(),
    get: () => document.documentElement.dataset.theme,
    set(pref) {
      persistThemePref(pref);
      apply();
    },
    apply,
  };
})();
