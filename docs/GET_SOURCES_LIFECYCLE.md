# `get_sources` Session Lifecycle

## Current contract

- `session_id` is a process-local handle backed by the running server's in-memory LRU cache.
- It is transient, non-durable, non-caller-bound, and should not be treated as a secret token.
- In a shared-daemon setup, any caller that holds a valid `session_id` inside the same running process can read the cached sources.
- Successful `get_sources` reads now also return additive `search_warnings` for that cached search session; legacy cache entries that never stored warning codes default to `search_warnings=[]`.

## Miss semantics

- `session_id_not_found_or_expired` is the unified miss contract.
- It currently covers process restart, TTL expiry, LRU eviction, and unreadable legacy-cache entries.

## Readiness semantics

- The `ready` state for `feature_readiness.get_sources` means the current process already holds at least one readable non-error source session.
- The `partial_ready` state for `feature_readiness.get_sources` means the interface exists, but the current process does not yet hold a readable session.
- `feature_readiness.get_sources` now includes additive `cache_summary` data with a lightweight cache snapshot: `total_sessions`, `readable_sessions`, `error_sessions`, and `partial_sessions`.
- This readiness is marked `transient` and does not lower the overall doctor status by itself.

## Why caller binding is not implemented yet

- The current MCP surface does not provide a stable caller-identity substrate to bind sessions against.
- Introducing caller binding or namespaces would be a product-level contract change, not a narrow bug fix.
- Until that model exists, the documented behavior remains possession-based within the same running process.
