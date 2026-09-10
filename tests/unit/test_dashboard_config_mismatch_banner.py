"""CFG-B3 follow-up: make a dashboard/scheduler config divergence impossible
to miss in the UI.

GET /api/health (added by CFG-B3) returns `config_mismatch` (a list of
diverging field names) and `scheduler`. Nothing in static/ fetched
/api/health before this story, so these tests pin the minimal wiring:

  * static/app/api.js gains ONE fetch function for /api/health, shaped like
    the existing helpers (auth header via fetchJson / X-Pipeline-Api-Key,
    throws on non-OK).
  * static/index.html gains ONE hidden banner element near the top.
  * static/app/main.js fetches health on load and reveals the banner ONLY
    when config_mismatch is a non-empty array; the banner text names the
    diverging fields and states plainly that the dashboard and the
    scheduler are working different plan stores.

Visibility follows the codebase's existing conditional-UI idiom (the
usage-banner precedent): the element ships with the `hidden` class in the
markup and JS toggles it via classList add/remove of "hidden" (or the
equivalent `hidden` property). Fail soft: a health-check failure must never
block the dashboard from rendering, so the wiring is guarded and the banner
stays hidden unless a non-empty config_mismatch arrives.

These tests assert against the static file SOURCE TEXT (the established
idiom in test_dashboard_frontend.py) — no byte/SHA-256 hash pinning of any
static file, so later sibling stories may legitimately edit the same files.
"""
import os
import re
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
STATIC = os.path.join(REPO_ROOT, "static")

API_JS = os.path.join(STATIC, "app", "api.js")
INDEX_HTML = os.path.join(STATIC, "index.html")
MAIN_JS = os.path.join(STATIC, "app", "main.js")

BANNER_ID = "config-mismatch-banner"


def _read(path):
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


class DashboardConfigMismatchBannerTest(unittest.TestCase):
    """Source-text contract for the config-mismatch banner wiring."""

    @classmethod
    def setUpClass(cls):
        cls.api_js = _read(API_JS)
        cls.index_html = _read(INDEX_HTML)
        cls.main_js = _read(MAIN_JS)

    def _health_helper(self):
        """Return (name, body) of the api.js function that fetches
        /api/health, or (None, None). Slices between `async function`
        declarations so brace matching can't misfire."""
        src = self.api_js
        starts = list(re.finditer(r"async\s+function\s+(\w+)\s*\(", src))
        for i, m in enumerate(starts):
            end = starts[i + 1].start() if i + 1 < len(starts) else src.find("export", m.start())
            if end == -1:
                end = len(src)
            body = src[m.start():end]
            if "/api/health" in body:
                return m.group(1), body
        return None, None

    # --- 1. static/app/api.js: one fetch function for /api/health ---------

    def test_api_js_references_api_health_endpoint(self):
        """api.js must fetch /api/health (grep 'api/health' static/ >= 1)."""
        self.assertIn("/api/health", self.api_js)

    def test_api_js_health_fetch_is_a_named_function(self):
        """The /api/health fetch lives in a named async helper, like
        fetchGuardLiveness/fetchPlanMetrics — not an inline fetch call."""
        name, _ = self._health_helper()
        self.assertIsNotNone(
            name,
            "api.js should define a named async function that fetches /api/health",
        )

    def test_api_js_health_helper_uses_existing_auth_and_error_idiom(self):
        """Same auth-header handling + error handling as the other helpers:
        either it delegates to fetchJson (which injects X-Pipeline-Api-Key
        and throws on !ok) or it does both itself."""
        _, body = self._health_helper()
        self.assertIsNotNone(body, "health helper body not found in api.js")
        uses_fetch_json = "fetchJson(" in body
        has_auth = 'X-Pipeline-Api-Key' in body
        has_error = ("!res.ok" in body) or ("throw" in body) or ("catch" in body)
        self.assertTrue(
            uses_fetch_json or (has_auth and has_error),
            "health helper must reuse fetchJson or handle auth + errors itself",
        )

    def test_api_js_exports_the_health_helper(self):
        """The helper is exported so main.js can import it."""
        name, _ = self._health_helper()
        self.assertIsNotNone(name)
        self.assertRegex(self.api_js, r"export\s*\{[^}]*\b" + re.escape(name) + r"\b")

    # --- 2. static/index.html: ONE hidden banner element near the top -----

    def test_index_html_has_banner_element_with_id(self):
        """index.html contains the banner element with its id."""
        self.assertIn(f'id="{BANNER_ID}"', self.index_html)

    def test_index_html_banner_is_hidden_by_default(self):
        """Hidden by default, matching the usage-banner precedent: the
        markup carries the `hidden` class (or the hidden attribute)."""
        tag = re.search(r"<[a-z]+[^>]*" + re.escape(f'id="{BANNER_ID}"') + r"[^>]*>", self.index_html)
        self.assertIsNotNone(tag, f"banner element with id={BANNER_ID} not found")
        self.assertTrue(
            re.search(r'class="[^"]*\bhidden\b', tag.group(0)) or re.search(r"\bhidden\b(?![\w-])", tag.group(0).replace('id="config-mismatch-banner"', "")),
            "banner must be hidden by default (hidden class or attribute)",
        )

    def test_index_html_banner_is_exactly_one_element_near_top(self):
        """ONE banner element, declared near the top of the page (before the
        module scripts at the end of the document)."""
        self.assertEqual(1, self.index_html.count(f'id="{BANNER_ID}"'))
        first_script = self.index_html.find("<script")
        banner_at = self.index_html.find(f'id="{BANNER_ID}"')
        self.assertLess(banner_at, first_script, "banner should be near the top, before the scripts")

    # --- 3. static/app/main.js: fetch on load, reveal only on divergence --

    def test_main_js_references_config_mismatch(self):
        """main.js reads the config_mismatch field from /api/health."""
        self.assertIn("config_mismatch", self.main_js)

    def test_main_js_toggles_banner_hidden(self):
        """Visibility follows the existing conditional-UI idiom: classList
        add/remove of "hidden" (usage-banner precedent) or the `hidden`
        property — on the element looked up by the banner id."""
        self.assertIn(BANNER_ID, self.main_js)
        after = self.main_js[self.main_js.find(BANNER_ID):]
        toggled = re.search(
            r'classList\.(add|remove|toggle)\s*\(\s*"hidden"|\.hidden\s*=', after
        )
        self.assertIsNotNone(toggled, "main.js must toggle the banner's hidden state")

    def test_main_js_does_not_use_style_display_for_banner(self):
        """Guard the idiom: no style.display toggling for this banner."""
        lookup = self.main_js.find(BANNER_ID)
        region = self.main_js[max(0, lookup - 200): lookup + 600]
        self.assertNotIn("style.display", region)

    def test_main_js_fetches_health_on_load(self):
        """main.js invokes the health helper exported by api.js at load time
        (not behind an unrelated user action only)."""
        name, _ = self._health_helper()
        self.assertIsNotNone(name, "api.js must define the health helper first")
        self.assertIn(name, self.main_js, "main.js must call the health helper")

    # --- banner text names fields + the two plan stores -------------------

    def test_banner_text_names_dashboard_and_scheduler_plan_stores(self):
        """The warning must state plainly that the dashboard and the
        scheduler are working different plan stores."""
        combined = (self.index_html + "\n" + self.main_js).lower()
        self.assertIn("dashboard", combined)
        self.assertIn("scheduler", combined)
        self.assertIn("plan store", combined)

    def test_banner_text_names_the_diverging_fields(self):
        """The banner text is built from the diverging field names, so the
        user sees WHICH fields disagree (join/map/forEach over
        config_mismatch)."""
        self.assertRegex(
            self.main_js,
            r"config_mismatch[^;\n]{0,200}(join|map|forEach|toString|JSON\.stringify)",
        )

    # --- NEGATIVE: empty/missing config_mismatch must not reveal banner ---

    def test_main_js_guards_on_non_empty_config_mismatch(self):
        """NEGATIVE: the reveal is guarded by a non-empty check (a .length
        test or Array.isArray) so an empty or missing config_mismatch never
        reveals the banner."""
        guarded = re.search(
            r"(config_mismatch[^;\n]{0,120}\.length|Array\.isArray\s*\(\s*config_mismatch)",
            self.main_js,
        )
        self.assertIsNotNone(
            guarded,
            "main.js must check config_mismatch is a non-empty array "
            "(.length or Array.isArray) before revealing the banner",
        )

    # --- FAIL SOFT: health failure must not break the page ----------------

    def test_main_js_health_wiring_is_error_guarded(self):
        """FAIL SOFT: the health call is wrapped in try/catch or .catch so an
        unreachable /api/health cannot throw during page boot. The guard must
        sit on the health wiring itself, not just anywhere in main.js."""
        name, _ = self._health_helper()
        self.assertIsNotNone(name, "api.js must define the health helper first")
        call_at = self.main_js.find(name)
        self.assertNotEqual(call_at, -1, "main.js must call the health helper")
        region = self.main_js[max(0, call_at - 400): call_at + 600]
        self.assertTrue(
            ("catch" in region) or ("try" in region),
            "main.js must guard the health-check call (try/catch or .catch)",
        )

    def test_main_js_does_not_await_health_unguarded_at_top_level(self):
        """Boundary: a bare top-level `await fetchHealth(...)` with no
        surrounding try would crash boot on network failure."""
        self.assertFalse(
            re.search(r"^\s*await\s+\w*[Hh]ealth", self.main_js, re.MULTILINE),
            "top-level unguarded await of the health fetch would break boot",
        )


class NoHashPinningGuardTest(unittest.TestCase):
    """NEGATIVE: this test file must not pin a SHA-256/byte hash of any
    static file — later sibling stories legitimately edit the same files and
    a hash pin would make them unwinnable. Assert on selectors/strings."""

    def test_no_sha256_or_hashlib_pinning_in_this_test_file(self):
        with open(__file__, "r", encoding="utf-8") as fh:
            source = fh.read()
        self.assertIsNone(
            re.search(r"[0-9a-fA-F]{64}", source),
            "no 64-hex-char digest literal may appear in this test file",
        )
        self.assertIsNone(
            re.search(r"import\s+hashlib", source),
            "no hashlib import may appear in this test file",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()