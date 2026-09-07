"use strict";

const $ = (selector) => document.querySelector(selector);
let currentSettings = null;

async function api(url, options = {}) {
  const response = await fetch(url, {
    headers: {"Content-Type": "application/json", ...(options.headers || {})},
    ...options,
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = Array.isArray(body.detail)
      ? body.detail.map((item) => String(item.msg || "输入内容无效")
        .replace(/^Value error,\s*/i, "")
        .replace(/^Field required$/i, "请填写全部必填内容")).join("；")
      : body.detail;
    throw new Error(detail || "请求失败");
  }
  return body;
}

function toast(message) {
  const node = $("#toast");
  node.textContent = message;
  node.classList.add("show");
  window.setTimeout(() => node.classList.remove("show"), 2400);
}

function showMessage(message, kind = "info") {
  const node = $("#settings-message");
  node.textContent = message;
  node.className = `settings-message ${kind}`;
}

function setKeyVisibility(visible) {
  const input = $("#model-api-key");
  input.type = visible ? "text" : "password";
  const button = $("#toggle-key");
  button.setAttribute("aria-label", visible ? "隐藏 API Key" : "显示 API Key");
  const icon = $("#toggle-key-icon");
  icon.setAttribute("href", `/static/icons.svg#${visible ? "eye-off" : "eye"}`);
}

function renderStatus(settings) {
  currentSettings = settings;
  $("#model-base-url").value = settings.base_url || "https://api.openai.com/v1";
  $("#model-name").value = settings.model || "";
  $("#analysis-mode-label").textContent = settings.analysis_mode_label;
  const dot = $("#model-status-dot");
  dot.classList.toggle("ready", settings.configured);
  $("#model-status-title").textContent = settings.configured ? "模型已配置" : "模型尚未配置";
  $("#model-status-copy").textContent = settings.configured
    ? `${settings.model} · ${settings.base_url}`
    : "填写接口和模型名称；云端服务还需要对应的 API Key。";
  const key = $("#key-status");
  key.textContent = settings.api_key_configured
    ? `已保存 · ${settings.api_key_source}`
    : !settings.api_key_required
    ? "本机接口无需密钥"
    : "尚未保存";
  key.classList.toggle("ready", !settings.api_key_required || settings.api_key_configured);
}

async function loadSettings() {
  try {
    renderStatus(await api("/api/settings/model"));
  } catch (error) {
    showMessage(error.message, "error");
  }
}

async function saveSettings(event) {
  event.preventDefault();
  const button = $("#save-model-settings");
  button.disabled = true;
  showMessage("正在安全保存配置…");
  try {
    const payload = {
      base_url: $("#model-base-url").value.trim(),
      model: $("#model-name").value.trim(),
      api_key: $("#model-api-key").value.trim() || null,
    };
    const result = await api("/api/settings/model", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    $("#model-api-key").value = "";
    await loadSettings();
    showMessage(
      result.configured
        ? "配置已保存，右侧 AI 对话现在可以使用。"
        : "接口和模型已保存；还需要填写 API Key。",
      result.configured ? "success" : "warning",
    );
    toast("模型配置已保存");
  } catch (error) {
    showMessage(error.message, "error");
  } finally {
    button.disabled = false;
  }
}

async function testConnection() {
  const hasUnsavedChanges = !currentSettings
    || $("#model-base-url").value.trim().replace(/\/$/, "") !== currentSettings.base_url
    || $("#model-name").value.trim() !== currentSettings.model
    || Boolean($("#model-api-key").value.trim());
  if (hasUnsavedChanges) {
    showMessage("请先保存当前填写的配置，再测试连接。", "warning");
    return;
  }
  const button = $("#test-model-settings");
  button.disabled = true;
  showMessage("正在调用模型测试连接，这会产生少量 token…");
  try {
    const result = await api("/api/settings/model/test", {method: "POST", body: "{}"});
    const usage = result.usage?.total_tokens != null
      ? `，本次使用 ${result.usage.total_tokens} token`
      : "，接口未返回 token 用量";
    showMessage(`连接成功：${result.model}${usage}`, "success");
    toast("模型连接正常");
  } catch (error) {
    showMessage(error.message, "error");
  } finally {
    button.disabled = false;
  }
}

document.addEventListener("DOMContentLoaded", () => {
  $("#model-settings-form").addEventListener("submit", saveSettings);
  $("#test-model-settings").addEventListener("click", testConnection);
  $("#toggle-key").addEventListener("click", () => {
    setKeyVisibility($("#model-api-key").type !== "text");
  });
  document.querySelectorAll("[data-theme-select]").forEach((select) => {
    select.addEventListener("change", (event) => window.DoukuTheme?.set(event.target.value));
  });
  window.DoukuTheme?.apply();
  loadSettings();
});
