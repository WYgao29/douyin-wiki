(() => {
  "use strict";
  const D = window.Douku;
  if (!D) return;

  let currentSettings = null;
  let bound = false;

  function showMessage(message, kind = "info") {
    const node = D.$("analysis-message");
    if (!node) return;
    node.textContent = message;
    node.className = `settings-message ${kind}`;
  }

  function selectedMode() {
    return document.querySelector("#analysis-view input[name='analysis-mode']:checked");
  }

  function updateProviderPanel() {
    const panel = D.$("provider-model-panel");
    if (!panel) return;
    const needsProvider = selectedMode()?.value === "provider";
    panel.classList.toggle("hidden", !needsProvider);
    if (!needsProvider || !currentSettings) return;
    if (currentSettings.configured) {
      D.$("provider-model-title").textContent = "将使用对话模型";
      D.$("provider-model-copy").textContent = `${currentSettings.model} · ${currentSettings.base_url}`;
    } else {
      D.$("provider-model-title").textContent = "需要先配置对话模型";
      D.$("provider-model-copy").textContent = "后台整理会调用同一套模型接口。网关 Agent 和本地模式不需要这一步。";
    }
  }

  function renderStatus(settings) {
    currentSettings = settings;
    document.querySelectorAll("#analysis-view input[name='analysis-mode']").forEach((input) => {
      input.checked = input.value === settings.analysis_mode;
    });
    const mode = settings.analysis_modes.find((item) => item.value === settings.analysis_mode);
    const ready = Boolean(mode?.web_can_complete);
    D.$("analysis-status-dot").classList.toggle("ready", ready);
    D.$("analysis-status-title").textContent = settings.analysis_mode_label;
    D.$("analysis-status-copy").textContent = mode?.web_copy || "请选择一种分析方式。";
    updateProviderPanel();
  }

  async function loadSettings() {
    try {
      renderStatus(await D.api("/api/settings/model"));
    } catch (error) {
      showMessage(error.message, "error");
    }
  }

  async function saveAnalysisMode() {
    const selected = selectedMode();
    if (!selected) {
      showMessage("请先选择一种分析方式。", "warning");
      return;
    }
    const button = D.$("save-analysis-mode");
    button.disabled = true;
    showMessage("正在保存分析方式…");
    try {
      const result = await D.api("/api/settings/analysis-mode", {
        method: "POST",
        body: JSON.stringify({mode: selected.value}),
      });
      await loadSettings();
      const extra = result.warning ? ` ${result.warning}` : "";
      showMessage(
        `${result.status}。${result.web_copy}。${result.worker_reload || ""}${extra}`,
        result.warning ? "warning" : "success",
      );
      D.toast("分析方式已保存");
    } catch (error) {
      showMessage(error.message, "error");
    } finally {
      button.disabled = false;
    }
  }

  function bind() {
    if (bound) return;
    bound = true;
    D.$("save-analysis-mode")?.addEventListener("click", saveAnalysisMode);
    document.querySelectorAll("#analysis-view input[name='analysis-mode']").forEach((input) => {
      input.addEventListener("change", updateProviderPanel);
    });
  }

  window.addEventListener("douku:route", async (event) => {
    if (event.detail.path !== "/settings/analysis") return;
    D.hideAllViews();
    D.setPage("analysis");
    D.setNav("analysis-nav");
    D.$("analysis-view").classList.remove("hidden");
    document.title = "导入分析 · 抖库";
    bind();
    await loadSettings();
  });
})();
