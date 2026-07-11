# Antminer Chip Tuner

Antminer Chip Tuner is a local, multi-miner tuning service and dashboard for
supported Antminer and Whatsminer firmware. It discovers miners on operator-selected
network ranges, selects a tuning strategy from each miner's capabilities, records
profiles and checkpoints, and keeps monitoring after the initial search completes.

The v4 data model uses each miner's MAC address as its stable identity. A DHCP address
may change without losing the miner's configuration or tuning history, and profiles are
kept separately when the same physical miner is reflashed to a different firmware.

> [!CAUTION]
> This software changes ASIC voltage, frequency, and power settings. Bad settings,
> inadequate cooling, firmware defects, or interrupted commands can damage hardware,
> cause downtime, or create a fire risk. Start conservatively, supervise the first
> complete tuning cycle, and never defeat the miner's hardware or firmware safeguards.
> You assume all risk. See [Safety](#safety) and the project license.

## Platform support

The platform registry currently contains five firmware families:

| Platform key | Firmware family | Control strategy |
|---|---|---|
| `epic` | ePIC PowerPlay-BMS on Antminer hardware | voltage sweep with per-chip frequency search |
| `luxos` | LuxOS / LUXminer on Antminer hardware | voltage sweep with per-chip frequency search |
| `braiins` | Braiins OS on Antminer hardware | wattage search while Braiins AutoTune owns V/F |
| `bixbit` | Bixbit firmware on Whatsminer hardware | adapter available; direct-voltage tuning blocked until firmware-reported PSU bounds can be validated |
| `whatsminer` | Stock MicroBT Whatsminer firmware | power-limit and frequency-grid search |

Support is capability-based, not a promise that every model and firmware release is
safe. Read the [support matrix](docs/support-matrix.md) before connecting hardware.

## Highlights

- Scanner-driven discovery with CIDR, address-range, and blacklist support
- Stable MAC-keyed fleet state with automatic IP refresh after DHCP changes
- Firmware-specific v4 defaults and per-miner overrides
- Per-chip tuning where the firmware exposes safe controls
- Power-target searches for firmware that owns its internal V/F curve
- Checkpoints, saved profiles, stock baselines, recovery, and continuous monitoring
- Authenticated local dashboard with fleet and per-miner views
- SQLite metrics retention plus per-miner JSONL tuning logs
- Optional Minerstat profitability calculations and MiningRigRentals synchronization
- Chart.js bundled locally; the dashboard does not need a CDN or internet access

## Requirements

- Python 3.11 or newer
- A host that can reach the miners' management APIs
- Miner credentials and an explicitly selected private network range to scan
- Adequate cooling, power delivery, and physical supervision

Required Python dependencies include `cryptography`, used for encrypted writes to
stock Whatsminer APIs, and `platformdirs`, used to select the per-user data directory.
They are installed automatically with the project. Those Whatsminer writes implement
a vendor-mandated legacy protocol; review the [protocol security limitations](SECURITY.md#protocol-mandated-legacy-cryptography)
before enabling control on a miner management network.

## Quick start

```bash
git clone https://github.com/Jacob101mcd/antminer-chip-tuner.git
cd antminer-chip-tuner

python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .

python -m tuner_app
```

On Windows PowerShell, activate the environment with
`.venv\Scripts\Activate.ps1`. The compatibility launcher `python tuner.py` and
the included `tuner-start.bat` launcher remain available for a checkout whose
`.venv` was created as shown above.

Open <http://127.0.0.1:8099>. On the first visit:

1. Create the dashboard password.
2. Open **Network & Scanner** from the dashboard settings.
3. Enter only the CIDR blocks or address ranges that you are authorized to probe,
   such as `192.0.2.0/28` for documentation or your real private LAN range locally.
4. Enter the miner firmware passwords to try, in order. Do not commit them.
5. Save, then choose **Scan now** to review what the configured range discovers.
6. When the range and credentials are correct, explicitly enable **Auto-register
   discovered miners**, save, and scan again. This setting is off by default.
7. Confirm each registered firmware type and hardware model before starting a tune.

The scanner fingerprints ePIC, stock Whatsminer, Bixbit, LuxOS, and Braiins in that
order. A detected miner is registered only when auto-registration is enabled. If a
vendor API and the local ARP table cannot provide a MAC, v4 creates a `syn-...`
identifier; replace it with the chassis MAC in the dashboard when possible.

### Dashboard binding

The dashboard binds to `127.0.0.1:8099` by default. Change the port with
`TUNER_PORT`. To serve the dashboard on a trusted LAN, first complete password setup
on loopback, stop the process, and then deliberately set a non-loopback host:

```bash
TUNER_HOST=0.0.0.0 TUNER_PORT=8099 python -m tuner_app
```

PowerShell uses `$env:TUNER_HOST = "0.0.0.0"`. Startup fails closed if a non-loopback
bind is requested before dashboard authentication has been configured. Authentication
does not replace a firewall, network segmentation, or TLS termination; do not expose
the built-in HTTP server directly to the internet.

Requests accept `Host: localhost`, loopback/private IP literals, and exact local DNS
names listed in the comma-separated `ANTMINER_TUNER_ALLOWED_HOSTS` environment variable.
For example, set it to `tuner.home.arpa` before browsing to that name. Wildcards are not
supported, and first-run password setup still requires both a loopback client and a
loopback `Host` value.

When a trusted reverse proxy is necessary, list only its IPs or CIDRs in
`ANTMINER_TUNER_TRUSTED_PROXIES`. Forwarded client addresses are ignored for every other
peer. The proxy must replace, not append to untrusted, incoming `X-Forwarded-For` data;
set `ANTMINER_TUNER_SECURE_COOKIES=1` when the browser-facing proxy connection is
always HTTPS. See [SECURITY.md](SECURITY.md) before enabling these options.

## How v4 identifies and stores miners

IP addresses are connection details. Canonical MAC addresses are identities.

- `<data-dir>/config.json` stores schema version 4, platform defaults, fleet
  operations, MAC-keyed miner records, and dashboard authentication state.
- `<data-dir>/{mac}.{firmware}.profile.json` stores the selected profile.
- `<data-dir>/{mac}.{firmware}.checkpoint.json` stores resumable in-flight work.
- `<data-dir>/{mac}.{firmware}.stock.json` stores the firmware-specific baseline.
- `<data-dir>/{mac}.log.jsonl` keeps a cross-firmware event history.
- `<data-dir>/metrics.db` stores fleet metrics in SQLite.

By default, `<data-dir>` is the operating system's per-user application-data directory
for `antminer-chip-tuner`. Set `ASIC_TUNER_DATA_DIR` to give each instance an explicit
private directory; for example, `ASIC_TUNER_DATA_DIR=/srv/tuner-braiins/data`. A local
`tuning_data/` override is ignored by this repository's `.gitignore`, but directories
elsewhere are not. Colons in MAC addresses are written as dashes in filenames. The
data can contain passwords, API credentials, device identifiers, logs, and operational
details, so restrict access and back it up privately.

The v4 REST API follows the same rule. Per-miner routes use `{mac}` path segments,
for example:

```text
GET  /tuner/live/aa-bb-cc-dd-ee-ff
GET  /tuner/log/aa-bb-cc-dd-ee-ff
GET  /tuner/export/aa-bb-cc-dd-ee-ff
GET  /tuner/metrics/aa-bb-cc-dd-ee-ff
POST /tuner/config/miner/aa-bb-cc-dd-ee-ff
```

Control requests and bulk requests likewise use `mac` or `macs`. Treat identifiers
returned by the scanner as opaque: a synthetic ID is valid until a real MAC becomes
available. All tuner routes except setup/login, the dashboard shell, static assets,
and the firmware-type list require a session cookie.

## Tuning strategies

### Voltage and frequency search

On firmware with direct V/F controls, the engine captures a stock baseline, explores
voltage/frequency cells, profiles chip or whole-miner stability according to available
capabilities, measures efficiency or profitability, saves the winning profile, and
enters a monitoring/perpetual-adjustment loop. Voltage and frequency transitions are
ordered and rate-limited, and recovery checkpoints are written between long-running
steps.

### Firmware-owned tuning

Braiins OS retains ownership of its internal voltage/frequency curve. The project
searches an operator-bounded wattage range and evaluates the resulting hashrate and
efficiency rather than issuing per-chip clocks.

Stock Whatsminer uses a power-limit/frequency search based on the controls exposed by
btminer firmware. The Bixbit adapter exposes whole-miner frequency and power controls,
but its current API integration does not provide validated live PSU minimum/maximum
voltage. The tuner therefore refuses Bixbit direct-voltage tuning; static S21-style
fallback values are informational only and never authorize a voltage write.

## Safety

- Verify the detected model, firmware, board count, voltage bounds, and temperature
  readings before pressing **Start Tuning**.
- Direct-voltage strategies fail closed unless the connected firmware reports a sane
  live PSU minimum and maximum. ePIC and LuxOS mark bounds verified only after their
  firmware responses pass validation. A static/spec fallback is never treated as
  permission to change voltage; there is no environment-variable bypass.
- Keep factory over-temperature shutdowns and fan controls enabled.
- Use conservative voltage, power, and thermal limits for an unvalidated model.
- The initial 82 °C board and 97 °C chip thresholds are conservative project
  defaults, not model specifications; lower them if the vendor limits require it.
- Supervise the first tuning run and at least one steady-state monitoring interval.
- Stop immediately for unstable power, repeated reboots, abnormal fan behavior,
  missing temperature telemetry, smoke, odor, or unexpected heat.
- Use **Stop** before maintenance. If a saved profile causes repeated failures, use
  **Reset Profile** and restore known-safe miner settings through the firmware UI.
- Never scan networks or operate devices without authorization.

Software checks are defense in depth, not a substitute for the miner's independent
thermal protection, appropriate over-current protection, ventilation, and an operator
who can remove power.

## Configuration and integrations

The dashboard is the supported configuration surface. Fleet operations include scan
ranges, scan credentials, API port, metrics retention, Minerstat settings, and MRR
credentials. Tuning defaults are separated by platform; per-miner settings are stored
under the miner's MAC and current firmware.

The scanner uses one fleet-wide `API_PORT`. A single instance therefore cannot manage
a mixed fleet whose firmware APIs require different ports. Braiins commonly uses an
HTTP port different from the TCP/HTTP port used by other platforms; run separate
instances with distinct `ASIC_TUNER_DATA_DIR` and `TUNER_PORT` values.

Minerstat and MiningRigRentals are disabled unless configured. Their management APIs
use HTTPS. Enabling MRR pool synchronization can also configure supported miners to
send mining traffic and a rig-specific login to MRR's Stratum-over-TCP endpoints. See
[PRIVACY.md](PRIVACY.md).

## Development

```bash
python -m pip install -e ".[dev,build]"
ruff check .
ruff format --check .
pytest -q
python -m build
```

See [CONTRIBUTING.md](CONTRIBUTING.md) before opening a pull request. Useful technical
references include:

- [Platform support matrix](docs/support-matrix.md)
- [ePIC quick reference](docs/epic/quickref.md)
- [LuxOS quick reference](docs/luxos/quickref.md)
- [Braiins OS quick reference](docs/braiins/quickref.md)
- [Bixbit quick reference](docs/bixbit/quickref.md)
- [Stock Whatsminer quick reference](docs/whatsminer/quickref.md)
- [Project and research provenance](docs/provenance.md)

## Research provenance

Antminer Chip Tuner is created and maintained by
[Jacob McDaniel](https://github.com/Jacob101mcd). Its tuning methodology grew from
McDaniel's 2024 University of Virginia independent research on Antminer S19 tuning and
was subsequently redesigned for multiple firmware families, persistent fleet
operation, and capability-driven control.

The repository intentionally preserves the public historical materials in
[`Independent-research-main/`](Independent-research-main/README.md). They are research
provenance, not the supported application or current operating instructions.

## License

Copyright 2024-2026 Jacob McDaniel.

Licensed under the [Apache License 2.0](LICENSE). Bundled third-party software retains
its own license; see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) and [NOTICE](NOTICE).
