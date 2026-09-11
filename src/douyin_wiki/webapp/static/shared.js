(() => {
  "use strict";
  const VIEW_IDS = [
    "library-view", "article-view", "topics-view", "topic-view", "trash-view",
    "imports-view", "imports-single-view", "imports-creators-view", "imports-favorites-view",
    "jobs-view", "job-detail-view", "auth-view", "system-view",
  ];
  const Douku = window.Douku || {};
  Douku.$ = (id) => document.getElementById(id);
  Douku.qs = (selector, root = document) => root.querySelector(selector);
  Douku.qsa = (selector, root = document) => [...root.querySelectorAll(selector)];
  Douku.api = async (url, options = {}) => {
    const response = await fetch(url, {
      headers: {"Content-Type": "application/json", ...(options.headers || {})},
      ...options,
    });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) {
      const detail = Array.isArray(body.detail)
        ? body.detail.map((item) => item.msg || item.detail || "输入内容无效").join("；")
        : body.detail;
      throw new Error(detail || "请求失败");
    }
    return body;
  };
  Douku.toast = (message) => {
    const node = Douku.$("toast");
    if (!node) return;
    node.textContent = message;
    node.classList.add("show");
    window.clearTimeout(Douku.toast.timer);
    Douku.toast.timer = window.setTimeout(() => node.classList.remove("show"), 2600);
  };
  Douku.node = (tag, text, className) => {
    const value = document.createElement(tag);
    if (text != null) value.textContent = text;
    if (className) value.className = className;
    return value;
  };
  Douku.hideAllViews = () => {
    VIEW_IDS.forEach((id) => Douku.$(id)?.classList.add("hidden"));
    Douku.qsa(".nav-item").forEach((node) => node.classList.remove("active"));
  };
  Douku.setPage = (name) => {
    Douku.$("app-shell")?.setAttribute("data-page", name);
  };
  Douku.setNav = (id) => {
    Douku.qsa(".nav-item").forEach((node) => node.classList.remove("active"));
    Douku.$(id)?.classList.add("active");
  };
  Douku.isOperationPath = (path) => (
    path === "/imports" || path.startsWith("/imports/")
    || path === "/jobs" || path.startsWith("/jobs/")
    || path === "/settings/auth" || path === "/settings/system"
  );
  Douku.navigate = (path, push = true) => {
    if (push) history.pushState({}, "", path);
    window.dispatchEvent(new CustomEvent("douku:route", {detail: {path}}));
  };
  Douku.emptyState = (title, copy, actionLabel, action) => {
    const section = Douku.node("section", null, "empty-state");
    const heading = Douku.node("h2", title);
    const text = Douku.node("p", copy);
    section.append(heading, text);
    if (actionLabel && action) {
      const button = Douku.node("button", actionLabel, "secondary-button");
      button.type = "button";
      button.addEventListener("click", action);
      section.append(button);
    }
    return section;
  };
  Douku.confirmDanger = async ({title, description, confirmLabel}) => new Promise((resolve) => {
    const dialog = Douku.$("destructive-dialog");
    if (!dialog) {
      resolve(window.confirm(description));
      return;
    }
    Douku.$("destructive-title").textContent = title;
    Douku.$("destructive-description").textContent = description;
    Douku.$("confirm-destructive").textContent = confirmLabel || "确认";
    Douku.$("destructive-error").classList.add("hidden");
    const finish = (ok) => {
      dialog.close();
      resolve(ok);
    };
    const onClose = () => {
      dialog.removeEventListener("close", onClose);
      if (dialog.returnValue === "cancel") resolve(false);
    };
    dialog.addEventListener("close", onClose, {once: true});
    Douku.$("confirm-destructive").onclick = (event) => {
      event.preventDefault();
      finish(true);
    };
    dialog.showModal();
  });
  window.Douku = Douku;
})();
