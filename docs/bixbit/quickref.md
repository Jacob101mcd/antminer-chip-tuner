# Bixbit Quick Reference

Platform key: `bixbit`  
Transport: JSON over TCP  
Strategy: adapter present; direct-voltage exploration blocked pending live PSU bounds

| Tuner operation | Bixbit command | Notes |
|---|---|---|
| Summary | `summary` | Normalized into the common fleet summary |
| Firmware information | `get_firmware_version` | Used as capability context |
| Topology | `get_board_slots_state` | Board count only; chip-level arrays are unavailable |
| Read V/F settings | `get_overclock_info` | Provides whole-miner target information |
| Set voltage | `set_overclock_info` with `voltage_target` | Wire command exists, but the tuner refuses it because this adapter cannot yet verify live PSU min/max bounds |
| Set frequency | `set_overclock_info` with `freq_target` | Whole-miner absolute target |
| Set power target | `set_user_power_limit` | Normal mode with a soft restart request |
| Start / stop | `power_on` / `power_off` | Normal mining-state controls |
| Reboot | `reboot` | Executes immediately |

Per-board and per-chip clocks, per-chip temperature/hashrate arrays, and coin
switching are not supported. Bixbit's own up-frequency/profile system is treated
as the internal perpetual tuner.

The static 11877-15182 mV values returned with topology are an informational
fallback, not evidence about the connected PSU. They are marked unverified. Phase 0
therefore stops before pool, perpetual-tune, frequency, or voltage mutations, and a
direct adapter voltage call also fails closed. Add a validated firmware-reported
minimum/maximum source before enabling this strategy; there is no unsafe bypass.

The scanner runs this fingerprint only after stock Whatsminer does not match.
The current connectivity check cannot prove every Bixbit firmware's password
semantics, so confirm authorization and command behavior on a supervised unit
before enabling tuning.
