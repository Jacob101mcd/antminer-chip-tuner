# Privacy

Antminer Chip Tuner is designed to run locally. It has no project-operated
cloud service, analytics, advertising, crash reporting, or automatic telemetry.
The dashboard uses the locally bundled Chart.js asset and does not contact a CDN.

## Data stored locally

The application's private data directory may contain:

- miner IP addresses, MAC addresses, hostnames, models, and firmware types;
- miner firmware passwords and scanner password lists in plaintext;
- optional Minerstat and MiningRigRentals credentials in plaintext;
- a salted scrypt hash of the dashboard password;
- tuning profiles, checkpoints, stock baselines, and per-miner logs;
- hashrate, power, efficiency, temperature, and profitability metrics;
- cached integration responses and an MRR request nonce.

Dashboard session tokens exist only in process memory. Restarting the process
clears them. The session cookie is HttpOnly and SameSite=Strict; because the
built-in server is plain HTTP, the cookie is not a substitute for TLS on an
untrusted network.

By default, the application uses the operating system's per-user application-data
directory for `antminer-chip-tuner`; `ASIC_TUNER_DATA_DIR` selects an explicit
directory instead. This repository ignores a local `tuning_data/` override, but that
does not protect another override or copies made by backup tools, support bundles,
shell history, or manual uploads. Restrict local file permissions and never commit or
publicly share the data directory.

## Network activity

The application makes network requests in these circumstances:

- The scanner probes only the ranges configured by the operator and skips the
  configured blacklist.
- Registered miners are polled and may receive control commands over their
  local firmware API.
- Minerstat is contacted over HTTPS only when its optional integration is
  configured and a manual or scheduled fetch runs.
- The MiningRigRentals management API is contacted over HTTPS only when its optional
  integration is enabled and used. Pool synchronization may configure supported
  miners with MRR's `stratum+tcp` endpoints; those miners then send the rig-specific
  login and mining/share traffic to MRR without transport encryption.
- Browsers connect to the dashboard address selected by the operator.

Those third-party services process data under their own terms and privacy
policies. Depending on the operation, transmitted data may include an API key,
account or rig identifier, hashrate, requested rig state, and—when MRR pool
synchronization is used—worker login and mining/share traffic. The project does not
control their retention practices.

## Sharing diagnostics

Before attaching diagnostics to an issue, remove:

- passwords, session cookies, API keys, and HMAC secrets;
- IP and MAC addresses, serial numbers, hostnames, and filesystem paths;
- wallet, pool worker, MRR account, and rig identifiers; and
- details that reveal a private site's layout, capacity, or operating schedule.

Use `example.com`, RFC 5737 IP addresses, and clearly synthetic identifiers in
public reproductions. If a security report necessarily contains sensitive data,
follow [SECURITY.md](SECURITY.md) instead of opening a public issue.

## Removing data

Use the dashboard's per-miner reset or removal controls when you want to delete
individual state. To remove all local project data, stop the application and delete
its selected data directory and any private backups. This does not delete information
previously sent to an optional third-party service.
