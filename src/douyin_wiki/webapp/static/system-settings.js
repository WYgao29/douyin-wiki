(() => {
  "use strict";
  const D = window.Douku;
  if (!D) return;

  function pill(label, ok) {
    const node = D.node("span", label, "status-pill");
    node.dataset.state = ok ? "authorized" : "expired";
    return node;
  }

  async function loadHealth() {
    const health = await D.api("/api/system/health");
    const root = D.$("system-health");
    root.replaceChildren(
      D.node("h2", "运行状态"),
      pill(health.worker.running ? "Worker 运行中" : "Worker 未运行", health.worker.running),
      D.node("p", `最后心跳：${health.worker.last_heartbeat_display}`),
      D.node("p", `队列 ${health.worker.queue_length} · 正在执行 ${health.jobs.running} · 等待本人 ${health.jobs.waiting_user} · 失败 ${health.jobs.failed}`),
      D.node("p", health.analysis.web_copy),
      D.node("p", `最近维护：${health.last_maintenance_display}`),
    );
  }

  async function loadStorage() {
    const data = await D.api("/api/system/storage");
    D.$("system-storage").replaceChildren(
      D.node("h2", "存储与媒体"),
      D.node("p", `资料 ${data.entry_count} 篇 · 永久保留媒体 ${data.kept_media_count} 份`),
      D.node("p", data.retention_label),
      D.node("p", `Vault ${Math.round(data.vault_bytes / 1024)} KB · 数据库 ${Math.round(data.database_bytes / 1024)} KB`),
    );
  }

  function renderPreview(target, result, emptyMessage) {
    const list = D.node("div", null, "preview-block");
    list.append(D.node("p", result.executed ? "已执行" : "预览（尚未执行）"));
    list.append(D.node("p", `将删除：${(result.will_delete || []).join("；") || "无"}`));
    list.append(D.node("p", `将保留：${(result.will_keep || []).join("；") || "无"}`));
    if (result.report) list.append(D.node("pre", JSON.stringify(result.report, null, 2).slice(0, 4000)));
    if (!result.report) list.append(D.node("p", emptyMessage || ""));
    target.replaceChildren(list);
  }

  window.addEventListener("douku:route", async (event) => {
    if (event.detail.path !== "/settings/system") return;
    D.hideAllViews();
    D.setPage("system");
    D.setNav("system-nav");
    D.$("system-view").classList.remove("hidden");
    document.title = "系统设置 · 抖库";
    try {
      await loadHealth();
      await loadStorage();
    } catch (error) {
      D.toast(error.message);
    }
  });

  D.$("system-doctor")?.addEventListener("click", async () => {
    try {
      const result = await D.api("/api/system/doctor", {method: "POST", body: "{}"});
      const root = D.$("system-doctor-result");
      root.replaceChildren(D.node("h3", result.overall ? "检查通过" : "存在需要处理的问题"));
      Object.entries(result).forEach(([key, value]) => {
        if (key === "overall" || !value || typeof value !== "object") return;
        root.append(D.node("p", `${key}：${value.ok ? "正常" : "异常"}${value.message ? ` · ${value.message}` : ""}`));
      });
    } catch (error) {
      D.toast(error.message);
    }
  });
  D.$("system-maintenance-preview")?.addEventListener("click", async () => {
    const result = await D.api("/api/system/maintenance", {method: "POST", body: JSON.stringify({confirmed: false})});
    renderPreview(D.$("system-maintenance-result"), result);
  });
  D.$("system-maintenance-apply")?.addEventListener("click", async () => {
    const preview = await D.api("/api/system/maintenance", {method: "POST", body: JSON.stringify({confirmed: false})});
    if (!window.confirm(`确认执行维护？将删除：${preview.will_delete.join("；")}。保留：${preview.will_keep.join("；")}`)) return;
    const result = await D.api("/api/system/maintenance", {method: "POST", body: JSON.stringify({confirmed: true})});
    renderPreview(D.$("system-maintenance-result"), result);
    D.toast("维护已执行");
    await loadHealth();
  });
  D.$("system-rebuild-preview")?.addEventListener("click", async () => {
    const result = await D.api("/api/system/rebuild", {method: "POST", body: JSON.stringify({confirmed: false})});
    renderPreview(D.$("system-rebuild-result"), result);
  });
  D.$("system-rebuild-apply")?.addEventListener("click", async () => {
    const preview = await D.api("/api/system/rebuild", {method: "POST", body: JSON.stringify({confirmed: false})});
    if (!window.confirm(`确认重建数据库？将删除：${preview.will_delete.join("；")}。保留：${preview.will_keep.join("；")}`)) return;
    const result = await D.api("/api/system/rebuild", {method: "POST", body: JSON.stringify({confirmed: true})});
    renderPreview(D.$("system-rebuild-result"), result);
    D.toast("重建已执行");
  });
  D.$("system-reload-worker")?.addEventListener("click", async () => {
    const result = await D.api("/api/system/worker/reload", {method: "POST", body: "{}"});
    D.toast(result.status);
    D.$("system-reload-note").textContent = result.note;
  });
})();
