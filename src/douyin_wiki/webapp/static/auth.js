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
    if (channel.account_hint) card.append(D.node("p", `账号：${channel.account_hint}`));
    card.append(
      D.node("p", channel.cookie_source_label, "hint-copy"),
      D.node("p", `上次检查：${channel.checked_display || "尚未检查"}`),
      D.node("p", channel.affected_job_count ? `受影响任务 ${channel.affected_job_count} 个` : "当前没有因此暂停的任务"),
      D.node("p", channel.message),
    );
    const actions = D.node("div", null, "operation-actions");
    const authorize = D.node("button", channel.user_state === "authorized" ? "重新授权" : "授权", "primary-button");
    authorize.type = "button";
    authorize.addEventListener("click", () => start(channel.channel));
    const check = D.node("button", "检查状态", "secondary-button");
    check.type = "button";
    check.addEventListener("click", () => load(true));
    actions.append(authorize, check);
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
    D.setNav("auth-nav");
    D.$("auth-view").classList.remove("hidden");
    document.title = "授权状态 · 抖库";
    renderSkeleton();
    try {
      const data = await load(false);
      if (data.cached) load(true).catch((error) => D.toast(error.message));
    } catch (error) {
      D.toast(error.message);
    }
  });
})();
