# Contributing

Contributions are welcome when they preserve safe defaults, operator control,
stable MAC-keyed state, and honest platform-support claims.

By submitting a contribution, you agree that it is licensed under the Apache
License 2.0 on the same terms as the project. Do not submit code, documentation,
protocol material, or fixtures that you do not have the right to redistribute.

## Development setup

Use Python 3.11 or newer:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

Before opening a pull request, run:

```bash
ruff check .
ruff format --check .
pytest -q
```

If you change packaging, also install `build`, build both distributions, and
test a fresh wheel install:

```bash
python -m pip install build
python -m build
```

## Workflow

1. Open or find an issue for a non-trivial change.
2. Create a focused branch from the current default branch.
3. Add tests that fail without the change and pass with it.
4. Update the README, support matrix, privacy/security notes, or vendor quick
   reference when behavior or support changes.
5. Submit a small pull request that explains the operator-visible effect,
   safety impact, verification performed, and rollback path.

Do not combine unrelated formatting or refactors with a behavior change.

## Safety and platform requirements

Changes that issue miner commands must:

- route behavior through capabilities or a documented tuning strategy;
- preserve voltage/frequency ordering, bounds, settle periods, stop handling,
  checkpointing, and recovery behavior;
- address offline, partial-response, reboot, timeout, and interrupted-command
  cases;
- keep the default dashboard bind on loopback and integrations opt-in; and
- avoid silently applying a control to unsupported firmware.

A new platform needs a registry entry, typed summary mapping, scanner
fingerprint, authentication behavior, capability flags, hardware-topology
handling, config visibility, tests, a support-matrix row, and a concise quick
reference. State and HTTP routes remain keyed by MAC even when the wire protocol
uses an IP address.

Hardware testing must be performed only on equipment you own or are authorized
to operate. Describe the environment and outcome without publishing deployment
identifiers. A live test on one unit is evidence for that combination, not every
model sold under the same family name.

## Test-data hygiene

Never commit real credentials, API responses, logs, IP/MAC addresses, serial
numbers, hostnames, pool workers, wallet addresses, account identifiers, or
private filesystem paths. Use:

- RFC 5737 IPs: `192.0.2.0/24`, `198.51.100.0/24`, or `203.0.113.0/24`;
- locally administered MACs such as `02:00:00:00:00:01`;
- `example.com` hostnames; and
- explicit placeholders such as `test-worker` and `REDACTED`.

Generated fixtures must be reproducible and must not embed a real miner secret.
Do not attach sensitive material to a public issue; use the private process in
[SECURITY.md](SECURITY.md) for vulnerabilities.

## Documentation style

Write for operators, state the safety boundary before the mechanism, and avoid
claims that are broader than the evidence. Link to upstream public documentation
instead of copying proprietary vendor manuals. Keep protocol quick references
concise and free of site-specific values or issue/PR archaeology.

