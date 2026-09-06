# P1 Remediation Design

**Date:** 2026-09-04

**Goal:** Eliminate the seven P1 defects found in the 2026-09-02 whole-project review without broadening the public product surface or changing the single-user, local-only deployment model.

## Scope

This change fixes:

1. concurrent first captures of the same Douyin work;
2. reanalysis overwriting newer user-owned entry state;
3. unsafe and partially applied database rebuilds;
4. API keys sent to non-loopback HTTP model endpoints;
5. one malformed source note breaking the whole Web catalog;
6. topic generation publishing an old source snapshot as current;
7. non-database-backed Markdown articles exposing unsupported mutations and article chat.

P2 findings from the same review remain out of scope unless a small supporting change is required to make a P1 fix correct.

## Global Constraints

- Preserve the existing local-only macOS architecture and Python 3.12 support.
- Preserve existing CLI and MCP command names and response shapes unless a new safety error is required.
- Do not change or restore the user's existing deletion of `scripts/hermes_event_monitor.py`.
- Do not discard user-authored Markdown, inspirations, favorites, media state, topic sources, jobs, chat history, or reminder state.
- Every production behavior change must begin with a failing regression test.
- Existing valid configurations and database rebuilds must continue to work.

## 1. Per-work Capture Serialization

After a capture resolves a stable Douyin work ID, the service acquires a cross-process lock specific to that work ID before performing duplicate detection, download, analysis, or final persistence. Lock filenames are derived only from a strict safe work-ID representation and live under `.douyin-wiki/locks/works/`.

The lock acquisition must not block the asyncio event loop. The service waits for the filesystem lock in a worker thread and releases it in a `finally` path. Lock ordering is:

1. per-work capture lock;
2. existing entry-operation lock;
3. existing short-lived Vault write lock.

After acquiring the per-work lock, the capture re-reads the database. If another task has already completed the work, the waiting task merges its inspirations through the existing entry mutation path, repairs missing projections if needed, and completes as a duplicate without downloading or analyzing again. Both video and image-note capture paths obey this rule.

The lock is an execution deduplicator, not a persistent queue state. A process crash releases the OS file lock automatically; normal job lease recovery remains responsible for retrying abandoned jobs.

## 2. Reanalysis Preserves Current User State

Reanalysis may compute from an earlier immutable evidence snapshot, but its final write must be based on the latest database record. Immediately before persistence, while holding the entry-operation lock, it reloads the entry and its data.

The merge changes only analysis-owned fields:

- title, summary, tags, analysis payload, model/provider provenance, prompt version, chunks, relations, and reminder candidates.

It preserves current user- or lifecycle-owned fields:

- inspirations;
- favorite flag;
- retention and media expiry;
- media status;
- raw/source paths and creation time;
- current metadata, creator context, cover information, reminder states, transcripts, OCR, and review state unless the reanalysis job explicitly owns a newer value.

If the entry was deleted before finalization, the existing not-found behavior aborts the write. Reanalysis must never recreate a deleted entry.

## 3. Atomic, Validated Knowledge Rebuild

Rebuild becomes a two-phase operation.

### Phase A: preflight

Before any deletion:

- parse every entry, creator, topic, and topic artifact sidecar;
- collect structured errors containing the Vault-relative path and validation error;
- reject missing payload blocks, invalid models, missing artifact files, unsafe identifiers/paths, and malformed YAML;
- build chunks, relations, reminder candidates, creator bundles, topic bundles, and the embedding signature entirely in memory;
- verify relation targets against the prepared entry-ID set.

Dry-run returns counts and all error collections. Apply refuses to start if any error collection is non-empty.

### Phase B: database transaction

`Database` exposes one knowledge-cache replacement operation that uses one SQLite connection and one transaction. It clears and restores entries, FTS rows, chunks, relations, reminders, creators, creator works, topics, topic sources, artifacts, and index metadata before committing.

Reusable private `*_conn` helpers may be extracted from existing public persistence methods. Public single-record behavior remains unchanged. An exception at any point rolls back the complete replacement, leaving the pre-rebuild database usable. Jobs, job events, chat sessions/messages, and maintenance history are not cleared.

Creator/topic loaders follow the same error-reporting contract already used by entry loading; they may no longer silently skip invalid managed sidecars.

## 4. Secure Model Endpoint Validation

A shared endpoint normalizer validates model base URLs for configuration loading, CLI configuration, and Web configuration.

Rules:

- scheme must be `http` or `https`;
- hostname is required;
- URL userinfo and fragments are rejected;
- non-loopback hosts require `https`;
- loopback hosts (`localhost` and loopback IP literals) may use `http` or `https`;
- normalized output removes only trailing slashes and does not expose credentials in errors.

Invalid persisted configuration fails validation before an adapter can construct an Authorization header. Web and CLI callers receive actionable, non-secret error messages. Existing loopback LM Studio/Ollama configurations continue to work without an API key.

## 5. Fault-isolated Web Catalog Loading

`LibraryCatalog.refresh()` treats each source note as an independent record. Parsing, frontmatter conversion, date conversion, and `LibraryItem` validation happen inside the per-file error boundary. A malformed file is omitted from the current projection while valid files remain available.

The catalog records structured load errors with Vault-relative paths and exception summaries for diagnostics. A successful later refresh clears errors for repaired files. No malformed content is echoed into HTML or API error responses.

Application startup succeeds when at least the Vault itself is readable, even if one source note is malformed.

## 6. Topic Artifact Optimistic Version Check

Topic generation captures the source revision and evidence set used for the model request. After the model returns, it reloads the topic before saving.

- If the source revision is unchanged, save the artifact as `current` and persist the current topic.
- If the source revision changed, preserve the generated artifact but save it as `needs_update`, retaining the old source revision and source-revision list that actually produced it.
- Never persist the pre-generation topic snapshot after the model call.
- If the topic was deleted, abort without recreating it.

This makes concurrent source edits visible without discarding an expensive generated result.

## 7. Read-only Dynamic Markdown Articles

The catalog continues to expose valid source Markdown that lacks an `entries` row, because dynamic Vault browsing is an intentional feature. Each `LibraryItem` and API payload gains a boolean indicating whether it is database-managed.

For unmanaged items:

- article reading and safe media rendering remain available;
- favorite, delete, inspiration, topic-selection, and other database mutations are hidden or disabled in the Web UI;
- mutation endpoints continue to reject missing database entries;
- article-scoped chat session creation is rejected with a clear conflict response instead of creating a session with no article evidence.

Managed articles retain existing behavior. Library-scoped chat remains available regardless of which read-only article is open.

## Error Handling

- Lock acquisition and release are exception-safe; lock errors fail only the affected job.
- Rebuild reports all preflight errors and performs no database mutation when validation fails.
- Rebuild transaction errors propagate while preserving the previous database contents.
- Endpoint validation errors never include secrets or credential-bearing URLs.
- Catalog errors are isolated to one file and retained for local diagnostics.
- Version conflicts produce explicit stale artifacts or safe request rejection rather than silent overwrite.

## Testing Strategy

Tests must cover:

- two concurrent jobs resolving to the same video and image-note ID, with different inspirations, proving one expensive pipeline execution and merged user data;
- reanalysis interleaved with inspiration and favorite changes;
- creator/topic/artifact sidecar validation errors preventing apply;
- a forced failure midway through transactional rebuild leaving the original database intact;
- remote HTTP endpoint rejection and loopback HTTP acceptance across config, Web, and CLI paths;
- an invalid source date being skipped while valid catalog entries and application startup remain available;
- a topic source revision change during generation producing `needs_update` without reverting topic sources;
- unmanaged Markdown articles remaining readable while mutations and article chat are unavailable;
- all existing project tests and Ruff checks.

## Success Criteria

- No P1 reproduction from the audit succeeds after the change.
- New tests demonstrate the previous failures before implementation and pass afterward.
- Full offline test suite and Ruff pass.
- No unrelated user changes are staged or committed.
