"""Node-eval DOM shims shared by the dashboard tests.

Each is a snippet of JS run before static/app.js loads, stubbing just enough
of document/window/fetch for app.js's top-level wiring to not throw
under Node. Files needing a different or extended shim keep their own.
"""

# Used by test_dashboard_planlist_diff_dataset and test_dashboard_planlist_nav_order.
PLANLIST_SHIM = r"""
    const noop = () => {};
    let __testIdCounter = 0;

    // Convert a `data-foo-bar` attribute name to its dataset key: `fooBar`.
    // Mirrors the HTML spec's dataset camelCase algorithm: lowercase the
    // leading char after each hyphen, drop the hyphens.
    function __datasetKey(attr) {
        // attr is the part after "data-", e.g. "plan-name" -> "planName".
        return attr.replace(/-([a-z])/g, (_, c) => c.toUpperCase());
    }

    // Parse a tiny subset of HTML (open/close tag pairs and text) into child
    // element nodes. Each parsed element gets its own __testId so it is
    // distinguishable, and supports class-based querySelector. Attributes we
    // care about (class, title, type) are captured.
    function __parseHtml(html, parent) {
        parent.__children = [];
        if (!html) return;
        const re = /<([a-zA-Z]+)((?:\s+[a-zA-Z-]+(?:="[^"]*")?)*)\s*>([\s\S]*?)<\/\1>|<([a-zA-Z]+)((?:\s+[a-zA-Z-]+(?:="[^"]*")?)*)\s*\/?>/g;
        let m;
        while ((m = re.exec(html)) !== null) {
            const tag = m[1] || m[4];
            const attrs = m[2] || m[5] || "";
            const inner = m[3] || "";
            const el = __newEl(tag);
            const are = /([a-zA-Z-]+)="([^"]*)"/g;
            let am;
            while ((am = are.exec(attrs)) !== null) {
                if (am[1] === "class") el.className = am[2];
                else if (am[1] === "title") el.title = am[2];
                else if (am[1] === "type") el.type = am[2];
                else el.setAttribute(am[1], am[2]);
            }
            if (inner) {
                __parseHtml(inner, el);
                el.__text = inner.replace(/<[^>]*>/g, "");
            }
            parent.__children.push(el);
            el.__parent = parent;
        }
    }

    function __newEl(tag) {
        const el = {
            __testId: ++__testIdCounter,
            __tag: tag || "div",
            __children: [],
            __parent: null,
            __text: "",
            __attrs: {},
            tagName: (tag || "div").toUpperCase(),
            className: "",
            title: "",
            type: "",
            checked: false,
            dataset: {},
            classList: {
                _classes: () => (el.className || "").split(/\s+/).filter(Boolean),
                add: (...c) => { const s = new Set(el.classList._classes()); c.forEach(x => s.add(x)); el.className = [...s].join(" "); },
                remove: (...c) => { const s = new Set(el.classList._classes()); c.forEach(x => s.delete(x)); el.className = [...s].join(" "); },
                toggle: (c, f) => { const s = new Set(el.classList._classes()); if (f === undefined) f = !s.has(c); if (f) s.add(c); else s.delete(c); el.className = [...s].join(" "); },
                contains: (c) => el.classList._classes().includes(c),
            },
            addEventListener: noop,
            removeEventListener: noop,
            setAttribute: (k, v) => {
                el.__attrs[k] = String(v);
                if (k === "class") el.className = String(v);
                else if (k === "title") el.title = String(v);
                else if (k === "type") el.type = String(v);
                else if (k.startsWith("data-")) el.dataset[__datasetKey(k.slice(5))] = String(v);
                else el[k] = String(v);
            },
            getAttribute: (k) => {
                if (k === "class") return el.className;
                if (k.startsWith("data-")) return el.dataset[__datasetKey(k.slice(5))];
                return el.__attrs[k] != null ? el.__attrs[k] : (el[k] != null ? String(el[k]) : null);
            },
            hasAttribute: (k) => {
                if (k === "class") return el.className !== "";
                if (k.startsWith("data-")) return el.dataset[__datasetKey(k.slice(5))] != null;
                return el.__attrs[k] != null;
            },
            appendChild: (child) => {
                if (child.__parent) {
                    const i = child.__parent.__children.indexOf(child);
                    if (i >= 0) child.__parent.__children.splice(i, 1);
                }
                el.__children.push(child);
                child.__parent = el;
                return child;
            },
            insertBefore: (child, ref) => {
                if (child.__parent) {
                    const i = child.__parent.__children.indexOf(child);
                    if (i >= 0) child.__parent.__children.splice(i, 1);
                }
                if (ref == null) { el.__children.push(child); }
                else {
                    const idx = el.__children.indexOf(ref);
                    if (idx < 0) el.__children.push(child);
                    else el.__children.splice(idx, 0, child);
                }
                child.__parent = el;
                return child;
            },
            removeChild: (child) => {
                const i = el.__children.indexOf(child);
                if (i >= 0) el.__children.splice(i, 1);
                child.__parent = null;
                return child;
            },
            remove: () => {
                if (el.__parent) el.__parent.removeChild(el);
            },
            querySelector: (sel) => el.__queryAll(sel)[0] || null,
            querySelectorAll: (sel) => el.__queryAll(sel),
            __queryAll: (sel) => {
                const out = [];
                const walk = (node) => {
                    for (const c of node.__children) {
                        if (__matches(c, sel)) out.push(c);
                        walk(c);
                    }
                };
                walk(el);
                return out;
            },
        };
        Object.defineProperty(el, "innerHTML", {
            get: () => el.__innerHTMLRaw || "",
            set: (v) => { el.__innerHTMLRaw = String(v); __parseHtml(String(v), el); },
            configurable: true,
        });
        Object.defineProperty(el, "textContent", {
            get: () => el.__text || "",
            set: (v) => { el.__text = String(v); el.__children = []; el.__innerHTMLRaw = String(v); },
            configurable: true,
        });
        return el;
    }

    function __matches(el, sel) {
        if (!sel) return false;
        if (sel.startsWith(".")) {
            return el.classList.contains(sel.slice(1));
        }
        if (sel.startsWith("[data-")) {
            const key = sel.slice(6, sel.indexOf("=") > 0 ? sel.indexOf("=") - 6 : sel.indexOf("]"));
            const dkey = __datasetKey(key);
            const val = sel.indexOf("=") > 0 ? sel.slice(sel.indexOf("=") + 2, sel.indexOf("]")) : null;
            if (val != null) return el.dataset[dkey] === val;
            return el.dataset[dkey] != null;
        }
        return el.__tag === sel;
    }

    const __nav = __newEl("div");
    globalThis.document = {
        addEventListener: noop,
        documentElement: { dataset: {} },
        getElementById: (id) => {
            if (id === "plan-list") return __nav;
            return __newEl("div");
        },
        createElement: (tag) => __newEl(tag),
    };
    globalThis.window = {
        location: { hash: "" },
        addEventListener: noop,
    };
    globalThis.localStorage = { getItem: () => null, setItem: noop };
    globalThis.fetch = () => new Promise(() => {});
    process.on("unhandledRejection", () => {});
    globalThis.setInterval = () => 0;
    globalThis.setTimeout = (fn, _ms) => { if (typeof fn === "function") { /* dropped */ } return 0; };
"""

# Used by test_dashboard_config_dropdowns, _notification_filter,
# _role_model_dropdown and _toast_dedup_key.
DROPDOWN_SHIM = r"""
        const noop = () => {};
        const fakeEl = {
            innerHTML: "",
            classList: { add: noop, remove: noop, toggle: noop, contains: () => false },
            addEventListener: noop,
            setAttribute: noop,
            appendChild: noop,
            querySelectorAll: () => [],
            dataset: {},
        };
        globalThis.document = {
            addEventListener: noop,
            documentElement: { dataset: {} },
            getElementById: () => ({ ...fakeEl, dataset: {}, addEventListener: noop }),
            createElement: () => ({ ...fakeEl, classList: { add: noop, remove: noop, contains: () => false } }),
        };
        globalThis.window = {
            location: { hash: "" },
            addEventListener: noop,
        };
        globalThis.localStorage = { getItem: () => null, setItem: noop };
        globalThis.fetch = () => new Promise(() => {});
        process.on("unhandledRejection", () => {});
        globalThis.setInterval = () => 0;
        globalThis.setTimeout = (fn, _ms) => { if (typeof fn === "function") { /* dropped */ } return 0; };
"""


# --- Near-identical shim pairs -------------------------------------------------
# Each pair differs by at most two lines of code. The shared template carries a
# marker comment where the extra lines go; the file that needs them gets the
# lines substituted in, the other gets the marker line dropped.


def _with_extra(template: str, marker: str, extra: str) -> str:
    """Replace the single ``// <marker>`` comment line in *template* with *extra*."""
    lines = template.split("\n")
    index = next(i for i, line in enumerate(lines) if line.strip() == f"// {marker}")
    return "\n".join(lines[:index] + ([extra] if extra else []) + lines[index + 1 :])


# Used by test_dashboard_toast_behavior and test_dashboard_toast_stripe_and_key.
TOAST_SHIM = r"""
    const noop = () => {};
    let __testIdCounter = 0;

    function __parseHtml(html, parent) {
        parent.__children = [];
        if (!html) return;
        const tokenRe = /<(\/?)([a-zA-Z]+)((?:\s+[a-zA-Z-]+(?:="[^"]*")?)*)\s*(\/?)>/g;
        const are = /([a-zA-Z-]+)="([^"]*)"/g;
        const stack = [parent];
        let lastIndex = 0;
        let m;
        while ((m = tokenRe.exec(html)) !== null) {
            if (m.index > lastIndex) {
                const top = stack[stack.length - 1];
                top.__ownText = (top.__ownText || "") + html.slice(lastIndex, m.index);
            }
            lastIndex = tokenRe.lastIndex;
            const isClose = m[1] === "/";
            const tag = m[2];
            if (isClose) {
                for (let i = stack.length - 1; i >= 1; i--) {
                    if (stack[i].__tag === tag) { stack.length = i; break; }
                }
                continue;
            }
            const selfClosing = m[4] === "/";
            const el = __newEl(tag);
            are.lastIndex = 0;
            let am;
            while ((am = are.exec(m[3] || "")) !== null) {
                if (am[1] === "class") el.className = am[2];
                else if (am[1] === "title") el.title = am[2];
                else if (am[1] === "type") el.type = am[2];
                else if (am[1].startsWith("aria-")) el.setAttribute(am[1], am[2]);
                else el.setAttribute(am[1], am[2]);
            }
            const top = stack[stack.length - 1];
            top.__children.push(el);
            el.__parent = top;
            if (!selfClosing) stack.push(el);
        }
        if (lastIndex < html.length) {
            const top = stack[stack.length - 1];
            top.__ownText = (top.__ownText || "") + html.slice(lastIndex);
        }
        const fillText = (node) => {
            let text = node.__ownText || "";
            for (const child of node.__children) {
                fillText(child);
                text += child.__text;
            }
            node.__text = text;
        };
        fillText(parent);
    }

    function __newEl(tag) {
        const el = {
            __testId: ++__testIdCounter,
            __tag: tag || "div",
            __children: [],
            __parent: null,
            __text: "",
            __listeners: {},
            tagName: (tag || "div").toUpperCase(),
            innerHTML: "",
            textContent: "",
            className: "",
            title: "",
            type: "",
            value: "",
            checked: false,
            style: {},
            dataset: {},
            classList: {
                _classes: () => (el.className || "").split(/\s+/).filter(Boolean),
                add: (...c) => { const s = new Set(el.classList._classes()); c.forEach(x => s.add(x)); el.className = [...s].join(" "); },
                remove: (...c) => { const s = new Set(el.classList._classes()); c.forEach(x => s.delete(x)); el.className = [...s].join(" "); },
                toggle: (c, f) => { const s = new Set(el.classList._classes()); if (f === undefined) f = !s.has(c); if (f) s.add(c); else s.delete(c); el.className = [...s].join(" "); },
                contains: (c) => el.classList._classes().includes(c),
            },
            addEventListener: (type, fn) => {
                if (!el.__listeners[type]) el.__listeners[type] = [];
                el.__listeners[type].push(fn);
            },
            removeEventListener: noop,
            __fire: (type, evt) => {
                for (const fn of (el.__listeners[type] || [])) fn(evt || {});
            },
            setAttribute: (k, v) => {
                if (k === "class") el.className = String(v);
                else if (k === "title") el.title = String(v);
                else if (k === "type") el.type = String(v);
                else if (k === "style") { /* store raw style string; ignore */ }
                else if (k.startsWith("data-")) el.dataset[k.slice(5)] = String(v);
                else el[k] = String(v);
            },
            getAttribute: (k) => {
                if (k === "class") return el.className;
                if (k === "style") return el.__styleStr || "";
                if (k.startsWith("data-")) return el.dataset[k.slice(5)];
                return el[k] != null ? String(el[k]) : null;
            },
            appendChild: (child) => {
                if (child.__parent) {
                    const i = child.__parent.__children.indexOf(child);
                    if (i >= 0) child.__parent.__children.splice(i, 1);
                }
                el.__children.push(child);
                child.__parent = el;
                return child;
            },
            removeChild: (child) => {
                const i = el.__children.indexOf(child);
                if (i >= 0) el.__children.splice(i, 1);
                child.__parent = null;
                return child;
            },
            remove: () => {
                if (el.__parent) el.__parent.removeChild(el);
            },
            querySelector: (sel) => el.__queryAll(sel)[0] || null,
            querySelectorAll: (sel) => el.__queryAll(sel),
            focus: noop,
            get children() { return el.__children; },
            __queryAll: (sel) => {
                const out = [];
                const walk = (node) => {
                    for (const c of node.__children) {
                        if (__matches(c, sel)) out.push(c);
                        walk(c);
                    }
                };
                walk(el);
                return out;
            },
        };
        Object.defineProperty(el, "innerHTML", {
            get: () => el.__innerHTMLRaw || "",
            set: (v) => {
                el.__innerHTMLRaw = String(v);
                __parseHtml(String(v), el);
            },
            configurable: true,
        });
        Object.defineProperty(el, "textContent", {
            get: () => el.__text || "",
            set: (v) => { el.__text = String(v); el.__children = []; el.__innerHTMLRaw = String(v); },
            configurable: true,
        });
        el.style = {
            __props: {},
            setProperty: (k, v) => { el.style.__props[k] = String(v); },
            getPropertyValue: (k) => (el.style.__props[k] != null ? String(el.style.__props[k]) : ""),
            removeProperty: (k) => { const v = el.style.__props[k]; delete el.style.__props[k]; return v != null ? String(v) : ""; },
        };
        return el;
    }

    function __matches(el, sel) {
        if (!sel) return false;
        if (sel.startsWith(".")) {
            const cls = sel.slice(1);
            return el.classList.contains(cls);
        }
        if (sel.startsWith("[data-")) {
            const key = sel.slice(6, sel.indexOf("=") > 0 ? sel.indexOf("=") - 6 : sel.indexOf("]"));
            const val = sel.indexOf("=") > 0 ? sel.slice(sel.indexOf("=") + 2, sel.indexOf("]")) : null;
            if (val != null) return el.dataset[key] === val;
            return el.dataset[key] != null;
        }
        return el.__tag === sel;
    }

    const __nav = __newEl("div");
    const __detail = __newEl("div");
    const __toastStack = __newEl("div");
    const __commsInput = __newEl("input");
    const __byId = { "plan-list": __nav, "plan-detail": __detail, "toast-stack": __toastStack, "comms-input": __commsInput };
    globalThis.document = {
        addEventListener: noop,
        documentElement: { dataset: {} },
        activeElement: null,
        body: __newEl("body"),
        hidden: false,
        getElementById: (id) => __byId[id] || __newEl("div"),
        createElement: (tag) => __newEl(tag),
        querySelector: (sel) => null,
        querySelectorAll: (sel) => [],
    };
    globalThis.window = {
        location: { hash: "" },
        addEventListener: noop,
    };
    globalThis.localStorage = { getItem: () => null, setItem: noop };
    globalThis.fetch = () => new Promise(() => {});
    process.on("unhandledRejection", () => {});
    globalThis.setInterval = () => 0;
    // Auto-fade timers are captured (not dropped) so tests can trigger them
    // deterministically instead of racing a real 6-second timeout.
    globalThis.__pendingTimers = [];
    globalThis.setTimeout = (fn, ms) => {
        globalThis.__pendingTimers.push({ fn, ms });
        return globalThis.__pendingTimers.length;
    };
    globalThis.__runTimers = () => {
        const timers = globalThis.__pendingTimers;
        globalThis.__pendingTimers = [];
        for (const t of timers) t.fn();
    };
"""

# Used by test_dashboard_board_diff (element innerHTML/textContent initialisers
# present) and test_dashboard_notifications_diff (absent).
_BOARD_DIFF_TEMPLATE = r"""
    const noop = () => {};
    let __testIdCounter = 0;

    // Parse a tiny subset of HTML (open/close tag pairs and text) into child
    // element nodes. Each parsed element gets its own __testId so it is
    // distinguishable, and supports class-based querySelector. Attributes we
    // care about (class, title, type, style, data-*) are captured.
    //
    // Stack-based tokenizer (not a lazy backreference regex): a naive
    // `<(\w+)...>(.*?)<\/\1>` match mis-pairs on same-tag nesting — e.g.
    // `<div class="column"><div class="column-body">...</div></div>` closes
    // the OUTER div at the FIRST `</div>` (the column-body's), silently
    // flattening every deeper descendant into siblings of `column` instead
    // of children of it. renderPlanDetail's real output nests `.column` >
    // `.column-body` > `.card` > several more divs several levels deep, so
    // this matters here even though it didn't when only renderBoard's
    // narrower, previously-tested output was ever parsed. Mirrors the
    // stack-based tokenizer in tests/unit/test_dashboard_notification_filter.py's
    // shim, which solved the identical problem there.
    function __parseHtml(html, parent) {
        parent.__children = [];
        if (!html) return;
        const tokenRe = /<(\/?)([a-zA-Z]+)((?:\s+[a-zA-Z-]+(?:="[^"]*")?)*)\s*(\/?)>/g;
        const are = /([a-zA-Z-]+)="([^"]*)"/g;
        const stack = [parent];
        let lastIndex = 0;
        let m;
        while ((m = tokenRe.exec(html)) !== null) {
            // Text between the previous token and this one belongs to
            // whichever element is currently on top of the stack.
            if (m.index > lastIndex) {
                const top = stack[stack.length - 1];
                top.__ownText = (top.__ownText || "") + html.slice(lastIndex, m.index);
            }
            lastIndex = tokenRe.lastIndex;
            const isClose = m[1] === "/";
            const tag = m[2];
            if (isClose) {
                for (let i = stack.length - 1; i >= 1; i--) {
                    if (stack[i].__tag === tag) { stack.length = i; break; }
                }
                continue;
            }
            const selfClosing = m[4] === "/";
            const el = __newEl(tag);
            are.lastIndex = 0;
            let am;
            while ((am = are.exec(m[3] || "")) !== null) {
                if (am[1] === "class") el.className = am[2];
                else if (am[1] === "title") el.title = am[2];
                else if (am[1] === "type") el.type = am[2];
                else el.setAttribute(am[1], am[2]);
            }
            const top = stack[stack.length - 1];
            top.__children.push(el);
            el.__parent = top;
            if (!selfClosing) stack.push(el);
        }
        if (lastIndex < html.length) {
            const top = stack[stack.length - 1];
            top.__ownText = (top.__ownText || "") + html.slice(lastIndex);
        }
        // Recursively fold each node's descendant text (tags stripped) into
        // __text, matching the field's original semantics — tests read e.g.
        // a `.card-badges` wrapper's __text expecting the concatenated text
        // of the <span> badges nested inside it, not just the whitespace
        // directly between the wrapper's own open/close tags.
        const fillText = (node) => {
            let text = node.__ownText || "";
            for (const child of node.__children) {
                fillText(child);
                text += child.__text;
            }
            node.__text = text;
        };
        fillText(parent);
    }

    function __newEl(tag) {
        const el = {
            __testId: ++__testIdCounter,
            __tag: tag || "div",
            __children: [],
            __parent: null,
            __text: "",
            tagName: (tag || "div").toUpperCase(),
            // @@ELEMENT_PROPS@@
            className: "",
            title: "",
            type: "",
            checked: false,
            style: {},
            dataset: {},
            classList: {
                _classes: () => (el.className || "").split(/\s+/).filter(Boolean),
                add: (...c) => { const s = new Set(el.classList._classes()); c.forEach(x => s.add(x)); el.className = [...s].join(" "); },
                remove: (...c) => { const s = new Set(el.classList._classes()); c.forEach(x => s.delete(x)); el.className = [...s].join(" "); },
                toggle: (c, f) => { const s = new Set(el.classList._classes()); if (f === undefined) f = !s.has(c); if (f) s.add(c); else s.delete(c); el.className = [...s].join(" "); },
                contains: (c) => el.classList._classes().includes(c),
            },
            addEventListener: noop,
            removeEventListener: noop,
            setAttribute: (k, v) => {
                if (k === "class") el.className = String(v);
                else if (k === "title") el.title = String(v);
                else if (k === "type") el.type = String(v);
                else if (k === "style") { /* store raw style string; ignore */ }
                else if (k.startsWith("data-")) el.dataset[k.slice(5)] = String(v);
                else el[k] = String(v);
            },
            getAttribute: (k) => {
                if (k === "class") return el.className;
                if (k === "style") return el.__styleStr || "";
                if (k.startsWith("data-")) return el.dataset[k.slice(5)];
                return el[k] != null ? String(el[k]) : null;
            },
            appendChild: (child) => {
                if (child.__parent) {
                    const i = child.__parent.__children.indexOf(child);
                    if (i >= 0) child.__parent.__children.splice(i, 1);
                }
                el.__children.push(child);
                child.__parent = el;
                return child;
            },
            insertBefore: (child, ref) => {
                if (child.__parent) {
                    const i = child.__parent.__children.indexOf(child);
                    if (i >= 0) child.__parent.__children.splice(i, 1);
                }
                if (ref) {
                    const idx = el.__children.indexOf(ref);
                    if (idx >= 0) el.__children.splice(idx, 0, child);
                    else el.__children.push(child);
                } else {
                    el.__children.push(child);
                }
                child.__parent = el;
                return child;
            },
            removeChild: (child) => {
                const i = el.__children.indexOf(child);
                if (i >= 0) el.__children.splice(i, 1);
                child.__parent = null;
                return child;
            },
            remove: () => {
                if (el.__parent) el.__parent.removeChild(el);
            },
            querySelector: (sel) => {
                return el.__queryAll(sel)[0] || null;
            },
            querySelectorAll: (sel) => {
                return el.__queryAll(sel);
            },
            contains: (other) => {
                if (!other) return false;
                if (other === el) return true;
                const walk = (node) => {
                    for (const c of node.__children) {
                        if (c === other) return true;
                        if (walk(c)) return true;
                    }
                    return false;
                };
                return walk(el);
            },
            focus: noop,
            __queryAll: (sel) => {
                const out = [];
                const walk = (node) => {
                    for (const c of node.__children) {
                        if (__matches(c, sel)) out.push(c);
                        walk(c);
                    }
                };
                walk(el);
                return out;
            },
        };
        // innerHTML setter: parse into children. We use a JS getter/setter via
        // Object.defineProperty so assignment `el.innerHTML = "..."` works.
        Object.defineProperty(el, "innerHTML", {
            get: () => el.__innerHTMLRaw || "",
            set: (v) => {
                el.__innerHTMLRaw = String(v);
                __parseHtml(String(v), el);
            },
            configurable: true,
        });
        // textContent setter: just store text, clear children.
        Object.defineProperty(el, "textContent", {
            get: () => el.__text || "",
            set: (v) => { el.__text = String(v); el.__children = []; el.__innerHTMLRaw = String(v); },
            configurable: true,
        });
        // style: support `el.style.setProperty("--badge-color", val)` and
        // `el.style.getPropertyValue("--badge-color")`, plus a raw string
        // assignment via setAttribute("style", ...). The diff updates the
        // --badge-color CSS var on the EXISTING card node, so the shim must
        // preserve it across calls.
        el.style = {
            __props: {},
            setProperty: (k, v) => { el.style.__props[k] = String(v); },
            getPropertyValue: (k) => (el.style.__props[k] != null ? String(el.style.__props[k]) : ""),
            removeProperty: (k) => { const v = el.style.__props[k]; delete el.style.__props[k]; return v != null ? String(v) : ""; },
        };
        return el;
    }

    function __matches(el, sel) {
        // Support a single ".classname" selector, a "[data-key=...]" attribute
        // selector, or a bare tag name.
        if (!sel) return false;
        if (sel.startsWith(".")) {
            const cls = sel.slice(1);
            return el.classList.contains(cls);
        }
        if (sel.startsWith("[data-")) {
            const key = sel.slice(6, sel.indexOf("=") > 0 ? sel.indexOf("=") - 6 : sel.indexOf("]"));
            const val = sel.indexOf("=") > 0 ? sel.slice(sel.indexOf("=") + 2, sel.indexOf("]")) : null;
            if (val != null) return el.dataset[key] === val;
            return el.dataset[key] != null;
        }
        return el.__tag === sel;
    }

    const __nav = __newEl("div");
    const __detail = __newEl("div");
    globalThis.document = {
        addEventListener: noop,
        documentElement: { dataset: {} },
        activeElement: null,
        body: __newEl("body"),
        hidden: false,
        getElementById: (id) => {
            if (id === "plan-list") return __nav;
            if (id === "plan-detail") return __detail;
            return __newEl("div");
        },
        createElement: (tag) => __newEl(tag),
        querySelector: (sel) => null,
        querySelectorAll: (sel) => [],
    };
    globalThis.window = {
        location: { hash: "" },
        addEventListener: noop,
    };
    globalThis.localStorage = { getItem: () => null, setItem: noop };
    globalThis.fetch = () => new Promise(() => {});
    process.on("unhandledRejection", () => {});
    globalThis.setInterval = () => 0;
    globalThis.setTimeout = (fn, _ms) => { if (typeof fn === "function") { /* dropped */ } return 0; };
"""
BOARD_DIFF_SHIM = _with_extra(
    _BOARD_DIFF_TEMPLATE,
    "@@ELEMENT_PROPS@@",
    r"""            innerHTML: "",
            textContent: "",""",
)
NOTIFICATIONS_DIFF_SHIM = _with_extra(_BOARD_DIFF_TEMPLATE, "@@ELEMENT_PROPS@@", "")

# Used by test_dashboard_planlist_diff and test_dashboard_overview_diff (adds a
# dedicated #plan-detail element).
_PLANLIST_DIFF_TEMPLATE = r"""
    const noop = () => {};
    let __testIdCounter = 0;

    function __parseHtml(html, parent) {
        parent.__children = [];
        if (!html) return;
        const re = /<([a-zA-Z]+)((?:\s+[a-zA-Z-]+(?:="[^"]*")?)*)\s*>([\s\S]*?)<\/\1>|<([a-zA-Z]+)((?:\s+[a-zA-Z-]+(?:="[^"]*")?)*)\s*\/?>/g;
        let m;
        while ((m = re.exec(html)) !== null) {
            const tag = m[1] || m[4];
            const attrs = m[2] || m[5] || "";
            const inner = m[3] || "";
            const el = __newEl(tag);
            const are = /([a-zA-Z-]+)="([^"]*)"/g;
            let am;
            while ((am = are.exec(attrs)) !== null) {
                if (am[1] === "class") el.className = am[2];
                else if (am[1] === "title") el.title = am[2];
                else if (am[1] === "type") el.type = am[2];
                else el.setAttribute(am[1], am[2]);
            }
            if (inner) {
                __parseHtml(inner, el);
                el.__text = inner.replace(/<[^>]*>/g, "");
            }
            parent.__children.push(el);
            el.__parent = parent;
        }
    }

    function __newEl(tag) {
        const el = {
            __testId: ++__testIdCounter,
            __tag: tag || "div",
            __children: [],
            __parent: null,
            __text: "",
            tagName: (tag || "div").toUpperCase(),
            innerHTML: "",
            textContent: "",
            className: "",
            title: "",
            type: "",
            checked: false,
            dataset: {},
            classList: {
                _classes: () => (el.className || "").split(/\s+/).filter(Boolean),
                add: (...c) => { const s = new Set(el.classList._classes()); c.forEach(x => s.add(x)); el.className = [...s].join(" "); },
                remove: (...c) => { const s = new Set(el.classList._classes()); c.forEach(x => s.delete(x)); el.className = [...s].join(" "); },
                toggle: (c, f) => { const s = new Set(el.classList._classes()); if (f === undefined) f = !s.has(c); if (f) s.add(c); else s.delete(c); el.className = [...s].join(" "); },
                contains: (c) => el.classList._classes().includes(c),
            },
            addEventListener: noop,
            removeEventListener: noop,
            setAttribute: (k, v) => {
                if (k === "class") el.className = String(v);
                else if (k === "title") el.title = String(v);
                else if (k === "type") el.type = String(v);
                else if (k.startsWith("data-")) el.dataset[k.slice(5)] = String(v);
                else el[k] = String(v);
            },
            getAttribute: (k) => {
                if (k === "class") return el.className;
                if (k.startsWith("data-")) return el.dataset[k.slice(5)];
                return el[k] != null ? String(el[k]) : null;
            },
            appendChild: (child) => {
                if (child.__parent) {
                    const i = child.__parent.__children.indexOf(child);
                    if (i >= 0) child.__parent.__children.splice(i, 1);
                }
                el.__children.push(child);
                child.__parent = el;
                return child;
            },
            removeChild: (child) => {
                const i = el.__children.indexOf(child);
                if (i >= 0) el.__children.splice(i, 1);
                child.__parent = null;
                return child;
            },
            remove: () => {
                if (el.__parent) el.__parent.removeChild(el);
            },
            querySelector: (sel) => {
                return el.__queryAll(sel)[0] || null;
            },
            querySelectorAll: (sel) => {
                return el.__queryAll(sel);
            },
            __queryAll: (sel) => {
                const out = [];
                const walk = (node) => {
                    for (const c of node.__children) {
                        if (__matches(c, sel)) out.push(c);
                        walk(c);
                    }
                };
                walk(el);
                return out;
            },
        };
        Object.defineProperty(el, "innerHTML", {
            get: () => el.__innerHTMLRaw || "",
            set: (v) => {
                el.__innerHTMLRaw = String(v);
                __parseHtml(String(v), el);
            },
            configurable: true,
        });
        Object.defineProperty(el, "textContent", {
            get: () => el.__text || "",
            set: (v) => { el.__text = String(v); el.__children = []; el.__innerHTMLRaw = String(v); },
            configurable: true,
        });
        return el;
    }

    function __matches(el, sel) {
        if (!sel) return false;
        if (sel.startsWith(".")) {
            const cls = sel.slice(1);
            return el.classList.contains(cls);
        }
        if (sel.startsWith("[data-")) {
            const key = sel.slice(6, sel.indexOf("=") > 0 ? sel.indexOf("=") - 6 : sel.indexOf("]"));
            const val = sel.indexOf("=") > 0 ? sel.slice(sel.indexOf("=") + 2, sel.indexOf("]")) : null;
            if (val != null) return el.dataset[key] === val;
            return el.dataset[key] != null;
        }
        return el.__tag === sel;
    }

    const __nav = __newEl("div");
    // @@PLAN_DETAIL_EL@@
    globalThis.document = {
        addEventListener: noop,
        documentElement: { dataset: {} },
        getElementById: (id) => {
            if (id === "plan-list") return __nav;
            // @@PLAN_DETAIL_LOOKUP@@
            return __newEl("div");
        },
        createElement: (tag) => __newEl(tag),
    };
    globalThis.window = {
        location: { hash: "" },
        addEventListener: noop,
    };
    globalThis.localStorage = { getItem: () => null, setItem: noop };
    globalThis.fetch = () => new Promise(() => {});
    process.on("unhandledRejection", () => {});
    globalThis.setInterval = () => 0;
    globalThis.setTimeout = (fn, _ms) => { if (typeof fn === "function") { /* dropped */ } return 0; };
"""
PLANLIST_DIFF_SHIM = _with_extra(
    _with_extra(_PLANLIST_DIFF_TEMPLATE, "@@PLAN_DETAIL_EL@@", ""),
    "@@PLAN_DETAIL_LOOKUP@@",
    "",
)
OVERVIEW_DIFF_SHIM = _with_extra(
    _with_extra(_PLANLIST_DIFF_TEMPLATE, "@@PLAN_DETAIL_EL@@", r"""    const __planDetail = __newEl("div");"""),
    "@@PLAN_DETAIL_LOOKUP@@",
    r"""            if (id === "plan-detail") return __planDetail;""",
)

# Used by test_dashboard_comms_send and test_dashboard_always_poll (adds
# document.hidden = false).
_COMMS_SEND_TEMPLATE = r"""
        const noop = () => {};
        const fakeEl = {
            innerHTML: "",
            classList: { add: noop, remove: noop, toggle: noop, contains: () => false },
            addEventListener: noop,
            setAttribute: noop,
            appendChild: noop,
            querySelectorAll: () => [],
            dataset: {},
        };
        globalThis.__commsThread = { appendChildCalls: 0, appendedClasses: [] };
        globalThis.__commsLanding = { styleCalls: [] };
        globalThis.__onAir = { classListAdd: [], classListRemove: [] };
        globalThis.__commsSend = { disabledSet: [], classListAdd: [], classListRemove: [] };
        const commsThreadEl = {
            ...fakeEl,
            dataset: {},
            addEventListener: noop,
            children: [],
            style: { display: "" },
            appendChild: function (child) {
                globalThis.__commsThread.appendChildCalls += 1;
                if (child && typeof child.className === "string") {
                    globalThis.__commsThread.appendedClasses.push(child.className);
                }
                return child;
            },
        };
        const commsLandingEl = {
            ...fakeEl,
            dataset: {},
            style: { display: "", set display(v) { globalThis.__commsLanding.styleCalls.push(v); }, get display() { return ""; } },
            classList: { add: noop, remove: noop, toggle: noop, contains: () => false },
        };
        const onAirEl = {
            ...fakeEl,
            dataset: {},
            classList: {
                add: function (c) { globalThis.__onAir.classListAdd.push(c); },
                remove: function (c) { globalThis.__onAir.classListRemove.push(c); },
                toggle: noop, contains: () => false,
            },
        };
        const commsSendEl = {
            ...fakeEl,
            dataset: {},
            disabled: false,
            set disabled(v) { globalThis.__commsSend.disabledSet.push(v); },
            get disabled() { return false; },
            classList: {
                add: function (c) { globalThis.__commsSend.classListAdd.push(c); },
                remove: function (c) { globalThis.__commsSend.classListRemove.push(c); },
                toggle: noop, contains: () => false,
            },
        };
        const commsInputEl = {
            ...fakeEl,
            dataset: {},
            value: "",
            addEventListener: noop,
        };
        globalThis.document = {
            addEventListener: noop,
            documentElement: { dataset: {} },
            // @@DOCUMENT_HIDDEN@@
            getElementById: function (id) {
                if (id === "comms-thread") return commsThreadEl;
                if (id === "comms-landing") return commsLandingEl;
                if (id === "on-air") return onAirEl;
                if (id === "comms-send") return commsSendEl;
                if (id === "comms-input") return commsInputEl;
                // NOTE: deliberately NO special-case for "auto-refresh" -
                // the implementation must not depend on that element
                // existing anymore. Return a generic fake element so any
                // stray lookup does not crash, but tests assert behavior
                // is correct regardless of this fallback.
                return { ...fakeEl, dataset: {}, addEventListener: noop, children: [] };
            },
            createElement: function (tag) {
                return {
                    tagName: tag,
                    innerHTML: "",
                    textContent: "",
                    className: "",
                    classList: {
                        add: noop, remove: noop,
                        toggle: function (c) { this._toggled = c; },
                        contains: () => false,
                    },
                    addEventListener: noop,
                    setAttribute: noop,
                    appendChild: function (child) { return child; },
                    querySelectorAll: () => [],
                    dataset: {},
                    style: { display: "" },
                };
            },
        };
        globalThis.window = {
            location: { hash: "" },
            addEventListener: noop,
        };
        globalThis.localStorage = { getItem: () => null, setItem: noop };
"""
COMMS_SEND_SHIM = _with_extra(_COMMS_SEND_TEMPLATE, "@@DOCUMENT_HIDDEN@@", "")
ALWAYS_POLL_SHIM = _with_extra(
    _COMMS_SEND_TEMPLATE,
    "@@DOCUMENT_HIDDEN@@",
    r"""            hidden: false,""",
)
