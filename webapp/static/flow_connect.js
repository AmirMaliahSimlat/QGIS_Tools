/**
 * Order-stage port wiring: drag from an output dot to a legal input,
 * emit NiceGUI event "flow-connect".
 */
(function () {
  const NS = "http://www.w3.org/2000/svg";

  function ensureMounted(root) {
    let svg = root.querySelector(":scope > .qt-flow-svg");
    if (!svg) {
      svg = document.createElementNS(NS, "svg");
      svg.classList.add("qt-flow-svg");
      svg.setAttribute("aria-hidden", "true");
      const defs = document.createElementNS(NS, "defs");
      defs.innerHTML =
        '<marker id="qt-flow-arrow" viewBox="0 0 10 10" refX="9" refY="5" ' +
        'markerWidth="7" markerHeight="7" orient="auto-start-reverse">' +
        '<path d="M 0 0 L 10 5 L 0 10 z" fill="#2dd4bf"></path></marker>' +
        '<marker id="qt-flow-arrow-drag" viewBox="0 0 10 10" refX="9" refY="5" ' +
        'markerWidth="7" markerHeight="7" orient="auto-start-reverse">' +
        '<path d="M 0 0 L 10 5 L 0 10 z" fill="#38bdf8"></path></marker>';
      svg.appendChild(defs);
      const wires = document.createElementNS(NS, "g");
      wires.classList.add("qt-flow-wires");
      svg.appendChild(wires);
      const drag = document.createElementNS(NS, "path");
      drag.classList.add("qt-flow-drag-line");
      drag.setAttribute("fill", "none");
      drag.setAttribute("stroke", "#38bdf8");
      drag.setAttribute("stroke-width", "2");
      drag.setAttribute("stroke-dasharray", "5 4");
      drag.setAttribute("marker-end", "url(#qt-flow-arrow-drag)");
      drag.style.display = "none";
      svg.appendChild(drag);
      root.insertBefore(svg, root.firstChild);
    }
    return {
      svg,
      wires: svg.querySelector(".qt-flow-wires"),
      drag: svg.querySelector(".qt-flow-drag-line"),
    };
  }

  function localPoint(root, clientX, clientY) {
    const r = root.getBoundingClientRect();
    return {
      x: clientX - r.left + root.scrollLeft,
      y: clientY - r.top + root.scrollTop,
    };
  }

  function dotCenter(root, el) {
    const r = el.getBoundingClientRect();
    const cr = root.getBoundingClientRect();
    return {
      x: r.left + r.width / 2 - cr.left + root.scrollLeft,
      y: r.top + r.height / 2 - cr.top + root.scrollTop,
    };
  }

  function curve(a, b) {
    const dy = Math.max(40, Math.abs(b.y - a.y) * 0.45);
    return (
      "M " +
      a.x +
      " " +
      a.y +
      " C " +
      a.x +
      " " +
      (a.y + dy) +
      ", " +
      b.x +
      " " +
      (b.y - dy) +
      ", " +
      b.x +
      " " +
      b.y
    );
  }

  function findDot(root, tool, param, role) {
    return root.querySelector(
      '.qt-port-dot[data-tool="' +
        CSS.escape(tool) +
        '"][data-param="' +
        CSS.escape(param) +
        '"][data-role="' +
        role +
        '"]'
    );
  }

  function clearHighlights(root) {
    root.querySelectorAll(".qt-port-dot.is-compatible, .qt-port-dot.is-blocked").forEach((el) => {
      el.classList.remove("is-compatible", "is-blocked");
    });
  }

  function parseEdges(root) {
    try {
      return JSON.parse(root.getAttribute("data-edges") || "[]");
    } catch (_e) {
      return [];
    }
  }

  /** True if adding from→to (replacing any wire into to/toParam) would cycle. */
  function wouldCreateCycle(edges, from, to, toParam) {
    if (from === to) return true;
    const succ = {};
    edges.forEach((e) => {
      if (e.to === to && e.to_param === toParam) return;
      if (!succ[e.from]) succ[e.from] = [];
      succ[e.from].push(e.to);
    });
    if (!succ[from]) succ[from] = [];
    succ[from].push(to);
    const seen = {};
    const stack = [to];
    while (stack.length) {
      const u = stack.pop();
      if (u === from) return true;
      if (seen[u]) continue;
      seen[u] = true;
      const next = succ[u] || [];
      for (let i = 0; i < next.length; i++) stack.push(next[i]);
    }
    return false;
  }

  function isLegalTarget(root, fromTool, ftype, target) {
    if (target.dataset.role !== "in") return false;
    if (target.dataset.ftype !== ftype) return false;
    if (target.dataset.tool === fromTool) return false;
    return !wouldCreateCycle(
      parseEdges(root),
      fromTool,
      target.dataset.tool,
      target.dataset.param
    );
  }

  /** Highlight only legal inputs; leave illegal ports untouched. */
  function highlightTargets(root, fromTool, ftype) {
    root.querySelectorAll('.qt-port-dot[data-role="in"]').forEach((el) => {
      if (isLegalTarget(root, fromTool, ftype, el)) {
        el.classList.add("is-compatible");
      }
    });
  }

  function redraw(root) {
    const layer = ensureMounted(root);
    const { svg, wires } = layer;
    svg.setAttribute("width", String(root.scrollWidth || root.clientWidth));
    svg.setAttribute(
      "height",
      String(Math.max(root.scrollHeight, root.clientHeight, 1))
    );
    wires.replaceChildren();
    parseEdges(root).forEach((e) => {
      const a = findDot(root, e.from, e.from_param, "out");
      const b = findDot(root, e.to, e.to_param, "in");
      if (!a || !b) return;
      const path = document.createElementNS(NS, "path");
      path.setAttribute("d", curve(dotCenter(root, a), dotCenter(root, b)));
      path.setAttribute("fill", "none");
      path.setAttribute("stroke", "#2dd4bf");
      path.setAttribute("stroke-width", "2");
      path.setAttribute("marker-end", "url(#qt-flow-arrow)");
      path.classList.add("qt-flow-wire");
      wires.appendChild(path);
    });
    return layer;
  }

  let dragState = null;

  function onMove(ev) {
    if (!dragState) return;
    const { root, layer, origin } = dragState;
    const p = localPoint(root, ev.clientX, ev.clientY);
    layer.drag.setAttribute("d", curve(origin, p));
    layer.drag.style.display = "";
  }

  function finish(ev) {
    if (!dragState) return;
    const src = dragState;
    dragState = null;
    src.layer.drag.style.display = "none";
    clearHighlights(src.root);
    document.removeEventListener("pointermove", onMove);
    document.removeEventListener("pointerup", finish);
    document.removeEventListener("pointercancel", finish);

    const el = document.elementFromPoint(ev.clientX, ev.clientY);
    const target = el && el.closest ? el.closest(".qt-port-dot") : null;
    if (!target || !src.root.contains(target)) return;
    if (!isLegalTarget(src.root, src.tool, src.ftype, target)) return;

    if (typeof emitEvent === "function") {
      emitEvent("flow-connect", {
        from: src.tool,
        from_param: src.param,
        to: target.dataset.tool,
        to_param: target.dataset.param,
      });
    }
  }

  function onPointerDown(ev) {
    const dot = ev.target && ev.target.closest ? ev.target.closest(".qt-port-dot") : null;
    if (!dot) return;
    // Read-only DAG preview (Configure): no drag wiring.
    if (dot.closest(".qt-pipe-dag")) return;
    // Outputs only — never start a wire from an input.
    if (dot.dataset.role !== "out") return;
    const root = dot.closest(".qt-flow-canvas");
    if (!root) return;
    if (ev.button != null && ev.button !== 0) return;
    ev.preventDefault();
    ev.stopPropagation();
    const layer = redraw(root);
    const origin = dotCenter(root, dot);
    dragState = {
      root,
      layer,
      tool: dot.dataset.tool,
      param: dot.dataset.param,
      ftype: dot.dataset.ftype,
      origin,
    };
    highlightTargets(root, dragState.tool, dragState.ftype);
    layer.drag.setAttribute("d", curve(origin, origin));
    layer.drag.style.display = "";
    document.addEventListener("pointermove", onMove);
    document.addEventListener("pointerup", finish);
    document.addEventListener("pointercancel", finish);
  }

  if (!window.__qtFlowDocBound) {
    document.addEventListener("pointerdown", onPointerDown, true);
    window.addEventListener("resize", () => {
      const root = document.querySelector(".qt-flow-canvas");
      if (root) redraw(root);
    });
    window.__qtFlowDocBound = true;
  }

  window.qtFlow = {
    mount(edges) {
      const root = document.querySelector(".qt-flow-canvas");
      if (!root) return;
      if (edges != null) {
        root.setAttribute("data-edges", JSON.stringify(edges));
      }
      if (!root._qtRo && typeof ResizeObserver !== "undefined") {
        root._qtRo = new ResizeObserver(() => redraw(root));
        root._qtRo.observe(root);
      }
      if (!root._qtScrollBound) {
        root.addEventListener("scroll", () => redraw(root), { passive: true });
        root._qtScrollBound = true;
      }
      requestAnimationFrame(() => redraw(root));
    },
  };
})();
