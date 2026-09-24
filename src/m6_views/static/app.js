/*
 * The page's behaviour, loaded on every page. Everything here enhances markup
 * that already works: without this file search is an ordinary form, tables are
 * ordinary tables, and the theme follows the computer's setting.
 *
 * MODULE_6.md §16.4 holds throughout: the browser positions, sorts, filters and
 * toggles; it never formats or derives a figure. A table sorts on the
 * `data-value` the server wrote beside each figure, never on the text.
 *
 * Every name is set with textContent, never as HTML -- fund names come from
 * AMFI's and fund houses' files, which this project reads but does not control.
 */
(function () {
  "use strict";

  var ROOT = document.body.getAttribute("data-root") || "";

  function store(key, value) {
    try {
      if (value === undefined) return window.localStorage.getItem(key);
      window.localStorage.setItem(key, value);
    } catch (e) { /* storage refused: a convenience lost, nothing else */ }
    return null;
  }

  // --- search ---------------------------------------------------------------
  //
  // Suggestions as you type, from /api/search on the server, or on the public
  // copy (no server, DECISIONS V1-72) from search.json matched here by the
  // server's rules: every word typed must appear, the words in the order typed
  // rank first, then names starting with the first word, then shorter names.

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

  function suggestions(input) {
    var list = input.parentNode.querySelector(".search__suggest");
    if (!list) return;
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
  }

  document.querySelectorAll("input[data-suggest], input[data-index]").forEach(suggestions);

  // --- theme ------------------------------------------------------------------
  //
  // theme.js applied a stored choice before the page was drawn; this is the
  // switch. charts.js watches the attribute and redraws in the new colours.

  function currentTheme() {
    var set = document.documentElement.getAttribute("data-theme");
    if (set) return set;
    return window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
  }

  document.querySelectorAll("[data-theme-toggle]").forEach(function (button) {
    button.addEventListener("click", function () {
      var next = currentTheme() === "dark" ? "light" : "dark";
      document.documentElement.setAttribute("data-theme", next);
      store("theme", next);
    });
  });

  // --- the menu on a narrow screen --------------------------------------------

  var menu = document.querySelector("[data-sidebar-toggle]");
  if (menu) {
    menu.addEventListener("click", function () {
      var open = document.body.classList.toggle("sidebar-open");
      menu.setAttribute("aria-expanded", String(open));
    });
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && document.body.classList.contains("sidebar-open")) {
        document.body.classList.remove("sidebar-open");
        menu.setAttribute("aria-expanded", "false");
        menu.focus();
      }
    });
  }

  // --- sortable tables ----------------------------------------------------------
  //
  // A header button sorts on the server's `data-value` (a number) or on the
  // cell's text. A cell with no figure sorts last in either direction: an
  // absent return is not the lowest return.

  // "number" sorts on the figure, "value" on the server's value as text (an
  // ISO date sorts correctly that way), "text" on what the cell says.
  function sortKey(row, column, mode) {
    var cell = row.cells[column];
    if (mode === "text") return (cell.textContent || "").trim().toLowerCase();
    var raw = cell.getAttribute("data-value");
    if (raw === null || raw === "") return null;
    return mode === "number" ? parseFloat(raw) : raw;
  }

  document.querySelectorAll("table[data-sortable]").forEach(function (table) {
    var body = table.tBodies[0];
    if (!body) return;
    table.querySelectorAll("th button[data-sort]").forEach(function (button) {
      var th = button.closest("th");
      var column = Array.prototype.indexOf.call(th.parentNode.children, th);
      var mode = button.getAttribute("data-sort");
      var numeric = mode !== "text";
      button.addEventListener("click", function () {
        // First click: figures largest first, names A to Z. Then it alternates.
        var current = th.getAttribute("aria-sort");
        var ascending = current === null ? !numeric : current !== "ascending";
        table.querySelectorAll("th[aria-sort]").forEach(function (other) {
          other.removeAttribute("aria-sort");
        });
        th.setAttribute("aria-sort", ascending ? "ascending" : "descending");
        var rows = Array.prototype.slice.call(body.rows);
        rows.sort(function (a, b) {
          var x = sortKey(a, column, mode);
          var y = sortKey(b, column, mode);
          if (x === null && y === null) return 0;
          if (x === null) return 1;
          if (y === null) return -1;
          if (x === y) return 0;
          return (x < y ? -1 : 1) * (ascending ? 1 : -1);
        });
        rows.forEach(function (row) { body.appendChild(row); });
      });
    });
  });

  // --- filtering the public copy's fund table -------------------------------------

  var table = document.querySelector("table[data-filterable]");
  if (table) {
    var family = document.querySelector("select[data-filter-family]");
    var text = document.querySelector("input[data-filter-text]");
    var count = document.querySelector("[data-filter-count]");
    var all = table.tBodies[0].rows;

    var apply = function () {
      var wanted = family ? family.value : "";
      var typed = text ? words(text.value) : [];
      var shown = 0;
      Array.prototype.forEach.call(all, function (row) {
        var name = words(row.cells[0].textContent || "").join(" ");
        var match = (!wanted || row.getAttribute("data-family") === wanted) &&
          typed.every(function (w) { return name.indexOf(w) !== -1; });
        row.hidden = !match;
        if (match) shown += 1;
      });
      if (count) {
        count.textContent = shown === all.length
          ? all.length + " funds"
          : shown + " of " + all.length + " funds";
      }
    };
    if (family) family.addEventListener("change", apply);
    if (text) text.addEventListener("input", apply);

    // A category tile narrows the table rather than jumping to the plain lists
    // below it, which are the way through with scripts off.
    document.querySelectorAll("a[data-family]").forEach(function (tile) {
      tile.addEventListener("click", function (e) {
        if (!family) return;
        e.preventDefault();
        family.value = tile.getAttribute("data-family");
        apply();
        table.closest("section").scrollIntoView({ block: "start" });
      });
    });
  }

  // --- recently viewed funds --------------------------------------------------------
  //
  // Kept in this browser only, for this reader: a fund page records itself, and
  // the home page lists the last few. Stored ids are checked before they become
  // links, so nothing in storage can become anything but a link to a fund page.

  var RECENT = "recent-funds";
  var FUND_ID = /^[A-Za-z0-9_-]{1,40}$/;

  function recent() {
    try {
      var parsed = JSON.parse(store(RECENT) || "[]");
      return Array.isArray(parsed) ? parsed.filter(function (f) {
        return f && FUND_ID.test(String(f.id)) && typeof f.name === "string";
      }) : [];
    } catch (e) { return []; }
  }

  var here = document.body.getAttribute("data-fund-id");
  if (here && FUND_ID.test(here)) {
    var name = document.body.getAttribute("data-fund-name") || here;
    var seen = recent().filter(function (f) { return f.id !== here; });
    seen.unshift({ id: here, name: name });
    store(RECENT, JSON.stringify(seen.slice(0, 6)));
  }

  document.querySelectorAll("[data-recent]").forEach(function (box) {
    var funds = recent();
    var list = box.querySelector("ul");
    if (!funds.length || !list) return;
    var slash = box.getAttribute("data-recent-slash") === "true" ? "/" : "";
    funds.forEach(function (fund) {
      var item = document.createElement("li");
      var link = document.createElement("a");
      link.href = ROOT + "/fund/" + encodeURIComponent(fund.id) + slash;
      link.textContent = fund.name;
      item.appendChild(link);
      list.appendChild(item);
    });
    box.hidden = false;
  });
})();
