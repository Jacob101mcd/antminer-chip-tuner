from __future__ import annotations

from tuner_app.http_server.handlers import (
    auth_routes,
    bulk_routes,
    control_routes,
    metrics_routes,
    miners_routes,
    minerstat_routes,
    mrr_routes,
    scanner_routes,
    static_routes,
    status_routes,
)

# Exact-match routes for GET. Maps `path` (no query string) to a (handler, body) -> None callable.
ROUTES_GET = {
    "/tuner/scanner/status": scanner_routes.scanner_status,
    "/": static_routes.serve_dashboard,
    "/index.html": static_routes.serve_dashboard,
    "/tuner/auth/status": auth_routes.auth_status,
    "/tuner/firmware_types": status_routes.firmware_types,
    "/tuner/status": status_routes.status,
    "/tuner/overview": status_routes.overview,
    "/tuner/config": status_routes.config,
    "/tuner/minerstat/snapshot": status_routes.minerstat_snapshot,
    "/tuner/mrr/whoami": mrr_routes.whoami,
    "/tuner/mrr/rigs": mrr_routes.rigs,
    "/tuner/mrr/rental_status": mrr_routes.rental_status,
}

# Prefix-match routes for GET. List of (prefix, handler) tuples — first match wins.
ROUTES_GET_PREFIX = [
    ("/tuner/live/", status_routes.live),
    ("/tuner/log/", status_routes.log),
    ("/tuner/export/", status_routes.export),
    ("/tuner/metrics/", metrics_routes.metrics),
    # Phase 7: serve the static asset tree (CSS, JS modules, vendored Chart.js)
    # alongside the dashboard shell at /. Prefix-matched so anything under
    # /static/<subdir>/... is fielded by serve_static.
    ("/static/", static_routes.serve_static),
]

# Exact-match routes for POST. Same callable shape.
ROUTES_POST = {
    "/tuner/login": auth_routes.login,
    "/tuner/logout": auth_routes.logout,
    "/tuner/setup": auth_routes.setup,
    "/tuner/start": control_routes.start,
    "/tuner/stop": control_routes.stop,
    "/tuner/delete_profile": control_routes.delete_profile,
    "/tuner/reset_stock": control_routes.reset_stock,
    "/tuner/retune_voltage": control_routes.retune_voltage,
    "/tuner/select_voltage_profile": control_routes.select_voltage_profile,
    "/tuner/remeasure_cell": control_routes.remeasure_cell,
    "/tuner/remeasure_queue/clear": control_routes.remeasure_queue_clear,
    "/tuner/remeasure_queue/process": control_routes.remeasure_queue_process,
    "/tuner/bulk/start": bulk_routes.bulk_start,
    "/tuner/bulk/stop": bulk_routes.bulk_stop,
    "/tuner/bulk/reset_profile": bulk_routes.bulk_reset_profile,
    "/tuner/bulk/apply_config": bulk_routes.bulk_apply_config,
    "/tuner/bulk/pools": bulk_routes.bulk_pools,
    "/tuner/bulk/remove": bulk_routes.bulk_remove,
    "/tuner/bulk/start_mining": bulk_routes.bulk_start_mining,
    "/tuner/bulk/stop_mining": bulk_routes.bulk_stop_mining,
    "/tuner/bulk/reboot": bulk_routes.bulk_reboot,
    "/tuner/bulk/set_power_limit": bulk_routes.bulk_set_power_limit,
    "/tuner/bulk/mrr_resync": bulk_routes.bulk_mrr_resync,
    "/tuner/bulk/retune_voltage": bulk_routes.bulk_retune_voltage,
    "/tuner/scanner/scan_now": scanner_routes.scanner_scan_now,
    "/tuner/scanner/stop": scanner_routes.scanner_stop,
    "/tuner/miners/remove": miners_routes.remove_miner,
    "/tuner/miners/set_mac": miners_routes.set_mac,
    "/tuner/config/defaults": miners_routes.config_defaults,
    "/tuner/config/fleet_ops": miners_routes.config_fleet_ops,
    "/tuner/mrr/resync": mrr_routes.resync,
    "/tuner/minerstat/fetch_now": minerstat_routes.fetch_now,
}

# Prefix-match routes for POST.
ROUTES_POST_PREFIX = [
    ("/tuner/config/miner/", miners_routes.config_miner),
]
