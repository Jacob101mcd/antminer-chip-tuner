# Project Provenance

Antminer Chip Tuner is authored and maintained by
[Jacob McDaniel](https://github.com/Jacob101mcd).

## Research lineage

The project grew from McDaniel's 2024 University of Virginia independent
research into efficiency and per-chip tuning on Antminer S19 hardware running
Luxor firmware. The historical source and tuning notes are intentionally kept
in [`Independent-research-main/`](../Independent-research-main/README.md).

That snapshot establishes research provenance; it is not the current v4
implementation. The production application was redesigned around a vendor
abstraction, capability-selected tuning strategies, scanner registration,
MAC-keyed state, firmware-specific profiles, recovery checkpoints, an
authenticated dashboard, and multi-miner operation.

## Public-release boundaries

Public examples use documentation-only network addresses and synthetic device
identifiers. The repository excludes real tuning state, credentials, logs,
support bundles, private site details, and local development artifacts.

The University of Virginia and the named miner/firmware vendors are provenance
or compatibility references only. They do not sponsor, endorse, certify, or
warrant this project.

## Licensing and third parties

Jacob McDaniel releases the project, including the historical research snapshot
unless a file says otherwise, under the Apache License 2.0. Runtime dependencies
and bundled third-party assets keep their own licenses. See the root
[`LICENSE`](../LICENSE), [`NOTICE`](../NOTICE), and
[`THIRD_PARTY_NOTICES.md`](../THIRD_PARTY_NOTICES.md).

The dashboard includes a local Chart.js 4.4.0 UMD bundle, which incorporates
`@kurkle/color` 0.3.2. Both are MIT-licensed; their complete notices are in the
third-party notice file.

