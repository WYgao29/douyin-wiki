(() => {
  "use strict";
  const D = window.Douku;
  if (!D) return;
  let pollTimer = null;
  let events = null;

  function stop() {
    clearTimeout(pollTimer);
    pollTimer = null;
    if (events) {
      events.close();
      events = null;
    }
  }

  function schedule(path) {
    stop();
    events = new EventSource("/api/events");
    const reload = () => {
      if (location.pathname === path || location.pathname.startsWith("/jobs")) {
        window.dispatchEvent(new CustomEvent("douku:route", {detail: {path: location.pathname}}));
      }
    };
    ["jobs", "auth", "library"].forEach((name) => events.addEventListener(name, reload));
    events.addEventListener("error", () => {
      events?.close();
      events = null;
      pollTimer = setTimeout(() => { loadFromPath(location.pathname); schedule(path); }, 8000);
    });
  }

  function routePath(value) {
    return String(value || "").split("?")[0];
  }

  function jobTitle(job) {
    return job.display_title || job.kind_label || job.kind;
  }

  function progressLabel(job) {
    if (job.status === "needs_selection") return "等待你";
    return `${Math.round((job.progress || 0) * 100)}%`;
  }

  function completionLine(job) {
    const warnings = Array.isArray(job.result?.warnings) ? job.result.warnings.filter(Boolean) : [];
    if (job.status === "completed_with_warnings" && warnings.length) {
      const first = String(warnings[0]);
      return warnings.length > 1 ? `${first}（另有 ${warnings.length - 1} 条提示）` : first;
    }
    const selection = job.result?.selection;
    if (selection && typeof selection === "object" && Number.isFinite(Number(selection.total))) {
      return `已入库 ${Number(selection.imported || 0)} / 共 ${selection.total} 个作品`;
    }
    const summary = job.result?.summary;
    if (summary && typeof summary === "object" && Number.isFinite(Number(summary.discovered))) {
      return `发现 ${summary.discovered} · 已入库 ${summary.imported || 0} · 失败 ${summary.failed || 0}`;
    }
    if (typeof summary === "string" && summary.trim()) return summary.trim();
    return "";
  }

  function listMessage(job) {
    if (job.status === "completed" || job.status === "completed_with_warnings") {
      const line = completionLine(job);
      if (line) return line;
    }
    return job.message_for_user || "";
  }

  function attentionRank(job) {
    if (!job.requires_user_action) return 3;
    if (job.status === "needs_selection") return 0;
    if (job.status === "failed") return 2;
    return 1;
  }

  function attentionGroup(job) {
    if (job.requires_user_action && job.status === "needs_selection") return "select";
    if (job.requires_user_action && job.status === "failed") return "failed";
    if (job.requires_user_action) return "attention";
    return "rest";
  }

  function actionButton(action) {
    if (!action) return null;
    const button = D.node("button", action.label, action.code === "retry" ? "secondary-button" : "primary-button");
    button.type = "button";
    button.addEventListener("click", async () => {
      try {
        if (action.endpoint) {
          await D.api(action.endpoint, {method: "POST", body: JSON.stringify({})});
          D.toast("已提交");
          loadFromPath(routePath(location.pathname));
        } else if (action.href) {
          D.navigate(action.href);
        }
      } catch (error) {
        D.toast(error.message);
      }
    });
    return button;
  }

  function dismissButton(job, {reloadDetail = false} = {}) {
    if (job.status !== "failed" || job.dismissed || !job.requires_user_action) return null;
    const button = D.node("button", "不再提醒", "secondary-button");
    button.type = "button";
    button.addEventListener("click", async () => {
      try {
        await D.api(`/api/jobs/${encodeURIComponent(job.id)}/dismiss`, {method: "POST", body: JSON.stringify({})});
        D.toast("已不再提醒");
        if (reloadDetail) await loadDetail(job.id);
        else await loadFromPath(routePath(location.pathname));
      } catch (error) {
        D.toast(error.message);
      }
    });
    return button;
  }

  function statusCluster(job) {
    const cluster = D.node("div", null, "job-status-cluster");
    const status = D.node("span", job.state_label, "status-pill");
    status.dataset.state = job.status;
    cluster.append(status);
    if (job.dismissed) cluster.append(D.node("span", "已忽略", "job-dismissed-mark"));
    return cluster;
  }

  function renderRow(job) {
    const row = D.node("article", null, "job-row");
    row.tabIndex = 0;
    row.setAttribute("role", "link");
    if (job.requires_user_action) row.dataset.attention = attentionGroup(job);
    const title = D.node("h2", jobTitle(job));
    const message = D.node("p", listMessage(job));
    if (job.status === "completed" || job.status === "completed_with_warnings") message.classList.add("job-oneline");
    const updated = String(job.updated_display || "").replace(/（北京时间）/g, "").trim();
    const meta = D.node("small", `${job.stage_label} · ${progressLabel(job)}${updated ? ` · ${updated}` : ""}`);
    row.append(title, statusCluster(job), message, meta);
    const actions = D.node("div", null, "job-row-actions");
    if (job.requires_user_action && job.next_action) {
      const action = actionButton(job.next_action);
      if (action) actions.append(action);
    }
    const dismiss = dismissButton(job);
    if (dismiss) actions.append(dismiss);
    if (actions.childNodes.length) row.append(actions);
    row.addEventListener("click", (event) => {
      if (event.target.closest("button")) return;
      D.navigate(`/jobs/${job.id}`);
    });
    row.addEventListener("keydown", (event) => {
      if (event.key === "Enter") D.navigate(`/jobs/${job.id}`);
    });
    return row;
  }

  function renderList(data) {
    const root = D.$("jobs-list");
    if (!data.items.length) {
      root.replaceChildren(D.emptyState("没有符合条件的任务", "提交导入或等待后台处理后，任务会出现在这里。"));
      return;
    }
    const items = [...data.items].sort((a, b) => attentionRank(a) - attentionRank(b));
    const groups = [];
    for (const job of items) {
      const key = attentionGroup(job);
      const current = groups[groups.length - 1];
      if (!current || current.key !== key) groups.push({key, items: [job]});
      else current.items.push(job);
    }
    const titles = {
      select: "待选择作品",
      attention: "需要本人操作",
      failed: "失败可重试",
      rest: "其他任务",
    };
    const showHeads = groups.some((group) => group.key !== "rest");
    const nodes = [];
    for (const group of groups) {
      if (showHeads && titles[group.key]) {
        const heading = D.node("p", titles[group.key], "job-group-title");
        heading.setAttribute("role", "heading");
        heading.setAttribute("aria-level", "2");
        nodes.push(heading);
      }
      nodes.push(...group.items.map(renderRow));
    }
    root.replaceChildren(...nodes);
  }

  async function loadList() {
    const params = new URLSearchParams(location.search);
    const query = new URLSearchParams();
    if (params.get("kind")) query.set("kind", params.get("kind"));
    if (params.get("status")) query.set("status", params.get("status"));
    if (params.get("requires_user_action") === "1") query.set("requires_user_action", "true");
    const data = await D.api(`/api/jobs?${query}`);
    renderList(data);
    D.$("jobs-count").textContent = `${data.total} 个任务`;
    const kind = D.$("jobs-filter-kind");
    const action = D.$("jobs-filter-action");
    if (kind) kind.value = params.get("kind") || "";
    if (action) {
      if (params.get("requires_user_action") === "1") action.value = "waiting";
      else {
        const status = params.get("status") || "all";
        action.value = status === "completed_with_warnings" ? "completed" : status;
      }
    }
  }

  async function loadDetail(jobId) {
    const root = D.$("job-detail-content");
    const job = await D.api(`/api/jobs/${encodeURIComponent(jobId)}`);
    document.title = `${jobTitle(job)} · 抖库`;
    const header = D.node("header", null, "page-heading");
    const titleBox = document.createElement("div");
    titleBox.append(D.node("h1", jobTitle(job)), D.node("p", job.message_for_user));
    header.append(titleBox, statusCluster(job));
    const progress = D.node(
      "p",
      job.status === "needs_selection"
        ? `阶段 ${job.stage_label} · 等待你`
        : `阶段 ${job.stage_label} · 进度 ${Math.round((job.progress || 0) * 100)}%`
    );
    const modelProgress = D.node("p", "", "hint-copy");
    if (job.analysis_progress && job.status === "analyzing") {
      const state = job.analysis_progress;
      const phases = {correction: "字幕校正", analysis: "分段分析", merge: "汇总", evidence: "证据校验"};
      const count = state.phase === "merge" ? ` · 已完成 ${state.completed_chunks} 次合并`
        : state.phase === "evidence" ? "" : ` · ${state.completed_chunks}/${state.total_chunks} 批`;
      const responseAt = state.last_response_at ? new Date(state.last_response_at).toLocaleString("zh-CN") : "等待首个响应";
      const usage = state.usage?.prompt_tokens != null ? ` · 最近输入 ${state.usage.prompt_tokens} tokens，输出 ${state.usage.completion_tokens || 0} tokens${state.usage.retry_count ? `，重试 ${state.usage.retry_count} 次` : ""}` : "";
      const waitingMinutes = state.last_response_at ? Math.floor((Date.now() - Date.parse(state.last_response_at)) / 60000) : 0;
      const waiting = job.status === "analyzing" && waitingMinutes >= 5 ? ` · 当前批次已等待 ${waitingMinutes} 分钟` : "";
      const totalCalls = job.llm_stats?.successful_calls ? ` · 累计成功调用 ${job.llm_stats.successful_calls} 次` : "";
      const phaseLabel = state.waiting_for_resource ? "等待模型校正资源" : (phases[state.phase] || state.phase);
      modelProgress.textContent = `${phaseLabel}${count} · 最近模型响应 ${responseAt}${usage}${totalCalls}${waiting}`;
    }
    const actions = D.node("div", null, "operation-actions");
    const action = actionButton(job.next_action);
    if (action) actions.append(action);
    const dismiss = dismissButton(job, {reloadDetail: true});
    if (dismiss) actions.append(dismiss);
    const openEntryViaAction = job.next_action?.code === "open_entry";
    if (job.entry_id && !openEntryViaAction) {
      const link = D.node("a", "打开知识资料", "secondary-button");
      link.href = `/articles/${encodeURIComponent(job.entry_id)}`;
      actions.append(link);
    }
    const analysis = D.node("p", `${job.analysis_mode_label || ""}`, "hint-copy");
    const media = D.node("section", null, "review-panel");
    const providers = job.media_provenance || {};
    if (providers.asr || providers.ocr) {
      media.append(D.node("h2", "媒体识别模型"));
      for (const [label, info] of [["ASR", providers.asr], ["OCR", providers.ocr]]) {
        if (!info?.provider && !info?.confidence_note) continue;
        media.append(D.node("p", `${label}：${info.provider || "未记录"}${info.model ? ` · ${info.model}` : ""}${info.fallback_reason ? ` · 回退原因：${info.fallback_reason}` : ""}`));
        if (info.confidence_note) media.append(D.node("p", info.confidence_note, "hint-copy"));
      }
    }
    const modelHealth = D.node("p", "", "hint-copy");
    const timeline = D.node("ol", null, "job-timeline");
    for (const event of job.timeline || []) {
      const item = D.node("li", `${event.state_label} · ${event.created_display}`);
      timeline.append(item);
    }
    const children = D.node("section", null, "job-children");
    const childList = job.children || [];
    const stats = job.child_stats || {};
    const hasChildWork = childList.length > 0 || Number(stats.total || 0) > 0
      || Number(stats.completed || 0) > 0 || Number(stats.running || 0) > 0
      || Number(stats.waiting_user || 0) > 0 || Number(stats.failed || 0) > 0;
    if (hasChildWork) {
      children.append(D.node("h2", "子任务"));
      if (job.child_stats) {
        children.append(D.node("p", `成功 ${stats.completed || 0} · 处理中 ${stats.running || 0} · 等待本人 ${stats.waiting_user || 0} · 失败 ${stats.failed || 0}`));
      }
      for (const child of childList) {
        const row = D.node("button", `${jobTitle(child)} · ${child.state_label} · ${child.message_for_user}`, "job-child");
        row.type = "button";
        row.addEventListener("click", () => D.navigate(`/jobs/${child.id}`));
        children.append(row);
      }
    }
    const warningsPanel = D.node("section", null, "review-panel job-warnings");
    const warnings = Array.isArray(job.result?.warnings) ? job.result.warnings.filter(Boolean) : [];
    if (warnings.length) {
      warningsPanel.append(D.node("h2", "提示"));
      warningsPanel.append(D.node("p", "任务已结束，但仍有需要留意的提示：", "hint-copy"));
      const list = D.node("ul", null, "job-warning-list");
      for (const warning of warnings) {
        list.append(D.node("li", String(warning)));
      }
      warningsPanel.append(list);
    }
    const review = D.node("section", null, "review-panel");
    if (job.review_issues?.length) {
      review.append(D.node("h2", "历史校对疑点"));
      review.append(D.node("p", "此任务按旧策略暂停。点击“按新模型校对策略重试”会重新校正并继续分析。", "hint-copy"));
      job.review_issues.forEach((issue) => {
        review.append(D.node("p", `疑点 ${issue.id}（${issue.start_ms}-${issue.end_ms}ms${issue.image_index ? ` · 图 ${issue.image_index}` : ""}）：${issue.raw_text}；${issue.reason || ""}`));
      });
    }
    const evidenceAudit = D.node("section", null, "review-panel");
    if (job.analysis_evidence_audit?.length) {
      evidenceAudit.append(D.node("h2", `证据核验记录（${job.analysis_evidence_audit.length}）`));
      for (const item of job.analysis_evidence_audit) {
        evidenceAudit.append(D.node("p", `${item.kind}${item.id ? ` ${item.id}` : ""}${item.timestamp_ms == null ? "" : ` · ${item.timestamp_ms}ms`}：${item.reason}`));
      }
    }
    const parts = [header, progress];
    if (modelProgress.textContent) parts.push(modelProgress);
    if (job.analysis_mode === "provider") {
      parts.push(modelHealth);
      try {
        const health = await D.api("/api/system/model-health");
        modelHealth.textContent = `模型服务：${health.message}`;
      } catch (error) {
        modelHealth.textContent = `模型服务状态暂不可查：${error.message}`;
      }
    }
    if (actions.childNodes.length) parts.push(actions);
    if ((analysis.textContent || "").trim()) parts.push(analysis);
    if (media.childNodes.length) parts.push(media);
    if (timeline.childNodes.length) parts.push(timeline);
    if (children.childNodes.length) parts.push(children);
    if (warningsPanel.childNodes.length) parts.push(warningsPanel);
    if (review.childNodes.length) parts.push(review);
    if (evidenceAudit.childNodes.length) parts.push(evidenceAudit);
    root.replaceChildren(...parts);
  }

  async function loadFromPath(path) {
    const match = path.match(/^\/jobs\/([^/]+)$/);
    D.hideAllViews();
    D.setNav("jobs-nav");
    if (match) {
      D.setPage("job-detail");
      D.$("job-detail-view").classList.remove("hidden");
      await loadDetail(decodeURIComponent(match[1]));
    } else {
      D.setPage("jobs");
      D.$("jobs-view").classList.remove("hidden");
      document.title = "任务中心 · 抖库";
      await loadList();
    }
  }

  window.addEventListener("douku:route", async (event) => {
    const path = routePath(event.detail.path);
    if (path !== "/jobs" && !path.startsWith("/jobs/")) {
      stop();
      return;
    }
    try {
      await loadFromPath(path);
      schedule(path);
    } catch (error) {
      D.toast(error.message);
    }
  });

  D.$("jobs-filter-action")?.addEventListener("change", () => {
    const value = D.$("jobs-filter-action").value;
    const params = new URLSearchParams(location.search);
    if (value === "waiting") params.set("requires_user_action", "1");
    else params.delete("requires_user_action");
    if (value && value !== "waiting" && value !== "all") params.set("status", value);
    else params.delete("status");
    D.navigate(`/jobs?${params}`);
  });
  D.$("jobs-filter-kind")?.addEventListener("change", () => {
    const params = new URLSearchParams(location.search);
    const value = D.$("jobs-filter-kind").value;
    if (value) params.set("kind", value);
    else params.delete("kind");
    D.navigate(`/jobs?${params}`);
  });
})();
