# Deployment preparation — NOT executed

The branch does not deploy anything. The manual Actions workflow only tests and builds
an allowlisted archive with a checksum. CI contains no SSH command and has no deployment
credentials. No root key or exchange key belongs in this repository or artifacts.

## Bootstrap (separate user approval and trusted server access needed)

1. Review/merge PR yourself; confirm paper parameters; check server OS, resource usage,
   existing services and path/account conflicts. Supported staging script: Debian/Ubuntu,
   Python 3.11+, systemd, `useradd`, and outbound HTTPS to api.kraken.com. No web port needed.
2. Run manual release-preparation workflow on an approved commit. Download named artifact
   with `gh run download RUN_ID -R shishikami/altare-paper-trader -n paper-release-COMMIT`.
   This needs repo read / Actions read authorization; do not copy a general-purpose broad
   GitHub token to the server. A checksum packaged alongside an archive is an integrity
   check, not authentication: authenticate GitHub HTTPS and approve the source commit.
3. Transfer the approved release through a trusted administrator path, verify SHA256SUMS,
   inspect archive paths before extraction into a new staging directory. Never blindly
   extract an untrusted archive as root. Alternatively arrange a repo-scoped read-only
   GitHub App/token on the server to pull this specific artifact over HTTPS. That separate
   bootstrap and credential approval has NOT been implemented or executed here.
4. From the reviewed extracted release, intentionally run
   `sudo bash deploy/install.sh --stage-only COMMIT`. This creates isolated paths/account
   and installs a disabled unit. It refuses existing installations/accounts/paths rather
   than replacing anything. Review any partial staging failure before retrying.
5. As administrator, edit `/etc/paper-trader/config.json`: preserve the four confirmed limits in `config/CONFIRMED_RULES.md`, review the remaining
   provisional values and day-loss policy, and set `parameters_confirmed` true only after
   separate user approval of remaining parameters and activation. Configure BEFORE ledger init;
   the config fingerprint is immutable thereafter. This marker is an operator guardrail,
   not cryptographic proof of user consent. Config and source are root-owned, not writable
   by the service. No secrets or `.env` are needed for the local paper implementation.
6. As `papertrader` from `/opt/paper-trader/current`, use `/usr/bin/python3 -m papertrader
   --config /etc/paper-trader/config.json --data /var/lib/paper-trader init`. It creates a
   NEW stopped ledger and STOP. Never import old OKX state. `market-check` checks public
   feed only without arming. `run --once` with STOP still set must report paused.
7. Only after separate activation approval: the same CLI `start --ack-paper-only` arms
   LOCAL simulation; then administrator may `systemctl enable --now paper-trader`.
   This PR performs neither step. A running unit with STOP remains paused across restarts.

Use global flags BEFORE the subcommand, e.g.:
```
cd /opt/paper-trader/current
sudo -u papertrader /usr/bin/python3 -m papertrader --config /etc/paper-trader/config.json --data /var/lib/paper-trader status
```
For service diagnostics: `systemctl status paper-trader` and
`journalctl -u paper-trader` (JSON events, do not publish private runtime logs).

## Kill, backup, rollback

- CLI `stop` writes durable STOP before touching the DB, and serializes a stopped state.
  A corrupt DB may make the command report failure but STOP is still written. Emergency
  administrator alternative: create `/var/lib/paper-trader/STOP` without touching code.
  For a fully quiesced backup/upgrade, also `systemctl stop paper-trader`. A transaction
  already committing when a file-only stop is created can finish; successful CLI stop
  waits for the serialized writer. No stop mechanism liquidates simulated holdings.
- For backups stop the unit first, copy the entire `/var/lib/paper-trader` and matching
  config securely outside Git, and retain ownership/permissions. SQLite journal and
  IDENTITY.json are one logical account; never delete a DB to reset an account.
- For upgrades: do not rerun the initial installer. Stage a reviewed, root-owned immutable
  release beside the old one, run its offline tests, stop/pause the service and back up.
  No automated upgrade/migration script is claimed in this MVP.
- Rollback code by pointing `current` to the prior reviewed release ONLY if ledger/config
  schema is compatible. Leave STOP set; inspect `status` and tests before any activation.
  Never roll back a live ledger independently of its fills to manufacture profits.
- Config changes deliberately fail closed. Migration/export tools are not implemented;
  do not erase a ledger as a workaround. User-reviewed separate new experiments need new
  data directories and must preserve old ledgers.

## Future standard SSH deployment option (not configured)

Use a dedicated non-root deploy user, restricted paths and narrowly constrained sudo for
this one unit. Use a distinct deployment key and pinned verified host public key. If the
user specifically approves, store ONLY the dedicated key in a scoped GitHub deployment
Environment Secret; workflows and anyone able to change trusted workflow code can use it.
Check private-repo plan support for required reviewers, protect deployment branches,
require manual `workflow_dispatch`, minimal job permissions, and pin action SHAs.
No fork/untrusted PR code should run with deployment secrets. Do not use Actions as a
network proxy or long-running trading host. GitHub runner reachability to TCP/25780 is
unverified. Self-hosted runners on a trading host expand repository-to-host execution
privileges and are not installed by this project.

Official references:
- https://docs.github.com/en/actions/how-tos/deploy/configure-and-manage-deployments/control-deployments
- https://docs.github.com/en/actions/how-tos/manage-workflow-runs/download-workflow-artifacts
- https://docs.github.com/en/actions/how-tos/write-workflows/choose-what-workflows-do/use-secrets
