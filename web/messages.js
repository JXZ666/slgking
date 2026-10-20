import { api, el, clear } from "./app.js";
import { currentAccount, loadSession } from "./login.js";
import { renderAnnouncements } from "./announcements.js";

const PAGE_SIZE = 20;
const tabs = [
  ["announcements", "公告"],
  ["activities", "活动"],
  ["replies", "回复"],
];
const privateViews = new Set();
const badgeNodes = new Set();
const badgeTickets = new WeakMap();
let generation = 0;

const identity = () => String(currentAccount()?.account_id || "");
const validId = (value) => typeof value === "string" && /^[A-Za-z0-9_-]{1,128}$/.test(value);
const value = (params, key) => typeof params?.get === "function" ? params.get(key) : params?.[key];
const messageHref = (tab, page = 1) => "#/messages?" + new URLSearchParams({ tab, ...(page > 1 ? { page: String(page) } : {}) });
const loginHref = (tab) => "#/login?return=" + encodeURIComponent(messageHref(tab));

function pageNumber(number) {
  const text = String(number || "1");
  return /^[1-9]\d{0,6}$/.test(text) && Number(text) <= 1000000 ? Number(text) : 1;
}

function errorText(error) {
  if (error.status === 400) return "请求内容不合法，请刷新后重试。";
  if (error.status === 401) return "登录已失效，请重新登录。";
  if (error.status === 429) return "请求较多，请稍后再试。";
  if (error.status >= 500) return "服务暂时不可用，请稍后重试。";
  return error.message || "网络暂时不可用，请稍后重试。";
}

function resetBadge(node) {
  node.dataset.rewardAvailable = "false";
  node.setAttribute("aria-label", "消息");
  node.title = "消息";
  const dot = node.querySelector(".message-reward-dot");
  if (dot) dot.hidden = true;
}

function showBadge(node, available) {
  resetBadge(node);
  if (!available) return;
  let dot = node.querySelector(".message-reward-dot");
  if (!dot) {
    dot = el("span", { class: "message-reward-dot", "aria-hidden": "true" });
    node.append(dot);
  }
  dot.hidden = false;
  node.dataset.rewardAvailable = "true";
  node.setAttribute("aria-label", "消息，有可领取活动奖励");
  node.title = "消息，有可领取活动奖励";
}

function rewardAvailable(result) {
  return result?.available === true && result.claimed === false
    && typeof result.campaign?.id === "string" && /^[a-z0-9][a-z0-9._-]{0,63}$/.test(result.campaign.id);
}

function paintBadges(available) {
  for (const node of badgeNodes) {
    if (!node.isConnected) badgeNodes.delete(node);
    else {
      badgeTickets.set(node, (badgeTickets.get(node) || 0) + 1);
      showBadge(node, available);
    }
  }
}

export function clearMessageState() {
  generation += 1;
  for (const node of badgeNodes) {
    resetBadge(node);
    if (!node.isConnected) badgeNodes.delete(node);
  }
  for (const record of privateViews) {
    if (record.page.isConnected) record.clear();
  }
  privateViews.clear();
}

export async function refreshMessageBadge(node) {
  if (!node) return false;
  badgeNodes.add(node);
  const ticket = (badgeTickets.get(node) || 0) + 1;
  badgeTickets.set(node, ticket);
  const account = identity(), stamp = generation;
  resetBadge(node);
  if (!account || !node.isConnected) return false;
  try {
    const result = await api("/web/account/rewards/maintenance");
    if (!node.isConnected || account !== identity() || stamp !== generation || badgeTickets.get(node) !== ticket) return false;
    const available = rewardAvailable(result);
    showBadge(node, available);
    return available;
  } catch (error) {
    if (account === identity() && stamp === generation && badgeTickets.get(node) === ticket) {
      resetBadge(node);
      if (error.status === 401) {
        clearMessageState();
        try { await loadSession(); } catch { /* Private state is already cleared. */ }
      }
    }
    return false;
  }
}

export async function renderMessages(node, params = {}) {
  clear(node);
  const boot = el("p", { class: "messages-feedback", role: "status", text: "正在确认账号状态…" });
  node.append(boot);
  let sessionError = null;
  try { await loadSession(); } catch (error) { sessionError = error; }
  if (!boot.isConnected || !node.contains(boot)) return;
  clear(node);
  for (const record of privateViews) if (!record.page.isConnected) privateViews.delete(record);
  const tab = tabs.some(([id]) => id === value(params, "tab")) ? value(params, "tab") : "announcements";
  const account = identity(), stamp = generation;
  const page = el("section", { class: "messages-page" });
  const navigation = el("nav", { class: "messages-tabs", "aria-label": "消息分类" });
  const pane = el("div", { class: "messages-pane", "data-message-tab": tab });
  for (const [id, label] of tabs) navigation.append(el("a", {
    href: messageHref(id), text: label, "aria-current": id === tab ? "page" : null,
  }));
  navigation.addEventListener("keydown", (event) => {
    const links = [...navigation.querySelectorAll("a")];
    const index = links.indexOf(document.activeElement);
    if (index < 0 || !["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    links[event.key === "Home" ? 0 : event.key === "End" ? links.length - 1
      : (index + (event.key === "ArrowRight" ? 1 : -1) + links.length) % links.length].focus();
  });
  page.append(el("h2", { text: "消息" }), navigation, pane);
  node.append(page);
  const alive = () => page.isConnected && node.contains(page) && account === identity() && stamp === generation;

  function showLogin(message = "登录后查看你的消息和活动。") {
    clear(pane);
    pane.append(el("p", { class: "messages-feedback", role: "status", text: message }));
    if (identity()) pane.append(el("button", {
      type: "button", class: "ghost", text: "重新读取消息", onclick: () => { void renderMessages(node, params); },
    }));
    else pane.append(el("a", { class: "messages-login", href: loginHref(tab), text: "前往登录" }));
  }

  const record = { page, clear: () => showLogin("账号状态已变化，请重新读取消息。") };
  if (account) privateViews.add(record);
  async function handleError(error, target) {
    if (!alive()) return;
    if (error.status === 401) {
      clearMessageState();
      showLogin("登录已失效，请重新登录。");
      try { await loadSession(); } catch { /* Do not restore private content on failure. */ }
      if (page.isConnected) showLogin("登录已失效，请重新登录。");
      return;
    }
    target.classList.add("error");
    target.textContent = errorText(error);
  }

  async function verifyAccount() {
    const latest = await loadSession();
    if (!alive() || latest?.account_id !== account) {
      if (page.isConnected && node.contains(page)) {
        clearMessageState();
        showLogin("账号状态已变化，请重新读取消息。");
      }
      return false;
    }
    return true;
  }

  if (sessionError) {
    pane.append(el("p", { class: "messages-feedback error", role: "status", text: errorText(sessionError) }),
      el("button", { type: "button", class: "ghost", text: "重新读取消息", onclick: () => { void renderMessages(node, params); } }));
    return;
  }
  if (tab === "announcements") {
    await renderAnnouncements(pane, { page: pageNumber(value(params, "page")) });
    return;
  }
  if (!account) { showLogin(); return; }
  const status = el("p", { class: "messages-feedback", role: "status", "aria-live": "polite" });
  pane.append(status);
  if (tab === "activities") await renderActivities();
  else await renderReplies();

  async function renderActivities() {
    const heading = el("h3", { text: "当前活动" });
    const refresh = el("button", { type: "button", class: "ghost", text: "刷新活动" });
    const body = el("div", { class: "messages-activity" });
    pane.replaceChildren(el("header", { class: "messages-heading" }, heading, refresh), status, body);
    let request = 0, claiming = false, campaign = null;
    function errorState(error) { return handleError(error, status); }
    async function load(preserveFeedback = false) {
      if (!alive()) return null;
      const ticket = ++request;
      campaign = null;
      clear(body);
      refresh.disabled = true;
      if (!preserveFeedback) { status.classList.remove("error", "good"); status.textContent = "正在读取活动…"; }
      try {
        if (!await verifyAccount()) return null;
        const result = await api("/web/account/rewards/maintenance");
        if (!alive() || ticket !== request) return null;
        if (typeof result.claimed !== "boolean" || typeof result.available !== "boolean") throw new Error("活动数据格式不正确，请刷新重试。");
        campaign = result.campaign;
        const available = rewardAvailable(result);
        paintBadges(available);
        if (!campaign) body.append(el("p", { class: "muted", text: "暂无可领取的活动。" }));
        else {
          body.append(el("h3", { text: campaign.title || "活动奖励" }),
            el("p", { class: "messages-activity-body", text: campaign.body || "" }));
          const button = el("button", {
            type: "button", class: "messages-claim", disabled: !available || claiming,
            text: result.claimed ? "已领取" : available ? `领取 ${campaign.amount} 积分` : "暂不可领取",
          });
          button.addEventListener("click", () => { void claim(button, String(campaign.id)); });
          body.append(button);
        }
        if (!preserveFeedback) status.textContent = result.claimed ? "此活动奖励已领取。" : "";
        return result;
      } catch (error) {
        if (alive() && ticket === request) { paintBadges(false); await errorState(error); }
        return null;
      } finally { if (alive() && ticket === request) refresh.disabled = claiming; }
    }
    async function claim(button, campaignId) {
      if (claiming || !alive()) return;
      claiming = true;
      refresh.disabled = button.disabled = true;
      status.classList.remove("error", "good");
      status.textContent = "正在领取…";
      let outcome = "", received = false;
      try {
        if (!await verifyAccount()) return;
        const result = await api("/web/account/rewards/maintenance/claim", { method: "POST", body: { campaign_id: campaignId } });
        if (!alive()) return;
        received = result.claimed === true;
        if (!received) throw new Error("领取结果尚未确认，请核对状态。");
        outcome = result.gained ? `已领取 ${result.gained} 积分。` : "此奖励已领取，不会重复发放。";
        paintBadges(false);
        window.dispatchEvent(new CustomEvent("slg-account-updated"));
      } catch (error) {
        if (!alive()) return;
        if (error.status === 401) { await errorState(error); return; }
        outcome = error.outcomeUnknown || error.status === 0
          ? "领取结果尚未确认，正在核对状态；不会自动重试。" : errorText(error);
        status.classList.add("error");
      } finally {
        claiming = false;
        if (alive()) {
          status.textContent = outcome;
          const checked = await load(true);
          if (alive() && checked) {
            const confirmed = checked.campaign?.id === campaignId && checked.claimed === true;
            status.textContent = received ? outcome : confirmed ? "已核对：活动奖励已领取，不会重复发放。"
              : outcome + " 已核对当前领取状态。";
            if (received || confirmed) {
              status.classList.remove("error"); status.classList.add("good");
              if (!received) window.dispatchEvent(new CustomEvent("slg-account-updated"));
            }
          } else if (alive() && received) {
            status.textContent = outcome + " 活动状态暂时无法刷新，请稍后核对。";
          }
        }
      }
    }
    refresh.addEventListener("click", () => { if (!claiming) void load(); });
    await load();
  }

  async function renderReplies() {
    let pageIndex = pageNumber(value(params, "page")), request = 0, reading = false, unread = 0;
    const refresh = el("button", { type: "button", class: "ghost", text: "刷新回复" });
    const markAll = el("button", { type: "button", class: "ghost", text: "全部已读", disabled: true });
    const count = el("p", { class: "messages-count muted" });
    const list = el("div", { class: "messages-notifications" });
    const pages = el("nav", { class: "messages-pages", "aria-label": "回复消息分页" });
    pane.replaceChildren(el("header", { class: "messages-heading" }, el("h3", { text: "回复我的" }),
      el("div", { class: "messages-tools" }, refresh, markAll)), status, count, list, pages);
    async function load() {
      if (!alive()) return { ok: false, stale: true };
      const ticket = ++request;
      clear(list); clear(pages); count.textContent = "";
      unread = 0;
      markAll.disabled = refresh.disabled = true;
      status.classList.remove("error", "good"); status.textContent = "正在读取回复…";
      try {
        if (!await verifyAccount()) return { ok: false, stale: true };
        const data = await api("/web/account/notifications?" + new URLSearchParams({ page: String(pageIndex), limit: String(PAGE_SIZE) }));
        if (!alive() || ticket !== request) return { ok: false, stale: true };
        if (!Array.isArray(data.notifications) || !Number.isSafeInteger(data.unread_count) || data.unread_count < 0) throw new Error("消息数据格式不正确，请刷新重试。");
        unread = data.unread_count;
        count.textContent = data.unread_count ? `${data.unread_count} 条未读` : "暂无未读回复";
        markAll.disabled = reading || data.unread_count === 0;
        for (const item of data.notifications) {
          const date = new Date(item.created);
          const link = validId(item.reply_id) && validId(item.id)
            ? el("a", { class: "messages-target", href: "#/discussion/" + encodeURIComponent(item.reply_id) + "?notification=" + encodeURIComponent(item.id), text: `${item.nickname || "用户"} 回复了你` })
            : el("span", { class: "messages-target", text: `${item.nickname || "用户"} 回复了你` });
          list.append(el("article", { class: "messages-notification", "data-notification-id": String(item.id || "") },
            el("div", { class: "messages-notification-main" }, link,
              el("span", { class: "messages-read-state muted", text: item.read === true ? "已读" : "未读" })),
            el("p", { class: "messages-game muted", text: item.game || "游戏评论" }),
            el("time", { class: "messages-date muted", text: Number.isNaN(date.getTime()) ? "" : date.toLocaleString() })));
        }
        if (!data.notifications.length) list.append(el("p", { class: "muted messages-empty", text: "暂无回复消息。" }));
        const totalPages = Math.max(1, Number(data.total_pages) || 1);
        const previous = el("button", { type: "button", class: "ghost", text: "上一页", disabled: pageIndex <= 1 });
        const next = el("button", { type: "button", class: "ghost", text: "下一页", disabled: pageIndex >= totalPages });
        previous.onclick = () => { location.hash = messageHref("replies", pageIndex - 1); };
        next.onclick = () => { location.hash = messageHref("replies", pageIndex + 1); };
        pages.append(previous, el("span", { text: `${pageIndex} / ${totalPages}` }), next);
        status.textContent = "";
        return { ok: true };
      } catch (error) {
        if (alive() && ticket === request) await handleError(error, status);
        return { ok: false, status: error.status || 0 };
      }
      finally { if (alive() && ticket === request) refresh.disabled = reading; }
    }
    markAll.onclick = async () => {
      if (reading || !alive()) return;
      reading = true; refresh.disabled = markAll.disabled = true;
      status.classList.remove("error", "good"); status.textContent = "正在标记…";
      try {
        if (!await verifyAccount()) return;
        await api("/web/account/notifications/read", { method: "POST", body: { all: true } });
        if (!alive()) return;
        window.dispatchEvent(new CustomEvent("slg-notifications-updated"));
        reading = false;
        const refreshed = await load();
        if (alive()) {
          status.classList.remove("error", "good");
          status.classList.add(refreshed.ok ? "good" : "error");
          status.textContent = refreshed.ok ? "已将你的回复消息标为已读。"
            : refreshed.status === 429 ? "已读已保存，列表刷新受限，请稍后刷新。"
              : "已读已保存，但消息列表刷新失败，请稍后刷新。";
        }
      } catch (error) {
        if (alive()) {
          if (error.outcomeUnknown || error.status === 0) {
            const refreshed = await load();
            if (alive()) {
              status.classList.add("error");
              status.textContent = refreshed.ok ? "标记结果尚未确认，已重新读取消息；不会自动重试。"
                : "标记结果尚未确认，消息刷新失败；不会自动重试，请稍后核对。";
            }
          } else await handleError(error, status);
        }
      } finally {
        reading = false;
        if (alive()) { refresh.disabled = false; markAll.disabled = unread === 0; }
      }
    };
    refresh.onclick = () => { if (!reading) void load(); };
    await load();
  }
}
