# Platform Support Matrix

The current application recognizes five firmware families using its schema-v4
platform registry. Every adapter is
experimental: a protocol implementation and automated tests do not guarantee
that an arbitrary model, PSU, hashboard, or firmware build is safe.

The project is independent and is not endorsed by Bitmain, MicroBT, ePIC,
Luxor, Braiins, or Bixbit.

## Capability matrix

| Platform key | Firmware / hardware scope | Strategy | Per-chip control | External power target | Internal autotune | Current evidence |
|---|---|---|---:|---:|---:|---|
| `epic` | ePIC PowerPlay-BMS on compatible Antminer hardware; S21 lineage | voltage and frequency search | Yes | No | No | automated coverage and live S21 project lineage |
| `luxos` | LuxOS / LUXminer on compatible Antminer hardware | voltage and frequency search | Yes | Yes | Yes | automated coverage and live S21 protocol validation |
| `braiins` | Braiins OS on compatible Antminer hardware | wattage binary search | No | Yes | Yes | automated adapter and strategy coverage; broader hardware validation needed |
| `bixbit` | Bixbit firmware on compatible Whatsminer hardware | direct-voltage tuning blocked pending live PSU bounds | No | Yes | Yes | read/control adapter coverage; voltage writes fail closed |
| `whatsminer` | stock MicroBT btminer firmware exposing the required token/encrypted command set | power-limit/frequency grid | No | Yes | Yes | deterministic auth fixtures and automated adapter/integration coverage; broader hardware validation needed |

“Internal autotune” means the firmware owns some or all low-level V/F behavior.
The tuner does not fake per-chip controls where a platform does not expose them.

## Voltage-bound provenance gate

Any strategy that issues direct PSU voltage commands must first obtain a sane minimum
and maximum from the connected firmware during Phase 0. ePIC derives these bounds from
`capabilities.Psu Info`; LuxOS derives them from its `limits` command. Missing,
non-numeric, inverted, or implausibly small values produce an informational static
fallback that is explicitly marked unverified and cannot authorize a voltage write.

Bixbit currently has no validated live-bound source in this adapter, so its direct
voltage strategy stops before changing pools, perpetual-tune state, frequency, or
voltage. Braiins OS and stock Whatsminer remain usable because their strategies leave
voltage selection to firmware and search power/frequency controls instead. There is no
unsafe environment-variable override for this gate.

## Protocol summary

| Platform | Transport | Scanner fingerprint | Important limitation |
|---|---|---|---|
| ePIC | HTTP JSON | `/summary` with ePIC operating-state shape | Direct V/F commands require firmware password; hardware bounds must be confirmed |
| LuxOS | TCP JSON, cgminer-derived | `version` response containing `LUXminer` | Mutations use a serialized login/command/logout session and are rate-gated |
| Braiins OS | HTTP REST with bearer token | `/api/v1/version/` semantic version shape | Commonly uses a different API port; V/F remains owned by Braiins AutoTune |
| Bixbit | TCP JSON | Bixbit status/summary shape | No validated firmware-reported PSU bounds; voltage writes are refused |
| stock Whatsminer | TCP JSON with token-derived encrypted writes | stock btminer version/token shape | No direct voltage, board-frequency, or chip-frequency controls |

## Before declaring a combination validated

A model/firmware combination should not be described as validated until all of
the following have been recorded with sanitized evidence:

1. Scanner detection, authentication, model, MAC, and topology are correct.
2. Read-only summary, power, temperature, fan, and hashrate values agree with
   the vendor UI or another trusted source.
3. Start, stop, reboot, and reset behavior have safe failure handling.
4. Every command used by its tuning strategy respects device and configured
   bounds, including interrupted or timed-out requests.
5. Checkpoint resume and profile reset have been exercised after a process and
   miner restart.
6. Thermal throttling and offline recovery have been tested without disabling
   independent firmware protections.
7. A supervised full tuning cycle and an extended steady-state soak complete
   without unsafe temperatures, reboot loops, or telemetry loss.

An observation on one unit applies only to that model, hashboard, PSU, firmware
build, and environment. Report new validation through a GitHub issue without
publishing deployment identifiers or secrets.

## Known fleet constraints

- Scanner ranges are opt-in; an empty range scans nothing.
- The scanner and all registered miners share one fleet-wide `API_PORT`. Do not
  combine platforms that require different API ports in one installation.
- Braiins OS commonly serves its API on port 80, while other supported
  firmwares commonly use port 4028. Run separate instances with distinct
  `ASIC_TUNER_DATA_DIR` and `TUNER_PORT` values when ports differ.
- MAC is the stable v4 identity. A `syn-...` fallback works, but should be
  replaced with the physical MAC before relying on DHCP-change recovery.
- Stock Bitmain firmware is not an implemented platform. Compatible Antminer
  hardware must run one of the supported alternative firmware families above.
- The project does not install, update, recover, or roll back miner firmware.

## Quick references

- [ePIC](epic/quickref.md)
- [LuxOS](luxos/quickref.md)
- [Braiins OS](braiins/quickref.md)
- [Bixbit](bixbit/quickref.md)
- [Stock Whatsminer](whatsminer/quickref.md)
