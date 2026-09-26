/*
 * Interactive charts: every `echart` view. DECISIONS V1-70.
 *
 * Reads each panel's JSON (templates/charts/echart.html) and draws it with the
 * vendored ECharts. MODULE_6.md §16.4 holds: every figure a reader is shown --
 * a tooltip, a bar's label, a tile's caption -- is a string formatted in
 * Python and shipped in the payload. The numbers beside those strings are used
 * only to place a point, exactly as sankey.js uses them for widths. Axis tick
 * labels are the one thing formatted here, because they are the scale rather
 * than a figure from the data.
 *
 * Tooltips use ECharts' rich-text mode, drawn as SVG text rather than inserted
 * as HTML: holding names come from fund houses' files, and nothing from those
 * files is ever written into the page as markup.
 *
 * Period tabs (`a[data-fragment]`) fetch one server-rendered panel and swap it
 * in place, so the headline, table, caveats and footer all change with the
 * chart. Without this file each tab is an ordinary link.
 *
 * Every colour is a token from app.css (DECISIONS V1-74), read when a chart is
 * drawn, so a chart redraws in the new colours when the theme changes.
 */
(function () {
  "use strict";

  if (!window.echarts) return;

  var specs = new WeakMap();
  var MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  var INR = new Intl.NumberFormat("en-IN", {
    style: "currency", currency: "INR", maximumFractionDigits: 0,
  });
  var calm = window.matchMedia("(prefers-reduced-motion: reduce)");

  function token(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }

  function palette() {
    var cats = [];
    for (var i = 1; i <= 8; i++) cats.push(token("--cat-" + i));
    return {
      fund: token("--accent"), bench: token("--bench"),
      ink: token("--ink"), soft: token("--ink-soft"), faint: token("--ink-faint"),
      rule: token("--line"), bg: token("--surface"), raised: token("--surface-2"),
      others: token("--synthetic"), onCat: token("--on-cat"), decal: token("--decal"),
      cats: cats,
    };
  }

  // A token's colour at an opacity: `#8b9cff` and 0.3 -> `rgba(139,156,255,0.3)`.
  function alpha(hex, a) {
    var m = /^#([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i.exec(hex);
    if (!m) return hex;
    return "rgba(" + parseInt(m[1], 16) + "," + parseInt(m[2], 16) + "," +
      parseInt(m[3], 16) + "," + a + ")";
  }

  // The soft fill under a line, fading to nothing at the axis.
  function fade(hex, top) {
    return new echarts.graphic.LinearGradient(0, 0, 0, 1, [
      { offset: 0, color: alpha(hex, top) },
      { offset: 1, color: alpha(hex, 0) },
    ]);
  }

  // §9.4: DD Mon YYYY, the same as every date the server prints.
  function day(value) {
    var d = new Date(value);
    return String(d.getUTCDate()).padStart(2, "0") + " " + MONTHS[d.getUTCMonth()] +
      " " + d.getUTCFullYear();
  }

  function ticks(kind) {
    if (kind === "inr") return function (v) { return INR.format(v); };
    if (kind === "fraction") return function (v) { return Math.round(v * 100) + "%"; };
    return function (v) { return String(v); };
  }

  function number(text) { return text === null ? null : parseFloat(text); }

  function colour(role, p) { return role === "benchmark" ? p.bench : p.fund; }

  function base(p) {
    return {
      animation: !calm.matches,
      textStyle: { color: p.ink, fontFamily: "inherit" },
      grid: { left: 8, right: 18, top: 40, bottom: 12, containLabel: true },
      legend: { top: 0, left: 0, textStyle: { color: p.ink }, itemGap: 18 },
      tooltip: {
        renderMode: "richText", backgroundColor: p.raised, borderColor: p.rule,
        borderWidth: 1, padding: [8, 12], textStyle: { color: p.ink, fontSize: 12 },
        axisPointer: { lineStyle: { color: p.faint, type: "dashed" } },
      },
    };
  }

  // One line per series under a date heading; each line is the server's label.
  function listed(items) {
    if (!items.length) return "";
    var out = [day(items[0].value[0])];
    items.forEach(function (it) {
      if (it.data && it.data.caption) out.push(it.seriesName + ": " + it.data.caption);
    });
    return out.join("\n");
  }

  var KINDS = {
    line: function (c, p, filled) {
      var o = base(p);
      o.grid.bottom = 48;
      o.tooltip.trigger = "axis";
      o.tooltip.formatter = listed;
      o.xAxis = {
        type: "time",
        axisLine: { lineStyle: { color: p.rule } },
        axisTick: { show: false },
        axisLabel: { color: p.faint, hideOverlap: true },
        splitLine: { show: false },
      };
      o.yAxis = {
        type: "value",
        max: c.kind === "area" ? 0 : null,
        axisLabel: { color: p.faint, formatter: ticks(c.y) },
        splitLine: { lineStyle: { color: p.rule } },
      };
      o.dataZoom = [
        // The wheel scrolls the page; Ctrl+wheel, a pinch or the slider zooms.
        // A chart that swallows the wheel traps a reader scrolling past it.
        { type: "inside", zoomOnMouseWheel: "ctrl", moveOnMouseWheel: false,
          preventDefaultMouseMove: false },
        { type: "slider", height: 18, bottom: 6, borderColor: p.rule,
          fillerColor: alpha(p.fund, 0.14), backgroundColor: "transparent",
          handleStyle: { color: p.fund, borderColor: p.fund },
          moveHandleStyle: { color: p.rule },
          dataBackground: { lineStyle: { color: p.faint }, areaStyle: { color: p.rule } },
          textStyle: { color: p.faint },
          labelFormatter: function (v) { return day(v); } },
      ];
      o.series = c.series.map(function (s) {
        var fund = s.role === "fund";
        var series = {
          name: s.name,
          type: "line",
          showSymbol: false,
          // §10.3: the benchmark is dashed as well as a different colour.
          lineStyle: { width: fund ? 2.4 : 1.6, type: fund ? "solid" : "dashed",
                       color: colour(s.role, p) },
          itemStyle: { color: colour(s.role, p) },
          data: s.points.map(function (pt) {
            return { value: [pt[0], number(pt[1])], caption: pt[2] };
          }),
        };
        // A soft fill under the fund's own line; none under the benchmark, and
        // none where the chart is read against a zero line.
        if (filled) {
          series.areaStyle = { color: fade(colour(s.role, p), fund ? 0.32 : 0.08) };
        } else if (fund && !c.zero_line) {
          series.areaStyle = { color: fade(p.fund, 0.18) };
        }
        if (fund && c.zero_line) {
          series.markLine = { silent: true, symbol: "none", label: { show: false },
                              lineStyle: { color: p.soft }, data: [{ yAxis: 0 }] };
        }
        if (fund && c.marks && c.marks.length) {
          series.markPoint = {
            symbol: "circle", symbolSize: 9,
            itemStyle: { color: colour(s.role, p), borderColor: p.bg, borderWidth: 2 },
            label: { show: true, position: "bottom", color: p.ink, distance: 8,
                     formatter: function (m) { return m.name; } },
            data: c.marks.map(function (m) {
              return { name: m.label, coord: [m.at, number(m.value)] };
            }),
          };
        }
        return series;
      });
      return o;
    },

    area: function (c, p) { return KINDS.line(c, p, true); },

    bar: function (c, p) {
      var o = base(p);
      o.grid.top = 56;  // room for the legend and the labels over the tallest bar
      o.tooltip.trigger = "axis";
      o.tooltip.formatter = function (items) {
        return [items[0].name].concat(items.map(function (it) {
          return it.seriesName + ": " + it.data.caption;
        })).join("\n");
      };
      o.xAxis = { type: "category", data: c.categories, axisTick: { show: false },
                  axisLabel: { color: p.soft }, axisLine: { lineStyle: { color: p.rule } } };
      o.yAxis = { type: "value", axisLabel: { color: p.faint, formatter: ticks(c.y) },
                  splitLine: { lineStyle: { color: p.rule } } };
      o.series = c.series.map(function (s) {
        var bench = s.role === "benchmark";
        return {
          name: s.name,
          type: "bar",
          barMaxWidth: 38,
          itemStyle: {
            color: colour(s.role, p),
            // §10.3: hatched as well as coloured.
            decal: bench ? { symbol: "rect", symbolSize: 1, dashArrayX: [1, 0],
                             dashArrayY: [2, 4], rotation: Math.PI / 4,
                             color: p.decal } : null,
            borderRadius: [4, 4, 0, 0],
          },
          data: s.values.map(function (v) {
            var n = number(v[0]);
            return { value: n, caption: v[1],
                     label: { position: n !== null && n < 0 ? "bottom" : "top" } };
          }),
          label: { show: true, color: p.ink, fontSize: 11,
                   formatter: function (d) { return d.data.caption; } },
        };
      });
      return o;
    },

    donut: function (c, p) {
      var o = base(p);
      var captions = {};
      c.slices.forEach(function (s) { captions[s.name] = s.label; });
      // A legend beside the ring rather than labels around it: small slices'
      // labels collide and truncate, a list never does.
      o.legend = {
        orient: "vertical", right: 0, top: "middle", icon: "circle", itemGap: 10,
        textStyle: { color: p.ink },
        formatter: function (name) { return name + "   " + captions[name]; },
      };
      o.tooltip.trigger = "item";
      o.tooltip.formatter = function (d) { return d.name + ": " + d.data.caption; };
      o.series = [{
        type: "pie",
        radius: ["46%", "72%"],
        center: ["28%", "50%"],
        itemStyle: { borderColor: p.bg, borderWidth: 2 },
        label: { show: false },
        data: c.slices.map(function (s, i) {
          return { name: s.name, value: number(s.value), caption: s.label,
                   itemStyle: { color: p.cats[i % p.cats.length] } };
        }),
      }];
      return o;
    },

    treemap: function (c, p) {
      var o = base(p);
      var groups = {};
      c.cells.forEach(function (cell) {
        if (!(cell.group in groups)) groups[cell.group] = Object.keys(groups).length;
      });
      o.legend.show = false;
      o.tooltip.trigger = "item";
      o.tooltip.formatter = function (d) {
        return d.name + "\n" + d.data.group + ": " + d.data.caption;
      };
      o.series = [{
        type: "treemap",
        roam: false,
        nodeClick: false,
        breadcrumb: { show: false },
        width: "100%", height: "100%", top: 0, left: 0,
        label: { show: true, color: p.onCat, overflow: "truncate", fontSize: 11,
                 formatter: function (d) { return d.name + "\n" + d.data.caption; } },
        itemStyle: { borderColor: p.bg, borderWidth: 2, gapWidth: 2, borderRadius: 4 },
        data: c.cells.map(function (cell) {
          // A tile under 1% is too small to letter; its name is on hover.
          return { name: cell.name, value: number(cell.value), caption: cell.label,
                   group: cell.group, label: { show: number(cell.value) >= 1 },
                   itemStyle: { color: cell.others ? p.others
                                : p.cats[groups[cell.group] % p.cats.length] } };
        }),
      }];
      return o;
    },

    // Funds as points: risk across, return up. §10.3: this fund is a larger
    // diamond with its name beside it, not only a different colour. A point's
    // area follows the fund's size where it is known (V1-80, after Fundoo); the
    // size is also in its tooltip, so the area is never the only way to read it.
    scatter: function (c, p) {
      var o = base(p);
      var largest = 0;
      c.series.forEach(function (s) {
        s.points.forEach(function (pt) { largest = Math.max(largest, number(pt[4] || null) || 0); });
      });
      function area(pt, fund) {
        var size = number(pt[4] || null);
        var d = largest > 0 && size ? 7 + 23 * Math.sqrt(size / largest) : 9;
        return fund ? Math.max(d, 16) : d;
      }
      o.grid.bottom = 28;
      o.grid.right = 28;
      o.tooltip.trigger = "item";
      o.tooltip.formatter = function (d) { return d.data.name + "\n" + d.data.caption; };
      function axis(kind, name, where) {
        return {
          type: "value", scale: true, name: name, nameLocation: "middle",
          nameGap: where === "x" ? 28 : 44, nameTextStyle: { color: p.soft },
          axisLabel: { color: p.faint, formatter: ticks(kind) },
          axisLine: { lineStyle: { color: p.rule } },
          splitLine: { lineStyle: { color: p.rule } },
        };
      }
      o.xAxis = axis(c.x, c.x_name, "x");
      o.yAxis = axis(c.y, c.y_name, "y");
      o.series = c.series.map(function (s) {
        var fund = s.role === "fund";
        return {
          name: s.name,
          type: "scatter",
          z: fund ? 3 : 2,
          symbol: fund ? "diamond" : "circle",
          symbolSize: function (value, d) { return d.data.size; },
          itemStyle: fund
            ? { color: p.fund, borderColor: p.bg, borderWidth: 2 }
            : { color: alpha(p.fund, 0.16), borderColor: alpha(p.fund, 0.55), borderWidth: 1 },
          // A short mark ("This fund"); the full name is in the legend.
          label: { show: fund, position: "top", distance: 8, color: p.ink,
                   fontWeight: 600, formatter: function () { return s.mark; } },
          emphasis: { scale: 1.4 },
          data: s.points.map(function (pt) {
            return { value: [number(pt[0]), number(pt[1])], name: pt[2], caption: pt[3],
                     size: area(pt, fund) };
          }),
        };
      });
      return o;
    },

    hbar: function (c, p) {
      var o = base(p);
      o.legend.show = false;
      o.grid.top = 4;
      o.tooltip.trigger = "item";
      o.tooltip.formatter = function (d) { return d.name + ": " + d.data.caption; };
      o.xAxis = { type: "value", show: false };
      o.yAxis = { type: "category", inverse: true,
                  data: c.bars.map(function (b) { return b.name; }),
                  axisLabel: { color: p.ink, width: 170, overflow: "truncate" },
                  axisLine: { show: false }, axisTick: { show: false } };
      o.series = [{
        type: "bar",
        barMaxWidth: 18,
        showBackground: true,
        backgroundStyle: { color: p.raised, borderRadius: 4 },
        itemStyle: { color: p.fund, borderRadius: 4 },
        label: { show: true, position: "right", color: p.ink,
                 formatter: function (d) { return d.data.caption; } },
        data: c.bars.map(function (b) { return { value: number(b.value), caption: b.label }; }),
      }];
      return o;
    },
  };

  function draw(el, spec) {
    var existing = echarts.getInstanceByDom(el);
    if (existing) existing.dispose();
    var bars = el.getAttribute("data-bars");
    if (bars) el.style.height = (Number(bars) * 30 + 16) + "px";
    var chart = echarts.init(el, null, { renderer: "svg" });
    chart.setOption(KINDS[spec.kind](spec, palette()));
    specs.set(el, spec);
    if (spec.kind === "line" || spec.kind === "area") zoomButtons(el, chart, spec);
  }

  // 1Y / 3Y / 5Y / All above a time chart (V1-80, after Fundoo's range buttons):
  // they move the zoom the slider already offers, and change no figure. Only
  // where the panel has no period tabs -- the public copy, which has no server
  // to fetch a period from -- and made here, so without scripts none appear.
  var ZOOMS = [["1Y", 1], ["3Y", 3], ["5Y", 5], ["All", 0]];

  function zoomButtons(el, chart, spec) {
    var panel = el.closest("section.view");
    if (panel && panel.querySelector(".tabs")) return;
    var existing = el.previousElementSibling;
    if (existing && existing.classList.contains("zoom")) {
      // Redrawn (a theme switch starts every chart at its full range): put
      // back the period the reader chose, so the pressed button is still true.
      var pressed = existing.querySelector('button[aria-pressed="true"]');
      if (pressed) pressed.click();
      return;
    }
    var last = null;
    spec.series.forEach(function (s) {
      s.points.forEach(function (pt) { if (!last || pt[0] > last) last = pt[0]; });
    });
    if (!last) return;
    var group = document.createElement("div");
    group.className = "zoom";
    group.setAttribute("role", "group");
    group.setAttribute("aria-label", "Show a period");
    ZOOMS.forEach(function (z) {
      var button = document.createElement("button");
      button.type = "button";
      button.textContent = z[0];
      button.setAttribute("aria-pressed", z[1] === 0 ? "true" : "false");
      button.addEventListener("click", function () {
        var end = new Date(last);
        var start = new Date(last);
        start.setUTCFullYear(end.getUTCFullYear() - z[1]);
        var live = echarts.getInstanceByDom(el) || chart;
        if (z[1] === 0) live.dispatchAction({ type: "dataZoom", start: 0, end: 100 });
        else live.dispatchAction({ type: "dataZoom", startValue: start.getTime(), endValue: end.getTime() });
        group.querySelectorAll("button").forEach(function (b) {
          b.setAttribute("aria-pressed", b === button ? "true" : "false");
        });
      });
      group.appendChild(button);
    });
    el.parentNode.insertBefore(group, el);
  }

  function init(root) {
    root.querySelectorAll('[data-chart="echart"]').forEach(function (box) {
      var script = box.querySelector("script.echart-data");
      if (!script) return;
      var payload = JSON.parse(script.textContent);
      box.querySelectorAll(".echart__canvas").forEach(function (el) {
        var spec = payload.charts[Number(el.getAttribute("data-index"))];
        if (spec && KINDS[spec.kind]) draw(el, spec);
      });
    });
  }

  function redrawAll() {
    document.querySelectorAll(".echart__canvas").forEach(function (el) {
      var spec = specs.get(el);
      if (spec) draw(el, spec);
    });
  }

  var resize = new ResizeObserver(function (entries) {
    entries.forEach(function (entry) {
      var chart = echarts.getInstanceByDom(entry.target);
      if (chart) chart.resize();
    });
  });

  function watch(root) {
    root.querySelectorAll(".echart__canvas").forEach(function (el) { resize.observe(el); });
  }

  // A period tab: swap this panel for the server's rendering of that period.
  document.addEventListener("click", function (e) {
    var link = e.target.closest ? e.target.closest("a[data-fragment]") : null;
    if (!link || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey) return;
    var panel = link.closest("section.view");
    if (!panel) return;
    e.preventDefault();
    panel.setAttribute("aria-busy", "true");
    fetch(link.getAttribute("data-fragment"), { headers: { Accept: "text/html" } })
      .then(function (r) {
        if (!r.ok) throw new Error(String(r.status));
        return r.text();
      })
      .then(function (html) {
        var fresh = new DOMParser().parseFromString(html, "text/html")
          .querySelector("section.view");
        if (!fresh) throw new Error("no panel in the response");
        panel.querySelectorAll(".echart__canvas").forEach(function (el) {
          var chart = echarts.getInstanceByDom(el);
          if (chart) chart.dispose();
        });
        panel.replaceWith(fresh);
        init(fresh);
        watch(fresh);
        history.replaceState(null, "", link.getAttribute("href"));
        var current = fresh.querySelector('.tab[aria-current="true"]');
        if (current) current.focus();
      })
      .catch(function () { window.location.assign(link.href); });
  });

  // The theme follows the system unless the reader chose one; either change
  // redraws every chart in the new colours.
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", redrawAll);
  new MutationObserver(redrawAll).observe(document.documentElement, {
    attributes: true, attributeFilter: ["data-theme"],
  });
  init(document);
  watch(document);
})();
