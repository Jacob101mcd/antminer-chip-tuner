# Support

Antminer Chip Tuner is maintained as an open-source project on a best-effort
basis. There is no guaranteed response time, uptime commitment, hardware
warranty, remote administration service, or emergency support.

## Before requesting help

1. Stop tuning if the miner is unstable or any safety limit is in doubt.
2. Confirm the model, firmware, API port, and capability row in
   [the support matrix](docs/support-matrix.md).
3. Reproduce with the latest release or default branch.
4. Check the tuner log and the miner's own logs for the first error.
5. Search existing GitHub issues.
6. Reduce the problem to one miner and the smallest safe configuration.

Use the repository's issue forms for reproducible defects and feature requests.
Questions about electrical work, fire safety, firmware installation, warranty,
or physical repair belong with the manufacturer or a qualified professional.

Security and safety-control vulnerabilities must be reported privately through
[SECURITY.md](SECURITY.md).

## Information to include

- project version or commit;
- operating system and Python version;
- miner model and hashboard topology;
- firmware family and version;
- whether discovery, monitoring, or a particular tuning phase failed;
- minimal safe steps to reproduce; and
- a short, sanitized log excerpt surrounding the first failure.

Do not upload an unreviewed application data directory or full support archive.
Remove credentials, cookies, IP/MAC addresses, serial numbers, hostnames, pool
workers, wallet-like values, account/rig identifiers, and site details. Replace
them with documentation addresses and synthetic IDs.

## Scope

An implemented protocol adapter is not proof that every hardware/firmware pair
has completed a hardware soak. Maintainers may ask for a safe test result before
marking a combination validated. Requests that require access to a private miner
or disclose credentials may be closed if they cannot be reproduced safely.
