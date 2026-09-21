// chart.js — builds a node/tag model from list data and draws it as a ring or as columns.
// Generic: items have a name, optional image and tags. A tag whose name matches an item
// in the list points at that item (card-to-card edge); other tags are hubs on an outer ring.

const Chart = (() => {
  const norm = s => s.normalize("NFKD").replace(/[\u0300-\u036f]/g, "").toLowerCase()
    .replace(/[-_/]+/g, " ").replace(/[^a-z0-9 ]+/g, "").replace(/\s+/g, " ").trim();
  const nameKeys = n => [norm(n), ...(n.includes("//") ? n.split("//").map(norm) : [])];
  const SLOT = 15, LABEL = 190, COL_W = 200, ROW_H = 19, HEAD_H = 64;

  let svg, root, layers, zoom, cb = {}, model = null, opts = {}, pin = null, hover = null;

  function init(svgEl, callbacks) {
    cb = callbacks;
    svg = d3.select(svgEl);
    root = svg.append("g");
    layers = {
      edges: root.append("g").attr("class", "edges"),
      hubs: root.append("g").attr("class", "hubs"),
      heads: root.append("g").attr("class", "heads"),
      nodes: root.append("g").attr("class", "nodes"),
    };
    zoom = d3.zoom().scaleExtent([0.15, 6]).on("zoom", e => root.attr("transform", e.transform));
    svg.call(zoom).on("dblclick.zoom", null);
    svg.on("click", () => setPin(null));
  }

  // ------------------------------------------------------------------ model
  function build(data, o) {
    const tags = new Map(data.tags.map(t => [t.id, { ...t, members: new Set(), target: null }]));
    const itemTags = new Map(data.items.map(i => [i.id, new Set()]));
    data.links.forEach(([i, t]) => itemTags.get(i)?.add(t));

    // nodes: stacked copies share a node when name, art and tags are identical
    const nodes = new Map();
    for (const it of data.items) {
      const tset = itemTags.get(it.id);
      const key = o.stack ? `${it.name}|${it.print_id}|${[...tset].sort().join(",")}` : `i${it.id}`;
      if (!nodes.has(key)) nodes.set(key, { key, name: it.name, print_id: it.print_id, item: it,
        items: [], tags: new Set([...tset].map(id => tags.get(id))) });
      nodes.get(key).items.push(it);
    }
    const byName = new Map();
    for (const n of nodes.values()) for (const k of nameKeys(n.name)) if (!byName.has(k)) byName.set(k, n);
    for (const n of nodes.values()) for (const t of n.tags) t.members.add(n);
    for (const t of tags.values()) t.target = byName.get(norm(t.name)) || null;

    // groups (ring sectors / columns): Commander, interaction tags, other tags, then leftovers
    const rank = t => t.name === "Commander" ? 0 : t.target ? 1 : 2;
    const tagOrder = [...tags.values()].filter(t => t.members.size)
      .sort((a, b) => rank(a) - rank(b) || a.name.localeCompare(b.name));
    const groups = tagOrder.map(t => ({ id: `t${t.id}`, tag: t, label: t.name, insts: [] }));
    const multi = { id: "multi", label: "Multiple tags", insts: [] }, none = { id: "none", label: "Untagged", insts: [] };
    const gById = new Map(groups.map(g => [g.id, g]));

    const insts = [];
    const sorted = [...nodes.values()].sort((a, b) => a.name.localeCompare(b.name));
    for (const n of sorted) {
      n.insts = [];
      let gs = [...n.tags].filter(t => t.members.size).sort((a, b) => rank(a) - rank(b) || a.name.localeCompare(b.name))
        .map(t => gById.get(`t${t.id}`));
      if (!o.dup) gs = gs.length > 1 ? [multi] : gs;
      if (!gs.length) gs = [none];
      for (const g of gs) {
        const inst = { key: `${n.key}@${g.id}`, node: n, group: g };
        g.insts.push(inst); n.insts.push(inst); insts.push(inst);
      }
    }
    const allGroups = [...groups, multi, none].filter(g => g.insts.length);
    layoutRing(allGroups);
    layoutColumns(allGroups);

    // edges
    const edges = [], hubs = [];
    for (const t of tags.values()) {
      if (!t.members.size) continue;
      if (!t.target) hubs.push(t);
      for (const m of t.members) {
        if (m === t.target) continue;
        const src = m.insts.find(i => i.group.tag === t) || m.insts[0];
        edges.push({ key: `${t.id}:${m.key}`, tag: t, from: m, to: t.target, src });
      }
    }
    for (const e of edges) if (e.to) e.mutual = [...e.to.tags].some(t => t.target === e.from);
    placeHubs(hubs, edges);
    return { nodes, tags, insts, groups: allGroups, edges, hubs, R: model_R };
  }

  let model_R = 200;
  function layoutRing(groups) {
    const gap = groups.length > 1 ? 1 : 0;
    const slots = groups.reduce((s, g) => s + g.insts.length + gap, 0);
    const R = model_R = Math.max(180, (slots * SLOT) / (2 * Math.PI));
    let s = 0;
    for (const g of groups) {
      for (const inst of g.insts) {
        const a = (s++ / slots) * 2 * Math.PI - Math.PI / 2;
        inst.ring = { a, x: R * Math.cos(a), y: R * Math.sin(a) };
      }
      s += gap;
    }
  }

  function layoutColumns(groups) {
    groups.forEach((g, ci) => {
      g.col = { x: ci * COL_W, y: 0 };
      g.insts.forEach((inst, ri) => { inst.cols = { x: ci * COL_W + 14, y: HEAD_H + ri * ROW_H }; });
    });
  }

  function placeHubs(hubs, edges) {
    const HR = model_R + LABEL;
    for (const t of hubs) {
      let sx = 0, sy = 0;
      for (const e of edges) if (e.tag === t) { sx += Math.cos(e.src.ring.a); sy += Math.sin(e.src.ring.a); }
      t.a = Math.atan2(sy, sx);
    }
    // nudge hubs apart so labels don't overlap
    const minGap = 22 / HR;
    hubs.sort((a, b) => a.a - b.a);
    for (let i = 1; i < hubs.length; i++) if (hubs[i].a - hubs[i - 1].a < minGap) hubs[i].a = hubs[i - 1].a + minGap;
    for (const t of hubs) t.hub = { x: HR * Math.cos(t.a), y: HR * Math.sin(t.a) };
  }

  // ------------------------------------------------------------------ render
  const deg = a => (a * 180) / Math.PI;
  const left = a => Math.cos(a) < 0;
  const nodeTf = i => opts.mode === "circle"
    ? `translate(${i.ring.x},${i.ring.y}) rotate(${deg(i.ring.a)})` : `translate(${i.cols.x},${i.cols.y}) rotate(0)`;
  const label = n => (n.name.length > 28 ? n.name.slice(0, 27) + "…" : n.name) + (n.items.length > 1 ? `  ×${n.items.length}` : "");
  const edgePath = e => {
    const a = e.src.ring;
    if (!e.to) return `M${a.x},${a.y}L${e.tag.hub.x},${e.tag.hub.y}`;
    const b = e.to.insts[0].ring;
    return `M${a.x},${a.y}Q${(a.x + b.x) * 0.15},${(a.y + b.y) * 0.15} ${b.x},${b.y}`;
  };

  function render(data, o, { refit = false } = {}) {
    opts = o;
    model = build(data, o);
    const t = svg.transition().duration(reduced() ? 0 : 650);
    svg.classed("mode-cols", o.mode !== "circle");

    layers.edges.selectAll("path").data(model.edges, e => e.key).join(
      en => en.append("path").attr("class", "edge").attr("opacity", 0).attr("d", edgePath),
      up => up, ex => ex.transition(t).attr("opacity", 0).remove())
      .classed("card", e => !!e.to).classed("mutual", e => !!e.mutual)
      .transition(t).attr("opacity", 1).attr("d", edgePath);

    const hubs = layers.hubs.selectAll("g.hub").data(model.hubs, h => h.id).join(en => {
      const g = en.append("g").attr("class", "hub").attr("opacity", 0);
      g.append("circle").attr("r", 5);
      g.append("text").attr("dy", "0.32em");
      return g;
    }, up => up, ex => ex.transition(t).attr("opacity", 0).remove());
    hubs.select("text").text(h => `#${h.name}`)
      .attr("x", h => (left(h.a) ? -9 : 9)).attr("text-anchor", h => (left(h.a) ? "end" : "start"));
    hubs.on("mouseenter", (ev, h) => setHover({ type: "tag", tag: h })).on("mouseleave", () => setHover(null))
      .on("click", (ev, h) => { ev.stopPropagation(); togglePin({ type: "tag", id: h.id }); })
      .transition(t).attr("opacity", 1).attr("transform", h => `translate(${h.hub.x},${h.hub.y})`);

    const heads = layers.heads.selectAll("g.head").data(model.groups, g => g.id).join(en => {
      const g = en.append("g").attr("class", "head").attr("opacity", 0);
      g.append("image").attr("width", 36).attr("height", 50).attr("y", -4);
      g.append("text").attr("class", "head-name").attr("y", 18);
      g.append("text").attr("class", "head-count").attr("y", 36);
      return g;
    }, up => up, ex => ex.remove());
    heads.select("image").attr("href", g => g.tag?.target?.print_id ? `/images/${g.tag.target.print_id}.jpg` : null)
      .attr("display", g => (g.tag?.target?.print_id ? null : "none"));
    heads.select(".head-name").attr("x", g => (g.tag?.target?.print_id ? 44 : 0))
      .text(g => (g.tag ? (g.tag.target ? "→ " : "#") : "") + (g.label.length > 20 ? g.label.slice(0, 19) + "…" : g.label));
    heads.select(".head-count").attr("x", g => (g.tag?.target?.print_id ? 44 : 0)).text(g => `${g.insts.length}`);
    heads.on("mouseenter", (ev, g) => g.tag && setHover({ type: "tag", tag: g.tag })).on("mouseleave", () => setHover(null))
      .on("click", (ev, g) => { ev.stopPropagation(); if (g.tag) togglePin({ type: "tag", id: g.tag.id }); })
      .transition(t).attr("opacity", 1).attr("transform", g => `translate(${g.col.x},${g.col.y})`);

    const nodes = layers.nodes.selectAll("g.node").data(model.insts, i => i.key).join(en => {
      const g = en.append("g").attr("class", "node").attr("opacity", 0).attr("transform", nodeTf);
      g.append("circle").attr("class", "dot").attr("r", 4);
      g.append("text").attr("class", "name").attr("dy", "0.32em");
      g.append("text").attr("class", "tagbtn").attr("dy", "0.32em").attr("text-anchor", "middle").text("#")
        .append("title").text("Tags");
      g.append("rect").attr("class", "quick").attr("width", 10).attr("height", 10).attr("y", -5).attr("rx", 2);
      return g;
    }, up => up, ex => ex.transition(t).attr("opacity", 0).remove());
    const flip = i => opts.mode === "circle" && left(i.ring.a);
    nodes.select(".name").text(i => label(i.node))
      .attr("text-anchor", i => (flip(i) ? "end" : "start"))
      .attr("transform", i => (flip(i) ? "rotate(180)" : null)).attr("x", i => (flip(i) ? -9 : 9));
    nodes.select(".dot").attr("r", i => 3.5 + Math.min(3, i.node.items.length - 1));
    nodes.select(".tagbtn").attr("x", -14).attr("transform", i => (flip(i) ? "rotate(180 -14 0)" : null));
    nodes.select(".quick").attr("x", -34)
      .classed("on", i => o.quickTag != null && [...i.node.tags].some(t => t.id === o.quickTag))
      .attr("display", o.quickTag != null ? null : "none");
    nodes.on("mouseenter", (ev, i) => { setHover({ type: "node", node: i.node }); cb.onHoverImage?.(i.node, ev); })
      .on("mousemove", (ev, i) => cb.onHoverImage?.(i.node, ev))
      .on("mouseleave", () => { setHover(null); cb.onHoverImage?.(null); })
      .on("click", (ev, i) => { ev.stopPropagation(); togglePin({ type: "node", itemId: i.node.items[0].id }); });
    nodes.select(".tagbtn").on("click", (ev, i) => { ev.stopPropagation(); cb.onTagMenu?.(i.node, ev); });
    nodes.select(".quick").on("click", (ev, i) => { ev.stopPropagation(); cb.onQuickTag?.(i.node); });
    nodes.transition(t).attr("opacity", 1).attr("transform", nodeTf);

    applyFocus();
    if (refit) fit(t);
    cb.onPin?.(resolve(pin), model);
  }

  function fit(t) {
    const box = svg.node().getBoundingClientRect();
    let x0, y0, x1, y1;
    if (opts.mode === "circle") { const r = model.R + LABEL + 60; [x0, y0, x1, y1] = [-r, -r, r, r]; }
    else {
      const rows = Math.max(...model.groups.map(g => g.insts.length), 1);
      [x0, y0, x1, y1] = [-40, -30, model.groups.length * COL_W, HEAD_H + rows * ROW_H + 20];
    }
    let k = Math.min(box.width / (x1 - x0), box.height / (y1 - y0), 2);
    if (opts.mode !== "circle") k = Math.max(k, 0.85); // columns stay readable; pan to see more
    const tx = opts.mode === "circle" ? box.width / 2 : 20 - x0 * k, ty = opts.mode === "circle" ? box.height / 2 : 20 - y0 * k;
    svg.transition(t).call(zoom.transform, d3.zoomIdentity.translate(tx, ty).scale(k));
  }

  // ------------------------------------------------------------------ focus
  function resolve(f) {
    if (!f || !model) return null;
    if (f.type === "tag") { const tag = model.tags.get(f.id); return tag ? { type: "tag", tag } : null; }
    if (f.type === "node") {
      if (f.node) return f;
      for (const n of model.nodes.values()) if (n.items.some(i => i.id === f.itemId)) return { type: "node", node: n };
    }
    return null;
  }

  function focusSets(f) {
    const nodes = new Set(), tags = new Set(), edges = new Set();
    if (f.type === "node") {
      const n = f.node;
      nodes.add(n);
      n.tags.forEach(t => tags.add(t));
      for (const e of model.edges) if (e.from === n || e.to === n) { edges.add(e); nodes.add(e.from); if (e.to) nodes.add(e.to); tags.add(e.tag); }
    } else {
      const t = f.tag;
      tags.add(t);
      t.members.forEach(m => nodes.add(m));
      if (t.target) nodes.add(t.target);
      for (const e of model.edges) if (e.tag === t) edges.add(e);
    }
    return { nodes, tags, edges };
  }

  function applyFocus() {
    const f = resolve(hover) || resolve(pin);
    svg.classed("focus", !!f);
    if (!f) { root.selectAll(".hl, .self").classed("hl", false).classed("self", false); return; }
    const s = focusSets(f);
    layers.nodes.selectAll("g.node").classed("hl", i => s.nodes.has(i.node))
      .classed("self", i => f.type === "node" && i.node === f.node);
    layers.edges.selectAll("path").classed("hl", e => s.edges.has(e));
    layers.hubs.selectAll("g.hub").classed("hl", h => s.tags.has(h));
    layers.heads.selectAll("g.head").classed("hl", g => !g.tag || s.tags.has(g.tag));
  }

  function setHover(f) { hover = f; applyFocus(); }
  function setPin(p) { pin = p; applyFocus(); cb.onPin?.(resolve(pin), model); }
  function togglePin(p) {
    const cur = resolve(pin), next = resolve(p);
    const same = cur && next && cur.type === next.type && (cur.tag ? cur.tag === next.tag : cur.node === next.node);
    setPin(same ? null : p);
  }
  const reduced = () => window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  return { init, render, setPin, getModel: () => model, norm, nameKeys };
})();
