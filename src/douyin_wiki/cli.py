from __future__ import annotations

import asyncio
import json
import webbrowser
from pathlib import Path
from typing import Annotated

import typer

from .auth_guidance import SubprocessAuthGuidanceLauncher
from .config import (
    AppConfig,
    default_config_path,
    llm_api_key_required,
    load_config,
    normalize_llm_base_url,
)
from .errors import DouyinWikiError
from .localization import (
    localize_for_user,
    parse_creator_decision,
    parse_job_status,
    parse_retention,
)
from .models import (
    AnalysisMode,
    CaptureOptions,
    CreatorWorkDecision,
    GatewayContext,
    InspirationInput,
    ReviewIssue,
    SourceKind,
    TranscriptCorrection,
)
from .secrets import store_secret
from .service import DouyinWikiService
from .setup import (
    LaunchAgentInstaller,
    VaultSetupMode,
    WebLaunchAgentInstaller,
    obsidian_vault_status,
    update_config_values,
    validate_vault_target,
    write_config,
)
from .setup import doctor as run_doctor
from .worker import Worker

app = typer.Typer(help="抖库：本地优先的抖音 AI 知识库")
jobs_app = typer.Typer(help="任务队列")
review_app = typer.Typer(help="逐字稿人工校对")
entry_app = typer.Typer(help="知识条目")
inspiration_app = typer.Typer(help="用户灵感")
reminder_app = typer.Typer(help="提醒事项")
maintenance_app = typer.Typer(help="知识库维护")
database_app = typer.Typer(help="可重建 SQLite 状态库")
worker_app = typer.Typer(help="后台 worker")
service_app = typer.Typer(help="macOS LaunchAgent")
gateway_app = typer.Typer(help="OpenClaw/Hermes Gateway Agent 交接")
auth_app = typer.Typer(help="浏览器登录与授权")
creator_app = typer.Typer(help="抖音博主批量采集与手动同步")
web_app = typer.Typer(help="抖库本机网页")
topic_app = typer.Typer(help="选定来源的专题研究")
app.add_typer(jobs_app, name="jobs")
app.add_typer(review_app, name="review")
app.add_typer(entry_app, name="entry")
app.add_typer(inspiration_app, name="inspiration")
app.add_typer(inspiration_app, name="purpose", hidden=True)
app.add_typer(reminder_app, name="reminder")
app.add_typer(maintenance_app, name="maintenance")
app.add_typer(database_app, name="database")
app.add_typer(worker_app, name="worker")
app.add_typer(service_app, name="service")
app.add_typer(gateway_app, name="gateway")
app.add_typer(auth_app, name="auth")
app.add_typer(creator_app, name="creator")
app.add_typer(web_app, name="web")
app.add_typer(topic_app, name="topic")


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


def _print(value) -> None:
    value = localize_for_user(value)
    typer.echo(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def _job_status_option(value: str):
    try:
        return parse_job_status(value)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc


def _creator_decision_option(value: str):
    try:
        return parse_creator_decision(value)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc


def _retention_option(value: str):
    try:
        return parse_retention(value)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc


def _parse_number_ranges(value: str | None) -> list[int]:
    if not value:
        return []
    result: set[int] = set()
    for raw in value.replace("，", ",").split(","):
        item = raw.strip()
        if not item:
            continue
        if "-" in item:
            start_text, end_text = item.split("-", 1)
            try:
                start, end = int(start_text), int(end_text)
            except ValueError as exc:
                raise typer.BadParameter(f"无效编号范围：{item}") from exc
            if start < 1 or end < start:
                raise typer.BadParameter(f"无效编号范围：{item}")
            result.update(range(start, end + 1))
        else:
            try:
                number = int(item)
            except ValueError as exc:
                raise typer.BadParameter(f"无效编号：{item}") from exc
            if number < 1:
                raise typer.BadParameter(f"无效编号：{item}")
            result.add(number)
    return sorted(result)


def _prompt_vault_target() -> tuple[Path, VaultSetupMode]:
    typer.echo("首次初始化需要先准备 Obsidian Vault。")
    typer.echo("  1. 新建独立 Vault（推荐）")
    typer.echo("  2. 使用已有 Obsidian Vault（只补充缺失文件，不覆盖已有笔记）")
    choice = typer.prompt("请选择", default="1").strip().lower()
    if choice in {"1", "new"}:
        parent = Path(
            typer.prompt(
                "Vault 上级目录",
                default=str(Path.home() / "Documents" / "Obsidian"),
            )
        )
        name = typer.prompt("Vault 名称", default="抖库").strip()
        if not name or Path(name).name != name or name in {".", ".."}:
            raise typer.BadParameter("Vault 名称只能是单个目录名称")
        return parent / name, VaultSetupMode.NEW
    if choice in {"2", "existing"}:
        path = Path(typer.prompt("已有 Vault 根目录"))
        return path, VaultSetupMode.EXISTING
    raise typer.BadParameter("请选择 1（新建）或 2（使用已有 Vault）")


@app.command("init")
def init_command(
    vault: Annotated[Path | None, typer.Option(help="Obsidian Vault 路径")] = None,
    config_path: Annotated[Path | None, typer.Option(help="配置文件路径")] = None,
    overwrite_config: Annotated[bool, typer.Option(help="覆盖已有配置")] = False,
) -> None:
    target_path = config_path or default_config_path()
    config_exists = target_path.exists()
    if config_exists:
        config = load_config(target_path)
        selected = vault or config.vault_path
        mode = VaultSetupMode.EXISTING if selected.expanduser().exists() else VaultSetupMode.NEW
        try:
            selected = validate_vault_target(selected, mode)
        except ValueError as exc:
            raise typer.BadParameter(str(exc)) from exc
        if vault and config.vault_path.expanduser().resolve() != selected and not overwrite_config:
            raise typer.BadParameter("已有配置使用其他 Vault；如需替换，请添加 --overwrite-config")
        if vault and overwrite_config:
            config = config.model_copy(update={"vault_path": selected})
    else:
        if vault:
            selected = vault
            mode = VaultSetupMode.EXISTING if selected.expanduser().exists() else VaultSetupMode.NEW
        else:
            selected, mode = _prompt_vault_target()
        try:
            selected = validate_vault_target(selected, mode)
        except ValueError as exc:
            raise typer.BadParameter(str(exc)) from exc
        config = AppConfig(vault_path=selected)

    service = DouyinWikiService(config)
    service.initialize(initialize_git=True)
    target = write_config(
        config,
        target_path,
        overwrite=overwrite_config or not config_exists,
    )
    obsidian = obsidian_vault_status(config.vault_path)
    next_steps = []
    if obsidian["action"]:
        next_steps.append(obsidian["action"])
    next_steps.extend(
        [
            "启动后台 worker：uv run douyin-wiki service install",
            "将 douyin-wiki-mcp 添加到 OpenClaw/Hermes，并允许 Gateway 工作流工具",
            "可选：若不使用 Gateway，再运行 configure-model 切换为 provider 模式",
        ]
    )
    _print(
        {
            "config_path": str(target),
            "vault_path": str(config.vault_path),
            "vault_mode": mode.value,
            "status": "initialized",
            "obsidian": obsidian,
            "next_steps": next_steps,
        }
    )


@app.command("configure-model")
def configure_model(
    model: Annotated[str, typer.Option(prompt=True, help="模型名称")],
    base_url: Annotated[str | None, typer.Option(help="OpenAI-compatible base URL")] = None,
    config_path: Annotated[Path | None, typer.Option()] = None,
) -> None:
    target = config_path or default_config_path()
    config = load_config(target)
    try:
        resolved_base_url = normalize_llm_base_url(base_url or config.llm.base_url)
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--base-url") from exc
    api_key: str | None = None
    if llm_api_key_required(resolved_base_url):
        api_key = typer.prompt(
            "API key",
            hide_input=True,
            confirmation_prompt=True,
        )
    llm = config.llm.model_copy(
        update={
            "enabled": True,
            "model": model,
            "base_url": resolved_base_url,
        }
    )
    config = config.model_copy(update={"llm": llm, "analysis_mode": AnalysisMode.PROVIDER})
    if target.exists():
        update_config_values(
            target,
            {
                None: {"analysis_mode": AnalysisMode.PROVIDER.value},
                "llm": {
                    "enabled": True,
                    "model": config.llm.model,
                    "base_url": config.llm.base_url,
                },
            },
        )
    else:
        write_config(config, target, overwrite=True)
    if api_key:
        store_secret(config.llm.api_key_env, api_key)
    installer = LaunchAgentInstaller(target)
    worker_plist = installer.launch_agents / f"{installer.WORKER_LABEL}.plist"
    restarted = False
    if worker_plist.exists():
        installer.install()
        restarted = True
    _print(
        {
            "status": "configured",
            "model": config.llm.model,
            "base_url": config.llm.base_url,
            "analysis_mode": config.analysis_mode.value,
            "secret": (
                f"macOS Keychain:{config.llm.api_key_env}" if api_key else "本机接口无需密钥"
            ),
            "worker_restarted": restarted,
        }
    )


@app.command("configure-analysis-mode")
def configure_analysis_mode(
    mode: Annotated[
        AnalysisMode,
        typer.Argument(help="gateway（推荐）、provider 或 local"),
    ] = AnalysisMode.GATEWAY,
    config_path: Annotated[Path | None, typer.Option()] = None,
) -> None:
    target = config_path or default_config_path()
    config = load_config(target).model_copy(update={"analysis_mode": mode})
    if target.exists():
        update_config_values(target, {None: {"analysis_mode": mode.value}})
    else:
        write_config(config, target, overwrite=True)
    installer = LaunchAgentInstaller(target)
    worker_plist = installer.launch_agents / f"{installer.WORKER_LABEL}.plist"
    restarted = False
    if worker_plist.exists():
        installer.install()
        restarted = True
    _print(
        {
            "status": "configured",
            "analysis_mode": mode.value,
            "worker_restarted": restarted,
            "note": (
                "校正和分析由 Gateway Agent 完成"
                if mode == AnalysisMode.GATEWAY
                else "后台 Worker 将直接完成校正和分析"
            ),
        }
    )


@app.command("capture")
def capture_command(
    share_text: Annotated[str, typer.Argument(help="整段抖音分享文本")],
    inspiration: Annotated[
        list[str] | None,
        typer.Option(
            "--inspiration",
            "-i",
            "--purpose",
            "-p",
            help="保存这条视频时触发你的灵感；旧 --purpose 仍兼容",
        ),
    ] = None,
    quote: Annotated[str | None, typer.Option()] = None,
    start_ms: Annotated[int | None, typer.Option()] = None,
    end_ms: Annotated[int | None, typer.Option()] = None,
    retention: Annotated[
        str, typer.Option(help="媒体保留方式：临时保留、永久保留或处理后清理")
    ] = "临时保留",
    allow_long: Annotated[bool, typer.Option()] = False,
    approve_cloud_analysis: Annotated[bool, typer.Option()] = False,
    approve_ai_analysis: Annotated[bool, typer.Option()] = False,
    gateway: Annotated[str | None, typer.Option(help="例如 openclaw 或 hermes")] = None,
    channel: Annotated[str | None, typer.Option()] = None,
    conversation_id: Annotated[str | None, typer.Option()] = None,
    message_id: Annotated[str | None, typer.Option()] = None,
    reply_target: Annotated[str | None, typer.Option()] = None,
    config_path: Annotated[Path | None, typer.Option()] = None,
) -> None:
    inspirations = [InspirationInput(text=value) for value in (inspiration or [])]
    anchored = any(value is not None for value in (quote, start_ms, end_ms))
    if anchored and len(inspirations) != 1:
        raise typer.BadParameter("quote/start-ms/end-ms 必须与且仅与一条 --inspiration 一起使用")
    if inspirations and anchored:
        inspirations[0] = inspirations[0].model_copy(
            update={"quote": quote, "start_ms": start_ms, "end_ms": end_ms}
        )
    job = _service(config_path).capture_douyin(
        share_text,
        inspirations,
        CaptureOptions(
            retention=_retention_option(retention),
            allow_long=allow_long,
            approve_cloud_analysis=approve_ai_analysis or approve_cloud_analysis,
        ),
        GatewayContext(
            gateway=gateway,
            channel=channel,
            conversation_id=conversation_id,
            message_id=message_id,
            reply_target=reply_target,
        )
        if gateway
        else None,
    )
    _print(job)


@creator_app.command("add")
def creator_add(
    source_text: Annotated[str, typer.Argument(help="博主主页或其任意作品分享文本")],
    inspiration: Annotated[list[str] | None, typer.Option("--inspiration", "-i")] = None,
    retention: Annotated[
        str, typer.Option(help="媒体保留方式：临时保留、永久保留或处理后清理")
    ] = "临时保留",
    allow_long: bool = False,
    gateway: str | None = None,
    conversation_id: str | None = None,
    config_path: Path | None = None,
) -> None:
    context = GatewayContext(gateway=gateway, conversation_id=conversation_id) if gateway else None
    _print(
        _service(config_path).capture_douyin_creator(
            source_text,
            [InspirationInput(text=value) for value in (inspiration or [])],
            CaptureOptions(retention=_retention_option(retention), allow_long=allow_long),
            context,
        )
    )


@creator_app.command("inventory")
def creator_inventory(
    job_id: str,
    page: int | None = None,
    limit: int | None = None,
    decision: Annotated[
        str | None, typer.Option(help="按作品状态筛选，例如：待入库、已选入库、未入库、已入库")
    ] = None,
    source_kind: SourceKind | None = None,
    query: str | None = None,
    config_path: Path | None = None,
) -> None:
    _print(
        _service(config_path).get_creator_inventory(
            job_id,
            page=page,
            limit=limit,
            decision=_creator_decision_option(decision) if decision else None,
            source_kind=source_kind,
            query=query,
        )
    )


@creator_app.command("select")
def creator_select(
    job_id: str,
    include: Annotated[str | None, typer.Option(help="例如 1-10,15")] = None,
    exclude: Annotated[str | None, typer.Option(help="例如 11-14")] = None,
    config_path: Path | None = None,
) -> None:
    if not include and not exclude:
        raise typer.BadParameter("至少提供 --include 或 --exclude")
    service = _service(config_path)
    results = []
    if include:
        results.append(
            service.set_creator_work_selection(
                job_id,
                CreatorWorkDecision.SELECTED,
                ordinals=_parse_number_ranges(include),
            )
        )
    if exclude:
        results.append(
            service.set_creator_work_selection(
                job_id,
                CreatorWorkDecision.SKIPPED,
                ordinals=_parse_number_ranges(exclude),
            )
        )
    _print(results[-1] if len(results) == 1 else {"updates": results})


@creator_app.command("confirm")
def creator_confirm(
    job_id: str,
    accept_partial: bool = False,
    config_path: Path | None = None,
) -> None:
    _print(_service(config_path).confirm_creator_import(job_id, accept_partial=accept_partial))


@creator_app.command("import")
def creator_import(
    creator_id: str,
    work_id: Annotated[list[str], typer.Option("--work-id")],
    gateway: str | None = None,
    conversation_id: str | None = None,
    config_path: Path | None = None,
) -> None:
    context = GatewayContext(gateway=gateway, conversation_id=conversation_id) if gateway else None
    _print(_service(config_path).import_creator_works(creator_id, work_id, gateway_context=context))


@creator_app.command("show")
def creator_show(creator_id: str, config_path: Path | None = None) -> None:
    _print(_service(config_path).get_creator(creator_id))


@creator_app.command("list")
def creator_list(config_path: Path | None = None) -> None:
    _print(_service(config_path).list_creators())


@creator_app.command("works")
def creator_works(creator_id: str, config_path: Path | None = None) -> None:
    _print(_service(config_path).list_creator_works(creator_id))


@creator_app.command("sync")
def creator_sync(
    creator_id: str,
    gateway: str | None = None,
    conversation_id: str | None = None,
    config_path: Path | None = None,
) -> None:
    context = GatewayContext(gateway=gateway, conversation_id=conversation_id) if gateway else None
    _print(_service(config_path).sync_creator(creator_id, gateway_context=context))


@auth_app.command("douyin")
def auth_douyin(
    timeout_seconds: Annotated[
        int, typer.Option(min=30, max=1800, help="等待浏览器登录的秒数")
    ] = 600,
    config_path: Annotated[Path | None, typer.Option()] = None,
) -> None:
    typer.echo("正在打开抖库专用浏览器，请在窗口中完成抖音登录……")
    _print(asyncio.run(_service(config_path).authenticate_douyin(timeout_seconds=timeout_seconds)))


@auth_app.command("video")
def auth_video(
    config_path: Annotated[Path | None, typer.Option()] = None,
) -> None:
    typer.echo("正在打开视频下载所使用的浏览器，请在浏览器中完成抖音登录……")
    _print(asyncio.run(_service(config_path).authenticate_video()))


@auth_app.command("status")
def auth_status(
    video_url: Annotated[
        str | None,
        typer.Option(help="可选抖音视频 URL；提供后由 yt-dlp 执行只读联网探测"),
    ] = None,
    config_path: Annotated[Path | None, typer.Option()] = None,
) -> None:
    _print(asyncio.run(_service(config_path).get_auth_status(video_url=video_url)))


@jobs_app.command("get")
def jobs_get(job_id: str, config_path: Path | None = None) -> None:
    _print(_service(config_path).get_job(job_id))


@jobs_app.command("list")
def jobs_list(
    status: Annotated[
        str | None, typer.Option(help="按中文任务状态筛选，例如：待处理、正在下载、已完成")
    ] = None,
    limit: int = 50,
    config_path: Path | None = None,
) -> None:
    _print(_service(config_path).list_jobs(_job_status_option(status) if status else None, limit))


@jobs_app.command("events")
def jobs_events(
    after_event_id: int = 0,
    all_events: bool = False,
    limit: int = 50,
    config_path: Path | None = None,
) -> None:
    _print(
        _service(config_path).list_job_events(
            after_event_id=after_event_id,
            unacknowledged_only=not all_events,
            limit=limit,
        )
    )


@jobs_app.command("ack-event")
def jobs_ack_event(event_id: int, config_path: Path | None = None) -> None:
    _print(_service(config_path).acknowledge_job_event(event_id))


@jobs_app.command("approve")
def jobs_approve(job_id: str, config_path: Path | None = None) -> None:
    _print(_service(config_path).approve_job(job_id))


@jobs_app.command("retry")
def jobs_retry(job_id: str, config_path: Path | None = None) -> None:
    _print(_service(config_path).retry_job(job_id))


@review_app.command("resolve")
def review_resolve(
    job_id: str,
    resolution: Annotated[list[str] | None, typer.Option(help="issue_id=修正文字")] = None,
    accept_uncertain: bool = False,
    config_path: Path | None = None,
) -> None:
    parsed = {}
    for value in resolution or []:
        if "=" not in value:
            raise typer.BadParameter("resolution 必须使用 issue_id=修正文字")
        issue_id, text = value.split("=", 1)
        parsed[issue_id] = text
    _print(_service(config_path).resolve_review(job_id, parsed, accept_uncertain=accept_uncertain))


@gateway_app.command("context")
def gateway_context(job_id: str, config_path: Path | None = None) -> None:
    _print(_service(config_path).get_analysis_context(job_id))


@gateway_app.command("monitor-events", hidden=True)
def gateway_monitor_events(config_path: Path | None = None) -> None:
    """Stable, silent-when-idle output for a Gateway monitor script."""
    events = _service(config_path).list_job_events(limit=100)
    if not events:
        return
    typer.echo(
        json.dumps(
            [event.model_dump(mode="json") for event in events],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )


@gateway_app.command("submit-correction")
def gateway_submit_correction(
    job_id: str,
    json_file: Path,
    producer: Annotated[str, typer.Option(help="Gateway/Agent 名称")],
    model: Annotated[str, typer.Option(help="生成校正的模型")] = "agent",
    config_path: Path | None = None,
) -> None:
    payload = json.loads(json_file.read_text(encoding="utf-8"))
    corrections_data = payload.get("corrections", []) if isinstance(payload, dict) else payload
    issues_data = payload.get("review_issues", []) if isinstance(payload, dict) else []
    _print(
        _service(config_path).submit_transcript_correction(
            job_id,
            [TranscriptCorrection.model_validate(item) for item in corrections_data],
            producer=producer,
            model=model,
            review_issues=[ReviewIssue.model_validate(item) for item in issues_data],
        )
    )


@gateway_app.command("submit-analysis")
def gateway_submit_analysis(
    job_id: str,
    json_file: Path,
    producer: Annotated[str, typer.Option(help="Gateway/Agent 名称")],
    model: Annotated[str, typer.Option(help="生成分析的模型")] = "agent",
    config_path: Path | None = None,
) -> None:
    payload = json.loads(json_file.read_text(encoding="utf-8"))
    _print(
        _service(config_path).submit_gateway_analysis(
            job_id, payload, producer=producer, model=model
        )
    )


@app.command("search")
def search_command(
    query: str, include_stale: bool = False, limit: int = 10, config_path: Path | None = None
) -> None:
    _print(_service(config_path).search_knowledge(query, include_stale=include_stale, limit=limit))


@topic_app.command("create")
def topic_create(
    title: str,
    entry_id: Annotated[list[str], typer.Option("--entry-id", help="专题来源文章 ID，可重复")],
    goal: Annotated[str, typer.Option(help="研究目标")] = "",
    instructions: Annotated[str, typer.Option(help="专题自定义指令")] = "",
    config_path: Path | None = None,
) -> None:
    _print(
        _service(config_path).create_topic(
            title,
            entry_id,
            goal=goal,
            instructions=instructions,
        )
    )


@topic_app.command("list")
def topic_list(config_path: Path | None = None) -> None:
    _print(_service(config_path).list_topics())


@topic_app.command("show")
def topic_show(topic_id: str, config_path: Path | None = None) -> None:
    _print(_service(config_path).get_topic(topic_id))


@topic_app.command("sources")
def topic_sources(
    topic_id: str,
    enable: Annotated[list[str] | None, typer.Option("--enable", help="启用文章 ID")] = None,
    disable: Annotated[list[str] | None, typer.Option("--disable", help="停用文章 ID")] = None,
    config_path: Path | None = None,
) -> None:
    service = _service(config_path)
    value = service.get_topic(topic_id)
    enabled_ids = set(enable or [])
    disabled_ids = set(disable or [])
    sources = [
        {
            "entry_id": source["entry_id"],
            "enabled": (
                True
                if source["entry_id"] in enabled_ids
                else False
                if source["entry_id"] in disabled_ids
                else source["enabled"]
            ),
        }
        for source in value["topic"]["sources"]
    ]
    _print(service.set_topic_sources(topic_id, sources))


@topic_app.command("search")
def topic_search(
    topic_id: str,
    query: str,
    include_stale: bool = False,
    limit: int = 10,
    config_path: Path | None = None,
) -> None:
    _print(
        _service(config_path).search_topic(
            topic_id, query, include_stale=include_stale, limit=limit
        )
    )


@topic_app.command("generate")
def topic_generate(
    topic_id: str,
    kind: Annotated[
        str,
        typer.Option(
            help="成果类型：overview/comparison/evidence_map/consensus/decision_brief/faq"
        ),
    ] = "overview",
    config_path: Path | None = None,
) -> None:
    allowed = {"overview", "comparison", "evidence_map", "consensus", "decision_brief", "faq"}
    if kind not in allowed:
        raise typer.BadParameter("不支持的成果类型")
    _print(asyncio.run(_service(config_path).generate_topic_artifact(topic_id, kind)))


@entry_app.command("show")
def entry_show(entry_id: str, documents: bool = False, config_path: Path | None = None) -> None:
    _print(_service(config_path).get_entry(entry_id, include_documents=documents))


@entry_app.command("submit-analysis")
def entry_submit_analysis(
    entry_id: str,
    json_file: Path,
    producer: Annotated[str, typer.Option(help="提交分析的 Agent")],
    model: Annotated[str, typer.Option(help="生成分析的模型")] = "agent",
    config_path: Path | None = None,
) -> None:
    payload = json.loads(json_file.read_text(encoding="utf-8"))
    _print(_service(config_path).submit_analysis(entry_id, payload, producer=producer, model=model))


@entry_app.command("reanalyze")
def entry_reanalyze(
    entry_id: str,
    force: bool = False,
    gateway: str | None = None,
    conversation_id: str | None = None,
    config_path: Path | None = None,
) -> None:
    context = GatewayContext(gateway=gateway, conversation_id=conversation_id) if gateway else None
    _print(_service(config_path).reanalyze_entry(entry_id, force=force, gateway_context=context))


@entry_app.command("reanalyze-all")
def entry_reanalyze_all(
    force: bool = False,
    gateway: str | None = None,
    conversation_id: str | None = None,
    config_path: Path | None = None,
) -> None:
    context = GatewayContext(gateway=gateway, conversation_id=conversation_id) if gateway else None
    _print(_service(config_path).reanalyze_all(force=force, gateway_context=context))


@inspiration_app.command("add")
def inspiration_add(
    entry_id: str,
    text: str,
    quote: str | None = None,
    start_ms: int | None = None,
    end_ms: int | None = None,
    config_path: Path | None = None,
) -> None:
    _print(
        _service(config_path).add_inspiration(
            entry_id,
            InspirationInput(text=text, quote=quote, start_ms=start_ms, end_ms=end_ms),
        )
    )


@app.command("migrate-vocabulary", hidden=True)
def migrate_vocabulary(config_path: Path | None = None) -> None:
    _print(_service(config_path).migrate_inspiration_vocabulary())


@app.command("migrate-remove-validation", hidden=True)
def migrate_remove_validation(config_path: Path | None = None) -> None:
    _print(_service(config_path).remove_external_validation())


@app.command("migrate-covers", hidden=True)
def migrate_covers(config_path: Path | None = None) -> None:
    _print(_service(config_path).backfill_video_covers())


@reminder_app.command("confirm")
def reminder_confirm(
    entry_id: str,
    reminder_id: str,
    due_at: str | None = None,
    title: str | None = None,
    config_path: Path | None = None,
) -> None:
    _print(
        _service(config_path).confirm_reminder(
            entry_id,
            reminder_id,
            due_at=due_at,
            title=title,
            confirmed=True,
        )
    )


@maintenance_app.command("run")
def maintenance_run(apply: bool = False, config_path: Path | None = None) -> None:
    _print(_service(config_path).run_maintenance(apply=apply))


@database_app.command("rebuild")
def database_rebuild(apply: bool = False, config_path: Path | None = None) -> None:
    """从隐藏侧车重建博主、作品、FTS、向量、关系与提醒缓存。"""
    _print(_service(config_path).rebuild_database_from_vault(apply=apply))


@worker_app.command("run")
def worker_run(
    forever: bool = False,
    once: bool = False,
    config_path: Path | None = None,
) -> None:
    if forever and once:
        raise typer.BadParameter("--forever 和 --once 不能同时使用")
    worker = Worker(_service(config_path))
    result = asyncio.run(worker.run_forever() if forever else worker.run_once())
    if not forever:
        _print(result.model_dump(mode="json") if result else {"status": "idle"})


@app.command("doctor")
def doctor_command(config_path: Path | None = None) -> None:
    _print(run_doctor(load_config(config_path)))


@service_app.command("install")
def service_install(config_path: Path | None = None) -> None:
    paths = LaunchAgentInstaller(config_path).install()
    _print({"installed": [str(path) for path in paths]})


@service_app.command("uninstall")
def service_uninstall(config_path: Path | None = None) -> None:
    paths = LaunchAgentInstaller(config_path).uninstall()
    _print({"removed": [str(path) for path in paths]})


@web_app.command("run")
def web_run(config_path: Path | None = None) -> None:
    """在前台运行本机网页服务。"""
    from .webapp import run_web

    run_web(config_path)


@web_app.command("open")
def web_open(config_path: Path | None = None) -> None:
    """使用默认浏览器打开抖库网页。"""
    config = load_config(config_path)
    address = f"http://{config.web.host}:{config.web.port}"
    opened = webbrowser.open(address)
    _print({"status": "已打开" if opened else "请手动打开", "address": address})


@web_app.command("install")
def web_install(config_path: Path | None = None) -> None:
    """登录 macOS 后自动运行并保持抖库网页存活。"""
    target = config_path or default_config_path()
    path = WebLaunchAgentInstaller(target).install()
    config = load_config(target)
    _print(
        {
            "status": "安装完成",
            "launch_agent": str(path),
            "address": f"http://{config.web.host}:{config.web.port}",
        }
    )


@web_app.command("uninstall")
def web_uninstall(config_path: Path | None = None) -> None:
    """移除网页常驻服务；不会删除资料库或聊天记录。"""
    removed = WebLaunchAgentInstaller(config_path).uninstall()
    _print({"status": "已移除" if removed else "未安装", "removed": str(removed or "")})


@web_app.command("status")
def web_status(config_path: Path | None = None) -> None:
    """检查网页服务与本机地址。"""
    config = load_config(config_path)
    _print(WebLaunchAgentInstaller(config_path).status(config))


def main() -> None:
    try:
        app()
    except DouyinWikiError as exc:
        typer.echo(
            json.dumps(
                {
                    "ok": False,
                    "error": {
                        "code": exc.code,
                        "message": str(exc),
                        "details": exc.details,
                    },
                },
                ensure_ascii=False,
            )
        )
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
