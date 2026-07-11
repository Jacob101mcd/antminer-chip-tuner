# Braiins OS Quick Reference

Platform key: `braiins`  
Transport: HTTP REST with token authentication  
Strategy: firmware-owned AutoTune with an external wattage search

Braiins OS does not expose the direct per-chip V/F controls used by the ePIC
and LuxOS strategies. The tuner changes a power target and evaluates the result
while Braiins AutoTune remains responsible for voltage and frequency.

| Tuner operation | Braiins API mapping | Notes |
|---|---|---|
| Authenticate | `POST /api/v1/auth/login` | Uses configured username and miner password; token is cached and refreshed after rejection |
| Summary | miner details, stats, and cooling endpoints | Responses are combined into the common `MinerSummary` model |
| Constraints | `GET /api/v1/configuration/constraints` | Used for topology and power-control context |
| Read performance mode | `GET /api/v1/performance/mode` | Reports manual/tuner mode; not a direct voltage endpoint |
| Enable firmware tune | `PUT /api/v1/performance/mode` | Selects tuner mode |
| Set power target | `PUT /api/v1/performance/power-target` | Primary tuning control; watts are operator-bounded |
| Start / stop | `PUT /api/v1/actions/start` and `/stop` | Idempotent firmware actions |
| Reboot | `PUT /api/v1/actions/reboot` | Executes immediately; generic delay argument is ignored |

Unsupported operations deliberately raise `NotImplementedError`: direct voltage,
global/board/chip clocks, and coin switching. Empty chip arrays are expected in
the dashboard for this platform.

The scanner fingerprints the unauthenticated version endpoint, then validates a
configured password. The version response does not contain a MAC; registration
fetches miner details after authentication and otherwise falls back to ARP or a
synthetic ID.

Many Braiins installations use HTTP port 80. Because v4 has one fleet-wide
`API_PORT`, keep miners that require different API ports in separate installed
instances with separate state directories.

