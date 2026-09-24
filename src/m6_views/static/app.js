/*
 * Search suggestions for the masthead box. Loaded on every page; small.
 *
 * The box is an ordinary GET form to /search, so it works without this file.
 * This adds suggestions as you type, from /api/search: at most ten funds, one
 * row each. Every name is set with textContent, never as HTML -- fund names
 * come from AMFI's files, which this project reads but does not control.
 */
(function () {
  "use strict";

  var input = document.querySelector("input[data-suggest]");
  if (!input) return;
  var list = input.parentNode.querySelector(".search__suggest");
  var timer = null;
  var asked = "";

  function close() {
    list.hidden = true;
    input.setAttribute("aria-expanded", "false");
  }

  function show(hits) {
    list.textContent = "";
    hits.forEach(function (hit) {
      var item = document.createElement("li");
      var link = document.createElement("a");
      link.href = hit.url;
      var name = document.createElement("span");
      name.className = "search__name";
      name.textContent = hit.name;
      var meta = document.createElement("span");
      meta.className = "search__meta";
      meta.textContent = hit.detail;
      link.appendChild(name);
      link.appendChild(meta);
      item.appendChild(link);
      list.appendChild(item);
    });
    list.hidden = hits.length === 0;
    input.setAttribute("aria-expanded", String(hits.length > 0));
  }

  function suggest() {
    var query = input.value.trim();
    if (query.length < 2) { close(); return; }
    if (query === asked) return;
    asked = query;
    fetch(input.getAttribute("data-suggest") + "?q=" + encodeURIComponent(query), {
      headers: { Accept: "application/json" },
    })
      .then(function (r) { return r.ok ? r.json() : []; })
      .then(function (hits) { if (query === asked) show(hits); })
      .catch(close);
  }

  input.setAttribute("aria-expanded", "false");
  input.setAttribute("aria-autocomplete", "list");
  input.addEventListener("input", function () {
    clearTimeout(timer);
    timer = setTimeout(suggest, 150);
  });
  input.addEventListener("keydown", function (e) {
    if (e.key === "ArrowDown" && !list.hidden) {
      var first = list.querySelector("a");
      if (first) { first.focus(); e.preventDefault(); }
    } else if (e.key === "Escape") {
      close();
    }
  });
  list.addEventListener("keydown", function (e) {
    var links = Array.prototype.slice.call(list.querySelectorAll("a"));
    var at = links.indexOf(document.activeElement);
    if (e.key === "ArrowDown" && at < links.length - 1) {
      links[at + 1].focus(); e.preventDefault();
    } else if (e.key === "ArrowUp") {
      (at > 0 ? links[at - 1] : input).focus(); e.preventDefault();
    } else if (e.key === "Escape") {
      close(); input.focus();
    }
  });
  document.addEventListener("click", function (e) {
    if (!input.parentNode.contains(e.target)) close();
  });
})();
