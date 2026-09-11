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

function selectedMode() {
  return document.querySelector("input[name='analysis-mode']:checked");
}

function updateProviderPanel() {
  const panel = $("#provider-model-panel");
  const needsProvider = selectedMode()?.value === "provider";
  panel.classList.toggle("hidden", !needsProvider);
  if (!needsProvider || !currentSettings) {
    return;
  }
  if (currentSettings.configured) {
    $("#provider-model-title").textContent = "将使用对话模型";
    $("#provider-model-copy").textContent = `${currentSettings.model} · ${currentSettings.base_url}`;
  } else {
    $("#provider-model-title").textContent = "需要先配置对话模型";
    $("#provider-model-copy").textContent = "后台整理会调用同一套模型接口。网关 Agent 和本地模式不需要这一步。";
  }
}

function renderStatus(settings) {
  currentSettings = settings;
  document.querySelectorAll("input[name='analysis-mode']").forEach((input) => {
    input.checked = input.value === settings.analysis_mode;
  });
  const mode = settings.analysis_modes.find((item) => item.value === settings.analysis_mode);
  const ready = Boolean(mode?.web_can_complete);
  $("#analysis-status-dot").classList.toggle("ready", ready);
  $("#analysis-status-title").textContent = settings.analysis_mode_label;
  $("#analysis-status-copy").textContent = mode?.web_copy || "请选择一种分析方式。";
  updateProviderPanel();
}

async function loadSettings() {
  try {
    renderStatus(await api("/api/settings/model"));
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
  const button = $("#save-analysis-mode");
  button.disabled = true;
  showMessage("正在保存分析方式…");
  try {
    const result = await api("/api/settings/analysis-mode", {
      method: "POST",
      body: JSON.stringify({mode: selected.value}),
    });
    await loadSettings();
    const extra = result.warning ? ` ${result.warning}` : "";
    showMessage(
      `${result.status}。${result.web_copy}。${result.worker_reload || ""}${extra}`,
      result.warning ? "warning" : "success",
    );
    toast("分析方式已保存");
  } catch (error) {
    showMessage(error.message, "error");
  } finally {
    button.disabled = false;
  }
}

document.addEventListener("DOMContentLoaded", () => {
  $("#save-analysis-mode").addEventListener("click", saveAnalysisMode);
  document.querySelectorAll("input[name='analysis-mode']").forEach((input) => {
    input.addEventListener("change", updateProviderPanel);
  });
  document.querySelectorAll("[data-theme-select]").forEach((select) => {
    select.addEventListener("change", (event) => window.DoukuTheme?.set(event.target.value));
  });
  window.DoukuTheme?.apply();
  loadSettings();
});
