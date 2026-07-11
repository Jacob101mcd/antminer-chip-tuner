# 2024 UVA Research Snapshot

This directory preserves the public historical materials from Jacob McDaniel's
2024 University of Virginia independent research into Antminer S19 efficiency
and per-chip tuning with Luxor firmware.

The nested `Independent-research-main/` directory contains the original
research scripts and `Tuning Methods.rtf` notes. They are retained to document
the ideas and experiments that preceded Antminer Chip Tuner.

## Historical, not production software

This snapshot is not the supported application. It predates the current vendor
abstraction, v4 MAC-keyed configuration, authenticated dashboard, persistent
recovery model, platform capability gates, and current safety checks. Its Python
dependencies and firmware assumptions are historical and may be obsolete.

Do not run these scripts against production hardware. Use the application and
instructions at the repository root, verify the current
[support matrix](../docs/support-matrix.md), and retain the miner's independent
hardware and firmware protections.

## Sanitization

Network values in the public snapshot are documentation-only examples. The
snapshot contains no live credentials or private deployment logs. If you cite or
extend the research, do not add real miner addresses, MACs, serial numbers,
workers, wallets, credentials, or site details.

## License scope

Copyright 2024 Jacob McDaniel.

Unless a file explicitly states otherwise, the source code and author-created
notes in this snapshot are part of Antminer Chip Tuner and are licensed under
the repository's [Apache License 2.0](../LICENSE). That license does not change
the licenses of third-party Python packages imported by the historical scripts;
obtain those packages separately under their respective terms.

University of Virginia, Antminer, and Luxor names describe provenance and the
historical research environment. They do not imply sponsorship, endorsement,
or warranty.

