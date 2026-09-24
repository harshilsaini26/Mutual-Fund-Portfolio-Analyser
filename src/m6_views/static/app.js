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

  var input = document.querySelector("input[data-suggest], input[data-index]");
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

  // The public copy has no server (DECISIONS V1-72): it ships every fund in
  // search.json, and this matches it here by the server's rules -- every word
  // typed must appear, the words in the order typed rank first, then names
  // starting with the first word, then shorter names.
  var index = null;

  function words(text) {
    return text.toLowerCase().replace(/[^a-z0-9]+/g, " ").trim().split(" ")
      .filter(Boolean).slice(0, 6);
  }

  function local(query) {
    var typed = words(query);
    var phrase = typed.join(" ");
    return index
      .map(function (hit) { return { hit: hit, name: words(hit.name).join(" ") }; })
      .filter(function (c) {
        return typed.every(function (w) { return c.name.indexOf(w) !== -1; });
      })
      .sort(function (a, b) {
        var keyA = [a.name.indexOf(phrase) === -1, a.name.indexOf(typed[0]) !== 0, a.name.length];
        var keyB = [b.name.indexOf(phrase) === -1, b.name.indexOf(typed[0]) !== 0, b.name.length];
        for (var i = 0; i < 3; i++) {
          if (keyA[i] !== keyB[i]) return keyA[i] < keyB[i] ? -1 : 1;
        }
        return a.name < b.name ? -1 : 1;
      })
      .slice(0, 10)
      .map(function (c) { return c.hit; });
  }

  function suggest() {
    var query = input.value.trim();
    if (query.length < 2) { close(); return; }
    if (query === asked) return;
    asked = query;
    var bundled = input.getAttribute("data-index");
    if (bundled) {
      var ready = index ? Promise.resolve() : fetch(bundled)
        .then(function (r) { return r.ok ? r.json() : []; })
        .then(function (all) { index = all; });
      ready.then(function () { if (query === asked) show(local(query)); }).catch(close);
      return;
    }
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
