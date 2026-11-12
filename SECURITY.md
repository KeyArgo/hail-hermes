# Security policy

_How to report a vulnerability in Hail Hermes._

Applies to Hail Hermes 1.0.0 · revised 2026-11-11

## Supported versions

| Version | Supported |
|---|---|
| 1.0.x | yes |
| older / unversioned builds | no |

## Report a vulnerability

Do **not** open a public issue for a security problem. Use GitHub's private vulnerability reporting
for this repository:

<https://github.com/KeyArgo/hail-hermes/security/advisories/new>

Include the version (the pane header or `GET /v1/version`), the platform, what you did, what
happened, and the smallest reproduction you can manage. If you cannot use the private report,
contact the maintainers through GitHub (for example a direct message to the repository owner) and
say that you have a security report; do not post the details publicly.

You should get an acknowledgement within a few days. Please give the maintainers time to ship a fix
before disclosing anything publicly.

## Scope

The security model is documented in [docs/SECURITY.md](docs/SECURITY.md). The areas that matter
most:

- pairing and the four-digit approval flow,
- the loopback control token,
- device token storage,
- LAN mode and the discovery beacon,
- the pane API routes and what they can write.

Out of scope: the security of the Hermes host itself, the model provider you configure, and a
machine where an attacker already has a shell.
