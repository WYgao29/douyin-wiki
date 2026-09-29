(() => {
  "use strict";
  const D = window.Douku;
  if (!D) return;
  let timer = null;
  let sessionId = null;

  function stop() {
    clearTimeout(timer);
    timer = null;
  }

  function renderSkeleton() {
    [
      ["auth-video-card", "视频下载授权"],
      ["auth-douyin-card", "抖音账号授权"],
    ].forEach(([id, title]) => {
      const card = D.node("article", null, "status-card");
      card.append(D.node("h2", title), D.node("p", "正在读取授权状态…", "hint-copy"));
      const pulse = D.node("div", null, "skeleton-row");
      card.append(pulse);
      D.$(id).replaceChildren(card);
    });
  }

  function renderChannel(container, channel) {
    container.replaceChildren();
    const card = D.node("article", null, "status-card");
    card.append(
      D.node("h2", channel.channel_label),
      D.node("p", channel.purpose),
      D.node("p", channel.user_state_label, "status-pill"),
    );
    card.lastChild.dataset.state = channel.user_state;
    card.append(D.node("p", `上次检查：${channel.checked_display || "尚未检查"}`));
    if (channel.user_state === "unconfirmed" && channel.message) {
      card.append(D.node("p", channel.message));
    }
    const technical = [];
    if (channel.user_state !== "unconfirmed" && channel.message) technical.push(channel.message);
    if (channel.detail) technical.push(channel.detail);
    if (channel.cookie_source_label) technical.push(channel.cookie_source_label);
    if (channel.account_hint) technical.push(`账号：${channel.account_hint}`);
    if (channel.affected_job_count) {
      technical.push(`受影响任务 ${channel.affected_job_count} 个`);
    }
    if (technical.length) {
      const folded = D.node("details", null, "auth-details");
      folded.append(D.node("summary", "详情"));
      technical.forEach((line) => folded.append(D.node("p", line, "hint-copy")));
      card.append(folded);
    }
    const actions = D.node("div", null, "operation-actions");
    const check = D.node("button", "检查", "primary-button");
    check.type = "button";
    check.addEventListener("click", async () => {
      check.disabled = true;
      check.textContent = "正在检查…";
      try {
        const data = await load(true);
        const current = data.channels?.[channel.channel];
        D.toast(current?.user_state_label || "检查完成");
      } catch (error) {
        D.toast(error.message || "检查失败");
      } finally {
        check.disabled = false;
        if (check.isConnected) check.textContent = "检查";
      }
    });
    const authorize = D.node("button", "重新授权", "secondary-button");
    authorize.type = "button";
    authorize.addEventListener("click", () => start(channel.channel));
    actions.append(check, authorize);
    if (channel.affected_job_count) {
      const link = D.node("a", "查看受影响任务", "secondary-button");
      link.href = "/jobs?requires_user_action=1";
      link.addEventListener("click", (event) => {
        event.preventDefault();
        D.navigate("/jobs?requires_user_action=1");
      });
      actions.append(link);
    }
    card.append(actions);
    container.append(card);
  }

  async function load(refresh = false) {
    const data = await D.api(`/api/auth/status${refresh ? "?refresh=true" : ""}`);
    renderChannel(D.$("auth-video-card"), data.channels.video);
    renderChannel(D.$("auth-douyin-card"), data.channels.douyin);
    const session = D.$("auth-session");
    if (sessionId) {
      const current = await D.api(`/api/auth/sessions/${encodeURIComponent(sessionId)}`);
      session.hidden = false;
      session.replaceChildren(
        D.node("h2", "当前授权会话"),
        D.node("p", current.stage === "waiting_login" ? "请在本机打开的浏览器中完成登录、扫码或验证码。" : current.stage),
        D.node("p", current.error_summary || ""),
      );
      if (current.stage === "succeeded") {
        D.toast(current.retried_job_ids?.length ? `授权成功，已继续 ${current.retried_job_ids.length} 个任务` : "授权已恢复");
        sessionId = null;
        stop();
      } else if (["failed", "timeout", "cancelled", "profile_locked", "account_changed"].includes(current.stage)) {
        D.toast(current.error_summary || "授权未完成");
        sessionId = null;
        stop();
      } else {
        const later = D.node("button", "稍后处理", "secondary-button");
        later.type = "button";
        later.addEventListener("click", async () => {
          await D.api(`/api/auth/sessions/${encodeURIComponent(current.id)}/cancel`, {method: "POST", body: "{}"});
          sessionId = null;
          stop();
          load(true);
        });
        session.append(later);
      }
    } else {
      session.hidden = true;
    }
    return data;
  }

  async function start(channel) {
    const session = await D.api("/api/auth/sessions", {
      method: "POST",
      body: JSON.stringify({channel}),
    });
    sessionId = session.id;
    D.toast("正在打开本机浏览器，请完成登录。抖库不会读取密码或 Cookie。");
    await load(true);
    stop();
    const tick = async () => {
      try { await load(true); } catch (error) { D.toast(error.message); }
      if (sessionId) timer = setTimeout(tick, 2500);
    };
    timer = setTimeout(tick, 2500);
  }

  window.addEventListener("douku:route", async (event) => {
    if (event.detail.path !== "/settings/auth") {
      stop();
      return;
    }
    D.hideAllViews();
    D.setPage("auth");
    D.setNav("settings-nav");
    D.$("auth-view").classList.remove("hidden");
    document.title = "授权状态 · 抖库";
    renderSkeleton();
    try {
      const data = await load(false);
      const video = data.channels?.video;
      const needsVerify = data.cached || video?.user_state === "unconfirmed" || video?.user_state === "unverified";
      if (needsVerify) load(true).catch((error) => D.toast(error.message));
    } catch (error) {
      D.toast(error.message);
    }
  });
})();
