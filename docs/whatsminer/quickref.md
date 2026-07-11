# Stock Whatsminer Quick Reference

Platform key: `whatsminer`  
Transport: btminer JSON over TCP  
Strategy: power-limit and target-frequency grid search

Stock Whatsminer read commands are plaintext. Mutating commands use a fresh
token/salt from the miner and AES-256-ECB request encryption; this is why the
Python `cryptography` package is a required dependency. Token-expiration errors
trigger one controlled refresh and retry.

| Tuner operation | Whatsminer command | Notes |
|---|---|---|
| Summary | `summary`, plus best-effort `devs`, `get_version`, `get_miner_info` | Supports wrapped, cgminer-style, and flat response shapes |
| Authenticate | `get_version`, then `get_token` | Confirms both reachability and encrypted-write setup |
| Topology | `devs` | Falls back conservatively when board data is unavailable |
| Set power target | encrypted `adjust_power_limit` | Primary absolute control in watts |
| Set target frequency | encrypted `set_target_freq` | Firmware percentage control, not an absolute MHz clock |
| Adjust ramp | encrypted `adjust_upfreq_speed` | Controls firmware frequency-ramp behavior |
| Power mode | encrypted low/normal/high mode command | Optional firmware mode control |
| Start / stop | encrypted `start_mining` / `stop_mining` | — |
| Reboot | encrypted `reboot` | Executes immediately; generic delay is ignored |

Direct voltage, global/board/chip MHz clocks, per-chip telemetry, and coin
switching are not exposed by this adapter. Those operations deliberately fail
instead of being approximated.

## Protocol security limitation

This adapter implements MicroBT's stock privilege API v2 wire protocol, which
mandates an MD5-crypt-derived challenge response and AES-256-ECB for writable
commands. Those primitives are compatibility requirements, not general-purpose
cryptography, and cannot be replaced without breaking the protocol. ECB lacks
modern confidentiality and integrity guarantees. Keep TCP 4028 reachable only
from a segmented, trusted management network, use a unique non-default miner
password where supported, and never reuse that password for another service.
See the [security policy](../../SECURITY.md#protocol-mandated-legacy-cryptography)
and the [official MicroBT v2.0.4 specification](https://www.whatsminer.com/file/WhatsminerAPI%20V2.0.4.pdf).

The scanner fingerprints stock btminer before Bixbit and obtains a stable MAC
from miner info, ARP, or a synthetic fallback. Passwords are tried only from the
operator-configured scanner list. Never publish token/auth vectors generated
from a real device password; committed fixtures must be synthetic and
reproducible.
