# macOS Authorization Guidance and Short-Link Resolution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Correct Douyin short-link source types from the final redirect and guide macOS users through expired authorization with a non-blocking system dialog, automatic verification, and safe task retry.

**Architecture:** The resolver will consume the complete HTTP redirect chain before choosing the canonical source type. A short-lived authorization helper process will own macOS dialogs and browser authorization while the Worker remains free; the helper will coordinate through existing service APIs, a per-channel file lock, and persisted `needs_auth` jobs.

**Tech Stack:** Python 3.12/3.13, asyncio, httpx, Pydantic, SQLite, `fcntl`, `subprocess`, macOS `osascript`, pytest, Ruff, uv.

**Spec:** `docs/superpowers/specs/2026-08-30-macos-auth-guidance-and-shortlink-resolution-design.md`

## Global Constraints

- The feature runs only on macOS when `[auth_guidance].enabled = true`; all other environments retain the current Agent/CLI instructions.
- Never read, return, log, or display Cookie values, passwords, Local Storage, or browser secrets.
- Use argument arrays for `subprocess` calls; never use `shell=True` or interpolate user text into AppleScript source.
- Persist `needs_auth` before launching the helper; helper launch failures must not replace the job error or block the Worker.
- Automatic retry applies only to jobs still in `needs_auth` with a matching authorization scope.
- Tests must use temporary Vaults and fake dialog/browser/process boundaries; they must not access the real config, Vault, browser profile, or macOS GUI.
- Follow strict RED → GREEN → REFACTOR for every production behavior.

---

### Task 1: Resolve short links from the final redirect

**Files:**
- Modify: `tests/test_share.py`
- Modify: `src/douyin_wiki/adapters/share.py`

**Interfaces:**
- Consumes: `extract_work_identity(value: str) -> tuple[str, SourceKind] | None`
- Produces: `DouyinShareResolver.resolve(share_text: str) -> ResolvedShare` whose `source_kind` and `canonical_url` reflect the final recognized redirect.

- [ ] **Step 1: Add failing redirect-chain tests**

Add literal, hand-checked cases to `tests/test_share.py`:

```python
@pytest.mark.asyncio
async def test_final_note_redirect_overrides_intermediate_video_route() -> None:
    work_id = "7659645255277039717"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "v.douyin.com":
            return httpx.Response(
                302,
                headers={"location": f"https://www.iesdouyin.com/share/video/{work_id}/"},
            )
        if request.url.host == "www.iesdouyin.com":
            return httpx.Response(
                302,
                headers={
                    "location": (
                        f"https://www.douyin.com/note/{work_id}"
                        "?previous_page=web_code_link"
                    )
                },
            )
        return httpx.Response(200)

    result = await DouyinShareResolver(
        transport=httpx.MockTransport(handler)
    ).resolve("https://v.douyin.com/example/")

    assert result.video_id == work_id
    assert result.source_kind == SourceKind.IMAGE_NOTE
    assert result.canonical_url == f"https://www.douyin.com/note/{work_id}"
    assert result.redirect_chain[-1].startswith(f"https://www.douyin.com/note/{work_id}")


@pytest.mark.asyncio
async def test_redirect_chain_rejects_conflicting_work_ids() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "v.douyin.com":
            return httpx.Response(
                302,
                headers={"location": "https://www.douyin.com/video/7659645255277039717"},
            )
        return httpx.Response(
            302,
            headers={"location": "https://www.douyin.com/note/7678561149449331835"},
        )

    with pytest.raises(InvalidShareTextError, match="作品 ID 不一致"):
        await DouyinShareResolver(
            transport=httpx.MockTransport(handler)
        ).resolve("https://v.douyin.com/example/")
```

Add this test helper and the separately named cases with literal expectations:

```python
def resolver_with_redirects(*locations: str) -> DouyinShareResolver:
    remaining = list(locations)

    def handler(_: httpx.Request) -> httpx.Response:
        if remaining:
            return httpx.Response(302, headers={"location": remaining.pop(0)})
        return httpx.Response(200)

    return DouyinShareResolver(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_final_video_redirect_stays_video() -> None:
    work_id = "7659645255277039717"
    result = await resolver_with_redirects(
        f"https://www.douyin.com/video/{work_id}"
    ).resolve("https://v.douyin.com/example/")
    assert result.source_kind == SourceKind.VIDEO
    assert result.canonical_url == f"https://www.douyin.com/video/{work_id}"


@pytest.mark.asyncio
async def test_final_video_redirect_overrides_intermediate_note_route() -> None:
    work_id = "7659645255277039717"
    result = await resolver_with_redirects(
        f"https://www.douyin.com/note/{work_id}",
        f"https://www.douyin.com/video/{work_id}",
    ).resolve("https://v.douyin.com/example/")
    assert result.source_kind == SourceKind.VIDEO
    assert result.canonical_url == f"https://www.douyin.com/video/{work_id}"


@pytest.mark.asyncio
async def test_short_link_redirect_limit_is_rejected() -> None:
    resolver = resolver_with_redirects(
        *(f"https://v.douyin.com/hop-{index}/" for index in range(9))
    )
    with pytest.raises(InvalidShareTextError, match="重定向次数过多"):
        await resolver.resolve("https://v.douyin.com/example/")


@pytest.mark.asyncio
async def test_direct_canonical_url_never_calls_transport() -> None:
    def unexpected_request(_: httpx.Request) -> httpx.Response:
        raise AssertionError("canonical URL must not perform an HTTP request")

    work_id = "7659645255277039717"
    resolver = DouyinShareResolver(transport=httpx.MockTransport(unexpected_request))
    result = await resolver.resolve(f"https://www.douyin.com/note/{work_id}")
    assert result.source_kind == SourceKind.IMAGE_NOTE
```

- [ ] **Step 2: Run the new tests and verify RED**

Run:

```bash
uv run --extra dev --locked pytest \
  tests/test_share.py::test_final_note_redirect_overrides_intermediate_video_route \
  tests/test_share.py::test_redirect_chain_rejects_conflicting_work_ids -v
```

Expected: the first test reports `SourceKind.VIDEO`; the second does not raise the required conflict error.

- [ ] **Step 3: Implement final-redirect resolution**

Refactor only the short-link branch of `DouyinShareResolver.resolve()`:

```python
observed_identity: tuple[str, SourceKind] | None = None

for _ in range(8):
    response = await client.get(current, follow_redirects=False)
    response_identity = extract_work_identity(str(response.url))
    if response_identity:
        observed_identity = _merge_identity(observed_identity, response_identity)
    location = response.headers.get("location")
    if not location:
        if observed_identity:
            return _resolved(original_url, observed_identity, chain)
        break
    current = urljoin(current, location)
    chain.append(current)
    location_identity = extract_work_identity(current)
    if location_identity:
        observed_identity = _merge_identity(observed_identity, location_identity)
else:
    raise InvalidShareTextError(
        "抖音短链重定向次数过多",
        details={"redirect_chain": chain},
    )
```

Add `_merge_identity()` so the same ID may update type while different IDs raise
`InvalidShareTextError("短链重定向中的作品 ID 不一致", details={"redirect_chain": chain})`.

- [ ] **Step 4: Run all resolver tests and verify GREEN**

Run:

```bash
uv run --extra dev --locked pytest tests/test_share.py -v
```

Expected: all tests pass and direct canonical URLs perform no HTTP request.

- [ ] **Step 5: Commit the resolver fix**

```bash
git add tests/test_share.py src/douyin_wiki/adapters/share.py
git commit -m "fix: resolve final Douyin short-link type"
```

---

### Task 2: Add authorization guidance configuration and scoped checks

**Files:**
- Modify: `tests/test_setup.py`
- Modify: `tests/test_service.py`
- Modify: `src/douyin_wiki/config.py`
- Modify: `src/douyin_wiki/service.py`

**Interfaces:**
- Produces: `AuthGuidanceSettings(enabled: bool, timeout_seconds: int, poll_seconds: float)`
- Produces: `DouyinWikiService.check_auth_scope(scope: str, video_url: str | None = None) -> AuthCheckResult`
- Preserves: `get_auth_status(video_url=None) -> dict[str, Any]` and `cookie_values_exposed=False`.

- [ ] **Step 1: Add failing configuration tests**

Add to `tests/test_setup.py`:

```python
def test_auth_guidance_defaults_round_trip_through_toml(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(render_default_config(AppConfig()), encoding="utf-8")

    loaded = load_config(config_path)

    assert loaded.auth_guidance.enabled is True
    assert loaded.auth_guidance.timeout_seconds == 600
    assert loaded.auth_guidance.poll_seconds == 5
    content = config_path.read_text(encoding="utf-8")
    assert "[auth_guidance]" in content
```

- [ ] **Step 2: Verify the configuration test fails**

Run:

```bash
uv run --extra dev --locked pytest \
  tests/test_setup.py::test_auth_guidance_defaults_round_trip_through_toml -v
```

Expected: FAIL because `AppConfig` has no `auth_guidance` field.

- [ ] **Step 3: Implement configuration**

Add to `src/douyin_wiki/config.py`:

```python
class AuthGuidanceSettings(BaseModel):
    enabled: bool = True
    timeout_seconds: int = Field(default=600, ge=30, le=1800)
    poll_seconds: float = Field(default=5, ge=2, le=30)


class AppConfig(BaseModel):
    auth_guidance: AuthGuidanceSettings = Field(default_factory=AuthGuidanceSettings)
```

Render the exact `[auth_guidance]` TOML section from the model values.

- [ ] **Step 4: Add failing scope-isolation tests**

In `tests/test_service.py`, use complete fake `AuthCheckResult` values and assert that
`check_auth_scope("image_note")` calls only the image-note adapter, while
`check_auth_scope("video", video_url="https://www.douyin.com/video/7659645255277039717")`
passes that exact URL only to the video adapter. The production change that makes these tests fail is probing unrelated adapters.

```python
result = await service.check_auth_scope(
    "video",
    video_url="https://www.douyin.com/video/7659645255277039717",
)
assert result.scope == "video"
assert result.server_verified is True
assert image_note_downloader.check_calls == 0
assert creator_adapter.check_calls == 0
```

- [ ] **Step 5: Verify scoped checks fail before implementation**

Run the new named tests with `pytest -v` and confirm failure is an `AttributeError` for
`check_auth_scope`, not fake setup failure.

- [ ] **Step 6: Implement scoped checks and refactor aggregate status**

Implement:

```python
async def check_auth_scope(
    self,
    scope: str,
    *,
    video_url: str | None = None,
) -> AuthCheckResult:
    if scope == "video":
        adapter = self.downloader
        kwargs = {"video_url": video_url}
        source = self.config.media.browser
    elif scope == "image_note":
        adapter = self.image_note_downloader
        kwargs = {}
        source = str(self.config.browser_profile_dir)
    elif scope == "creator":
        adapter = self.creator_adapter
        kwargs = {}
        source = str(self.config.browser_profile_dir)
    else:
        raise ValueError(f"不支持的授权范围：{scope}")
    if not hasattr(adapter, "check_auth"):
        return AuthCheckResult(
            scope=scope,
            state="unavailable",
            ok=False,
            cookie_source=source,
            message="当前注入的适配器不支持认证状态检查",
        )
    return await adapter.check_auth(**kwargs)
```

Make `get_auth_status()` gather three calls to this method and retain its existing JSON contract.

- [ ] **Step 7: Run targeted configuration and service tests**

```bash
uv run --extra dev --locked pytest tests/test_setup.py tests/test_service.py -v
```

Expected: all targeted tests pass.

- [ ] **Step 8: Commit configuration and scoped checks**

```bash
git add tests/test_setup.py tests/test_service.py src/douyin_wiki/config.py src/douyin_wiki/service.py
git commit -m "feat: add scoped authorization guidance settings"
```

---

### Task 3: Build the authorization coordinator with no GUI dependencies

**Files:**
- Create: `tests/test_auth_guidance.py`
- Create: `src/douyin_wiki/auth_guidance.py`
- Modify: `src/douyin_wiki/database.py`
- Modify: `tests/test_database.py`

**Interfaces:**
- Produces: `AuthChannel = Literal["video", "douyin"]`
- Produces: `AuthGuidanceLauncher.launch(scope: str, trigger_job_id: str) -> bool`
- Produces: `NoopAuthGuidanceLauncher`
- Produces: `AuthGuidanceCoordinator.run(scope: str, trigger_job_id: str) -> AuthGuidanceOutcome`
- Produces: `Database.list_jobs(status: JobStatus | None = None, limit: int | None = 50)`.

- [ ] **Step 1: Add a failing unlimited-job query test**

In `tests/test_database.py`, create 55 `needs_auth` jobs using the real temporary database, then assert:

```python
jobs = database.list_jobs(status=JobStatus.NEEDS_AUTH, limit=None)
assert len(jobs) == 55
```

Verify RED: current `LIMIT ?` cannot bind `None` as the intended unlimited query.

- [ ] **Step 2: Implement `limit=None` without changing the default**

Build SQL so `LIMIT ?` is appended only when `limit is not None`. Keep descending creation order and existing callers unchanged. Run `tests/test_database.py` to GREEN and commit with Task 3 at the end.

- [ ] **Step 3: Add coordinator behavior tests before its implementation**

Create `tests/test_auth_guidance.py` with real temporary `Database` jobs and fakes only for external boundaries. Cover these observable outcomes:

```python
@dataclass
class GuidanceHarness:
    database: Database
    coordinator: AuthGuidanceCoordinator
    dialog: FakeDialog
    auth: FakeAuthBackend
    channel_lock: FakeChannelLock
    jobs: dict[str, JobRecord]


@pytest.mark.asyncio
async def test_confirmed_video_auth_retries_only_matching_paused_jobs(
    guidance_harness: GuidanceHarness,
):
    harness = guidance_harness
    outcome = await harness.coordinator.run("video", harness.jobs["trigger_video"].id)
    assert outcome.status == "retried"
    assert outcome.retried_job_ids == (
        harness.jobs["trigger_video"].id,
        harness.jobs["second_video"].id,
    )
    assert harness.database.get_job(
        harness.jobs["image_note"].id
    ).status == JobStatus.NEEDS_AUTH
    assert harness.database.get_job(
        harness.jobs["completed"].id
    ).status == JobStatus.COMPLETED


@pytest.mark.asyncio
async def test_cancel_keeps_jobs_paused_and_does_not_open_browser(
    guidance_harness: GuidanceHarness,
):
    harness = guidance_harness
    harness.dialog.confirmed = False
    outcome = await harness.coordinator.run("image_note", harness.jobs["image_note"].id)
    assert outcome.status == "cancelled"
    assert harness.database.get_job(
        harness.jobs["image_note"].id
    ).status == JobStatus.NEEDS_AUTH
    assert harness.auth.authenticate_calls == []


@pytest.mark.asyncio
async def test_video_timeout_never_retries_job(
    guidance_harness: GuidanceHarness,
):
    harness = guidance_harness
    harness.auth.video_checks_always_fail = True
    outcome = await harness.coordinator.run("video", harness.jobs["trigger_video"].id)
    assert outcome.status == "timeout"
    assert harness.database.get_job(
        harness.jobs["trigger_video"].id
    ).status == JobStatus.NEEDS_AUTH
```

Add these exact cases:

```python
@pytest.mark.asyncio
async def test_video_without_canonical_url_fails_without_retry(
    guidance_harness: GuidanceHarness,
):
    harness = guidance_harness
    trigger_job = harness.jobs["video_without_resolved"]
    outcome = await harness.coordinator.run("video", trigger_job.id)
    assert outcome.status == "failed"
    assert harness.database.get_job(trigger_job.id).status == JobStatus.NEEDS_AUTH


@pytest.mark.asyncio
async def test_job_that_leaves_needs_auth_during_verification_is_not_requeued(
    guidance_harness: GuidanceHarness,
):
    harness = guidance_harness
    trigger_job = harness.jobs["trigger_video"]
    harness.auth.fail_job_before_ready = trigger_job.id
    outcome = await harness.coordinator.run("video", trigger_job.id)
    assert outcome.retried_job_ids == ()
    assert harness.database.get_job(trigger_job.id).status == JobStatus.FAILED


@pytest.mark.asyncio
async def test_douyin_channel_retries_only_ready_scopes(
    guidance_harness: GuidanceHarness,
):
    harness = guidance_harness
    harness.auth.ready_scopes = {"image_note"}
    image_job = harness.jobs["image_note"]
    creator_job = harness.jobs["creator"]
    outcome = await harness.coordinator.run("image_note", image_job.id)
    assert outcome.retried_job_ids == (image_job.id,)
    assert harness.database.get_job(creator_job.id).status == JobStatus.NEEDS_AUTH


@pytest.mark.asyncio
async def test_busy_channel_lock_suppresses_duplicate_prompt(
    guidance_harness: GuidanceHarness,
):
    harness = guidance_harness
    harness.channel_lock.available = False
    outcome = await harness.coordinator.run("creator", harness.jobs["creator"].id)
    assert outcome.status == "unavailable"
    assert harness.dialog.confirm_calls == []
```

- [ ] **Step 4: Run coordinator tests and verify RED**

```bash
uv run --extra dev --locked pytest tests/test_auth_guidance.py -v
```

Expected: collection/import failure because the coordinator does not exist.

- [ ] **Step 5: Implement minimal coordinator ports and outcome model**

Define explicit protocols and values in `auth_guidance.py`:

```python
AuthChannel = Literal["video", "douyin"]


@dataclass(frozen=True)
class AuthGuidanceOutcome:
    status: Literal["cancelled", "unavailable", "timeout", "failed", "retried"]
    retried_job_ids: tuple[str, ...] = ()
    message: str = ""


class AuthGuidanceLauncher(Protocol):
    def launch(self, *, scope: str, trigger_job_id: str) -> bool: ...


class NoopAuthGuidanceLauncher:
    def launch(self, *, scope: str, trigger_job_id: str) -> bool:
        return False
```

Inject a dialog protocol, a non-blocking channel-lock protocol, a clock/sleep function, and a service protocol. The coordinator must not import or execute `osascript` or `subprocess`.

- [ ] **Step 6: Implement the minimal confirm/auth/check/retry state machine**

Required order:

```text
acquire channel lock
  → read affected needs_auth jobs
  → recheck auth before prompting
  → prompt once if still required
  → authenticate selected channel
  → poll/verify applicable scopes
  → reread needs_auth jobs
  → retry only matching current states
  → notify outcome
  → release lock
```

For the `douyin` channel, verify `image_note` and `creator` independently and retry only scopes whose check returns `ok=True`. For video, require both `ok=True` and `server_verified=True`.

- [ ] **Step 7: Run coordinator and database tests to GREEN**

```bash
uv run --extra dev --locked pytest tests/test_auth_guidance.py tests/test_database.py -v
```

Expected: all tests pass without opening a GUI or reading a real browser profile.

- [ ] **Step 8: Commit the coordinator core**

```bash
git add tests/test_auth_guidance.py tests/test_database.py \
  src/douyin_wiki/auth_guidance.py src/douyin_wiki/database.py
git commit -m "feat: coordinate automatic authorization recovery"
```

---

### Task 4: Add the macOS dialog, lock, and detached launcher

**Files:**
- Modify: `tests/test_auth_guidance.py`
- Modify: `src/douyin_wiki/auth_guidance.py`

**Interfaces:**
- Produces: `MacOSDialog.confirm(channel, affected_counts) -> bool`
- Produces: `MacOSDialog.notify(title, message) -> None`
- Produces: `FileChannelLock.acquire(channel) -> ContextManager[bool]`
- Produces: `SubprocessAuthGuidanceLauncher(config_path: Path, settings: AuthGuidanceSettings)`
- Produces: module entry point `python -m douyin_wiki.auth_guidance --config-path PATH --scope SCOPE --job-id ID`.

- [ ] **Step 1: Add failing command-boundary tests**

Use an injected command runner and process launcher. Assert literal argv and consumer-visible results, not implementation source text:

```python
confirmed = dialog.confirm("video", {"video": 2})
assert confirmed is True
assert runner.commands == [[
    "osascript",
    "-e",
    DIALOG_SCRIPT,
    "--",
    "视频",
    "2",
]]

launched = launcher.launch(scope="image_note", trigger_job_id="a" * 32)
assert launched is True
assert process_launcher.commands == [[
    sys.executable,
    "-m",
    "douyin_wiki.auth_guidance",
    "--config-path",
    str(config_path),
    "--scope",
    "image_note",
    "--job-id",
    "a" * 32,
]]
```

Add cases for Cancel exit code, malformed scope, malformed job ID, disabled settings, non-Darwin platform, command failure, and two processes contending for the same `douyin` lock.

- [ ] **Step 2: Run the new adapter tests and verify RED**

Run only the new named tests and confirm missing classes/functions are the failure cause.

- [ ] **Step 3: Implement fixed AppleScript templates and argument passing**

Use constant AppleScript source that reads values from `argv`; never interpolate job titles, errors, or share text into source. Run commands through an injected callable using:

```python
subprocess.run(
    argv,
    check=False,
    capture_output=True,
    text=True,
    timeout=30,
)
```

Interpret only the expected button result. Do not include command output in user messages or logs.

- [ ] **Step 4: Implement advisory file locks**

Open the exact state-directory lock file and acquire `fcntl.LOCK_EX | fcntl.LOCK_NB`. Map `image_note` and `creator` to `auth-guidance-douyin.lock`; map `video` to `auth-guidance-video.lock`. Hold the file descriptor for the coordinator lifetime and always release it in `finally`.

- [ ] **Step 5: Implement detached process launch and module entry point**

Validate job IDs with `re.fullmatch(r"[0-9a-f]{32}", value)` and scopes against the three exact values. Use:

```python
subprocess.Popen(
    argv,
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
    close_fds=True,
    start_new_session=True,
)
```

The module entry point loads only the passed config, initializes runtime state, constructs the coordinator, and exits with status 0 for success/cancel/timeout and nonzero only for malformed invocation or initialization failure.

- [ ] **Step 6: Run adapter tests to GREEN**

```bash
uv run --extra dev --locked pytest tests/test_auth_guidance.py -v
```

Expected: all tests pass with fake runners; no system dialog opens.

- [ ] **Step 7: Commit the macOS adapter**

```bash
git add tests/test_auth_guidance.py src/douyin_wiki/auth_guidance.py
git commit -m "feat: add native macOS authorization assistant"
```

---

### Task 5: Launch guidance only after `needs_auth` is persisted

**Files:**
- Modify: `tests/conftest.py`
- Modify: `tests/test_service.py`
- Modify: `tests/test_image_note.py`
- Modify: `src/douyin_wiki/service.py`
- Modify: `src/douyin_wiki/cli.py`

**Interfaces:**
- `DouyinWikiService(..., auth_guidance_launcher: AuthGuidanceLauncher | None = None)` defaults to `NoopAuthGuidanceLauncher`.
- CLI `_service(config_path)` injects `SubprocessAuthGuidanceLauncher` using the resolved config path.

- [ ] **Step 1: Add failing service-order and fallback tests**

Create an inspecting fake launcher that reads the real temporary database during `launch()`:

```python
class InspectingLauncher:
    def __init__(self, database):
        self.database = database
        self.calls = []

    def launch(self, *, scope: str, trigger_job_id: str) -> bool:
        persisted = self.database.get_job(trigger_job_id)
        self.calls.append((scope, trigger_job_id, persisted.status, persisted.result))
        return True
```

Extend the video and image-note authorization tests to assert:

```python
assert launcher.calls == [(
    "video",
    job.id,
    JobStatus.NEEDS_AUTH,
    paused.result,
)]
```

Add a launcher that raises `OSError` and assert the Worker still returns the original `needs_auth`, `cookie_required`/`browser_auth_required`, `next_command`, and unlocked state.

- [ ] **Step 2: Run the service tests and verify RED**

Run the two existing auth tests plus the new launch-failure test. Expected: no launcher calls because production integration is absent.

- [ ] **Step 3: Inject the no-op launcher and trigger after persistence**

Add the constructor dependency and, in the auth exception branch, use the already persisted `outcome`:

```python
try:
    self.auth_guidance_launcher.launch(
        scope=str(outcome.result["auth_scope"]),
        trigger_job_id=job.id,
    )
except Exception:
    pass
```

Do not change `outcome`, its error fields, or its retry command when launch fails.

- [ ] **Step 4: Inject the real launcher at the CLI composition root**

Resolve the exact config path once:

```python
def _service(config_path: Path | None = None) -> DouyinWikiService:
    resolved_config_path = config_path or default_config_path()
    config = load_config(resolved_config_path)
    service = DouyinWikiService(
        config,
        auth_guidance_launcher=SubprocessAuthGuidanceLauncher(
            config_path=resolved_config_path,
            settings=config.auth_guidance,
        ),
    )
    service.initialize_runtime()
    return service
```

Keep direct library/test construction no-op unless a launcher is explicitly injected.

- [ ] **Step 5: Run service, image-note, and CLI/setup tests to GREEN**

```bash
uv run --extra dev --locked pytest \
  tests/test_service.py tests/test_image_note.py tests/test_setup.py -v
```

Expected: all pass and tests open no GUI.

- [ ] **Step 6: Commit Worker integration**

```bash
git add tests/conftest.py tests/test_service.py tests/test_image_note.py \
  src/douyin_wiki/service.py src/douyin_wiki/cli.py
git commit -m "feat: launch auth recovery after paused jobs persist"
```

---

### Task 6: Update durable documentation and run release-grade verification

**Files:**
- Modify: `README.md`
- Modify: `docs/gateway-agents.md`
- Modify: `docs/troubleshooting-auth-and-shortlink-type.md`
- Modify: `CHANGELOG.md`

**Interfaces:**
- Documents the system dialog, Cancel behavior, automatic verification/retry, config switch, and corrected final-redirect semantics.

- [ ] **Step 1: Update user and Gateway documentation**

Document these exact behaviors:

- `auth_guidance.enabled=false` disables system dialogs without disabling manual commands.
- “稍后处理” leaves the job paused and does not create a prompt loop.
- Successful authorization retries only matching jobs still paused for authorization.
- Agent instructions remain the fallback and must not request Cookie values.
- Re-submitting a canonical `/note/` URL is an old-version workaround, not the expected post-fix flow.

- [ ] **Step 2: Run documentation and static checks**

```bash
git diff --check
uv run --extra dev --locked ruff check .
```

Expected: both commands exit 0.

- [ ] **Step 3: Run all offline tests with required optional dependencies**

```bash
uv run --extra dev --extra embeddings --locked pytest
```

Expected: all non-live tests pass; live tests remain skipped without `DOUYIN_WIKI_RUN_LIVE=1`.

- [ ] **Step 4: Build the release artifacts**

```bash
uv build
```

Expected: source distribution and wheel build successfully from the locked source tree.

- [ ] **Step 5: Run the repository verification gate when present**

```bash
./scripts/verify
```

Expected: exit 0. If the target branch still lacks this script, record that exact baseline limitation and preserve the Ruff, full pytest, and build evidence from Steps 2–4.

- [ ] **Step 6: Review the complete diff against `main`**

```bash
git diff --check main...HEAD
git diff --stat main...HEAD
git status --short
```

Confirm no real config, browser profile, Vault data, SQLite database, media, generated distribution, `.venv`, or Cookie information is tracked.

- [ ] **Step 7: Commit documentation**

```bash
git add README.md CHANGELOG.md docs/gateway-agents.md \
  docs/troubleshooting-auth-and-shortlink-type.md
git commit -m "docs: explain automatic authorization recovery"
```

- [ ] **Step 8: Stop before live deployment**

Do not reinstall LaunchAgents, run a real capture, mutate the real database, or perform the manual GUI authorization check until the user explicitly approves live verification and deployment from the long-lived `main` checkout.
