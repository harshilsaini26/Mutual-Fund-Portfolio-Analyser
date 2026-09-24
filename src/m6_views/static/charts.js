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
      fund: token("--series-fund"), bench: token("--series-bench"),
      ink: token("--ink"), soft: token("--ink-soft"), rule: token("--rule"),
      bg: token("--bg"), others: token("--synthetic"), cats: cats,
    };
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
        renderMode: "richText", backgroundColor: p.bg, borderColor: p.rule,
        textStyle: { color: p.ink },
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
        axisLabel: { color: p.soft, hideOverlap: true },
        splitLine: { show: false },
      };
      o.yAxis = {
        type: "value",
        max: c.kind === "area" ? 0 : null,
        axisLabel: { color: p.soft, formatter: ticks(c.y) },
        splitLine: { lineStyle: { color: p.rule, type: "dashed" } },
      };
      o.dataZoom = [
        // The wheel scrolls the page; Ctrl+wheel, a pinch or the slider zooms.
        // A chart that swallows the wheel traps a reader scrolling past it.
        { type: "inside", zoomOnMouseWheel: "ctrl", moveOnMouseWheel: false,
          preventDefaultMouseMove: false },
        { type: "slider", height: 18, bottom: 6, borderColor: p.rule,
          textStyle: { color: p.soft }, labelFormatter: function (v) { return day(v); } },
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
        if (filled) series.areaStyle = { opacity: fund ? 0.2 : 0.06, color: colour(s.role, p) };
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
      o.tooltip.trigger = "axis";
      o.tooltip.formatter = function (items) {
        return [items[0].name].concat(items.map(function (it) {
          return it.seriesName + ": " + it.data.caption;
        })).join("\n");
      };
      o.xAxis = { type: "category", data: c.categories,
                  axisLabel: { color: p.ink }, axisLine: { lineStyle: { color: p.rule } } };
      o.yAxis = { type: "value", axisLabel: { color: p.soft, formatter: ticks(c.y) },
                  splitLine: { lineStyle: { color: p.rule, type: "dashed" } } };
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
                             color: "rgba(255,255,255,0.5)" } : null,
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
        label: { show: true, color: "#fff", overflow: "truncate", fontSize: 11,
                 formatter: function (d) { return d.name + "\n" + d.data.caption; } },
        itemStyle: { borderColor: p.bg, borderWidth: 1, gapWidth: 1 },
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
        itemStyle: { color: p.fund, borderRadius: [0, 3, 3, 0] },
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

  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", redrawAll);
  init(document);
  watch(document);
})();
