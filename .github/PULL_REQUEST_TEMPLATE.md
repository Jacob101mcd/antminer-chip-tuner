## Summary

Describe the operator-visible change and why it is needed.

## Safety and compatibility

- Miner commands or tuning behavior changed:
- Failure, timeout, offline, and interruption behavior:
- Platforms/models affected:
- Rollback path:

## Verification

List automated tests and any supervised hardware validation. Use only sanitized,
non-identifying evidence.

## Checklist

- [ ] The change is focused and has tests that fail without it.
- [ ] `ruff check .`, `ruff format --check .`, and `pytest -q` pass.
- [ ] Safe loopback, authentication, scanner opt-in, and capability gates remain intact.
- [ ] Documentation and the support matrix match the implementation.
- [ ] No credentials, cookies, real IP/MAC addresses, serials, hostnames, workers,
      wallets, account IDs, private paths, logs, or site details are included.
- [ ] New third-party material has compatible licensing and attribution.
- [ ] I have the right to submit this contribution under Apache-2.0.

