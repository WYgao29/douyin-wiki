(() => {
  "use strict";
  const D = window.Douku;
  if (!D) return;

  let currentSettings = null;
  let bound = false;

  function showMessage(message, kind = "info") {
    const node = D.$("model-message");
    if (!node) return;
    node.textContent = message;
    node.className = `settings-message ${kind}`;
  }

  function setKeyVisibility(visible) {
    const input = D.$("model-api-key");
    input.type = visible ? "text" : "password";
    D.$("toggle-key").setAttribute("aria-label", visible ? "隐藏 API Key" : "显示 API Key");
    D.$("toggle-key-icon").setAttribute("href", `/static/icons.svg#${visible ? "eye-off" : "eye"}`);
  }

  function renderStatus(settings) {
    currentSettings = settings;
    D.$("model-base-url").value = settings.base_url || "https://api.openai.com/v1";
    D.$("model-name").value = settings.model || "";
    const label = D.$("analysis-mode-label");
    if (label) label.textContent = settings.analysis_mode_label;
    D.$("model-status-dot").classList.toggle("ready", settings.configured);
    D.$("model-status-title").textContent = settings.configured ? "模型已配置" : "模型尚未配置";
    D.$("model-status-copy").textContent = settings.configured
      ? `${settings.model} · ${settings.base_url}`
      : "填写接口和模型名称；云端服务还需要对应的 API Key。";
    const key = D.$("key-status");
    key.textContent = settings.api_key_configured
      ? `已保存 · ${settings.api_key_source}`
      : !settings.api_key_required
      ? "本机接口无需密钥"
      : "尚未保存";
    key.classList.toggle("ready", !settings.api_key_required || settings.api_key_configured);
  }

  async function loadSettings() {
    try {
      renderStatus(await D.api("/api/settings/model"));
    } catch (error) {
      showMessage(error.message, "error");
    }
  }

  async function saveSettings(event) {
    event.preventDefault();
    const button = D.$("save-model-settings");
    button.disabled = true;
    showMessage("正在安全保存配置…");
    try {
      const payload = {
        base_url: D.$("model-base-url").value.trim(),
        model: D.$("model-name").value.trim(),
        api_key: D.$("model-api-key").value.trim() || null,
      };
      const result = await D.api("/api/settings/model", {
        method: "POST",
        body: JSON.stringify(payload),
      });
      D.$("model-api-key").value = "";
      await loadSettings();
      showMessage(
        result.configured
          ? "配置已保存，右侧 AI 对话现在可以使用。"
          : "接口和模型已保存；还需要填写 API Key。",
        result.configured ? "success" : "warning",
      );
      D.toast("模型配置已保存");
    } catch (error) {
      showMessage(error.message, "error");
    } finally {
      button.disabled = false;
    }
  }

  async function testConnection() {
    const hasUnsavedChanges = !currentSettings
      || D.$("model-base-url").value.trim().replace(/\/$/, "") !== currentSettings.base_url
      || D.$("model-name").value.trim() !== currentSettings.model
      || Boolean(D.$("model-api-key").value.trim());
    if (hasUnsavedChanges) {
      showMessage("请先保存当前填写的配置，再测试连接。", "warning");
      return;
    }
    const button = D.$("test-model-settings");
    button.disabled = true;
    showMessage("正在调用模型测试连接，这会产生少量 token…");
    try {
      const result = await D.api("/api/settings/model/test", {method: "POST", body: "{}"});
      const usage = result.usage?.total_tokens != null
        ? `，本次使用 ${result.usage.total_tokens} token`
        : "，接口未返回 token 用量";
      showMessage(`连接成功：${result.model}${usage}`, "success");
      D.toast("模型连接正常");
    } catch (error) {
      showMessage(error.message, "error");
    } finally {
      button.disabled = false;
    }
  }

  function bind() {
    if (bound) return;
    bound = true;
    D.$("model-settings-form")?.addEventListener("submit", saveSettings);
    D.$("test-model-settings")?.addEventListener("click", testConnection);
    D.$("toggle-key")?.addEventListener("click", () => {
      setKeyVisibility(D.$("model-api-key").type !== "text");
    });
  }

  window.addEventListener("douku:route", async (event) => {
    if (event.detail.path !== "/settings/model") return;
    D.hideAllViews();
    D.setPage("model");
    D.setNav("model-nav");
    D.$("model-view").classList.remove("hidden");
    document.title = "对话模型 · 抖库";
    bind();
    await loadSettings();
  });
})();
