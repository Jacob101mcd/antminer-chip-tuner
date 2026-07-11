# LuxOS / LUXminer Quick Reference

Platform key: `luxos`  
Transport: cgminer-style JSON over TCP  
Strategy: voltage search with per-chip frequency tuning

Read commands do not require a session. Mutating commands are serialized as a
`logon` → command → `logoff` transaction. The transport rate-gates connection
attempts and backs off after a refused connection to avoid overwhelming miner
firmware.

| Tuner operation | LuxOS command(s) | Notes |
|---|---|---|
| Summary | `summary`, `version`, `stats`, `tunerstatus`, `fans`, `config`, `power`, `voltageget` | Required summary plus best-effort auxiliary fields |
| Lightweight liveness | `summary` | Used in recovery loops to avoid a multi-command burst |
| Topology / limits | `limits`, `frequencyget`, `devdetails` | Validated and normalized into common hardware bounds |
| Per-board/chip telemetry | `temps`, `healthchipget`, `frequencyget` | `healthchipget` is fetched once per board, not once per chip |
| Set voltage | `voltageset` | Value is snapped to the firmware voltage grid |
| Set frequency | `frequencyset` | Supports all-board, board, and chip forms |
| Firmware autotune | `atmset`, `autotunerset` | Both controls are toggled together |
| Power target | `powertargetset` | Parameter must use `power=<watts>` form |
| Pools | `pools`, `addpool`, `switchpool` | Adds missing pools and switches to the selected entry |
| Start / stop | `curtail wakeup` / `curtail sleep` | Session-required |
| Reboot | `rebootdevice` | Generic delay argument is ignored |

`temps` uses the response's board `ID`. Intake and exhaust positions are mapped
from the response metadata rather than assumed from their numeric ordering.
Per-chip temperature and hashrate come from the bulk `healthchipget(board)`
response.

The scanner identifies LuxOS through the `LUXminer` version field, validates a
password with a short session, and reads `config` for the MAC when available.
Never assume that firmware-reported limits are safe for a particular PSU or
hashboard; compare them with the physical model before tuning.

