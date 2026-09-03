# P1 Remediation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove all seven audited P1 failure modes while preserving the local-only product model, durable user state, and existing public command shapes.

**Architecture:** Add safety at the narrowest ownership boundary: normalize model endpoints in configuration, isolate each catalog record, serialize capture by stable work ID, merge reanalysis against the latest entry, version-check topic output, and replace the rebuildable database projection in one transaction. Keep Vault Markdown as the long-term source of truth and SQLite as a replaceable projection.

**Tech Stack:** Python 3.12, asyncio, `fcntl`, SQLite, Pydantic v2, FastAPI, vanilla JavaScript, pytest/pytest-asyncio, Ruff.

**Spec:** `docs/superpowers/specs/2026-09-04-p1-remediation-design.md`

## Global Constraints

- Preserve the existing local-only macOS architecture and Python 3.12 support.
- Preserve existing CLI and MCP command names and response shapes unless a new safety error is required.
- Do not change or restore the user's existing deletion of `scripts/hermes_event_monitor.py`.
- Do not discard user-authored Markdown, inspirations, favorites, media state, topic sources, jobs, chat history, or reminder state.
- Every production behavior change must begin with a failing regression test.
- Existing valid configurations and database rebuilds must continue to work.
- Use lock order: per-work capture lock, then entry-operation lock, then short-lived Vault lock.
- Never block the asyncio event loop while waiting for a filesystem lock.

---

### Task 1: Secure Model Endpoint Normalization

**Files:**
- Modify: `src/douyin_wiki/config.py`
- Modify: `src/douyin_wiki/cli.py`
- Modify: `src/douyin_wiki/webapp/app.py`
- Test: `tests/test_setup.py`
- Test: `tests/test_web.py`

**Interfaces:**
- Produces: `normalize_llm_base_url(value: str) -> str`, used by `LLMSettings`, CLI configuration, and Web settings.
- Preserves: `llm_api_key_required(base_url: str) -> bool` and all settings response keys.

- [ ] **Step 1: Add failing normalizer/config tests**

```python
@pytest.mark.parametrize("url", [
    "http://models.example/v1",
    "ftp://models.example/v1",
    "https://user:secret@models.example/v1",
    "https://models.example/v1#fragment",
])
def test_llm_settings_reject_unsafe_endpoints(url: str) -> None:
    with pytest.raises(ValueError):
        LLMSettings(base_url=url)

@pytest.mark.parametrize("url", [
    "http://localhost:11434/v1/",
    "http://127.0.0.1:1234/v1/",
    "http://[::1]:1234/v1/",
    "https://models.example/v1/",
])
def test_llm_settings_normalize_safe_endpoints(url: str) -> None:
    assert not LLMSettings(base_url=url).base_url.endswith("/")
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run: `uv run pytest tests/test_setup.py -k 'unsafe_endpoints or normalize_safe_endpoints' -v`

Expected: unsafe remote HTTP/userinfo/fragment inputs are accepted or not normalized.

- [ ] **Step 3: Implement the shared validator**

```python
def normalize_llm_base_url(value: str) -> str:
    candidate = value.strip()
    parsed = urlsplit(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("模型接口必须是有效的 HTTP(S) URL")
    if parsed.username is not None or parsed.password is not None or parsed.fragment:
        raise ValueError("模型接口不能包含凭据或 URL 片段")
    loopback = parsed.hostname.lower() == "localhost"
    if not loopback:
        try:
            loopback = ip_address(parsed.hostname).is_loopback
        except ValueError:
            loopback = False
    if parsed.scheme == "http" and not loopback:
        raise ValueError("非本机模型接口必须使用 HTTPS")
    return candidate.rstrip("/")
```

Attach it to `LLMSettings.base_url` with a Pydantic `field_validator(mode="before")`. Call the same function before CLI/Web persistence so invalid values become actionable validation errors before any key is stored or provider is constructed. Error messages must not interpolate the supplied URL.

- [ ] **Step 4: Add and run Web/CLI regression coverage**

Add a Web test asserting remote HTTP returns `422`, no secret is persisted, and loopback HTTP still saves. Add a CLI/setup test calling the existing configuration path with remote HTTP and asserting it fails before `store_secret`.

Run: `uv run pytest tests/test_setup.py tests/test_web.py -k 'model or endpoint' -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/douyin_wiki/config.py src/douyin_wiki/cli.py src/douyin_wiki/webapp/app.py tests/test_setup.py tests/test_web.py
git commit -m "fix: reject insecure model endpoints"
```

### Task 2: Fault-Isolated Catalog Records

**Files:**
- Modify: `src/douyin_wiki/webapp/catalog.py`
- Test: `tests/test_web.py`

**Interfaces:**
- Produces: `LibraryCatalog.load_errors -> list[dict[str, str]]`, where every item has Vault-relative `path` and sanitized `error`.
- Preserves: `refresh() -> list[LibraryItem]`, `list_items()`, and startup behavior.

- [ ] **Step 1: Add a failing malformed-date isolation test**

```python
def test_catalog_skips_bad_date_and_keeps_valid_articles(tmp_path: Path) -> None:
    config, service = _web_fixture(tmp_path)
    bad = config.vault_path / "wiki" / "sources" / "坏日期_456.md"
    bad.write_text("---\ntype: source\nvideo_id: '456'\ncaptured_at: not-a-date\n---\n# 坏日期", encoding="utf-8")
    app = create_app(config, service=service, start_watcher=False)
    with TestClient(app) as client:
        response = client.get("/api/library")
        assert response.status_code == 200
        assert {item["entry_id"] for item in response.json()["items"]} == {"dy-123"}
    assert app.state.catalog.load_errors[0]["path"] == "wiki/sources/坏日期_456.md"
```

- [ ] **Step 2: Run it and verify RED**

Run: `uv run pytest tests/test_web.py::test_catalog_skips_bad_date_and_keeps_valid_articles -v`

Expected: catalog refresh/startup raises during date conversion.

- [ ] **Step 3: Move the whole record conversion inside the per-file boundary**

In `refresh`, call `_read_item` inside `try/except Exception`; append only `{"path": relative_path, "error": f"{type(exc).__name__}: {exc}"}` and continue. Reset the error list on every refresh, and expose a locked copy through `load_errors`. `_read_item` may still return `None` for intentionally ignored non-source Markdown, but malformed source data must raise into the boundary.

- [ ] **Step 4: Cover error clearing and run focused tests**

Repair the bad file, call `refresh()`, assert its error disappears and both entries load.

Run: `uv run pytest tests/test_web.py -k 'catalog or creator_sources or library' -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/douyin_wiki/webapp/catalog.py tests/test_web.py
git commit -m "fix: isolate malformed catalog records"
```

### Task 3: Read-Only Dynamic Markdown Articles

**Files:**
- Modify: `src/douyin_wiki/models.py`
- Modify: `src/douyin_wiki/webapp/catalog.py`
- Modify: `src/douyin_wiki/webapp/app.py`
- Modify: `src/douyin_wiki/webapp/static/app.js`
- Test: `tests/test_web.py`

**Interfaces:**
- Produces: `LibraryItem.database_managed: bool`, serialized unchanged by Pydantic API payloads.
- Consumes: catalog lookup plus `Database.get_entry()` to distinguish readable Markdown from mutable entries.

- [ ] **Step 1: Add failing API behavior tests**

Create valid source Markdown for `video_id: '456'` without inserting an `entries` row, then assert:

```python
item = next(item for item in client.get("/api/library").json()["items"] if item["entry_id"] == "dy-456")
assert item["database_managed"] is False
assert client.get("/api/articles/dy-456").status_code == 200
response = client.post("/api/chat/sessions", json={"scope": "entry", "context_entry_id": "dy-456"})
assert response.status_code == 409
assert service.database.list_chat_sessions() == []
```

Also assert the fixture-backed `dy-123` returns `database_managed is True` and can create entry chat.

- [ ] **Step 2: Run and verify RED**

Run: `uv run pytest tests/test_web.py -k 'unmanaged_markdown or database_managed' -v`

Expected: field is missing and unmanaged entry chat is created.

- [ ] **Step 3: Add the managed flag and server-side conflict**

Add `database_managed: bool = True` to `LibraryItem`; set it to `entry is not None` in `_read_item`. In `create_session`, require both a catalog item and `item.database_managed` for entry scope, returning HTTP `409` with a clear read-only explanation for an unmanaged article; keep `400` for absent/invalid IDs.

- [ ] **Step 4: Hide unsupported client mutations**

In card/list rendering, append favorite and topic-selection controls only when `item.database_managed`. In article rendering and chat-context selection, suppress favorite, delete, inspiration, and entry-chat actions for unmanaged items while leaving article reading and library chat intact. Ensure `toggleTopicEntry` cannot add an unmanaged ID even when called programmatically.

- [ ] **Step 5: Run focused Web tests**

Run: `uv run pytest tests/test_web.py -k 'article or chat or creator_sources or unmanaged or database_managed' -v`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/douyin_wiki/models.py src/douyin_wiki/webapp/catalog.py src/douyin_wiki/webapp/app.py src/douyin_wiki/webapp/static/app.js tests/test_web.py
git commit -m "fix: make unmanaged markdown read only"
```

### Task 4: Per-Work Capture Serialization

**Files:**
- Modify: `src/douyin_wiki/vault.py`
- Modify: `src/douyin_wiki/service.py`
- Test: `tests/test_service.py`
- Test: `tests/test_image_note.py`

**Interfaces:**
- Produces: `VaultWriter.work_capture_locked(work_id: str) -> ContextManager[None]` with lock files under `.douyin-wiki/locks/works/`.
- Consumes: resolved strict numeric Douyin work IDs and existing `_add_inspiration_locked`/repair paths.

- [ ] **Step 1: Add failing concurrent first-capture tests**

Use two jobs resolving to the same ID, a downloader that blocks the first call on an `asyncio.Event`, and two concurrent `service.process_claimed_job(...)` calls. Assert video and image-note variants each perform one download/analysis pipeline, both jobs complete, and the stored entry contains both distinct inspirations.

```python
first_task = asyncio.create_task(service.process_claimed_job(first_claimed))
await downloader.started.wait()
second_task = asyncio.create_task(service.process_claimed_job(second_claimed))
downloader.release.set()
first, second = await asyncio.gather(first_task, second_task)
assert downloader.calls == 1
assert {item.text for item in service.database.get_entry(first.result["entry_id"]).inspirations} == {"一", "二"}
assert any(job.result.get("duplicate") for job in (first, second))
```

- [ ] **Step 2: Run and verify RED**

Run: `uv run pytest tests/test_service.py -k 'concurrent_first_capture' tests/test_image_note.py -k 'concurrent_first_capture' -v`

Expected: downloader/analysis runs twice or one inspiration is lost.

- [ ] **Step 3: Add the strict filesystem lock**

Validate `work_id` using the stable numeric Douyin ID grammar already accepted by resolution; reject unsafe values before path construction. Implement `work_capture_locked` using `fcntl.flock`, exception-safe unlock, and `.douyin-wiki/locks/works/<work_id>.lock`.

- [ ] **Step 4: Acquire it asynchronously after resolution**

Factor the post-resolution body into a helper. Enter the synchronous context manager from `asyncio.to_thread` (or an async wrapper whose blocking flock runs in `to_thread`) and release in `finally`. Once acquired, re-read the entry; merge inspirations through `_add_inspiration_locked`, repair projections, and return duplicate without expensive work. Apply the same outer lock before dispatching to the image-note path.

- [ ] **Step 5: Run capture regressions**

Run: `uv run pytest tests/test_service.py -k 'capture or duplicate' tests/test_image_note.py -v`

Expected: PASS with one expensive execution per stable work ID.

- [ ] **Step 6: Commit**

```bash
git add src/douyin_wiki/vault.py src/douyin_wiki/service.py tests/test_service.py tests/test_image_note.py
git commit -m "fix: serialize captures by work id"
```

### Task 5: Reanalysis Final-State Merge

**Files:**
- Modify: `src/douyin_wiki/service.py`
- Test: `tests/test_analysis_v2.py`

**Interfaces:**
- Produces: one locked finalization path that reloads `EntryRecord` and `data_json` and replaces only analysis-owned fields.
- Preserves: inspirations, favorite/retention/media state, paths/timestamps, metadata/creator/cover, transcript/OCR/review state, and created reminder state.

- [ ] **Step 1: Add a failing interleaving test**

Use a blocking analysis provider. Start reanalysis, wait until the provider has captured its old evidence, then call `add_inspiration` and `set_entry_favorite(True)`, release the provider, and assert the final entry keeps both changes while the new analysis title/summary/tags are installed.

- [ ] **Step 2: Run and verify RED**

Run: `uv run pytest tests/test_analysis_v2.py -k 'reanalysis_preserves_concurrent_user_state' -v`

Expected: the stale `entry`/`current_data` snapshot overwrites inspiration or favorite state.

- [ ] **Step 3: Implement locked latest-state finalization**

After validating model output, enter `vault.entry_operations_locked()`, call `database.get_entry(entry_id)` and `database.get_entry_data(entry_id)`, update only `analysis`, `provider`, `model`, and `prompt_version` in data, and only `title`, `summary`, `tags`, `updated_at` in the latest entry. Prepare chunks/relations/reminders from that merged pair. Write documents and persist the bundle without reacquiring the entry lock (extract a `_persist_entry_documents_and_bundle_locked` helper if required). Let missing-entry errors propagate; never upsert from the stale snapshot.

- [ ] **Step 4: Cover deletion and run focused tests**

Add a test deleting the entry while analysis is blocked and assert completion raises the existing not-found error and no row/documents are recreated.

Run: `uv run pytest tests/test_analysis_v2.py -k 'reanalysis' -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/douyin_wiki/service.py tests/test_analysis_v2.py
git commit -m "fix: merge reanalysis into latest entry state"
```

### Task 6: Topic Artifact Optimistic Version Check

**Files:**
- Modify: `src/douyin_wiki/service.py`
- Test: `tests/test_topics.py`

**Interfaces:**
- Produces: generated artifacts whose `source_revision`/`source_revisions` describe the request evidence and whose `status` reflects the latest topic revision.
- Preserves: current topic sources and aborts if the topic was deleted.

- [ ] **Step 1: Add a failing source-change-during-generation test**

Use a provider that calls `service.set_topic_sources(...)` from its `stream` before yielding a cited answer. Assert the returned artifact is `needs_update`, retains the old source revision/list, and `service.get_topic(topic_id)["topic"]["sources"]` contains the newly selected sources.

- [ ] **Step 2: Run and verify RED**

Run: `uv run pytest tests/test_topics.py -k 'source_changes_during_generation' -v`

Expected: `_persist_topic(topic)` rewrites the pre-generation topic snapshot or artifact remains `current`.

- [ ] **Step 3: Reload and compare before saving**

After the provider returns, call `_refresh_topic(topic_id)` again. Construct the artifact using the original request topic revision and request `revisions`; set `status="current"` only when the latest `source_revision` equals the request revision, otherwise `"needs_update"`. Save the artifact, then persist the reloaded latest topic. If reload raises not-found, save nothing and propagate.

- [ ] **Step 4: Add deleted-topic coverage and run tests**

Run: `uv run pytest tests/test_topics.py -v`

Expected: PASS; stale output remains auditable without rolling topic sources backward.

- [ ] **Step 5: Commit**

```bash
git add src/douyin_wiki/service.py tests/test_topics.py
git commit -m "fix: detect stale topic generation"
```

### Task 7: Atomic Validated Database Rebuild

**Files:**
- Modify: `src/douyin_wiki/vault.py`
- Modify: `src/douyin_wiki/database.py`
- Modify: `src/douyin_wiki/service.py`
- Test: `tests/test_database.py`
- Test: `tests/test_service.py`
- Test: `tests/test_creator.py`
- Test: `tests/test_topics.py`

**Interfaces:**
- Produces: `VaultWriter.last_creator_load_errors`, `last_topic_load_errors`, `last_artifact_load_errors`; strict creator/topic loading; `Database.replace_knowledge_cache(...)` using one connection/transaction.
- Consumes: fully prepared entry bundles, creator bundles, topic bundles, and embedding signature; no indexer/database writes during preflight.
- Preserves: `jobs`, `job_events`, `creator_run_items`, valid `creator_works.last_job_id` links, Web chat rows, and maintenance history.

- [ ] **Step 1: Add failing strict-loader tests**

Corrupt one creator sidecar payload, one topic frontmatter record, and one referenced/missing artifact. For each, run dry-run and assert the Vault-relative path appears in the matching error collection; run apply and assert it raises while the original entry/topic/creator rows remain.

- [ ] **Step 2: Run and verify RED**

Run: `uv run pytest tests/test_service.py tests/test_creator.py tests/test_topics.py -k 'rebuild and (creator or topic or artifact)' -v`

Expected: corrupt records are silently skipped and apply proceeds.

- [ ] **Step 3: Make all Vault loaders collect structured errors**

Reset each error list per load. Treat missing YAML blocks, invalid top-level values/models, missing artifact files, malformed YAML, and unsafe IDs/paths as errors rather than `continue`. Return valid bundles for dry-run counts but make any non-empty collection block apply.

- [ ] **Step 4: Add a failing transaction rollback test**

Prepare a valid rebuild, monkeypatch a connection-level restore helper to raise after base entries are inserted, call apply, and assert the complete original knowledge projection (entries, chunks, creators, topics, artifacts, index metadata) is unchanged. Also assert a job/event, creator run item and its `last_job_id` link, chat session/message, and maintenance record survive a successful rebuild.

- [ ] **Step 5: Run and verify RED**

Run: `uv run pytest tests/test_database.py -k 'replace_knowledge_cache or rebuild_rollback' -v`

Expected: current multi-connection clear/restore leaves an empty or partial projection.

- [ ] **Step 6: Extract connection-scoped persistence helpers**

Create private `_upsert_entry_conn`, `_persist_entry_bundle_conn`, `_restore_creator_bundle_conn`, `_restore_topic_bundle_conn`, and `_set_index_metadata_conn` helpers that accept one `sqlite3.Connection`. Keep public methods as transaction-owning wrappers so existing call sites do not change.

- [ ] **Step 7: Implement one atomic replacement**

```python
def replace_knowledge_cache(self, *, entries, creators, topics, embedding_signature):
    with self.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        self._clear_knowledge_cache_conn(conn, include_creators=True)
        for entry, data, chunks, relations, reminders in entries:
            self._persist_entry_bundle_conn(conn, entry, data, chunks, relations, reminders)
        for creator, works in creators:
            self._restore_creator_bundle_conn(conn, creator, works)
        for topic, artifacts in topics:
            self._restore_topic_bundle_conn(conn, topic, artifacts)
        self._set_index_metadata_conn(conn, "embedding_signature", embedding_signature)
```

The connection context must roll back on every exception. Before deleting creators, snapshot `creator_run_items` and existing valid `creator_works.last_job_id` relationships inside the transaction; after restoring creators/works, restore rows whose referenced job and work still exist. Clear only rebuildable knowledge tables; preserve jobs/events, creator job progress, chat, and maintenance.

- [ ] **Step 8: Move all preparation before mutation**

In `_rebuild_database_from_vault_locked`, build entry chunks with `persist=False`, validate reminders and relations against the complete prepared entry-ID set, validate every creator/topic/artifact, compute the embedding signature without writing it, and include all four error collections in dry-run. Apply calls only `replace_knowledge_cache` after all collections are empty.

- [ ] **Step 9: Run rebuild and full subsystem tests**

Run: `uv run pytest tests/test_database.py tests/test_service.py tests/test_creator.py tests/test_topics.py -k 'rebuild or replace_knowledge_cache or database_can_be_rebuilt' -v`

Expected: PASS with rollback integrity and non-knowledge state preserved.

- [ ] **Step 10: Commit**

```bash
git add src/douyin_wiki/vault.py src/douyin_wiki/database.py src/douyin_wiki/service.py tests/test_database.py tests/test_service.py tests/test_creator.py tests/test_topics.py
git commit -m "fix: rebuild knowledge cache atomically"
```

### Task 8: Whole-Project Verification

**Files:**
- Verify only: all production and test files changed by Tasks 1-7

**Interfaces:**
- Consumes: all seven fixes and their regression tests.
- Produces: clean full-suite and Ruff evidence.

- [ ] **Step 1: Run the complete offline suite**

Run: `uv run pytest -m 'not live'`

Expected: all tests PASS.

- [ ] **Step 2: Run lint**

Run: `uv run ruff check .`

Expected: no errors.

- [ ] **Step 3: Verify the worktree boundary**

Run: `git status --short && git diff --check`

Expected: `scripts/hermes_event_monitor.py` remains an unrelated unstaged deletion and is absent from every task commit; no whitespace errors.
