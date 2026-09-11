(() => {
  "use strict";
  const D = window.Douku;
  if (!D) return;
  let jobId = null;
  let page = 1;
  let current = null;

  async function loadCreators() {
    const data = await D.api("/api/creators");
    const root = D.$("creators-list");
    if (!data.items?.length) {
      root.replaceChildren(D.emptyState("还没有已同步的博主", "输入主页或作品链接后开始清点。"));
      return;
    }
    root.replaceChildren(...data.items.map((creator) => {
      const card = D.node("article", null, "status-card");
      card.append(
        D.node("h2", creator.nickname),
        D.node("p", `${creator.work_count || 0} 个作品 · 上次同步 ${creator.last_synced_at || "尚未同步"}`),
      );
      const sync = D.node("button", "手动同步", "secondary-button");
      sync.type = "button";
      sync.addEventListener("click", async () => {
        const job = await D.api(`/api/creators/${encodeURIComponent(creator.id)}/sync`, {
          method: "POST",
          body: "{}",
        });
        D.navigate(`/jobs/${job.id}`);
      });
      const skipped = (creator.counts && (creator.counts.skipped || creator.counts["未入库"])) || 0;
      if (skipped) {
        const button = D.node("button", "导入以前跳过的作品", "secondary-button");
        button.type = "button";
        button.addEventListener("click", async () => {
          const works = await D.api(`/api/creators/${encodeURIComponent(creator.id)}/works`);
          const ids = (works.items || []).filter((item) => item.decision === "skipped").map((item) => item.work_id);
          if (!ids.length) {
            D.toast("没有已跳过作品");
            return;
          }
          const job = await D.api(`/api/creators/${encodeURIComponent(creator.id)}/import-skipped`, {
            method: "POST",
            body: JSON.stringify({work_ids: ids}),
          });
          D.navigate(`/jobs/${job.id}`);
        });
        card.append(button);
      }
      card.append(sync);
      return card;
    }));
  }

  function renderInventory() {
    const root = D.$("creator-items");
    root.replaceChildren();
    D.$("creator-status").textContent = current.job
      ? `${current.job.state_label} · ${current.job.message_for_user}`
      : current.status;
    D.$("creator-summary").textContent = current.partial
      ? "清单可能不完整，确认前需要额外勾选。"
      : `共 ${current.total} 个作品`;
    D.$("creator-partial-row").hidden = !current.partial || current.status !== "needs_selection";
    for (const item of current.items || []) {
      const row = D.node("label", null, "work-row");
      const input = document.createElement("input");
      input.type = "checkbox";
      input.checked = item.work?.decision === "selected" || item.decision === "selected";
      input.disabled = current.status !== "needs_selection";
      const work = item.work || item;
      input.addEventListener("change", async () => {
        await D.api(`/api/creator-imports/${encodeURIComponent(jobId)}/selection`, {
          method: "POST",
          body: JSON.stringify({
            decision: input.checked ? "selected" : "skipped",
            work_ids: [work.work_id],
          }),
        });
        await loadInventory();
      });
      row.append(input, D.node("span", `${work.title} · ${work.source_kind}`));
      root.append(row);
    }
    D.$("creator-page").textContent = `第 ${current.page} 页`;
    D.$("creator-confirm").disabled = current.status !== "needs_selection";
  }

  async function loadInventory() {
    if (!jobId) return;
    const params = new URLSearchParams({page, limit: 50, query: D.$("creator-query").value});
    current = await D.api(`/api/creator-imports/${encodeURIComponent(jobId)}?${params}`);
    renderInventory();
  }

  async function startInventory(event) {
    event.preventDefault();
    const job = await D.api("/api/creators/inventory", {
      method: "POST",
      body: JSON.stringify({source_text: D.$("creator-source").value}),
    });
    jobId = job.id;
    page = 1;
    D.toast("已开始清点博主作品");
    history.replaceState({}, "", `/imports/creators?job=${encodeURIComponent(jobId)}`);
    await loadInventory();
  }

  window.addEventListener("douku:route", async (event) => {
    if (event.detail.path !== "/imports/creators") return;
    D.hideAllViews();
    D.setPage("imports-creators");
    D.$("imports-creators-view").classList.remove("hidden");
    document.title = "博主导入 · 抖库";
    const params = new URLSearchParams(location.search);
    jobId = params.get("job");
    try {
      await loadCreators();
      if (jobId) await loadInventory();
    } catch (error) {
      D.toast(error.message);
    }
  });

  D.$("creator-form")?.addEventListener("submit", (event) => {
    startInventory(event).catch((error) => D.toast(error.message));
  });
  D.$("creator-select-all")?.addEventListener("click", async () => {
    await D.api(`/api/creator-imports/${encodeURIComponent(jobId)}/selection`, {
      method: "POST",
      body: JSON.stringify({decision: "selected"}),
    });
    await loadInventory();
  });
  D.$("creator-exclude-all")?.addEventListener("click", async () => {
    await D.api(`/api/creator-imports/${encodeURIComponent(jobId)}/selection`, {
      method: "POST",
      body: JSON.stringify({decision: "skipped"}),
    });
    await loadInventory();
  });
  D.$("creator-confirm")?.addEventListener("click", async () => {
    const job = await D.api(`/api/creator-imports/${encodeURIComponent(jobId)}/confirm`, {
      method: "POST",
      body: JSON.stringify({accept_partial: D.$("creator-partial").checked}),
    });
    D.navigate(`/jobs/${job.id}`);
  });
  D.$("creator-previous")?.addEventListener("click", async () => {
    page = Math.max(1, page - 1);
    await loadInventory();
  });
  D.$("creator-next")?.addEventListener("click", async () => {
    page += 1;
    await loadInventory();
  });
})();
