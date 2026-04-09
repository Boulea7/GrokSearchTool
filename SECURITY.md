# Security Policy

## Reporting a Vulnerability

Please do not disclose security-sensitive issues in a public GitHub issue.

Preferred reporting routes:

1. Use GitHub private vulnerability reporting if it is enabled for this repository.
2. If private reporting is not available, contact the maintainer through GitHub private channels and share only the minimum details needed to start triage.

When reporting a vulnerability, include:

- affected version or commit
- deployment context or client context
- reproduction steps
- impact assessment
- whether credentials, tokens, or third-party services are involved

## What Counts as Security-Relevant

Examples include:

- credential leakage
- unsafe handling of external content
- SSRF-style fetch or crawl abuse
- redirect preflight behavior that weakens SSRF boundaries or changes how degraded checks are surfaced
- improper logging of secrets
- insecure default configuration
- tool behavior that exposes private data unexpectedly

Current runtime note:

- `web_fetch` / `web_map` perform a redirect preflight before dispatching provider calls
- private or loopback redirect targets are hard-rejected before provider dispatch
- redirect preflight request-level failures currently degrade to `skipped_due_to_error` and continue downstream provider calls, so this path should be treated as a best-effort safety boundary rather than a hard-stop guarantee

## Response Goals

- initial triage acknowledgment: as soon as practical
- severity assessment after reproduction
- coordinated fix and disclosure when a confirmed issue exists

## Supported Versions

Security fixes are applied to the current maintained mainline of this repository.
