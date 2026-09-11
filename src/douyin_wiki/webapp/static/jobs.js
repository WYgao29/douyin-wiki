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

  function actionButton(action, jobId) {
    if (!action) return null;
    const button = D.node("button", action.label, action.code === "retry" ? "secondary-button" : "primary-button");
    button.type = "button";
    button.addEventListener("click", async () => {
      try {
        if (action.endpoint) {
          await D.api(action.endpoint, {method: "POST", body: JSON.stringify({})});
          D.toast("已提交");
          loadFromPath(location.pathname);
        } else if (action.href) {
          D.navigate(action.href);
        }
      } catch (error) {
        D.toast(error.message);
      }
    });
    return button;
  }

  function renderList(data) {
    const root = D.$("jobs-list");
    if (!data.items.length) {
      root.replaceChildren(D.emptyState("没有符合条件的任务", "提交导入或等待后台处理后，任务会出现在这里。"));
      return;
    }
    root.replaceChildren(...data.items.map((job) => {
      const row = D.node("article", null, "job-row");
      row.tabIndex = 0;
      row.setAttribute("role", "link");
      const title = D.node("h2", job.kind_label || job.kind);
      const status = D.node("span", job.state_label, "status-pill");
      status.dataset.state = job.status;
      const message = D.node("p", job.message_for_user);
      const meta = D.node("small", `${job.stage_label} · ${Math.round((job.progress || 0) * 100)}% · ${job.updated_display || ""}`);
      row.append(title, status, message, meta);
      if (job.requires_user_action && job.next_action) {
        const action = actionButton(job.next_action, job.id);
        if (action) row.append(action);
      }
      row.addEventListener("click", (event) => {
        if (event.target.closest("button")) return;
        D.navigate(`/jobs/${job.id}`);
      });
      row.addEventListener("keydown", (event) => {
        if (event.key === "Enter") D.navigate(`/jobs/${job.id}`);
      });
      return row;
    }));
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
  }

  async function loadDetail(jobId) {
    const root = D.$("job-detail-content");
    const job = await D.api(`/api/jobs/${encodeURIComponent(jobId)}`);
    document.title = `${job.kind_label} · 抖库`;
    const header = D.node("header", null, "page-heading");
    const titleBox = document.createElement("div");
    titleBox.append(D.node("h1", job.kind_label), D.node("p", job.message_for_user));
    const pill = D.node("span", job.state_label, "status-pill");
    pill.dataset.state = job.status;
    header.append(titleBox, pill);
    const progress = D.node("p", `阶段 ${job.stage_label} · 进度 ${Math.round((job.progress || 0) * 100)}%`);
    const actions = D.node("div", null, "operation-actions");
    const action = actionButton(job.next_action, job.id);
    if (action) actions.append(action);
    if (job.entry_id) {
      const link = D.node("a", "打开知识资料", "secondary-button");
      link.href = `/articles/${encodeURIComponent(job.entry_id)}`;
      actions.append(link);
    }
    const analysis = D.node("p", `${job.analysis_mode_label || ""}`, "hint-copy");
    const timeline = D.node("ol", null, "job-timeline");
    for (const event of job.timeline || []) {
      const item = D.node("li", `${event.state_label} · ${event.created_display}`);
      timeline.append(item);
    }
    const children = D.node("section", null, "job-children");
    if (job.child_stats) {
      children.append(D.node("h2", "子任务"));
      children.append(D.node("p", `成功 ${job.child_stats.completed} · 处理中 ${job.child_stats.running} · 等待本人 ${job.child_stats.waiting_user} · 失败 ${job.child_stats.failed}`));
    }
    for (const child of job.children || []) {
      const row = D.node("button", `${child.kind_label} · ${child.state_label} · ${child.message_for_user}`, "job-child");
      row.type = "button";
      row.addEventListener("click", () => D.navigate(`/jobs/${child.id}`));
      children.append(row);
    }
    const review = D.node("section", null, "review-panel");
    if (job.review_issues?.length) {
      review.append(D.node("h2", "人工复核"));
      const form = document.createElement("form");
      job.review_issues.forEach((issue) => {
        const label = D.node("label", `疑点 ${issue.id}（${issue.start_ms}-${issue.end_ms}ms${issue.image_index ? ` · 图 ${issue.image_index}` : ""}）`);
        const input = document.createElement("textarea");
        input.rows = 2;
        input.value = issue.raw_text;
        input.dataset.issue = issue.id;
        label.append(input, D.node("small", issue.reason || ""));
        form.append(label);
      });
      const save = D.node("button", "提交修改", "primary-button");
      save.type = "submit";
      const accept = D.node("button", "接受全部不确定内容", "secondary-button");
      accept.type = "button";
      accept.addEventListener("click", async () => {
        try {
          await D.api(`/api/jobs/${encodeURIComponent(jobId)}/review`, {
            method: "POST",
            body: JSON.stringify({accept_uncertain: true}),
          });
          D.toast("已接受不确定内容");
          loadDetail(jobId);
        } catch (error) {
          D.toast(error.message);
        }
      });
      form.addEventListener("submit", async (event) => {
        event.preventDefault();
        const resolutions = {};
        form.querySelectorAll("textarea[data-issue]").forEach((input) => {
          resolutions[input.dataset.issue] = input.value;
        });
        try {
          await D.api(`/api/jobs/${encodeURIComponent(jobId)}/review`, {
            method: "POST",
            body: JSON.stringify({resolutions}),
          });
          D.toast("复核已提交");
          loadDetail(jobId);
        } catch (error) {
          D.toast(error.message);
        }
      });
      form.append(save, accept);
      review.append(form);
    }
    root.replaceChildren(header, progress, actions, analysis, timeline, children, review);
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
    const path = event.detail.path;
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
