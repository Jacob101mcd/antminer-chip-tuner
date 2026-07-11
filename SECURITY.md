# Security Policy

## Supported versions

Security fixes are made on the default branch and included in the next release.
Only the latest tagged release is supported; older releases may not receive
backports.

| Version | Supported |
|---|---|
| Default branch | Yes |
| Latest release | Yes |
| Older releases | No |

## Reporting a vulnerability

Do not open a public issue for a suspected vulnerability. Use
[GitHub private vulnerability reporting](https://github.com/Jacob101mcd/antminer-chip-tuner/security/advisories/new)
and include:

- the affected version or commit;
- the platform, firmware family, and relevant configuration;
- reproducible steps or a minimal proof of concept;
- expected impact and any known workaround; and
- whether device credentials, API keys, or hardware safety may be affected.

Remove real passwords, API keys, wallet or worker identifiers, serial numbers,
MAC addresses, hostnames, public IP addresses, and private deployment details.
Use RFC 5737 addresses such as `192.0.2.10` and locally administered example
MACs such as `02:00:00:00:00:01`.

Reports are handled on a best-effort basis. The maintainer will acknowledge a
complete report when practical, investigate, coordinate a fix and disclosure,
and credit the reporter if requested. Please do not test against devices or
networks you do not own or have explicit permission to operate.

## Security model

- The dashboard binds to loopback by default.
- A non-loopback bind is rejected until dashboard authentication is configured.
- Dashboard passwords are stored as salted scrypt hashes.
- Miner passwords and optional third-party API credentials are operational secrets
  stored locally in `<data-dir>/config.json`; they are not encrypted at rest. The
  default is the operating system's per-user application-data directory, and
  `ASIC_TUNER_DATA_DIR` selects an explicit location.
- Session tokens are held in memory and sent in an HttpOnly, SameSite=Strict
  cookie. Restarting the process invalidates sessions. When a reverse proxy provides
  HTTPS for every browser connection, set `ANTMINER_TUNER_SECURE_COOKIES=1` to add the
  cookie's `Secure` attribute; do not set it for direct HTTP access.
- Request `Host` values are restricted to `localhost`, private/loopback IP literals,
  or exact DNS names in `ANTMINER_TUNER_ALLOWED_HOSTS`. This is a DNS-rebinding
  defense; do not add public or wildcard names. Initial password setup additionally
  requires a loopback `Host` and an effective loopback client address.
- `X-Forwarded-For` is ignored unless the directly connected peer belongs to
  `ANTMINER_TUNER_TRUSTED_PROXIES`. Configure narrow proxy IPs/CIDRs only, and make
  the proxy replace untrusted incoming forwarding headers. Duplicate, malformed,
  overlong, or excessively deep forwarding chains are ignored.
- POST bodies are capped at 1 MiB. Duplicate, malformed, negative, or oversized
  `Content-Length` values and unsupported transfer encodings are rejected before
  the body is read. Protected POST routes authenticate before reading their bodies.
- The built-in server provides HTTP, not TLS. Use a trusted private network or
  a carefully configured TLS reverse proxy; never expose it directly to the
  public internet.

Authentication, the scanner, miner protocol handling, path validation, secret
handling, and commands that change voltage, frequency, power, pools, or mining
state are all considered security-sensitive.

## Protocol-mandated legacy cryptography

Two optional integrations require legacy primitives that must not be copied into
new authentication, password-storage, or encryption code:

- The stock Whatsminer v2 privilege API mandates an MD5-crypt-derived challenge
  response and AES-256 in ECB mode for writable commands. The implementation is
  confined to `tuner_app/miner/whatsminer.py` and exists only for wire-protocol
  compatibility with the [official MicroBT v2.0.4 specification](https://www.whatsminer.com/file/WhatsminerAPI%20V2.0.4.pdf).
  ECB does not provide modern message confidentiality or integrity. Keep miner
  management interfaces on a segmented, trusted LAN; firewall TCP 4028 from
  untrusted networks; replace vendor-default credentials where supported; and do
  not reuse that password elsewhere.
- MiningRigRentals API v2 mandates HMAC-SHA1 for `x-api-sign`. The client uses it
  only for the documented request signature, over HTTPS, with a monotonically
  increasing nonce, as required by the [MRR API v2 authentication specification](https://www.miningrigrentals.com/apidocv2).
  Use a dedicated, least-privilege API credential and rotate it if exposure is
  suspected.

CodeQL findings for these exact compatibility calls are reviewed as accepted
upstream-protocol constraints. The dashboard password remains protected with
scrypt; neither MD5 nor SHA-1 is used to store local credentials.

## Hardware-safety disclosures

A defect that can bypass configured thermal/power bounds, apply settings to the
wrong MAC, lose control after an IP change, or issue an unsafe command sequence
should be reported privately as a vulnerability. If hardware is in immediate
danger, stop the tuner, use the vendor's safe shutdown procedure, and remove
power when it is safe to do so. This project cannot provide emergency response.
