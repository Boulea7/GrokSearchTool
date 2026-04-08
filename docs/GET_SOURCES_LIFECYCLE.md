# `get_sources` Session Lifecycle

## Current contract

- `session_id` is a process-local handle backed by the running server's in-memory LRU cache.
- It is transient, non-durable, non-caller-bound, and should not be treated as a secret token.
- In a shared-daemon setup, any caller that holds a valid `session_id` inside the same running process can read the cached sources.

## Miss semantics

- `session_id_not_found_or_expired` is the unified miss contract.
- It currently covers process restart, TTL expiry, LRU eviction, and unreadable legacy-cache entries.

## Readiness semantics

- `feature_readiness.get_sources = ready` means the current process already holds at least one readable non-error source session.
- `feature_readiness.get_sources = partial_ready` means the interface exists, but the current process does not yet hold a readable session.
- This readiness is marked `transient` and does not lower the overall doctor status by itself.

## Why caller binding is not implemented yet

- The current MCP surface does not provide a stable caller-identity substrate to bind sessions against.
- Introducing caller binding or namespaces would be a product-level contract change, not a narrow bug fix.
- Until that model exists, the documented behavior remains possession-based within the same running process.
