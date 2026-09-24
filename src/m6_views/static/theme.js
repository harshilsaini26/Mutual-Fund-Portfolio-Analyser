/*
 * The theme a reader chose, applied before the page is drawn. DECISIONS V1-74.
 *
 * Loaded in <head> without `defer`, so a reader who picked the light theme never
 * sees the dark one flash first. With nothing stored, the stylesheet follows the
 * computer's own setting (`prefers-color-scheme`). The switch itself is in
 * app.js. Storage can be missing or refused (a private window, blocked site
 * data); the page then simply follows the system.
 */
(function () {
  "use strict";
  try {
    var chosen = window.localStorage.getItem("theme");
    if (chosen === "light" || chosen === "dark") {
      document.documentElement.setAttribute("data-theme", chosen);
    }
  } catch (e) { /* no storage: follow the system setting */ }
})();
