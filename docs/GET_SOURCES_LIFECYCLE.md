# `get_sources` Session Lifecycle

## Current contract

- `session_id` is a process-local handle backed by the running server's in-memory LRU cache.
- It is transient, non-durable, non-caller-bound, and should not be treated as a secret token.
- In a shared-daemon setup, any caller that holds a valid `session_id` inside the same running process can read the cached sources.
- Successful `get_sources` reads now also return additive `search_warnings` for that cached search session; legacy cache entries that never stored warning codes default to `search_warnings=[]`.
- Returned source rows are lossy aggregate display rows. When multiple inputs collapse into one row, additive `contributors` carries contributor-level attribution, while top-level `provider` remains the winner provider for that row.
- `source` remains a legacy-overloaded field: older cache entries may still use it as a provider alias when `origin_type` is absent.

## Miss semantics

- `session_id_not_found_or_expired` is the unified miss contract.
- It currently covers process restart, TTL expiry, LRU eviction, and unreadable legacy-cache entries.

## Readiness semantics

- The `ready` state for `feature_readiness.get_sources` means the current process already holds at least one readable non-error source session.
- The `partial_ready` state for `feature_readiness.get_sources` means the interface exists, but the current process does not yet hold a readable session.
- The `not_ready` state for `feature_readiness.get_sources` means `web_search` itself is not ready yet and the current process does not already hold a readable session.
- If the current process already holds a readable session, `get_sources` can still report `ready` even when `web_search` is currently not ready; that upstream problem is surfaced through `degraded_by` rather than by downgrading the cached-session readability signal.
- `feature_readiness.get_sources` now includes additive `cache_summary` data with a lightweight cache snapshot: `total_sessions`, `readable_sessions`, `error_sessions`, `partial_sessions`, and `unreadable_sessions`.
- This readiness is marked `transient` and does not lower the overall doctor status by itself.

## Why caller binding is not implemented yet

- The current MCP surface does not provide a stable caller-identity substrate to bind sessions against.
- Introducing caller binding or namespaces would be a product-level contract change, not a narrow bug fix.
- Until that model exists, the documented behavior remains possession-based within the same running process.

## Lifecycle state matrix

The key distinction is that `feature_readiness.get_sources.status` describes whether the interface is usable in the current process, while `get_sources` response fields such as `error` and `source_state` describe what happened for a specific `session_id`.

| State | Surface | Meaning | Stable observable contract |
| --- | --- | --- | --- |
| `not_ready` | `feature_readiness.get_sources.status` | `web_search` prerequisites are still missing, so `get_sources` cannot become useful yet. | `status=not_ready`, `transient=true` |
| `partial_ready` | `feature_readiness.get_sources.status` | The interface exists, but the current process does not yet hold a readable session. | `status=partial_ready`, `transient=true` |
| `ready` | `feature_readiness.get_sources.status` | The current process already holds at least one readable non-error source session. | `status=ready`, `transient=true` |
| `miss` | `get_sources` response | The requested `session_id` is unavailable because it never existed in this process anymore. | `sources=[]`, `sources_count=0`, `error=session_id_not_found_or_expired` |
| `unavailable_due_to_search_error` | `get_sources.source_state` | The session exists, but the originating `web_search` ended in `search_status=error`. | `source_state=unavailable_due_to_search_error`, `sources=[]`, `sources_count=0` |
| `empty` | `get_sources.source_state` | The session exists and the search succeeded, but no readable sources survived extraction/standardization. | `source_state=empty`, `search_status=ok`, `sources=[]`, `sources_count=0` |

<!-- docs-contract:get-sources-lifecycle:start -->
```json
{
  "doc": "get_sources_lifecycle",
  "version": 1,
  "feature_readiness_states": {
    "not_ready": {
      "surface": "feature_readiness.get_sources.status",
      "depends_on": "web_search readiness",
      "observable": {
        "status": "not_ready",
        "transient": true
      }
    },
    "partial_ready": {
      "surface": "feature_readiness.get_sources.status",
      "depends_on": "source cache state",
      "observable": {
        "status": "partial_ready",
        "transient": true
      }
    },
    "ready": {
      "surface": "feature_readiness.get_sources.status",
      "depends_on": "source cache state",
      "observable": {
        "status": "ready",
        "transient": true
      }
    }
  },
  "result_states": {
    "miss": {
      "surface": "get_sources response",
      "observable": {
        "sources": [],
        "sources_count": 0,
        "error": "session_id_not_found_or_expired"
      }
    },
    "unavailable_due_to_search_error": {
      "surface": "get_sources.source_state",
      "observable": {
        "source_state": "unavailable_due_to_search_error",
        "search_status": "error",
        "sources": [],
        "sources_count": 0
      }
    },
    "empty": {
      "surface": "get_sources.source_state",
      "observable": {
        "source_state": "empty",
        "search_status": "ok",
        "search_error": null,
        "sources": [],
        "sources_count": 0
      }
    }
  }
}
```
<!-- docs-contract:get-sources-lifecycle:end -->
