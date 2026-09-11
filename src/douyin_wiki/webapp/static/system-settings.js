(() => {
  "use strict";
  const D = window.Douku;
  if (!D) return;

  const CHECK_LABELS = {
    analysis_mode: "分析方式",
    "yt-dlp": "yt-dlp",
    ffmpeg: "FFmpeg",
    ffprobe: "FFprobe",
    whisper: "Whisper CLI",
    swift: "Swift",
    osascript: "自动化脚本",
    git: "Git",
    playwright: "Playwright",
    web_dependencies: "Web 依赖",
    web: "Web 服务",
    browser: "本机浏览器",
    douyin_browser_profile: "抖音浏览器配置",
    vault: "Vault",
    obsidian_vault: "Obsidian 库",
    database: "数据库",
    llm: "后台模型",
    web_llm: "对话模型",
    embeddings: "向量检索",
    mlx_whisper: "MLX Whisper",
    transcription: "转录",
    ocr: "OCR",
    reminders: "提醒事项",
  };

  function shortTime(value) {
    return String(value || "—").replace(/（北京时间）/g, "").trim() || "—";
  }

  function formatBytes(bytes) {
    const size = Number(bytes) || 0;
    if (size < 1024) return `${size} B`;
    if (size < 1024 ** 2) return `${(size / 1024).toFixed(1)} KB`;
    if (size < 1024 ** 3) return `${(size / 1024 ** 2).toFixed(1)} MB`;
    return `${(size / 1024 ** 3).toFixed(1)} GB`;
  }

  function pill(label, state) {
    const node = D.node("span", label, "status-pill");
    node.dataset.state = state;
    return node;
  }

  function row(label, value, state) {
    const item = D.node("div", null, "system-row");
    item.append(D.node("span", label));
    if (state) item.append(pill(value, state));
    else item.append(D.node("span", value, "system-row-value"));
    return item;
  }

  function metric(title, value, copy, href) {
    const card = href ? D.node("a", null, "system-metric") : D.node("article", null, "system-metric");
    if (href) {
      card.href = href;
      card.addEventListener("click", (event) => {
        if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
        event.preventDefault();
        D.navigate(href);
      });
    }
    card.append(D.node("span", title), D.node("strong", value), D.node("p", copy));
    return card;
  }

  function listBlock(title, items) {
    const block = D.node("div", null, "system-preview-list");
    block.append(D.node("strong", title));
    const list = document.createElement("ul");
    (items.length ? items : ["无"]).forEach((item) => list.append(D.node("li", item)));
    block.append(list);
    return block;
  }

  function reportCopy(report) {
    if (!report || typeof report !== "object") return "";
    const parts = [];
    if (Array.isArray(report.media_candidates)) parts.push(`过期媒体 ${report.media_candidates.length} 份`);
    if (Array.isArray(report.media_removed)) parts.push(`已清理 ${report.media_removed.length} 份`);
    if (Array.isArray(report.orphan_pages) && report.orphan_pages.length) {
      parts.push(`孤立页面 ${report.orphan_pages.length} 个`);
    }
    if (typeof report.stale_chunks === "number" && report.stale_chunks) {
      parts.push(`过期切片 ${report.stale_chunks} 条`);
    }
    if (typeof report.loaded_entries === "number") parts.push(`将载入 ${report.loaded_entries} 篇`);
    if (typeof report.entry_count === "number") parts.push(`资料 ${report.entry_count} 篇`);
    return parts.join(" · ");
  }

  function renderPreview(target, result) {
    const box = D.node("div", null, "system-preview");
    box.append(D.node("p", result.executed ? "已执行" : "预览，尚未改动文件", "system-preview-kicker"));
    const lists = D.node("div", null, "system-preview-lists");
    lists.append(
      listBlock("将删除", result.will_delete || []),
      listBlock("将保留", result.will_keep || []),
    );
    box.append(lists);
    const extra = reportCopy(result.report);
    if (extra) box.append(D.node("p", extra, "hint-copy"));
    target.replaceChildren(box);
  }

  function renderHealth(health, storage) {
    const jobs = health.jobs || {};
    const worker = health.worker || {};
    const analysis = health.analysis || {};
    D.$("system-overview").replaceChildren(
      metric(
        "Worker",
        worker.running ? "运行中" : "未运行",
        worker.running ? `队列 ${worker.queue_length || 0}` : "后台没有心跳",
      ),
      metric("分析方式", analysis.mode_label || "未设置", analysis.web_copy || "", "/settings/analysis"),
      metric(
        "任务",
        `${jobs.waiting_user || 0} 待操作`,
        `处理中 ${jobs.running || 0} · 失败 ${jobs.failed || 0}`,
        "/jobs",
      ),
      metric(
        "存储",
        formatBytes(storage.vault_bytes),
        `${storage.entry_count || 0} 篇 · 数据库 ${formatBytes(storage.database_bytes)}`,
      ),
    );
    D.$("system-health").replaceChildren(
      row("Worker", worker.running ? "运行中" : "未运行", worker.running ? "authorized" : "failed"),
      row("最后心跳", shortTime(worker.last_heartbeat_display)),
      row("队列", String(worker.queue_length ?? 0)),
      row("正在执行", worker.running_job_id ? "有任务" : "空闲"),
      row("分析方式", analysis.mode_label || "未设置"),
      row("最近维护", shortTime(health.last_maintenance_display)),
    );
    D.$("system-storage").replaceChildren(
      row("知识资料", `${storage.entry_count || 0} 篇`),
      row("永久保留媒体", `${storage.kept_media_count || 0} 份`),
      row("临时媒体", storage.retention_label || "—"),
      row("Vault", formatBytes(storage.vault_bytes)),
      row("数据库", formatBytes(storage.database_bytes)),
    );
  }

  function renderDoctor(result) {
    const root = D.$("system-doctor-result");
    const box = D.node("div", null, "system-preview");
    box.append(D.node(
      "p",
      result.overall ? "检查通过" : "存在需要处理的问题",
      result.overall ? "system-preview-kicker is-ok" : "system-preview-kicker is-bad",
    ));
    const rows = D.node("div", null, "system-rows");
    Object.entries(CHECK_LABELS).forEach(([key, label]) => {
      const check = result[key];
      if (!check || typeof check !== "object") return;
      const item = row(label, check.ok ? "正常" : "异常", check.ok ? "authorized" : "failed");
      if (check.message) {
        item.classList.add("has-copy");
        item.append(D.node("small", check.message));
      }
      rows.append(item);
    });
    box.append(rows);
    root.replaceChildren(box);
  }

  async function load() {
    const [health, storage] = await Promise.all([
      D.api("/api/system/health"),
      D.api("/api/system/storage"),
    ]);
    renderHealth(health, storage);
  }

  async function withButton(button, work) {
    if (!button) return;
    button.disabled = true;
    try {
      await work();
    } catch (error) {
      D.toast(error.message);
    } finally {
      button.disabled = false;
    }
  }

  window.addEventListener("douku:route", async (event) => {
    if (event.detail.path !== "/settings/system") return;
    D.hideAllViews();
    D.setPage("system");
    D.setNav("system-nav");
    D.$("system-view").classList.remove("hidden");
    document.title = "系统设置 · 抖库";
    try {
      await load();
    } catch (error) {
      D.toast(error.message);
    }
  });

  D.$("system-doctor")?.addEventListener("click", () => withButton(D.$("system-doctor"), async () => {
    renderDoctor(await D.api("/api/system/doctor", {method: "POST", body: "{}"}));
  }));
  D.$("system-maintenance-preview")?.addEventListener("click", () => withButton(
    D.$("system-maintenance-preview"),
    async () => {
      renderPreview(
        D.$("system-maintenance-result"),
        await D.api("/api/system/maintenance", {method: "POST", body: JSON.stringify({confirmed: false})}),
      );
    },
  ));
  D.$("system-maintenance-apply")?.addEventListener("click", () => withButton(
    D.$("system-maintenance-apply"),
    async () => {
      const preview = await D.api("/api/system/maintenance", {method: "POST", body: JSON.stringify({confirmed: false})});
      const ok = await D.confirmDanger({
        title: "确认执行维护？",
        description: `将删除：${(preview.will_delete || []).join("；") || "无"}。保留：${(preview.will_keep || []).join("；") || "无"}。`,
        confirmLabel: "执行维护",
      });
      if (!ok) return;
      renderPreview(
        D.$("system-maintenance-result"),
        await D.api("/api/system/maintenance", {method: "POST", body: JSON.stringify({confirmed: true})}),
      );
      D.toast("维护已执行");
      await load();
    },
  ));
  D.$("system-rebuild-preview")?.addEventListener("click", () => withButton(
    D.$("system-rebuild-preview"),
    async () => {
      renderPreview(
        D.$("system-rebuild-result"),
        await D.api("/api/system/rebuild", {method: "POST", body: JSON.stringify({confirmed: false})}),
      );
    },
  ));
  D.$("system-rebuild-apply")?.addEventListener("click", () => withButton(
    D.$("system-rebuild-apply"),
    async () => {
      const preview = await D.api("/api/system/rebuild", {method: "POST", body: JSON.stringify({confirmed: false})});
      const ok = await D.confirmDanger({
        title: "确认重建数据库？",
        description: `将删除：${(preview.will_delete || []).join("；") || "无"}。保留：${(preview.will_keep || []).join("；") || "无"}。`,
        confirmLabel: "重建数据库",
      });
      if (!ok) return;
      renderPreview(
        D.$("system-rebuild-result"),
        await D.api("/api/system/rebuild", {method: "POST", body: JSON.stringify({confirmed: true})}),
      );
      D.toast("重建已执行");
    },
  ));
  D.$("system-reload-worker")?.addEventListener("click", () => withButton(
    D.$("system-reload-worker"),
    async () => {
      const result = await D.api("/api/system/worker/reload", {method: "POST", body: "{}"});
      D.toast(result.status);
      D.$("system-reload-note").textContent = result.note || result.status;
      await load();
    },
  ));
})();
