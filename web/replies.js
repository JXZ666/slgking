import { api, el, clear } from "./app.js";
import { currentAccount, loadSession } from "./login.js";
import { renderAuthor, applyAuthorAppearance } from "./author.js";

const PAGE_SIZE = 20;
const PREFIX = "slgking:reply-draft:";
const ID = /^[A-Za-z0-9_-]{1,128}$/;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const STATES = { approved: "已公开", pending: "待审核", rejected: "未通过", hidden: "已隐藏", deleted: "已删除" };
const drafts = new Map();
const views = new Set();
const identity = () => String(currentAccount()?.account_id || "");
const canWrite = (account) => !!account && account !== "developer";
const length = (value) => Array.from(value).length;

function keyFor(account, root, target) {
  return PREFIX + [account, root, target || "root"].map(encodeURIComponent).join(":");
}
function readDraft(key) {
  let value = drafts.get(key);
  try { value = JSON.parse(sessionStorage.getItem(key) || "null") || value; } catch { /* optional storage */ }
  return value && typeof value.content === "string" && value.content.length <= 2000 && UUID.test(value.operation_id)
    ? value : { content: "", operation_id: crypto.randomUUID() };
}
function saveDraft(key, draft) {
  drafts.set(key, { ...draft });
  try { sessionStorage.setItem(key, JSON.stringify(draft)); } catch { /* optional storage */ }
}
function removeDraft(key) {
  drafts.delete(key);
  try { sessionStorage.removeItem(key); } catch { /* optional storage */ }
}

export function clearReplyDrafts(accountId) {
  const prefix = PREFIX + (accountId ? encodeURIComponent(accountId) + ":" : "");
  for (const key of drafts.keys()) if (key.startsWith(prefix)) drafts.delete(key);
  try { for (const key of Object.keys(sessionStorage)) if (key.startsWith(prefix)) sessionStorage.removeItem(key); } catch { /* optional storage */ }
  for (const view of [...views]) if (!accountId || view.account === String(accountId)) view.invalidate();
}

const detachObserver = new MutationObserver(() => {
  for (const view of views) if (!view.node.isConnected) views.delete(view);
  if (!views.size) detachObserver.disconnect();
});
window.addEventListener("slg-account-updated", () => {
  for (const view of [...views]) {
    if (view.account !== identity()) view.invalidate();
    else queueMicrotask(() => { if (view.node.isConnected && views.has(view)) view.refresh?.(); });
  }
});

function track(node, account, invalidate, refresh = null) {
  const view = { node, account, refresh, invalidate: () => { views.delete(view); invalidate(); } };
  views.add(view);
  detachObserver.observe(document.body, { childList: true, subtree: true });
  return view;
}
function statusBox() {
  return el("p", { class: "reply-status muted", role: "status", "aria-live": "polite" });
}
function loginHref() {
  return "#/login?return=" + encodeURIComponent(location.hash);
}
function showError(node, error, submitting = false) {
  clear(node);
  node.classList.add("error");
  node.textContent = error.outcomeUnknown ? submitting === "action" ? "操作结果尚未确认，请刷新讨论核对；不会自动重试。" : "提交结果尚未确认，草稿已保留；请核对我的回复状态后再操作。"
    : error.status === 401 ? "登录已过期，请重新登录。"
      : error.status === 429 ? "回复或请求过于频繁，请稍后重试。"
        : error.status === 0 ? (submitting ? "网络不可用，正文已保留。" : "网络不可用，请重试。")
        : submitting === "action" ? "操作未完成，请刷新讨论后重试。"
          : submitting ? "提交未完成，正文已保留，请稍后重试。" : "读取失败，未显示旧内容，请重试。";
  if (error.status === 401) node.append(" ", el("a", { href: loginHref(), text: "前往登录" }));
}
function dateText(value) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "" : date.toLocaleString();
}
function pageData(data) {
  if (!data || data.ok !== true || !Array.isArray(data.replies)
      || !Number.isSafeInteger(data.page) || data.page < 1
      || data.replies.length > PAGE_SIZE) throw new Error("Invalid discussion page");
  return data;
}
function addPager(node, data, go, label) {
  const page = data.page;
  const total = Number.isSafeInteger(data.total_pages) && data.total_pages >= 0 ? data.total_pages : 0;
  if (total <= 1 && !data.has_previous && !data.has_next) return;
  node.append(el("nav", { class: "reply-pager", "aria-label": label },
    el("button", { type: "button", class: "ghost", text: "上一页", disabled: page <= 1, onclick: () => go(page - 1) }),
    el("span", { class: "muted small", text: `${page} / ${Math.max(1, total)}` }),
    el("button", { type: "button", class: "ghost", text: "下一页", disabled: page >= total, onclick: () => go(page + 1) })));
}

function createThread(node, root, { account, focusId = "", onTargetRendered, onUnavailable } = {}) {
  const rootId = String(root.id || root.cloud_id || "");
  let invalid = false, request = 0, ownRequest = 0, page = 1, ownPage = 1, sending = false, target = null;
  let draft = readDraft(keyFor(account, rootId, null));
  const status = statusBox();
  const listing = el("div", { class: "reply-list" });
  const pages = el("div");
  const ownStatus = statusBox();
  const ownListing = el("div", { class: "reply-list" });
  const ownPages = el("div");
  const mine = el("details", { class: "reply-mine" }, el("summary", { text: "我的回复与审核状态" }), ownStatus, ownListing, ownPages);
  const refresh = el("button", { type: "button", class: "ghost reply-refresh", text: "刷新回复" });
  const thread = el("section", { class: "reply-thread", "aria-label": "评论回复" },
    el("div", { class: "reply-heading" }, el("h4", { text: "回复" }), refresh), status, listing, pages);
  node.append(thread);
  const alive = () => !invalid && thread.isConnected && identity() === account;
  const view = track(thread, account, () => {
    invalid = true; request++; ownRequest++;
    clear(thread);
    thread.append(el("p", { class: "reply-status muted", role: "status", text: "账号已切换，请重新打开讨论。" }));
  }, () => {
    ownRequest++; clear(ownListing); clear(ownPages);
    void (async () => { await loadPublic(page); if (alive()) await loadOwn(ownPage); })();
  });
  const unavailable = () => {
    invalid = true; request++; ownRequest++;
    views.delete(view);
    clear(thread);
    if (onUnavailable) onUnavailable();
    else thread.append(el("p", { class: "reply-status error", role: "status", text: "原评论已不可查看。" }));
  };
  async function confirmedAccount() {
    const session = await loadSession();
    if (session?.account_id !== account || !alive()) {
      if (thread.isConnected && identity() !== account) view.invalidate();
      return false;
    }
    return true;
  }
  async function act(item, action, button, feedback) {
    if (!alive() || sending || !canWrite(account)) return;
    if (action === "delete" && !window.confirm("确定删除这条追评？")) return;
    button.disabled = true;
    try {
      if (!await confirmedAccount()) return;
      const result = await api("/web/comment-replies/" + encodeURIComponent(item.id) + (action === "report" ? "/report" : ""),
        action === "delete" ? { method: "DELETE" } : { method: "POST", body: {} });
      if (!alive()) return;
      if (result?.ok !== true) throw Object.assign(new Error("Unconfirmed reply action"), { outcomeUnknown: true });
      if (action === "report") { feedback.classList.remove("error"); feedback.textContent = "举报已提交。"; button.textContent = "已举报"; }
      else { await loadPublic(page); if (alive()) await loadOwn(ownPage); }
    } catch (error) {
      if (!alive()) return;
      if (error.status === 404) { await loadPublic(page); return; }
      showError(feedback, error, "action"); button.disabled = false;
    }
  }
  function row(item, own = false) {
    if (!item || !ID.test(item.id) || item.root_id !== rootId || typeof item.content !== "string") throw new Error("Invalid reply");
    const feedback = statusBox();
    const controls = el("div", { class: "reply-actions" });
    const card = el("article", { class: "reply-item", "data-reply-id": item.id },
      el("div", { class: "reply-meta" }, renderAuthor(item), el("time", { class: "muted small", text: dateText(item.created) }),
        own ? el("span", { class: "reply-state", text: STATES[item.status] || "状态待确认" }) : null),
      item.reply_to_nickname ? el("p", { class: "reply-target muted small", text: "回复 " + item.reply_to_nickname }) : null,
      el("p", { class: "reply-content", text: item.content }), controls, feedback);
    applyAuthorAppearance(card, item);
    if (canWrite(account)) {
      if (item.status === "approved") controls.append(el("button", { type: "button", class: "ghost", text: "回复", onclick: () => {
        if (!alive() || sending) return;
        changeTarget(item); input.focus();
      } }));
      if ((own && item.status !== "deleted") || (!own && item.status === "approved")) {
        const action = own ? "delete" : "report";
        const button = el("button", { type: "button", class: "ghost", text: own ? "删除" : "举报" });
        button.onclick = () => { void act(item, action, button, feedback); };
        controls.append(button);
      }
    }
    return card;
  }
  async function loadPublic(next = 1, focus = "") {
    if (!alive()) return;
    const ticket = ++request;
    clear(listing); clear(pages);
    status.classList.remove("error"); status.textContent = "正在读取回复…";
    refresh.disabled = true;
    try {
      const query = new URLSearchParams({ page: String(next), limit: String(PAGE_SIZE) });
      if (focus) query.set("focus_id", focus);
      const data = pageData(await api((account ? "/web/comments/" : "/comments/") + encodeURIComponent(rootId) + "/replies?" + query));
      if (!alive() || ticket !== request) return;
      if (data.root_id !== rootId) throw new Error("Invalid discussion root");
      const rows = data.replies.map((item) => row(item, item.owned === true || item.own === true));
      page = data.page;
      listing.append(...rows);
      if (!rows.length) listing.append(el("p", { class: "muted small", text: "暂无公开回复。" }));
      addPager(pages, data, (value) => { void loadPublic(value); }, "公开回复分页");
      status.textContent = "共 " + (Number(data.total_count) || 0) + " 条公开回复";
      if (focus) {
        const card = rows.find((element) => element.dataset.replyId === focus);
        if (!card) { clear(listing); clear(pages); status.textContent = "目标回复已不可查看，没有标记消息已读。"; return; }
        card.classList.add("reply-focused"); card.tabIndex = -1;
        await new Promise(requestAnimationFrame);
        if (!alive() || ticket !== request || !card.isConnected) return;
        card.focus({ preventScroll: true }); card.scrollIntoView({ block: "center" });
        if (onTargetRendered) await onTargetRendered(card);
      }
    } catch (error) {
      if (!alive() || ticket !== request) return;
      clear(listing); clear(pages);
      if (error.status === 404) { unavailable(); return; }
      showError(status, error);
      status.append(" ", el("button", { type: "button", class: "ghost", text: "重试", onclick: () => { void loadPublic(next, focus); } }));
    } finally { if (alive() && ticket === request) refresh.disabled = false; }
  }
  async function loadOwn(next = 1) {
    if (!alive() || !canWrite(account)) return;
    const ticket = ++ownRequest;
    clear(ownListing); clear(ownPages); ownStatus.textContent = "正在读取审核状态…";
    ownStatus.classList.remove("error");
    try {
      const query = new URLSearchParams({ root_id: rootId, page: String(next), limit: String(PAGE_SIZE) });
      const data = pageData(await api("/web/account/comment-replies?" + query));
      if (!alive() || ticket !== ownRequest) return;
      ownPage = data.page;
      ownListing.append(...data.replies.map((item) => row(item, true)));
      if (!data.replies.length) ownListing.append(el("p", { class: "muted small", text: "还没有追评投稿。" }));
      addPager(ownPages, data, (value) => { void loadOwn(value); }, "本人回复分页");
      ownStatus.textContent = "";
    } catch (error) {
      if (!alive() || ticket !== ownRequest) return;
      clear(ownListing); clear(ownPages); showError(ownStatus, error);
      ownStatus.append(" ", el("button", { type: "button", class: "ghost", text: "重试", onclick: () => { void loadOwn(next); } }));
    }
  }
  const targetLabel = el("p", { class: "reply-composer-target", text: "回复原评论" });
  const inputId = "reply-input-" + crypto.randomUUID();
  const input = el("textarea", { id: inputId, class: "reply-input", rows: "3", minlength: "2", maxlength: "1000", required: true, placeholder: "写下追评（2–500 字）" });
  const count = el("span", { class: "muted small" });
  const feedback = statusBox();
  const submit = el("button", { type: "submit", text: "发布追评" });
  const reset = el("button", { type: "button", class: "ghost", text: "回复原评论", onclick: () => { if (!sending) changeTarget(null); } });
  function restore() { input.value = draft.content; count.textContent = length(input.value.trim()) + " / 500"; }
  function changeTarget(item) {
    saveDraft(keyFor(account, rootId, target?.id), draft);
    target = item;
    draft = readDraft(keyFor(account, rootId, target?.id)); restore();
    targetLabel.textContent = target ? "回复 " + (target.nickname || "用户") : "回复原评论";
    reset.hidden = !target; feedback.textContent = "";
  }
  input.addEventListener("input", () => {
    if (!alive()) return;
    if (draft.content !== input.value) draft = { content: input.value, operation_id: crypto.randomUUID() };
    saveDraft(keyFor(account, rootId, target?.id), draft);
    count.textContent = length(input.value.trim()) + " / 500";
  });
  const composer = el("form", { class: "reply-composer" }, targetLabel,
    el("label", { for: inputId, class: "small", text: "追评" }), input,
    el("div", { class: "reply-composer-footer" }, count, reset, submit), feedback);
  composer.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!alive() || sending || !canWrite(account)) return;
    const content = input.value.trim();
    if (length(content) < 2 || length(content) > 500) { feedback.textContent = "追评需要 2–500 字。"; input.focus(); return; }
    const key = keyFor(account, rootId, target?.id);
    saveDraft(key, draft);
    const body = { content, reply_to_id: target?.id || null, operation_id: draft.operation_id };
    if (new TextEncoder().encode(JSON.stringify(body)).length > 2048) { feedback.textContent = "追评请求超过大小限制，请缩短正文。"; return; }
    sending = true; submit.disabled = input.disabled = reset.disabled = true;
    feedback.classList.remove("error"); feedback.textContent = "正在提交追评…";
    try {
      if (!await confirmedAccount()) return;
      const result = await api("/web/comments/" + encodeURIComponent(rootId) + "/replies", { method: "POST", body });
      if (!alive()) return;
      const record = result?.reply;
      if (result.ok === true && ID.test(record?.id) && ["approved", "pending"].includes(record?.status)) {
        removeDraft(key); target = null; draft = readDraft(keyFor(account, rootId, null));
        if (key === keyFor(account, rootId, null)) draft = { content: "", operation_id: crypto.randomUUID() };
        restore(); targetLabel.textContent = "回复原评论"; reset.hidden = true;
        feedback.textContent = record.status === "approved" ? "追评已公开。" : "追评已提交审核，可在我的回复中查看。";
        mine.open = true;
        await loadPublic(1); if (alive()) await loadOwn(1);
      } else {
        feedback.classList.add("error");
        feedback.textContent = record?.status === "rejected" ? "追评未通过审核，正文已保留，请修改后再提交。" : "提交结果尚未确认，草稿已保留，请先核对我的回复状态。";
        if (record?.status === "rejected") { mine.open = true; await loadOwn(1); }
      }
    } catch (error) {
      if (!alive()) return;
      if (error.status === 404) {
        feedback.classList.add("error");
        feedback.textContent = "回复对象已不可查看，正文已保留；可切换为回复原评论。";
        await loadPublic(page); if (alive()) await loadOwn(ownPage);
        return;
      }
      showError(feedback, error, true);
    } finally { sending = false; if (alive()) submit.disabled = input.disabled = reset.disabled = false; }
  });
  restore(); reset.hidden = true;
  if (canWrite(account)) thread.append(mine, composer);
  else thread.append(el("p", { class: "muted small" }, account ? "开发者模式仅支持阅读讨论。" : el("a", { href: loginHref(), text: "登录后发布追评" })));
  refresh.onclick = async () => { if (!sending) { await loadPublic(page); if (alive()) await loadOwn(ownPage); } };
  void (async () => { await loadPublic(1, focusId); if (alive()) await loadOwn(1); })();
  return { invalidate: view.invalidate };
}

export function attachReplies(host, root, { owned = false } = {}) {
  const rootId = String(root?.id || root?.cloud_id || "");
  if (!ID.test(rootId) || root.status !== "approved" || host.querySelector(":scope > .reply-discussion")) return null;
  const count = Number.isSafeInteger(root.reply_count) && root.reply_count >= 0 ? root.reply_count : 0;
  const details = el("details", { class: "reply-discussion", "data-root-id": rootId },
    el("summary", { text: "回复 · " + count }));
  const body = el("div", { class: "reply-discussion-body" }); details.append(body); host.append(details);
  let started = false;
  details.addEventListener("toggle", () => {
    if (!details.open || started || !details.isConnected) return;
    started = true;
    createThread(body, { ...root, id: rootId, owned }, { account: identity(), onUnavailable: () => {
      clear(host); host.append(el("p", { class: "reply-status error", role: "status", text: "原评论已不可查看，未显示旧内容。" }));
    } });
  });
  return details;
}

export async function renderDiscussion(node, replyId, params = new URLSearchParams()) {
  clear(node);
  const page = el("section", { class: "discussion-page" }, el("a", { href: "#/messages?tab=replies", text: "返回回复消息" }),
    el("h2", { text: "评论讨论" }));
  const status = statusBox(); const contents = el("div", { class: "discussion-contents" });
  page.append(status, contents); node.append(page);
  if (!ID.test(replyId)) { status.textContent = "讨论链接无效。"; return; }
  try { await loadSession(); } catch { /* Public discussion stays readable. */ }
  const account = identity();
  const view = track(page, account, () => {
    clear(contents); status.textContent = "账号已切换，请重新打开讨论。";
  }, () => { void renderDiscussion(node, replyId, params); });
  const alive = () => page.isConnected && node.contains(page) && account === identity() && views.has(view);
  status.textContent = "正在读取讨论…";
  let read = false;
  try {
    const context = await api("/comment-replies/" + encodeURIComponent(replyId) + "/context");
    if (!alive()) return;
    if (context?.ok !== true || context.reply_id !== replyId || !context.root || !ID.test(context.root.id || context.root.cloud_id)
        || typeof context.root.content !== "string") throw new Error("Invalid discussion context");
    const root = context.root;
    const rootCard = el("article", { class: "discussion-root" }, renderAuthor(root),
      el("p", { class: "discussion-root-content", text: root.content }));
    applyAuthorAppearance(rootCard, root);
    if (typeof context.game === "string" && context.game) contents.append(el("a", { class: "discussion-game", href: "#/game/" + encodeURIComponent(context.game), text: "查看游戏" }));
    contents.append(rootCard); status.textContent = "";
    const notification = typeof params.get === "function" ? params.get("notification") : params.notification;
    createThread(contents, root, { account, focusId: replyId, onUnavailable: () => {
      if (!alive()) return;
      clear(contents); status.textContent = "讨论已不可查看，未显示旧内容；没有标记消息已读。";
    }, onTargetRendered: async (card) => {
      if (!alive() || !card.isConnected || !account || !ID.test(notification) || read) return;
      try {
        const session = await loadSession();
        if (!alive() || session?.account_id !== account || !card.isConnected) return;
        const result = await api("/web/account/notifications/read", { method: "POST", body: { ids: [notification] } });
        if (!alive() || !card.isConnected) return;
        if (result?.ok !== true) throw new Error("Unconfirmed notification state");
        read = true; window.dispatchEvent(new CustomEvent("slg-notifications-updated"));
      } catch {
        if (alive()) status.textContent = "讨论已打开，但已读状态尚未确认；可返回消息页重新核对。";
      }
    } });
  } catch (error) {
    if (!alive()) return;
    clear(contents);
    status.textContent = error.status === 404 ? "讨论已删除或不可查看，没有标记消息已读。" : "讨论读取失败，未显示旧内容；没有标记消息已读。";
    status.append(" ", el("button", { type: "button", class: "ghost", text: "重试", onclick: () => { void renderDiscussion(node, replyId, params); } }));
  }
}
