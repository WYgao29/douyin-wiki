(() => {
  "use strict";
  const D = window.Douku;
  if (!D) return;

  async function loadHints() {
    try {
      const analysis = await D.api("/api/system/analysis");
      D.$("single-analysis-copy").textContent = `${analysis.web_copy}。${analysis.token_hint}。`;
    } catch (_error) {
      D.$("single-analysis-copy").textContent = "提交后由后台处理。";
    }
  }

  async function submit(event) {
    event.preventDefault();
    const error = D.$("single-error");
    error.classList.add("hidden");
    const inspirations = [];
    const text = D.$("single-inspiration").value.trim();
    if (text) {
      inspirations.push({
        text,
        quote: D.$("single-quote").value.trim() || null,
        start_ms: D.$("single-start").value ? Number(D.$("single-start").value) : null,
        end_ms: D.$("single-end").value ? Number(D.$("single-end").value) : null,
      });
    }
    try {
      const job = await D.api("/api/captures", {
        method: "POST",
        body: JSON.stringify({
          share_text: D.$("single-share").value,
          inspirations,
          retention: D.$("single-retention").value,
          allow_long: D.$("single-allow-long").checked,
          approve_cloud_analysis: D.$("single-approve-ai").checked,
        }),
      });
      D.toast("已加入任务队列");
      D.navigate(`/jobs/${job.id || job.job_id}`);
    } catch (err) {
      error.textContent = err.message;
      error.classList.remove("hidden");
    }
  }

  window.addEventListener("douku:route", async (event) => {
    if (event.detail.path !== "/imports/single") return;
    D.hideAllViews();
    D.setPage("imports-single");
    D.$("imports-single-view").classList.remove("hidden");
    document.title = "单条导入 · 抖库";
    await loadHints();
  });

  D.$("single-form")?.addEventListener("submit", submit);
})();
