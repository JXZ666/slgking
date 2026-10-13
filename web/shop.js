import { api, el, clear } from "./app.js";
import { currentAccount, loadSession } from "./login.js";
import { renderAuthor, applyAuthorAppearance } from "./author.js";

const accountId = () => String(currentAccount()?.account_id || "");
const category = item => item.kind === "title" ? "头衔" : item.appearance === "avatar_frame" ? "头像框" : "名片框";

export async function renderShop(node) {
  clear(node);
  const identity = accountId();
  const alive = () => node.isConnected && node.contains(list) && accountId() === identity;
  const status = el("p", { class: "shop-status muted", role: "status", "aria-live": "polite" });
  const balance = el("strong", { class: "shop-balance", text: "积分 …" });
  const list = el("div", { class: "shop-grid" });
  const preview = el("section", { class: "shop-preview", "aria-label": "试搭预览" });
  const filter = el("select", { "aria-label": "商品分类" }, ...["全部", "头衔", "头像框", "名片框"].map(v => el("option", { value: v, text: v })));
  const scope = el("select", { "aria-label": "货架或库存" }, el("option", { value: "shelf", text: "商品货架" }), el("option", { value: "owned", text: "我的库存" }));
  const refresh = el("button", { type: "button", class: "ghost", text: "刷新" });
  node.append(el("header", { class: "shop-heading" }, el("h2", { text: "积分商城" }), balance),
    el("div", { class: "shop-tools" }, filter, scope, refresh), preview, status, list);
  let catalogue = { items: [], titles: [] }, account = null, busy = false, revision = 0;
  let blocked = false, outfit = null;
  const owned = () => new Set(account?.owned_item_ids || []);
  function previewItem(item) {
    outfit ||= { ...(account?.equipped_appearances || {}) };
    if (item?.kind === "decoration") outfit[item.appearance] = item.id;
    const title = item?.kind === "title" ? item : account?.equipped_title;
    const data = { nickname: account?.nickname || "试搭预览", author_title_id: title?.id || "", author_title_name: title?.name || "", author_appearances: outfit };
    clear(preview);
    preview.append(el("h3", { text: "试搭预览" }));
    const card = el("div", { class: "shop-preview-author" }, renderAuthor(data));
    applyAuthorAppearance(card, data);
    preview.append(card);
    if (item) preview.append(el("p", { class: "muted", text: `正在试搭：${item.name}` }));
  }
  function draw() {
    clear(list);
    balance.textContent = account ? `积分 ${account.balance ?? 0}` : "未登录";
    const items = [...catalogue.items.filter(i => ["title", "decoration"].includes(i.kind))];
    if (scope.value === "owned") {
      for (const title of account?.titles || []) if (!items.some(i => i.id === title.id)) items.push({ ...title, kind: "title", available: false });
    }
    const inventory = owned();
    for (const title of account?.titles || []) inventory.add(title.id);
    const shown = items.filter(i => (filter.value === "全部" || category(i) === filter.value) && (scope.value === "owned" ? inventory.has(i.id) : i.selling !== false));
    for (const item of shown) {
      const has = inventory.has(item.id);
      const equipped = item.kind === "title" ? account?.equipped_title?.id === item.id : account?.equipped_appearances?.[item.appearance] === item.id;
      const tryButton = el("button", { type: "button", class: "ghost", text: "试搭", onclick: () => previewItem(item) });
      const action = el("button", { type: "button", text: !identity ? "登录后兑换" : has ? equipped ? "卸下" : "装备" : item.available === false ? "已下架" : `兑换 · ${item.cost} 积分` });
      action.disabled = busy || blocked || (identity && !has && item.available === false);
      action.addEventListener("click", () => {
        if (!identity) { location.hash = "#/login?return=" + encodeURIComponent("#/shop"); return; }
        if (!has && !confirm(`兑换「${item.name}」将扣除 ${item.cost} 积分，确认兑换？`)) return;
        void transact(has ? "/web/account/equip" : "/web/account/shop/buy", has ? item.kind === "title" ? { title_id: equipped ? "" : item.id } : { appearance_slot: item.appearance, item_id: equipped ? "" : item.id } : { item_id: item.id });
      });
      list.append(el("article", { class: "shop-item" }, el("h3", { text: item.name }), el("p", { class: "shop-kind muted", text: `${category(item)} · ${item.rarity || item.subcategory || "普通"}${has ? " · 已拥有" : ""}` }), el("p", { class: "shop-description", text: item.description || "" }), el("div", { class: "shop-actions" }, tryButton, action)));
    }
    if (!shown.length) list.append(el("p", { class: "muted", text: scope.value === "owned" ? "暂无此类物品。" : "暂无可展示的商品。" }));
  }
  async function fetchState() {
    const ticket = ++revision;
    refresh.disabled = true;
    status.textContent = "正在读取商城…";
    try {
      const nextCatalogue = await api("/web/shop");
      const nextAccount = identity ? await api("/web/account/me") : null;
      if (!alive() || ticket !== revision) return false;
      catalogue = nextCatalogue; account = nextAccount?.account || nextAccount; blocked = false; outfit = null;
      previewItem(null); draw(); status.textContent = "";
      return true;
    } catch (error) {
      if (alive() && ticket === revision) status.textContent = error.status === 401 ? "会话已过期，请重新登录。" : error.message || "商城暂时无法读取。";
      return false;
    } finally { if (alive() && ticket === revision) refresh.disabled = false; }
  }
  async function transact(path, body) {
    if (busy || blocked || !alive()) return;
    busy = true; draw(); status.textContent = "正在处理…";
    let message = "";
    try {
      await api(path, { method: "POST", body });
      if (!alive()) return;
      message = path.endsWith("buy") ? "兑换成功。" : "装扮已更新。";
      await loadSession(true);
      if (!alive()) return;
      window.dispatchEvent(new CustomEvent("slg-account-updated"));
    } catch (error) {
      if (!alive()) return;
      blocked = Boolean(error.outcomeUnknown || error.status === 0);
      const errors = { "insufficient balance": "积分余额不足。", "insufficient points": "积分余额不足。", "already owned": "已拥有该物品。", "item unavailable": "商品已下架，请刷新货架。", "item not owned": "尚未拥有该物品，不能装备。", "title not owned": "尚未拥有该称号，不能装备。", "cosmetic not owned": "尚未拥有该装扮，不能装备。", "cosmetic unavailable": "装扮不适用于此槽位或已不可用。", "invalid appearance slot": "装扮槽位不匹配。", "bad appearance slot": "装扮槽位不匹配。" };
      message = blocked ? "操作结果尚未确认，正在核对库存；不会自动重试。" : error.status === 401 ? "会话已过期，请重新登录。" : errors[error.message] || error.message || "操作失败，请刷新后重试。";
    } finally {
      busy = false;
      if (alive()) {
        const resolved = await fetchState();
        if (!alive()) return;
        if (!resolved) blocked = true;
        draw();
        status.textContent = message + (resolved ? " 库存已刷新。" : " 库存核对失败，请先刷新核对后再操作。");
      }
    }
  }
  filter.addEventListener("change", draw); scope.addEventListener("change", draw);
  refresh.addEventListener("click", () => { if (!busy) void fetchState(); });
  const resume = () => {
    if (!alive()) { document.removeEventListener("visibilitychange", resume); return; }
    if (!document.hidden && !busy) void fetchState();
  };
  document.addEventListener("visibilitychange", resume);
  const observer = new MutationObserver(() => {
    if (!alive()) { document.removeEventListener("visibilitychange", resume); observer.disconnect(); }
  });
  observer.observe(node, { childList: true });
  await fetchState();
}
