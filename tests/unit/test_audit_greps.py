from __future__ import annotations

import pathlib
import subprocess
from unittest import TestCase

PROJECT_ROOT = pathlib.Path(__file__).parent.parent.parent


class TestAuditGreps(TestCase):
    def test_no_firmware_type_string_checks_in_tuner_app(self) -> None:
        cmd = ["grep", "-rE", r"firmware_type\(\) ==|firmware_type\(\) !=", "tuner_app/"]
        result = subprocess.run(cmd, capture_output=True, cwd=PROJECT_ROOT)
        matches = result.stdout.decode()
        self.assertEqual(
            result.returncode,
            1,
            f"firmware_type() string checks found in tuner_app/:\n{matches}",
        )
        self.assertEqual(matches, "", "Expected empty stdout for firmware_type() check")

    def test_platform_tuple_consistency(self) -> None:
        pattern = r'\("epic",\s*"bixbit",\s*"luxos",\s*"braiins"(,\s*"whatsminer")?\)'
        cmd = [
            "grep",
            "-rE",
            pattern,
            "tuner_app/",
            "--include=*.py",
            "--exclude=constants.py",
        ]
        result = subprocess.run(cmd, capture_output=True, cwd=PROJECT_ROOT)
        matches = result.stdout.decode()
        self.assertEqual(
            result.returncode,
            1,
            f"Inline platform-tuple literals found in tuner_app/ "
            f"(use `_PLATFORMS` from `tuner_app.constants` instead):\n{matches}",
        )
        self.assertEqual(matches, "", "Expected empty stdout for platform-tuple literal check")

    def test_no_silent_epic_fallbacks_in_registration_boundaries(self) -> None:
        cmd = [
            "grep",
            "-rE",
            'or "epic"',
            "tuner_app/scanner/",
            "tuner_app/manager/",
            "tuner_app/http_server/handlers/",
        ]
        result = subprocess.run(cmd, capture_output=True, cwd=PROJECT_ROOT)
        matches = result.stdout.decode()
        self.assertEqual(
            result.returncode,
            1,
            f"Silent epic fallbacks found in registration boundaries:\n{matches}",
        )
        self.assertEqual(matches, "", "Expected empty stdout for silent epic fallback check")

    def test_no_stale_hide_on_bixbit_css_class(self) -> None:
        cmd = ["grep", "-rE", "hide-on-bixbit", "tuner_app/static/"]
        result = subprocess.run(cmd, capture_output=True, cwd=PROJECT_ROOT)
        matches = result.stdout.decode()
        self.assertEqual(
            result.returncode,
            1,
            f"Stale hide-on-bixbit CSS class found:\n{matches}",
        )
        self.assertEqual(matches, "", "Expected empty stdout for stale hide-on-bixbit check")

    def test_no_stale_vendor_tags_in_frontend(self) -> None:
        pattern = "vendor: 'epic'|vendor: 'bixbit'|vendor: 'luxos'|vendor: 'braiins'"
        cmd = ["grep", "-E", pattern, "tuner_app/static/js/main.js"]
        result = subprocess.run(cmd, capture_output=True, cwd=PROJECT_ROOT)
        matches = result.stdout.decode()
        self.assertEqual(
            result.returncode,
            1,
            f"Stale vendor tags found in frontend:\n{matches}",
        )
        self.assertEqual(matches, "", "Expected empty stdout for stale vendor tag check")

    def test_no_stale_current_detail_firmware_type_variable(self) -> None:
        cmd = ["grep", "currentDetailFirmwareType", "tuner_app/static/js/main.js"]
        result = subprocess.run(cmd, capture_output=True, cwd=PROJECT_ROOT)
        matches = result.stdout.decode()
        self.assertEqual(
            result.returncode,
            1,
            f"Stale currentDetailFirmwareType variable found:\n{matches}",
        )
        self.assertEqual(
            matches, "", "Expected empty stdout for stale currentDetailFirmwareType check"
        )  # noqa: E501

    def test_no_flat_iteration_over_state_config(self) -> None:
        """Forbid `for X in state.CONFIG` in production tuner_app/ code.

        state.CONFIG is a v3 nested dict ({"defaults": {...}, "fleet_ops": {...}}).
        Iterating over it yields top-level keys ("defaults", "fleet_ops") which is
        almost never what the caller wants.  Use iter_all_config_keys() for a union
        of all flat keys, or iterate over state.CONFIG["fleet_ops"] / a platform
        bucket directly.
        """
        cmd = [
            "grep",
            "-rEn",
            r"for [a-zA-Z_]+ in state\.CONFIG[^\[]",
            "tuner_app/",
            "--include=*.py",
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, cwd=PROJECT_ROOT)
        self.assertEqual(
            result.stdout.strip(),
            "",
            f"Forbidden flat iteration on state.CONFIG found:\n{result.stdout}",
        )

    def test_no_legacy_compat_shim_in_config_defaults(self) -> None:
        """The legacy flat-body compat path was removed in Phase 6.

        The sentinel variable and logger helper must not exist anywhere in
        tuner_app/ — any hit means the shim was re-introduced.
        """
        cmd = [
            "grep",
            "-rE",
            "_legacy_defaults_logged_once|_log_legacy_defaults_shape_once",
            "tuner_app/",
        ]
        result = subprocess.run(cmd, capture_output=True, cwd=PROJECT_ROOT)
        matches = result.stdout.decode()
        self.assertEqual(
            result.returncode,
            1,
            f"Legacy config_defaults compat shim found in tuner_app/:\n{matches}",
        )
        self.assertEqual(matches, "", "Expected empty stdout for legacy config_defaults shim check")

    def test_no_legacy_compat_shim_in_bulk_apply(self) -> None:
        """The legacy bulk-apply compat path was removed in Phase 6.

        The sentinel variable and logger helper must not exist anywhere in
        tuner_app/ — any hit means the shim was re-introduced.
        """
        cmd = [
            "grep",
            "-rE",
            "_legacy_bulk_apply_logged_once|_log_legacy_bulk_apply_shape_once",
            "tuner_app/",
        ]
        result = subprocess.run(cmd, capture_output=True, cwd=PROJECT_ROOT)
        matches = result.stdout.decode()
        self.assertEqual(
            result.returncode,
            1,
            f"Legacy bulk_apply_config compat shim found in tuner_app/:\n{matches}",
        )
        self.assertEqual(matches, "", "Expected empty stdout for legacy bulk_apply shim check")

    def test_no_direct_state_config_key_reads_in_engine_and_manager(self) -> None:
        """Engine and manager code must not read state.CONFIG[var] (unquoted key).

        Engine code reads config via EffectiveConfig (self.config[k]). The only
        legitimate state.CONFIG reads use the two top-level string keys:
        state.CONFIG["defaults"] and state.CONFIG["fleet_ops"]. An unquoted
        subscript (variable or expression as index) almost certainly means the
        engine is reading tuning config directly from state.CONFIG rather than
        going through EffectiveConfig, which bypasses per-miner overrides and the
        per-platform resolution chain.
        """
        cmd = [
            "grep",
            "-rEn",
            r"state\.CONFIG\[[^\"']",
            "tuner_app/tuning_engine/",
            "tuner_app/manager/",
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, cwd=PROJECT_ROOT)
        self.assertEqual(
            result.stdout.strip(),
            "",
            f"Direct state.CONFIG[var] reads found in engine/manager:\n{result.stdout}",
        )

    def test_partition_invariant_no_overlap(self) -> None:
        """FLEET_OPS_KEYS and CONFIG_DEFAULTS_PER_PLATFORM_KEYS must be disjoint.

        Adding the same key to both sets would make it ambiguous whether
        EffectiveConfig resolves it from the per-platform bucket or fleet_ops,
        and would break the /tuner/config/defaults rejection of fleet-ops keys.
        The assert in defaults.py already enforces this at import time; this
        test makes the invariant explicit and visible in the audit-grep suite.
        """
        from tuner_app.config.defaults import CONFIG_DEFAULTS_PER_PLATFORM_KEYS
        from tuner_app.constants import FLEET_OPS_KEYS

        overlap = FLEET_OPS_KEYS & CONFIG_DEFAULTS_PER_PLATFORM_KEYS
        self.assertEqual(
            overlap,
            frozenset(),
            f"FLEET_OPS_KEYS and CONFIG_DEFAULTS_PER_PLATFORM_KEYS overlap: {overlap}",
        )

    def test_frontend_defaults_post_uses_new_shape(self) -> None:
        """All /tuner/config/defaults POST calls in main.js must include 'platform'.

        After Phase 6 the legacy flat-body path is removed; any POST without
        'platform' will be rejected with 400. This grep catches callers that
        use JSON.stringify({...}) without a platform field.

        Strategy: count lines that contain '/tuner/config/defaults' in main.js,
        then assert that every such line has 'platform' somewhere on the same
        line OR the immediately adjacent stringify block includes 'platform'.
        We use a simpler proxy: grep for the literal string combination
        '/tuner/config/defaults' AND verify there are no nearby stringify calls
        whose body lacks 'platform'. The grep below checks that no call site
        uses a flat non-platform body by searching for the legacy pattern
        ``JSON.stringify({[^}]*})`` adjacent to the endpoint — if present that's
        a single-line body without 'platform'.
        """
        main_js = PROJECT_ROOT / "tuner_app" / "static" / "js" / "main.js"
        content = main_js.read_text(encoding="utf-8")
        # Find every occurrence of the endpoint URL and verify the surrounding
        # line or the line right before/after contains "platform".
        import re

        for m in re.finditer(r"fetchJSON\s*\(\s*['\"]\/tuner\/config\/defaults['\"]", content):
            # Extract a window of ~200 chars after the match to find the body.
            window = content[m.start() : m.start() + 400]
            self.assertIn(
                "platform",
                window,
                f"fetchJSON call to /tuner/config/defaults near offset {m.start()} "
                f"does not include 'platform' in the request body:\n{window[:200]}",
            )

    def test_frontend_bulk_apply_post_uses_new_shape(self) -> None:
        """All /tuner/bulk/apply_config POST calls in main.js must include 'platform'."""
        main_js = PROJECT_ROOT / "tuner_app" / "static" / "js" / "main.js"
        content = main_js.read_text(encoding="utf-8")
        import re

        for m in re.finditer(r"fetchJSON\s*\(\s*['\"]\/tuner\/bulk\/apply_config['\"]", content):
            window = content[m.start() : m.start() + 400]
            self.assertIn(
                "platform",
                window,
                f"fetchJSON call to /tuner/bulk/apply_config near offset {m.start()} "
                f"does not include 'platform' in the request body:\n{window[:200]}",
            )

    def test_fan_speed_rendering_uses_firmware_type(self) -> None:
        """The s-fan element rendering must select the unit suffix based on firmware_type.

        ePIC reports fan_speed as a percentage (0-100); all other vendors
        (Bixbit, Braiins, LuxOS) report it as RPM. The live LuxOS test showed
        "6660%" which is nonsensical. The fix reads s.firmware_type to pick
        '%' (ePIC) vs ' RPM' (all others). This grep enforces the contract so
        a future regression cannot revert to unconditional '%'.
        """
        main_js = PROJECT_ROOT / "tuner_app" / "static" / "js" / "main.js"
        content = main_js.read_text(encoding="utf-8")
        # Find the s-fan assignment line and verify it references firmware_type
        # in the same or nearby context (within 200 chars before the assignment).
        idx = content.find("getElementById('s-fan')")
        self.assertNotEqual(idx, -1, "'s-fan' element not found in main.js")
        # Check that 'firmware_type' appears within 200 chars before the s-fan line
        window = content[max(0, idx - 200) : idx + 100]
        self.assertIn(
            "firmware_type",
            window,
            "s-fan rendering does not reference firmware_type — fan unit suffix will be "
            f"wrong for non-ePIC vendors. Context:\n{window}",
        )

    def test_no_legacy_miner_ip_global_in_frontend(self) -> None:
        """A13 hard cutover: ``let minerIP =`` was replaced by the
        ``currentMiner`` object. Any reintroduction would silently route
        per-miner API calls back to IP-keyed paths and bypass the v4
        canonical MAC plumbing.
        """
        main_js = PROJECT_ROOT / "tuner_app" / "static" / "js" / "main.js"
        content = main_js.read_text(encoding="utf-8")
        self.assertNotIn(
            "let minerIP",
            content,
            "Legacy `let minerIP =` global re-introduced in main.js — "
            "use currentMiner.mac / currentMac() instead.",
        )

    def test_no_legacy_ips_body_in_bulk_post_calls(self) -> None:
        """A12+A13: bulk endpoint POST bodies must use `macs:`.

        After the hard cutover the backend rejects `{ips:...}` with HTTP 400.
        The grep walks every fetchJSON call to /tuner/bulk/* and asserts
        ``ips:`` does not appear in the body argument.
        """
        main_js = PROJECT_ROOT / "tuner_app" / "static" / "js" / "main.js"
        content = main_js.read_text(encoding="utf-8")
        import re

        for m in re.finditer(r"fetchJSON\s*\(\s*['\"\`]\/tuner\/bulk\/[^'\"\`]+", content):
            window = content[m.start() : m.start() + 600]
            self.assertNotRegex(
                window[:600],
                r"\bips\s*:\s*",
                f"fetchJSON call to a /tuner/bulk/* endpoint near offset {m.start()} "
                f"sends a legacy `ips:` body field. Use `macs:` instead.\n\n"
                f"{window[:200]}",
            )

    def test_no_legacy_ip_body_in_per_miner_post_calls(self) -> None:
        """A12+A13: per-miner control POST bodies must use `mac:`.

        The grep checks fetchJSON / fetch calls to /tuner/start, /tuner/stop,
        /tuner/delete_profile, /tuner/reset_stock, /tuner/retune_voltage,
        /tuner/select_voltage_profile, /tuner/remeasure_*, /tuner/miners/remove,
        /tuner/mrr/resync. Any literal ``ip:`` body field is a regression.
        """
        main_js = PROJECT_ROOT / "tuner_app" / "static" / "js" / "main.js"
        content = main_js.read_text(encoding="utf-8")
        import re

        endpoints = [
            "/tuner/start",
            "/tuner/stop",
            "/tuner/delete_profile",
            "/tuner/reset_stock",
            "/tuner/retune_voltage",
            "/tuner/select_voltage_profile",
            "/tuner/remeasure_cell",
            "/tuner/remeasure_queue/clear",
            "/tuner/remeasure_queue/process",
            "/tuner/miners/remove",
            "/tuner/mrr/resync",
        ]
        for ep in endpoints:
            for m in re.finditer(
                r"fetch(?:JSON)?\s*\(\s*['\"\`]" + re.escape(ep) + r"['\"\`]",
                content,
            ):
                window = content[m.start() : m.start() + 400]
                # ``ip:`` directly inside JSON.stringify({...}) is the regression
                self.assertNotRegex(
                    window,
                    r"JSON\.stringify\s*\(\s*\{[^}]*\bip\s*:",
                    f"fetch call to {ep} near offset {m.start()} sends a "
                    f"legacy `ip:` body field. Use `mac:` instead.\n\n{window[:200]}",
                )

    def test_wattage_chart_hidden_by_capability_class(self) -> None:
        """The wattage-chart-card must be hidden when the miner lacks wattage_search_strategy.

        The 'Wattage Search (Braiins)' chart is only meaningful for Braiins miners.
        The fix adds a 'hide-no-wattage-search' class toggle in updateStatus (JS)
        and a CSS rule that hides .wattage-chart-card when that class is present.
        This test asserts both sides of the contract exist.
        """
        main_js = PROJECT_ROOT / "tuner_app" / "static" / "js" / "main.js"
        content = main_js.read_text(encoding="utf-8")
        self.assertIn(
            "hide-no-wattage-search",
            content,
            "hide-no-wattage-search class toggle not found in main.js — "
            "the wattage chart will be visible for all miners regardless of vendor",
        )
        # Also verify the CSS rule exists in the detail stylesheet
        import re

        css_dir = PROJECT_ROOT / "tuner_app" / "static" / "css"
        css_content = "\n".join(f.read_text(encoding="utf-8") for f in css_dir.glob("*.css"))
        # Must have a rule that hides .wattage-chart-card under this class
        self.assertTrue(
            re.search(r"hide-no-wattage-search.*wattage-chart-card", css_content),
            "CSS rule hiding .wattage-chart-card under .hide-no-wattage-search not found "
            "in tuner_app/static/css/*.css",
        )

    # ─────────────────────────────────────────────────────────────────
    # PR5 / A14 — final invariants from the MAC-keyed storage migration
    # ─────────────────────────────────────────────────────────────────

    def test_save_config_writes_v4_schema_version(self) -> None:
        """``save_config_to_disk`` must write ``"version": 4`` (v4 schema).

        v3 files (per-platform defaults, IP-keyed miner_configs) were the
        previous schema; v4 added MAC-keyed miner_configs with per-platform
        nested overrides. Writing a lower version would corrupt new v4 entries
        on next load (the migration funnel only goes forward, not back).

        Enforces a single literal ``"version": 4`` exists in
        ``tuner_app/config/persistence.py`` and no stale ``"version": 3`` /
        ``"version": 2`` write-side literals remain.
        """
        persistence = PROJECT_ROOT / "tuner_app" / "config" / "persistence.py"
        content = persistence.read_text(encoding="utf-8")
        # The save_config_to_disk payload must include "version": 4
        self.assertIn(
            '"version": 4',
            content,
            'save_config_to_disk must write "version": 4 — v4 is the current schema',
        )
        # No write-side regressions to older schema versions
        self.assertNotIn(
            '"version": 3',
            content,
            'Stale write of "version": 3 found in persistence.py — '
            'all writes must use "version": 4',
        )
        self.assertNotIn(
            '"version": 2',
            content,
            'Stale write of "version": 2 found in persistence.py — '
            'all writes must use "version": 4',
        )

    def test_no_legacy_engines_ip_index_in_tuner_app(self) -> None:
        """Engine registry must be MAC-keyed: no ``engines[ip]`` literals.

        A9 re-keyed ``TunerManager.engines`` from ``dict[ip, TuningEngine]`` to
        ``dict[mac, TuningEngine]``. Any reintroduction of ``engines[ip]`` would
        silently use IP as the lookup key — on a DHCP IP change this would lose
        the running engine reference, and tear down + respawn the engine
        instead of the lock-free ``refresh_engine_ip(mac, new_ip)`` flow.
        """
        cmd = [
            "grep",
            "-rEn",
            r"\bengines\[ip\]",
            "tuner_app/",
            "--include=*.py",
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, cwd=PROJECT_ROOT)
        self.assertEqual(
            result.stdout.strip(),
            "",
            f"Legacy ``engines[ip]`` literal found in tuner_app/:\n{result.stdout}",
        )

    def test_no_legacy_rental_cache_ip_index_in_tuner_app(self) -> None:
        """RentalCache must be MAC-keyed: no ``_cache[ip]`` literals.

        A10 re-keyed ``RentalCache._cache`` from ``dict[ip, ...]`` to
        ``dict[mac, ...]``. The DHCP-move invariant becomes load-bearing:
        cache entries survive an IP swap because they're pinned to the MAC.
        Any ``_cache[ip]`` literal would defeat that invariant.
        """
        cmd = [
            "grep",
            "-rEn",
            r"_cache\[ip\]",
            "tuner_app/",
            "--include=*.py",
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, cwd=PROJECT_ROOT)
        self.assertEqual(
            result.stdout.strip(),
            "",
            f"Legacy ``_cache[ip]`` literal found in tuner_app/:\n{result.stdout}",
        )

    def test_no_miner_data_path_with_engine_ip_in_tuner_app(self) -> None:
        """Per-miner file paths must be MAC-keyed: no ``_miner_data_path(engine.ip``.

        A8 migrated per-miner file paths from IP-based to MAC-based naming.
        Cross-platform files (logs) use ``_miner_data_path(engine.mac, ...)``;
        per-platform files (profile, checkpoint, stock) use
        ``_miner_platform_path(engine.mac, engine.firmware_type, ...)``.
        Any ``_miner_data_path(engine.ip, ...)`` call would write to a stale
        IP-keyed path that the loader no longer reads — silently losing the
        miner's tuning data.
        """
        cmd = [
            "grep",
            "-rEn",
            r"_miner_data_path\(engine\.ip",
            "tuner_app/",
            "--include=*.py",
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, cwd=PROJECT_ROOT)
        self.assertEqual(
            result.stdout.strip(),
            "",
            f"Legacy ``_miner_data_path(engine.ip, ...)`` call found in tuner_app/:\n"
            f"{result.stdout}",
        )

    def test_no_miner_platform_path_with_engine_ip_in_tuner_app(self) -> None:
        """Per-platform file paths must use ``engine.mac``, never ``engine.ip``.

        Companion to ``test_no_miner_data_path_with_engine_ip_in_tuner_app``:
        ``_miner_platform_path`` is the per-platform variant (profile,
        checkpoint, stock). Same hazard — passing ``engine.ip`` instead of
        ``engine.mac`` writes to a stale path the loader won't find.
        """
        cmd = [
            "grep",
            "-rEn",
            r"_miner_platform_path\(engine\.ip",
            "tuner_app/",
            "--include=*.py",
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, cwd=PROJECT_ROOT)
        self.assertEqual(
            result.stdout.strip(),
            "",
            f"Legacy ``_miner_platform_path(engine.ip, ...)`` call found in tuner_app/:\n"
            f"{result.stdout}",
        )

    def test_effective_config_production_callers_use_mac(self) -> None:
        """Production ``EffectiveConfig(...)`` callers must pass a MAC, not an IP.

        The transitional adapter in ``EffectiveConfig.__init__`` accepts either
        an IPv4 address (reverse-lookup against ``MINER_CONFIGS[mac]['ip']``,
        with v3 fallback) or a canonical MAC / synth ID (direct lookup). The
        IP path exists for test fixtures that bypass migration; production code
        must always go through the MAC-keyed path so that DHCP IP changes don't
        require a stale-cache flush.

        After A7-A11 migrated all consumers to MAC, this audit grep enforces
        the invariant that ``tuner_app/`` callers pass a variable named
        ``mac``. State.py has a doc-comment example that's exempt — the grep
        excludes it.
        """
        cmd = [
            "grep",
            "-rEn",
            r"EffectiveConfig\(",
            "tuner_app/",
            "--include=*.py",
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, cwd=PROJECT_ROOT)
        # Filter out comment-only matches (lines starting with whitespace then '#')
        # and the EffectiveConfig class definition itself.
        offending = []
        for line in result.stdout.splitlines():
            # line shape: 'tuner_app/foo.py:42:    EffectiveConfig(...)
            try:
                _path, _lineno, code = line.split(":", 2)
            except ValueError:
                continue
            stripped = code.lstrip()
            # Skip comment-only mentions (doc strings / inline reference docs)
            if stripped.startswith("#"):
                continue
            # Skip the class definition / docstring imports in effective.py itself
            if "/config/effective.py" in line:
                continue
            # Acceptable shapes: EffectiveConfig(mac) and EffectiveConfig(self.mac).
            # Anything else (e.g., EffectiveConfig(ip), EffectiveConfig(some_var))
            # is a regression — we want a single canonical name so a future grep
            # stays simple and noise-free.
            if "EffectiveConfig(mac)" in code or "EffectiveConfig(self.mac)" in code:
                continue
            offending.append(line)
        self.assertEqual(
            offending,
            [],
            "Production EffectiveConfig(...) callers must use the canonical "
            "``mac`` argument name. Other names hide whether the value is an IP "
            "(legacy fallback) or a MAC (v4 direct lookup). Offending lines:\n"
            + "\n".join(offending),
        )

    # ─────────────────────────────────────────────────────────────────
    # Phase B / B14 — persistent multi-timeframe statistics invariants
    # ─────────────────────────────────────────────────────────────────

    def test_sqlite3_connect_only_in_metrics_store(self) -> None:
        """``sqlite3.connect`` calls must be confined to ``tuner_app/metrics/store.py``.

        The metrics module owns SQLite for the time-series tables.  Any other
        ``tuner_app/`` caller opening a sqlite connection has likely
        implemented a competing persistence path — that's a design smell and
        worth flagging at the audit-grep tier.
        """
        cmd = [
            "grep",
            "-rEn",
            r"sqlite3\.connect\(",
            "tuner_app/",
            "--include=*.py",
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, cwd=PROJECT_ROOT)
        offending = []
        for line in result.stdout.splitlines():
            if "tuner_app/metrics/store.py:" in line:
                continue
            offending.append(line)
        self.assertEqual(
            offending,
            [],
            "sqlite3.connect() must only appear in tuner_app/metrics/store.py:\n"
            + "\n".join(offending),
        )

    def test_metrics_store_uses_wal_journal_mode(self) -> None:
        """The metrics store must enable WAL journal mode.

        WAL is the load-bearing decision behind the concurrent-read invariant
        (B3): writers serialize on the application lock; readers run in
        parallel.  Removing the PRAGMA would silently regress to default
        rollback-journal mode where readers block writers.
        """
        store_py = PROJECT_ROOT / "tuner_app" / "metrics" / "store.py"
        content = store_py.read_text(encoding="utf-8")
        self.assertIn(
            "PRAGMA journal_mode=WAL",
            content,
            "tuner_app/metrics/store.py must enable WAL journal mode",
        )

    def test_record_sample_callsite_only_in_monitor(self) -> None:
        """``store.record_sample`` may only be called from the monitor cycle.

        The single-call-site rule keeps the metrics path simple: there is one
        path that decides when to record and one path that handles failure.
        Tests in tests/unit/ are excluded — they exercise the store directly
        via record_sample(...) by design.
        """
        cmd = [
            "grep",
            "-rEn",
            r"\.record_sample\(|store\.record_sample\(",
            "tuner_app/",
            "--include=*.py",
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, cwd=PROJECT_ROOT)
        offending = []
        for line in result.stdout.splitlines():
            # The definition itself in store.py is the canonical implementation;
            # the monitor.py helper is the one allowed call site.
            if "tuner_app/metrics/store.py:" in line:
                continue
            if "tuner_app/tuning_engine/monitor.py:" in line:
                continue
            offending.append(line)
        self.assertEqual(
            offending,
            [],
            "record_sample call sites outside tuner_app/tuning_engine/monitor.py "
            "(only the monitor cycle should record):\n" + "\n".join(offending),
        )

    def test_dashboard_html_has_metrics_range_selector(self) -> None:
        """B12: the dashboard shell must include a ``#metrics-range`` selector.

        The frontend's range-driven chart-replacement logic depends on the
        selector existing in the served HTML — if the markup gets stripped in
        a future restructure, the JS would silently no-op.
        """
        dashboard = PROJECT_ROOT / "tuner_app" / "static" / "dashboard.html"
        content = dashboard.read_text(encoding="utf-8")
        self.assertIn(
            'id="metrics-range"',
            content,
            "dashboard.html must contain the <select id='metrics-range'> element",
        )

    def test_no_chip_tune_comparison_bar_in_main_js(self) -> None:
        """The duplicate chip-tune comparison bar was removed; the top-3 results
        table below it is the canonical comparison surface."""
        cmd = [
            "grep",
            "-nE",
            "chip-tune-comparison-bar|comparisonCard",
            "tuner_app/static/js/main.js",
        ]
        result = subprocess.run(cmd, capture_output=True, cwd=PROJECT_ROOT)
        matches = result.stdout.decode()
        self.assertEqual(
            result.returncode,
            1,
            f"Stale chip-tune comparison bar references in main.js:\n{matches}",
        )

    def test_no_chip_tune_comparison_bar_in_detail_css(self) -> None:
        """The .chip-tune-comparison-bar CSS rule was deleted along with the bar itself."""
        cmd = [
            "grep",
            "-nE",
            "chip-tune-comparison-bar",
            "tuner_app/static/css/detail.css",
        ]
        result = subprocess.run(cmd, capture_output=True, cwd=PROJECT_ROOT)
        matches = result.stdout.decode()
        self.assertEqual(
            result.returncode,
            1,
            f"Stale chip-tune-comparison-bar CSS rule found:\n{matches}",
        )

    def test_no_profit_recompute_endpoints_in_routes_py(self) -> None:
        """The /tuner/profit/recompute_preview and /tuner/profit/apply endpoints
        were deleted when the operator-confirmed Recompute & Apply UI was retired.
        Auto-apply now happens inside /tuner/minerstat/fetch_now via the shared
        apply_profit_recompute helper."""
        cmd = [
            "grep",
            "-nE",
            "profit/recompute_preview|profit/apply|profit_recompute_preview|profit_apply",
            "tuner_app/http_server/routes.py",
            "tuner_app/http_server/handlers/minerstat_routes.py",
        ]
        result = subprocess.run(cmd, capture_output=True, cwd=PROJECT_ROOT)
        matches = result.stdout.decode()
        self.assertEqual(
            result.returncode,
            1,
            f"Stale profit recompute/apply endpoint references found:\n{matches}",
        )

    def test_no_recompute_handlers_in_main_js(self) -> None:
        """The frontend Recompute & Apply modal/handlers were deleted along with
        the backend endpoints. Auto-apply runs implicitly inside minerstatFetchNow."""
        cmd = [
            "grep",
            "-nE",
            "openProfitRecompute|applyProfitRecompute|showProfitPreviewModal",
            "tuner_app/static/js/main.js",
        ]
        result = subprocess.run(cmd, capture_output=True, cwd=PROJECT_ROOT)
        matches = result.stdout.decode()
        self.assertEqual(
            result.returncode,
            1,
            f"Stale recompute handler references in main.js:\n{matches}",
        )

    def test_no_recompute_button_in_dashboard_html(self) -> None:
        """The Recompute & Apply button was removed from the minerstat card."""
        cmd = [
            "grep",
            "-nE",
            "openProfitRecomputeModal|Recompute &amp; Apply|Recompute & Apply",
            "tuner_app/static/dashboard.html",
        ]
        result = subprocess.run(cmd, capture_output=True, cwd=PROJECT_ROOT)
        matches = result.stdout.decode()
        self.assertEqual(
            result.returncode,
            1,
            f"Stale Recompute & Apply button in dashboard.html:\n{matches}",
        )

    # ─────────────────────────────────────────────────────────────────
    # LuxOS live-validation invariants (May 2026 sweep)
    # Each grep locks in a wire-format or perf invariant verified on
    # Representative LUXminer 2026.4.3 protocol shape.
    # "Lessons from the LuxOS live-tune validation".
    # ─────────────────────────────────────────────────────────────────

    def test_no_per_chip_healthchipget_in_luxos(self) -> None:
        """``healthchipget(board, chip)`` form is forbidden — bulk-only.

        With the 1.0 s LuxOS connection rate gate, the per-chip form takes
        ~5.4 minutes per call (324 chips × 1 s). The bulk per-board form
        ``healthchipget(board)`` returns all chips on that board in one
        TCP round-trip, ~3 seconds total. Verified live on LUXminer
        2026.4.3 — board 0 returns 108 chip entries in CHIPS array.
        """
        cmd = [
            "grep",
            "-nE",
            r'send_cmd\("healthchipget", str\(.*\), str\(.*\)\)',
            "tuner_app/miner/luxos.py",
        ]
        result = subprocess.run(cmd, capture_output=True, cwd=PROJECT_ROOT)
        matches = result.stdout.decode()
        self.assertEqual(
            result.returncode,
            1,
            f"Per-chip ``healthchipget(b, c)`` call found — use bulk per-board "
            f"``healthchipget(b)`` only:\n{matches}",
        )

    def test_no_hardcoded_vendor_labels_in_capability_gated_paths(self) -> None:
        """Capability-gated tuning_engine code must not carry hardcoded vendor
        log prefixes.

        Once a code path is gated by ``has_external_power_limit()`` /
        ``supports_per_chip_tuning()`` / ``has_internal_perpetual_tune()``,
        a ``"Bixbit:"`` / ``"LuxOS:"`` etc. log prefix is dead noise — the
        ``firmware_type`` is already on every JSONL entry. ePIC's
        ``set_power_limit`` is a no-op, but Bixbit / LuxOS / Braiins all
        share the same gated branch; vendor labels confuse the operator.

        Vendor-scoped modules under ``tuning_engine/`` (e.g.,
        ``braiins_phases.py``) are exempt — their entire content is one
        vendor's algorithm, dispatched via ``tuning_strategy()``, and a
        vendor label there is documentation, not noise.
        """
        cmd = [
            "grep",
            "-rnE",
            r'"Bixbit:|"LuxOS:|"Braiins:|"ePIC:',
            "tuner_app/tuning_engine/",
            "--include=*.py",
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, cwd=PROJECT_ROOT)
        # Vendor-scoped modules are exempt — they implement one vendor's
        # entire algorithm and a vendor label there is informative.
        VENDOR_SCOPED = {"braiins_phases.py"}
        offending = []
        for line in result.stdout.splitlines():
            try:
                path, _, _ = line.split(":", 2)
            except ValueError:
                continue
            filename = path.rsplit("/", 1)[-1]
            if filename in VENDOR_SCOPED:
                continue
            offending.append(line)
        self.assertEqual(
            offending,
            [],
            "Hardcoded vendor labels in shared / capability-gated "
            "tuning_engine code:\n" + "\n".join(offending),
        )

    def test_no_legacy_chip_info_field_names_in_luxos(self) -> None:
        """LUXminer 2026.4.3 ``healthchipget`` chip entries do not have
        ``Score`` or ``Hash`` fields — those are guesses from the cgminer
        convention that don't survive contact with this firmware.
        Verified-live shape: ``{Board, Chip, ChipTemp, Frequency, GHS 1m,
        GHS 5m, GHS 15m, Healthy, BadHashCount, ReadErrors, ReadTimeouts,
        WriteErrors, IsChecking}``. Use ``GHS 5m`` for hashrate-per-chip
        and ``Healthy`` for dead-chip detection.
        """
        cmd = [
            "grep",
            "-nE",
            r'chip_info\["(Score|Hash)"\]|chip\["(Score|Hash)"\]',
            "tuner_app/miner/luxos.py",
        ]
        result = subprocess.run(cmd, capture_output=True, cwd=PROJECT_ROOT)
        matches = result.stdout.decode()
        self.assertEqual(
            result.returncode,
            1,
            f"Legacy chip-info field names (Score / Hash) used in luxos.py — "
            f'use "GHS 5m" / "Healthy" instead:\n{matches}',
        )

    def test_no_legacy_board_field_in_luxos_temps(self) -> None:
        """LUXminer 2026.4.3 ``temps`` board entries use ``ID`` as the index
        field, not ``Board``. Reading ``board_data["Board"]`` raises KeyError
        at the first Phase 2 stabilize tick.

        Docstring references (lines containing ``\\``board_data`` —
        markdown-style code spans inside docstrings explaining the historical
        bug) are exempt; only real subscript accesses are caught.
        """
        cmd = [
            "grep",
            "-nE",
            r'board_data\["Board"\]',
            "tuner_app/miner/luxos.py",
        ]
        result = subprocess.run(cmd, capture_output=True, cwd=PROJECT_ROOT)
        offending = []
        for line in result.stdout.decode().splitlines():
            # Skip docstring/markdown matches (literal wrapped in ``…``).
            if "``board_data" in line:
                continue
            offending.append(line)
        self.assertEqual(
            offending,
            [],
            'Legacy ``board_data["Board"]`` access in luxos.py — '
            'use ``board_data.get("ID", i)`` (post-2026.4.3 wire shape):\n' + "\n".join(offending),
        )

    def test_powertargetset_uses_keyvalue_param(self) -> None:
        """LuxOS ``powertargetset`` requires a ``power=<watts>`` key=value
        param, not a bare positional ``<watts>``. Bare-watts returns
        ``Invalid key/value format`` on LUXminer 2026.4.3.
        """
        cmd = [
            "grep",
            "-nE",
            r'send_cmd\("powertargetset", str\(int\(',
            "tuner_app/miner/luxos.py",
        ]
        result = subprocess.run(cmd, capture_output=True, cwd=PROJECT_ROOT)
        matches = result.stdout.decode()
        self.assertEqual(
            result.returncode,
            1,
            f"Bare-watts ``powertargetset`` form in luxos.py — use "
            f'f"power={{int(watts)}}" instead:\n{matches}',
        )

    def test_validate_config_firmware_type_uses_supported_firmware_types(self) -> None:
        """``validate_config`` must enumerate firmware_types via the registry,
        not via a hardcoded list.

        ``supported_firmware_types()`` reads from ``MINER_API_REGISTRY``,
        so adding a new vendor is one line in registry.py. A hardcoded
        list in validation.py would silently reject the new vendor with
        ``firmware_type must be one of ['epic', 'bixbit', ...]`` until
        the operator hand-edits validation.py to match.
        """
        validation_py = PROJECT_ROOT / "tuner_app" / "config" / "validation.py"
        content = validation_py.read_text(encoding="utf-8")
        self.assertIn(
            "from tuner_app.miner.registry import supported_firmware_types",
            content,
            "validation.py must import supported_firmware_types from tuner_app.miner.registry",
        )
        # No hardcoded full-set firmware_type list — the registry is the source of truth.
        # Match a 4-element list/tuple of vendor strings (any of the known set).
        import re

        # Pattern: a list/tuple literal containing all four current vendor names.
        # Triggers if someone replaces ``in supported_firmware_types()`` with
        # ``in ['epic', 'bixbit', 'luxos', 'braiins']``.
        offending = re.findall(
            r"""\[\s*['"](?:epic|bixbit|luxos|braiins)['"]\s*,"""
            r"""\s*['"](?:epic|bixbit|luxos|braiins)['"]\s*,"""
            r"""\s*['"](?:epic|bixbit|luxos|braiins)['"]\s*,"""
            r"""\s*['"](?:epic|bixbit|luxos|braiins)['"]\s*\]""",
            content,
        )
        self.assertEqual(
            offending,
            [],
            "Hardcoded firmware_type list in validation.py — use "
            f"supported_firmware_types() from the registry. Found:\n{offending}",
        )

    def test_phase_pill_stopped_red_present(self) -> None:
        css_path = PROJECT_ROOT / "tuner_app" / "static" / "css" / "overview.css"
        content = css_path.read_text(encoding="utf-8")
        pattern = (
            r"\.phase-pill\.stopped\s*\{[^}]*"
            r"rgba\(244,\s*67,\s*54,\s*0\.18\)[^}]*"
            r"var\(--red\)[^}]*\}"
        )
        self.assertRegex(content, pattern)

    def test_phase_pill_stopping_amber_present(self) -> None:
        css_path = PROJECT_ROOT / "tuner_app" / "static" / "css" / "overview.css"
        content = css_path.read_text(encoding="utf-8")
        pattern = (
            r"\.phase-pill\.stopping\s*\{[^}]*"
            r"rgba\(255,\s*193,\s*7,\s*0\.18\)[^}]*"
            r"var\(--yellow\)[^}]*\}"
        )
        self.assertRegex(content, pattern)

    def test_phase_pill_idle_muted_present(self) -> None:
        css_path = PROJECT_ROOT / "tuner_app" / "static" / "css" / "overview.css"
        content = css_path.read_text(encoding="utf-8")
        pattern = r"\.phase-pill\.idle\s*\{"
        self.assertRegex(content, pattern)

    def test_detail_tuner_bucket_span_present_in_dashboard_html(self) -> None:
        html_path = PROJECT_ROOT / "tuner_app" / "static" / "dashboard.html"
        content = html_path.read_text(encoding="utf-8")
        pattern = r'<span\s+id=["\']detail-tuner-bucket["\']'
        self.assertRegex(content, pattern)

    def test_main_js_updates_detail_tuner_bucket_element(self) -> None:
        js_path = PROJECT_ROOT / "tuner_app" / "static" / "js" / "main.js"
        content = js_path.read_text(encoding="utf-8")
        # Check for the element ID reference
        self.assertRegex(content, r"detail-tuner-bucket")
        # Check for tuner_bucket reference within proximity (same logical block;
        # \s\S allows matching across newlines/semicolons since the new code spans
        # multiple lines).
        self.assertRegex(
            content,
            r"detail-tuner-bucket[\s\S]{0,600}(?:s\.tuner_bucket|tuner_bucket)",
        )

    def test_phase_pill_for_still_reads_tuner_bucket_in_main_js(self) -> None:
        js_path = PROJECT_ROOT / "tuner_app" / "static" / "js" / "main.js"
        content = js_path.read_text(encoding="utf-8")
        pattern = r"phasePillFor[^}]*m\.tuner_bucket"
        self.assertRegex(content, pattern)

    def test_no_whatsminer_pl_max_w_references(self) -> None:
        """WHATSMINER_PL_MAX_W was dropped in favor of POWER_LIMIT_W as the
        grid-search power axis upper bound. Any new reference is a regression.
        """
        cmd = [
            "grep",
            "-rE",
            "--exclude=test_audit_greps.py",
            "--exclude-dir=__pycache__",
            "WHATSMINER_PL_MAX_W",
            "tuner_app/",
            "tuner_app/static/",
            "tests/",
        ]
        result = subprocess.run(cmd, capture_output=True, cwd=PROJECT_ROOT)
        matches = result.stdout.decode()
        self.assertEqual(
            result.returncode,
            1,
            f"WHATSMINER_PL_MAX_W references still exist:\n{matches}",
        )
        self.assertEqual(matches, "", "Expected empty stdout for WHATSMINER_PL_MAX_W grep")

    def test_voltage_chip_tune_strategy_gate_on_vf_perpetual_settle_categories(self) -> None:
        """All keys in the Voltage Settle, V/F Exploration, and Perpetual Tune
        categories must have `requires: 'voltage_chip_tune_strategy'` in their
        CFG_META entry — the Whatsminer/Braiins miners receive the disabled
        vendorMismatch tooltip for these knobs on the per-miner config tab.
        """
        import re

        main_js = (PROJECT_ROOT / "tuner_app" / "static" / "js" / "main.js").read_text(
            encoding="utf-8"
        )
        target_categories = {
            "Voltage Settle",
            "V/F Exploration (dynamic state machine)",
            "Perpetual Tune",
        }
        for cat_name in target_categories:
            # Find the CONFIG_CATEGORIES entry for this category. Pattern:
            #   {name:'<cat_name>', ..., keys:['KEY1','KEY2',...]},
            pat = r"\{\s*name\s*:\s*'" + re.escape(cat_name) + r"'[^}]*keys\s*:\s*\[([^\]]+)\]"
            m = re.search(pat, main_js)
            self.assertIsNotNone(m, f"Category {cat_name!r} not found in CONFIG_CATEGORIES")
            keys_blob = m.group(1)
            keys = re.findall(r"'([A-Z_]+)'", keys_blob)
            self.assertGreater(len(keys), 0, f"No keys parsed from category {cat_name!r}")
            for key in keys:
                entry_pat = r"\b" + re.escape(key) + r"\s*:\s*\{([^}]*)\}"
                entry_m = re.search(entry_pat, main_js)
                self.assertIsNotNone(
                    entry_m,
                    f"CFG_META entry for {key} (category {cat_name}) not found in main.js",
                )
                entry_body = entry_m.group(1)
                self.assertIn(
                    "requires: 'voltage_chip_tune_strategy'",
                    entry_body,
                    f"Category {cat_name!r} key {key} is missing "
                    f"`requires: 'voltage_chip_tune_strategy'` in its CFG_META entry:\n"
                    f"{entry_body}",
                )
