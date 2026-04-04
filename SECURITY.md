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
- improper logging of secrets
- insecure default configuration
- tool behavior that exposes private data unexpectedly

## Response Goals

- initial triage acknowledgment: as soon as practical
- severity assessment after reproduction
- coordinated fix and disclosure when a confirmed issue exists

## Supported Versions

Security fixes are applied to the current maintained mainline of this repository.
