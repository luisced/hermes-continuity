# Development and publication

Use an existing full Hermes runtime and a private temporary directory selected by `TMPDIR` when your host requires it. Run the default suite without installing dependencies or contacting real providers:

```sh
HERMES_DISABLE_LAZY_INSTALLS=1 hermes --run-module unittest discover -s tests -t . -q
python3 scripts/build_release.py
```

The package builder writes ZIP, tar.gz, a content manifest and SHA256SUMS under `dist/`. Check archive integrity, verify required modules and guides are present, extract to a clean directory and run the native suite again there before release.

The tests use fixture identities, temporary SQLite stores and synthetic provider/transport boundaries. Optional real-provider cases are opt-in. A synthetic send must never be represented as a real recipient receipt.

## Public-source boundary

This repository is a curated source snapshot, not an export of a personal runtime or the original private development history. It excludes conversation history, notes, database files, credentials, backups, local deployment settings and private execution reports. Do not add those files to fixtures, screenshots, issues or release assets.

Configuration examples must use placeholders. Test fixtures must respect the host-selected temporary directory instead of pinning a developer home path. Keep general lifecycle logic independent of a particular person, connector, note system or messaging platform.

## Compatibility

Native intake supports explicitly reviewed state schemas 32 and 33. A newer schema must be reviewed against the host's migration and provenance contracts before being admitted. Matching column names alone are not sufficient. The rest of the native SDK and operator configuration must also satisfy the documented contracts.

Use strict RED–GREEN–REFACTOR for behavior changes. Preserve pause/revocation, identity, epoch, revision, unknown-send, concurrent-claim and complete-ledger ambiguity tests. An active ticker is not proof of useful delivery; live deployment acceptance remains separate from the fixture suite.
