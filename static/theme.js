/*
 * The theme a reader chose, applied before the page is drawn. DECISIONS V1-74.
 *
 * Loaded in <head> without `defer`, so a reader who picked the light theme never
 * sees the other flash first. With nothing stored the page is light, the
 * design's first form (DECISIONS V1-80). The switch itself is in app.js.
 * Storage can be missing or refused (a private window, blocked site data); the
 * page is then simply light.
 */
(function () {
  "use strict";
  try {
    var chosen = window.localStorage.getItem("theme");
    if (chosen === "light" || chosen === "dark" || chosen === "matrix") {
      document.documentElement.setAttribute("data-theme", chosen);
    }
  } catch (e) { /* no storage: the light default */ }
})();
