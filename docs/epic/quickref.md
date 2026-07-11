# ePIC PowerPlay-BMS Quick Reference

Platform key: `epic`  
Transport: HTTP JSON  
Strategy: voltage search with per-chip frequency tuning

| Tuner operation | ePIC endpoint | Notes |
|---|---|---|
| Summary | `GET /summary` | Operating state, hashboards, power, fans, and hostname |
| Stable identity | `GET /network` | MAC is read from `dhcp.mac_address` when available |
| Hardware bounds | `GET /capabilities` | Model, board topology, and PSU information are cached |
| Clock telemetry | `GET /clocks` | Per-chip MHz arrays by board |
| Temperature | `GET /temps`, `GET /temps/chip` | Board inlet/outlet plus per-chip values |
| Chip health/hashrate | `GET /hashrate` | Per-chip health and hashrate arrays |
| Voltage | `GET /voltages`, `POST /tune/voltage` | Millivolt control |
| Frequency | `POST /tune/clock/all`, `/board`, `/chip` | Whole-miner, board, and chip forms |
| Firmware perpetual tune | `GET/POST /perpetualtune` | Disabled or restored as required by the strategy |
| Pools / coin | `POST /coin` | Up to the firmware-supported stratum entries |
| Start / stop | `POST /miner` | Uses firmware mining-state values |
| Reboot | `POST /reboot` | Optional firmware delay value |

Mutating requests carry the configured miner password and a `param` payload.
The scanner identifies ePIC from the operating-state shape in `/summary`, reads
the MAC from `/network`, and validates candidate passwords with a safe read-style
command before registration.

Do not assume that every firmware build reports trustworthy PSU bounds. Confirm
the physical miner, hashboard, PSU, cooling, and vendor limits before starting a
new model/firmware combination.

