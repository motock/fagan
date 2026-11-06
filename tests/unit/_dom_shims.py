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
