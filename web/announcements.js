import { api, el, clear } from "./app.js";
import { currentAccount, loadSession } from "./login.js";

const identity = () => String(currentAccount()?.account_id || "");

export async function renderAnnouncementSummary(node) {
  const account = identity();
  try {
    const data = await api("/web/announcements?page=1&limit=1");
    if (!node.isConnected || account !== identity()) return;
    clear(node);
    node.append(el("a", { class: "announcement-summary", href: "#/announcements" }, el("span", { class: "muted", text: "公告" }), el("strong", { text: data.current?.title || "查看历史公告" }), el("span", { "aria-hidden": "true", text: "→" })));
  } catch {
    if (node.isConnected && account === identity()) {
      clear(node); node.append(el("a", { class: "announcement-summary", href: "#/announcements", text: "查看公告" }));
    }
  }
}

export async function renderAnnouncements(node, params = {}) {
  clear(node);
  const account = identity(), alive = () => node.isConnected && node.contains(contents) && account === identity();
  let page = Math.max(1, Number(params.page || 1) || 1), revision = 0, claiming = false;
  const status = el("p", { class: "announcement-status muted", role: "status", "aria-live": "polite" });
  const contents = el("div", { class: "announcement-list" });
  const pages = el("nav", { class: "announcement-pages", "aria-label": "公告分页" });
  const refresh = el("button", { type: "button", class: "ghost", text: "刷新公告" });
  node.append(el("header", { class: "announcement-heading" }, el("h2", { text: "公告" }), refresh), status, contents, pages);
  function article(item, current = false) {
    return el("article", { class: "announcement-item" }, el("p", { class: "announcement-label muted", text: current ? "当前公告" : "历史公告" }), el("h3", { text: item.title || "公告" }), el("p", { class: "announcement-body", text: item.body || "" }));
  }
  async function load() {
    const ticket = ++revision;
    refresh.disabled = true; status.textContent = "正在读取公告…";
    try {
      const data = await api(`/web/announcements?page=${page}&limit=20`);
      if (!alive() || ticket !== revision) return;
      clear(contents); clear(pages);
      if (data.current) {
        const entry = article(data.current, true); contents.append(entry);
        if (data.current.reward) await addReward(entry, data.current.reward, ticket);
        if (!alive() || ticket !== revision) return;
      }
      for (const item of data.history || []) contents.append(article(item));
      if (!contents.childElementCount) contents.append(el("p", { class: "muted", text: "暂无公告。" }));
      const previous = el("button", { type: "button", class: "ghost", text: "上一页", disabled: page <= 1 });
      const next = el("button", { type: "button", class: "ghost", text: "下一页", disabled: page >= (data.total_pages || 1) });
      previous.onclick = () => { page--; void load(); }; next.onclick = () => { page++; void load(); };
      pages.append(previous, el("span", { text: `${page} / ${Math.max(1, data.total_pages || 1)}` }), next);
      status.textContent = "";
    } catch (error) { if (alive() && ticket === revision) status.textContent = error.message || "公告暂时无法读取，请刷新重试。"; }
    finally { if (alive() && ticket === revision) refresh.disabled = false; }
  }
  async function addReward(entry, reward, ticket) {
    const box = el("div", { class: "announcement-reward" }); entry.append(box);
    if (!account) {
      box.append(el("a", { href: "#/login?return=" + encodeURIComponent("#/announcements"), text: "登录后查看公告奖励" })); return;
    }
    const feedback = el("p", { class: "muted", role: "status", "aria-live": "polite" });
    const button = el("button", { type: "button", text: "正在查询奖励…", disabled: true });
    const check = el("button", { type: "button", class: "ghost", text: "核对领取状态" });
    box.append(button, check, feedback);
    async function query() {
      button.disabled = true; check.disabled = true;
      try {
        const result = await api("/web/account/rewards/maintenance");
        if (!alive() || ticket !== revision || !entry.isConnected) return null;
        const valid = result.campaign && result.campaign.id === reward.id && result.available !== false;
        button.textContent = result.claimed ? "已领取" : valid ? `领取 ${result.campaign.amount || reward.amount || ""} 积分` : "奖励已结束";
        button.disabled = result.claimed || !valid || claiming;
        return result;
      } catch (error) {
        if (alive() && entry.isConnected) { button.textContent = "暂不可领取"; feedback.textContent = error.status === 401 ? "会话已过期，请重新登录。" : error.message; }
        return null;
      } finally { if (alive()) check.disabled = false; }
    }
    check.onclick = () => { if (!claiming) void query(); };
    button.onclick = async () => {
      if (claiming || !alive()) return;
      claiming = true; button.disabled = true; check.disabled = true;
      let message = "";
      try {
        const result = await api("/web/account/rewards/maintenance/claim", { method: "POST", body: { campaign_id: reward.id } });
        if (!alive() || ticket !== revision) return;
        message = result.gained ? `已领取 ${result.gained} 积分。` : "此奖励已领取，不会重复发放。";
        await loadSession(true);
        if (!alive()) return;
        window.dispatchEvent(new CustomEvent("slg-account-updated"));
      } catch (error) {
        if (!alive()) return;
        message = error.outcomeUnknown || error.status === 0 ? "领取结果尚未确认，正在核对状态；不会自动重试。" : error.message;
      } finally {
        claiming = false;
        if (alive() && ticket === revision && entry.isConnected) { await query(); feedback.textContent = message; }
      }
    };
    await query();
  }
  refresh.onclick = () => { if (!claiming) void load(); };
  const resume = () => {
    if (!alive()) { document.removeEventListener("visibilitychange", resume); return; }
    if (!document.hidden && !claiming) void load();
  };
  document.addEventListener("visibilitychange", resume);
  const observer = new MutationObserver(() => {
    if (!alive()) { document.removeEventListener("visibilitychange", resume); observer.disconnect(); }
  });
  observer.observe(node, { childList: true });
  await load();
}
