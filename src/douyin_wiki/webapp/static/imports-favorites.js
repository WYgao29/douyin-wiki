(() => {
  "use strict";
  const D = window.Douku;
  if (!D) return;
  const $ = (id) => document.getElementById(`favorites-${id}`);
  const view = D.$("imports-favorites-view");
  if (!view) return;
  const base = "/api/favorites/imports";
  let jobId = null, page = 1, current = null, busy = false, timer = null, generation = 0;
  const ongoing = new Set(["queued", "inventorying", "dispatching", "monitoring"]);
  const terminal = new Set(["completed", "completed_with_warnings", "failed", "needs_auth"]);
  const node = D.node;
  async function request(path, body) {
    return D.api(path, body === undefined ? {} : {
      method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body),
    });
  }
  function stopPolling() { clearTimeout(timer); timer = null; }
  function active() { return !view.classList.contains("hidden"); }
  function controls() {
    view.querySelectorAll("button[data-action]").forEach((button) => { button.disabled = busy; });
    ["scope", "history", "filter", "query", "images", "mode"].forEach((id) => { if ($(id)) $(id).disabled = busy; });
    if ($("scan")) $("scan").disabled = busy || ($("scope").value === "folders" && !$("folders").querySelector("input:checked"));
    const canSelect = current?.status === "needs_selection" && !current.directory_only && !current.confirmed;
    if ($("confirm")) $("confirm").disabled = busy || !canSelect || !current.summary.selected || (!current.complete && !$("partial").checked);
    ["select-all", "exclude-all"].forEach((id) => { if ($(id)) $(id).disabled = busy || !canSelect; });
    if ($("previous")) $("previous").disabled = busy || !current || page <= 1;
    if ($("next")) $("next").disabled = busy || !current?.has_more;
    if ($("retry")) $("retry").disabled = busy || !current || !(terminal.has(current.status) || current.summary.failed);
    view.querySelectorAll("input[data-work]").forEach((input) => {
      input.disabled = busy || !canSelect || input.dataset.eligible !== "true";
    });
  }
  async function run(action) {
    if (busy) return;
    busy = true; stopPolling(); generation++; if ($("error")) $("error").textContent = "";
    controls();
    try { await action(); } catch (error) { if ($("error")) $("error").textContent = error.message; }
    finally { busy = false; controls(); schedule(); }
  }
  function schedule() {
    stopPolling();
    if (active() && jobId && ongoing.has(current?.status)) timer = setTimeout(() => run(load), 2500);
  }
  async function history() {
    const rows = await request(base);
    const selected = $("history").value;
    $("history").replaceChildren(node("option", "选择已有任务"));
    $("history").firstChild.value = "";
    for (const item of rows) {
      const option = node("option", `${item.nickname || "收藏清点"} · ${item.status_label || item.status} · ${item.job_id.slice(0, 8)}`);
      option.value = item.job_id; $("history").append(option);
    }
    $("history").value = jobId || selected;
  }
  function render(data) {
    current = data;
    $("status").textContent = `${data.nickname || "收藏任务"} · ${data.status_label || data.status}`;
    const summary = data.summary;
    const waits = {needs_auth: "需要登录", awaiting_agent_analysis: "待 AI 处理",
      waiting_confirmation: "待确认", needs_review: "待复核"};
    $("waits").textContent = Object.entries(waits).filter(([status]) => summary.child_status_counts?.[status])
      .map(([status, label]) => `${label} ${summary.child_status_counts[status]}`).join(" · ");
    $("summary").textContent = `发现 ${summary.discovered || 0} · 新增 ${summary.new || 0} · 已选择 ${summary.selected || 0} · 已入库 ${summary.imported || 0} · 处理中 ${summary.active || 0} · 已完成 ${summary.completed || 0} · 失败 ${summary.failed || 0}`;
    $("warnings").replaceChildren(...(data.warnings || []).map((text) => node("li", text)));
    $("auth").hidden = !(data.status === "needs_auth" || summary.child_status_counts?.needs_auth);
    $("gateway").hidden = !(data.analysis_mode === "gateway" && data.confirmed);
    $("partial-row").hidden = data.complete || data.directory_only || data.confirmed;
    $("retry").textContent = data.confirmed ? "重试失败作品" : "重试清点";
    if (data.error_message) $("error").textContent = data.error_message;
    const chosen = new Set([...$("folders").querySelectorAll("input:checked")].map((input) => input.value));
    $("folders").replaceChildren();
    for (const folder of data.folders || []) {
      const label = node("label", null, "check-row");
      const input = node("input"); input.type = "checkbox"; input.value = folder.id;
      input.checked = chosen.has(folder.id); input.addEventListener("change", controls);
      label.append(input, node("span", `${folder.name}${folder.reported_count == null ? "" : `（${folder.reported_count}）`}`));
      $("folders").append(label);
    }
    if (!data.folders?.length) $("folders").append(node("p", "暂无可识别的收藏夹。可选择全部收藏。"));
    const filter = $("filter").value;
    $("filter").replaceChildren(node("option", "全部收藏夹")); $("filter").firstChild.value = "";
    for (const folder of data.folders || []) {
      const option = node("option", folder.name); option.value = folder.id; $("filter").append(option);
    }
    $("filter").value = filter;
    $("items").replaceChildren();
    for (const item of data.items || []) {
      const row = node("div", null, "favorites-item");
      const input = node("input"); input.type = "checkbox"; input.checked = item.selected;
      input.dataset.work = item.work_id;
      input.dataset.eligible = String(!["unsupported", "unavailable"].includes(item.disposition) &&
        item.source_kind !== "article" && (item.source_kind !== "image_note" || data.include_images));
      input.setAttribute("aria-label", `选择 ${item.title}`);
      input.addEventListener("change", () => {
        const selected = input.checked;
        if (busy) { input.checked = !selected; return; }
        run(async () => {
          try { await request(`${base}/${encodeURIComponent(jobId)}/selection`, {selected, work_ids: [item.work_id]}); }
          catch (error) { input.checked = !selected; throw error; }
          await load();
        });
      });
      const content = node("div");
      let title = node("span", item.title);
      try {
        const url = new URL(item.canonical_url);
        if (url.protocol === "https:" && url.hostname === "www.douyin.com") {
          title = node("a", item.title); title.href = url.href; title.target = "_blank"; title.rel = "noopener noreferrer";
        }
      } catch (_error) { /* Keep malformed URLs as text. */ }
      content.append(title, node("small", `${item.author || "未知作者"} · ${item.source_kind_label || item.source_kind} · ${item.disposition_label || item.disposition}${item.is_new ? " · 新发现" : ""}${item.job_status_label ? ` · ${item.job_status_label}` : ""}`));
      if (item.error_message) content.append(node("small", item.error_message));
      row.append(input, content); $("items").append(row);
    }
    if (!data.items?.length) $("items").append(node("p", data.directory_only ? "目录读取完成后，可选择收藏夹并更新收藏。" : "当前筛选下暂无作品。"));
    $("page").textContent = `第 ${page} 页 · 匹配 ${data.total || 0} 条`;
    controls();
  }
  async function load() {
    if (!jobId) return;
    const token = generation;
    const params = new URLSearchParams({page, limit: 50, query: $("query").value});
    if ($("filter").value) params.set("folder_id", $("filter").value);
    if ($("only-new")?.checked) params.set("only_new", "true");
    const data = await request(`${base}/${encodeURIComponent(jobId)}?${params}`);
    if (token === generation && active()) render(data);
  }
  async function start(directoryOnly) {
    const folderIds = [...$("folders").querySelectorAll("input:checked")].map((input) => input.value);
    if (!directoryOnly && $("scope").value === "folders" && !folderIds.length) throw new Error("请先选择至少一个收藏夹");
    const result = await request(base, {
      directory_only: directoryOnly,
      include_images: $("images").checked,
      update_mode: $("mode")?.value || "incremental",
      folder_ids: !directoryOnly && $("scope").value === "folders" ? folderIds : null,
    });
    jobId = result.job_id; page = 1; current = null; $("partial").checked = false;
    $("filter").value = ""; $("query").value = "";
    await load(); await history();
  }
  window.addEventListener("douku:route", async (event) => {
    if (event.detail.path !== "/imports/favorites") {
      stopPolling();
      generation += 1;
      return;
    }
    D.hideAllViews();
    D.setPage("imports-favorites");
    view.classList.remove("hidden");
    document.title = "我的收藏 · 抖库";
    const params = new URLSearchParams(location.search);
    if (params.get("job")) jobId = params.get("job");
    await run(async () => { await history(); if (jobId) await load(); });
  });
  $("directory")?.addEventListener("click", () => run(() => start(true)));
  $("scan")?.addEventListener("click", () => run(() => start(false)));
  $("scope")?.addEventListener("change", () => { $("folders").hidden = $("scope").value !== "folders"; controls(); });
  $("history")?.addEventListener("change", () => {
    if (!$("history").value || busy) return;
    jobId = $("history").value; page = 1; $("partial").checked = false;
    $("query").value = ""; $("filter").value = ""; run(load);
  });
  $("refresh")?.addEventListener("click", () => run(async () => { await history(); await load(); }));
  $("search")?.addEventListener("click", () => run(async () => { page = 1; await load(); }));
  $("filter")?.addEventListener("change", () => run(async () => { page = 1; await load(); }));
  $("only-new")?.addEventListener("change", () => run(async () => { page = 1; await load(); }));
  $("query")?.addEventListener("keydown", (event) => { if (event.key === "Enter") { event.preventDefault(); $("search").click(); } });
  $("previous")?.addEventListener("click", () => run(async () => { page--; await load(); }));
  $("next")?.addEventListener("click", () => run(async () => { page++; await load(); }));
  [true, false].forEach((selected) => $(selected ? "select-all" : "exclude-all")?.addEventListener("click", () => run(async () => {
    await request(`${base}/${encodeURIComponent(jobId)}/selection`, {selected}); await load();
  })));
  $("partial")?.addEventListener("change", controls);
  $("confirm")?.addEventListener("click", () => run(async () => {
    await request(`${base}/${encodeURIComponent(jobId)}/confirm`, {accept_partial: $("partial").checked});
    await load(); await history();
    if (jobId) D.navigate(`/jobs/${jobId}`);
  }));
  $("retry")?.addEventListener("click", () => run(async () => {
    await request(`${base}/${encodeURIComponent(jobId)}/retry`, {}); await load();
  }));
  $("clear-cache")?.addEventListener("click", () => run(async () => {
    const preview = await request("/api/favorites/cache/clear", {confirmed: false});
    const ok = window.confirm(`${preview.will_delete.join("；")}。保留：${preview.will_keep.join("；")}。共 ${preview.run_count} 份缓存。`);
    if (!ok) return;
    const result = await request("/api/favorites/cache/clear", {confirmed: true});
    D.toast(`已清除 ${result.run_count} 份本地收藏缓存`);
  }));
  $("delete-history")?.addEventListener("click", () => run(async () => {
    if (!jobId) throw new Error("请先选择一条导入记录");
    const preview = await D.api(`/api/imports/${encodeURIComponent(jobId)}`, {
      method: "DELETE", body: JSON.stringify({confirmed: false}),
    });
    const ok = window.confirm(`${preview.will_delete.join("；")}。保留：${preview.will_keep.join("；")}`);
    if (!ok) return;
    await D.api(`/api/imports/${encodeURIComponent(jobId)}`, {
      method: "DELETE", body: JSON.stringify({confirmed: true}),
    });
    jobId = null; current = null; await history();
    $("status").textContent = "已删除该导入记录，已入库资料仍保留。";
  }));
  controls();
})();
