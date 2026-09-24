/*
 * The Sankey. MODULE_6.md §16.2 — d3-sankey, "no good React wrapper; use D3
 * directly in a ref'd SVG". There is no React here either, so it is D3 directly
 * in an SVG, which is what the recommendation amounted to.
 *
 * §16.4: the frontend does not compute. This file parses the string Decimals
 * the payload carries, and it does so at exactly one point — where d3-sankey
 * needs a number to produce a pixel width. A coordinate is not a figure: getting
 * one wrong makes the chart look wrong, not the number wrong. Every figure the
 * reader sees was formatted in Python and is already in the DOM, in the
 * accessible table beneath this diagram.
 *
 * Two requirements from Appendix A are enforced upstream, in the builder, and
 * only rendered here: the node cap with its `__OTHERS__` bucket, and the
 * synthetics that must stay visible. This file must never drop a node.
 */
(function () {
  "use strict";

  var script = document.getElementById("sankey-data");
  var svg = document.getElementById("sankey");
  if (!script || !svg || !window.d3 || !window.d3.sankey) return;

  var payload = JSON.parse(script.textContent);
  var nodes = payload.nodes.map(function (n) { return Object.assign({}, n); });
  var links = payload.links.map(function (l) {
    return { source: l.source, target: l.target, value: Number(l.value) };
  });

  // A link whose endpoint was aggregated away would throw inside the layout and
  // take the page down with it. The builder does not produce those, so this is
  // a guard against a future change rather than a known case — and dropping the
  // link is the wrong repair, so it is reported instead.
  var known = {};
  nodes.forEach(function (n) { known[n.id] = true; });
  var orphans = links.filter(function (l) {
    return !known[l.source] || !known[l.target];
  });
  if (orphans.length) {
    svg.insertAdjacentHTML(
      "afterend",
      '<p class="chart-error">This diagram could not be drawn: ' +
        orphans.length +
        " flow(s) point at a company that is not in the node list. The table" +
        " below is unaffected.</p>"
    );
    return;
  }

  var width = Math.max(svg.clientWidth || svg.parentNode.clientWidth || 900, 640);
  var height = Math.max(220, Math.min(nodes.length * 22, 1400));
  svg.setAttribute("viewBox", "0 0 " + width + " " + height);
  svg.setAttribute("height", height);

  var layout = d3
    .sankey()
    .nodeId(function (d) { return d.id; })
    .nodeWidth(14)
    .nodePadding(8)
    .extent([[1, 6], [width - 1, height - 6]]);

  var graph;
  try {
    graph = layout({ nodes: nodes, links: links });
  } catch (err) {
    svg.insertAdjacentHTML(
      "afterend",
      '<p class="chart-error">This diagram could not be laid out (' +
        err.name +
        "). The same flows are in the table below.</p>"
    );
    return;
  }

  var root = d3.select(svg);

  root
    .append("g")
    .attr("class", "links")
    .selectAll("path")
    .data(graph.links)
    .join("path")
    .attr("class", function (d) {
      // Synthetic destinations keep their own class so the flow INTO missing
      // mass is as visible as the node itself. §10.2.
      return "link" + (d.target.kind === "synthetic" ? " link--synthetic" : "");
    })
    .attr("d", d3.sankeyLinkHorizontal())
    .attr("stroke-width", function (d) { return Math.max(1, d.width); })
    .append("title")
    .text(function (d) {
      return d.source.label + " to " + d.target.label;
    });

  var node = root
    .append("g")
    .attr("class", "nodes")
    .selectAll("g")
    .data(graph.nodes)
    .join("g")
    .attr("class", function (d) { return "node node--" + d.kind + " node--" + d.side; });

  node
    .append("rect")
    .attr("x", function (d) { return d.x0; })
    .attr("y", function (d) { return d.y0; })
    .attr("height", function (d) { return Math.max(1, d.y1 - d.y0); })
    .attr("width", function (d) { return d.x1 - d.x0; })
    // Fills come from the stylesheet (`.node--left`, `.node--right`,
    // `.node--aggregate`, `.node--synthetic`), so they follow the theme. The
    // hatch on synthetics is the redundant channel §10.3 requires:
    // __UNRESOLVED__ must not be told from a company by colour alone.
    .append("title")
    .text(function (d) { return d.label; });

  node
    .append("text")
    .attr("x", function (d) { return d.x0 < width / 2 ? d.x1 + 6 : d.x0 - 6; })
    .attr("y", function (d) { return (d.y1 + d.y0) / 2; })
    .attr("dy", "0.35em")
    .attr("text-anchor", function (d) { return d.x0 < width / 2 ? "start" : "end"; })
    .text(function (d) { return d.label; });

  // The hatch the synthetics are filled with.
  root
    .insert("defs", ":first-child")
    .html(
      '<pattern id="hatch" width="6" height="6" patternUnits="userSpaceOnUse"' +
        ' patternTransform="rotate(45)">' +
        '<rect class="hatch__ground" width="6" height="6"></rect>' +
        '<line class="hatch__stripe" x1="0" y1="0" x2="0" y2="6"></line>' +
        "</pattern>"
    );
})();
