# Security policy

## Supported versions

Security fixes are made on the default branch and included in the next release.
After releases begin, only the latest released version will receive fixes.

## Reporting a vulnerability

Please do not open a public issue for a suspected vulnerability. Use the
repository host's private vulnerability-reporting feature. If that feature is
not available, contact a maintainer privately through the contact information
on their repository-hosting profile and ask for a secure reporting channel.

Include the affected version or commit, impact, reproduction steps, and any
known mitigation. Maintainers will coordinate disclosure after a fix is
available. Do not include secrets, personal data, or unsafe proof-of-concept
payloads in a report.

Reports about QEMU, UEFI firmware, Bazel, or CPython itself should normally go
to the upstream project. A problem in how `rules_ostest` constructs images,
starts QEMU, handles QMP or network sockets, or processes untrusted inputs is in
scope here.
