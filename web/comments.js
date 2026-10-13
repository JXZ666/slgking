import { api, el, clear } from "./app.js";
import { currentAccount, loadSession } from "./login.js";
import { renderAuthor, applyAuthorAppearance } from "./author.js";

const PAGE_SIZE = 20;
const states = { pending: "待审核", approved: "已公开", rejected: "未通过", hidden: "已隐藏", deleted: "已删除" };
const DRAFT_PREFIX = "slgking:comment-draft:";
const drafts = new Map();
const identity = () => currentAccount()?.account_id || "";
const alive = (node, account) => node.isConnected && identity() === account;
const privateNotice = "个人评论保存在云账号，仅本人在网页端可见；与客户端本机记录不自动同步。";

export function clearCommentDrafts(accountId) {
  const prefix = DRAFT_PREFIX + (accountId ? encodeURIComponent(accountId) + ":" : "");
  for (const key of drafts.keys()) if (key.startsWith(prefix)) drafts.delete(key);
  try { for (const key of Object.keys(sessionStorage)) if (key.startsWith(prefix)) sessionStorage.removeItem(key); } catch { /* optional storage */ }
}

function draftKey(account, game, mode) {
  return DRAFT_PREFIX + encodeURIComponent(account) + ":" + encodeURIComponent(game) + ":" + mode;
}
function readDraft(key) {
  try {
    const value = JSON.parse(sessionStorage.getItem(key) || "null");
    if (value && typeof value.content === "string" && typeof value.operation_id === "string") return value;
  } catch { /* fall back to memory */ }
  return drafts.get(key) || { content: "", operation_id: crypto.randomUUID() };
}
function saveDraft(key, value) {
  drafts.set(key, value);
  try { sessionStorage.setItem(key, JSON.stringify(value)); } catch { /* optional storage */ }
}
function removeDraft(key) {
  drafts.delete(key);
  try { sessionStorage.removeItem(key); } catch { /* optional storage */ }
}

function statusBox() {
  return el("div", { class: "status comment-status", role: "status", "aria-live": "polite" });
}

function loginHref() {
  const destination = location.hash;
  return /^#\/(game\/[^?]+|comments\/my)(?:\?.*)?$/.test(destination)
    ? "#/login?return=" + encodeURIComponent(destination) : "#/login";
}

function errorMessage(error) {
  if (error.outcomeUnknown) return "提交结果尚未确认，草稿已保留。请先核对已保存的个人评论或公开投稿，避免重复提交。";
  if (error.status === 401) return "请登录后操作。登录后需要重新提交。";
  if (error.status === 429) return "操作过于频繁，请稍后重试。";
  if (error.status === 0) return "网络不可用，请稍后重试。";
  return "操作暂时未完成，请稍后重试。";
}

function showError(status, error) {
  clear(status);
  status.className = "status comment-status error";
  status.append(errorMessage(error));
  if (error.status === 401) status.append(" ", el("a", { href: loginHref(), text: "前往登录" }));
}

function dateLabel(value) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "" : date.toLocaleString();
}

function commentCard(item, own, refresh, showGame = false, allowInteraction = true) {
  const account = identity();
  const status = statusBox();
  const actions = el("div", { class: "comment-actions" });
  const card = el("article", { class: "comment-item" },
    el("div", { class: "comment-meta" },
      renderAuthor(item),
      el("span", { class: "muted small", text: dateLabel(item.ts) }),
      own ? el("span", { class: "comment-state", text: states[item.status] || "审核中" }) : null),
    showGame ? el("a", { class: "comment-game", href: "#/game/" + encodeURIComponent(item.game), text: item.game_title || item.game }) : null,
    el("p", { class: "comment-body", text: item.content || "" }), actions, status);
  applyAuthorAppearance(card, item);
  const target = "/web/comments/" + encodeURIComponent(item.id);
  function action(text, path, body, method = "POST") {
    const button = el("button", { type: "button", class: "ghost comment-action", text });
    button.addEventListener("click", async () => {
      if (!alive(card, account)) return;
      if (method === "DELETE" && !window.confirm("确定删除这条游戏评论？")) return;
      button.disabled = true;
      try {
        await api(target + path, { method, ...(body ? { body } : {}) });
        if (!alive(card, account)) return;
        if (path === "/report") {
          status.className = "status comment-status good";
          status.textContent = "举报已提交。";
          button.textContent = "已举报";
        } else await refresh();
      } catch (error) {
        if (!alive(card, account)) return;
        showError(status, error);
        button.disabled = false;
      }
    });
    return button;
  }
  if (item.status === "approved") {
    const likes = item.likes ?? item.votes_up ?? 0;
    const dislikes = item.dislikes ?? item.votes_down ?? 0;
    if (own || !currentAccount() || !allowInteraction) {
      actions.append(el("span", { class: "muted small", text: "赞 " + likes + " · 踩 " + dislikes }));
    } else {
      actions.append(action("赞 " + likes, "/vote", { value: 1 }),
        action("踩 " + dislikes, "/vote", { value: -1 }),
        action("撤销投票", "/vote", { value: 0 }), action("举报", "/report", {}));
    }
  }
  if (own && !["deleted", "removed"].includes(item.status)) actions.append(action("删除", "", null, "DELETE"));
  return card;
}

function personalCard(item, refresh, showGame = false) {
  const account = identity();
  const status = statusBox();
  const remove = el("button", { type: "button", class: "ghost comment-action", text: "删除" });
  const card = el("article", { class: "comment-item personal-comment" },
    el("div", { class: "comment-meta" }, el("strong", { text: "仅本人可见" }), el("span", { class: "muted small", text: dateLabel(item.ts) })),
    showGame ? el("a", { class: "comment-game", href: "#/game/" + encodeURIComponent(item.game), text: item.game_title || item.game }) : null,
    el("p", { class: "comment-body", text: item.content }), remove, status);
  remove.addEventListener("click", async () => {
    if (!alive(card, account) || !window.confirm("确定删除这条个人评论？")) return;
    remove.disabled = true;
    try {
      await api("/web/personal-comments/" + encodeURIComponent(item.id), { method: "DELETE" });
      if (alive(card, account)) await refresh();
    } catch (error) { if (alive(card, account)) { showError(status, error); remove.disabled = false; } }
  });
  return card;
}

function pager(host, data, go) {
  const total = Number(data.total_pages) || 0;
  const page = Number(data.page) || 1;
  if (total <= 1) return;
  host.append(el("nav", { class: "comment-pager", "aria-label": "评论分页" },
    el("button", { class: "ghost", type: "button", text: "上一页", disabled: page <= 1, onclick: () => go(page - 1) }),
    el("span", { class: "muted small", text: page + " / " + total }),
    el("button", { class: "ghost", type: "button", text: "下一页", disabled: page >= total, onclick: () => go(page + 1) })));
}

export async function renderGameComments(node, slug) {
  // Direct game links must finish resolving the cookie session before actions render.
  try { await loadSession(); } catch { /* Public reviews remain available. */ }
  if (!node.isConnected) return;
  const account = identity();
  let page = 1;
  let ownPage = 1;
  let sort = "latest";
  let publicRequest = 0;
  const publicStatus = statusBox();
  const list = el("div", { class: "comment-list" });
  const ownList = el("div", { class: "comment-list" });
  const privateStatus = statusBox();
  const privateDetails = el("details", { class: "comment-own" },
    el("summary", { text: "我的公开投稿与审核状态" }), privateStatus, ownList,
    el("a", { href: "#/comments/my?mode=public", text: "查看全部公开投稿" }));
  const personalList = el("div", { class: "comment-list" });
  const personalStatus = statusBox();
  let personalPage = 1;
  let personalRequest = 0;
  let ownRequest = 0;
  const personalDetails = el("details", { class: "comment-own" }, el("summary", { text: "我的个人评论（云端）" }),
    el("p", { class: "muted small", text: privateNotice }), personalStatus, personalList);
  const newest = el("button", { type: "button", class: "ghost", text: "最新", "aria-pressed": "true" });
  const popular = el("button", { type: "button", class: "ghost", text: "热门", "aria-pressed": "false" });
  for (const [button, mode] of [[newest, "latest"], [popular, "popular"]]) {
    button.addEventListener("click", () => {
      sort = mode;
      page = 1;
      newest.setAttribute("aria-pressed", String(mode === "latest"));
      popular.setAttribute("aria-pressed", String(mode === "popular"));
      void loadPublic();
    });
  }
  const section = el("section", { class: "game-comments" },
    el("div", { class: "comment-heading" }, el("h3", { text: "游戏评论" }),
      el("div", { class: "comment-sort", "aria-label": "评论排序" }, newest, popular)), publicStatus, list);
  node.append(section);
  if (currentAccount()) {
    section.append(personalDetails, privateDetails);
    let mode = "personal";
    let draft = readDraft(draftKey(account, slug, mode));
    const input = el("textarea", { required: true, minlength: "1", maxlength: "500", rows: "4", "aria-label": "游戏评论", placeholder: "写下仅自己可见的游戏记录（1–500 字）" });
    const count = el("span", { class: "muted small", text: "0 / 500" });
    const status = statusBox();
    const submit = el("button", { type: "submit", text: "保存个人评论" });
    const notice = el("p", { class: "muted small comment-privacy", text: privateNotice });
    const personal = el("button", { type: "button", class: "ghost", text: "个人（仅本人）", "aria-pressed": "true" });
    const published = el("button", { type: "button", class: "ghost", text: "公开", "aria-pressed": "false" });
    const segments = el("div", { class: "comment-mode", role: "group", "aria-label": "评论可见范围" }, personal, published);
    const restore = () => { input.value = draft.content; count.textContent = input.value.trim().length + " / 500"; };
    restore();
    input.addEventListener("input", () => {
      if (!alive(section, account)) return;
      draft = { content: input.value, operation_id: crypto.randomUUID() };
      saveDraft(draftKey(account, slug, mode), draft);
      count.textContent = input.value.trim().length + " / 500";
    });
    for (const [button, next] of [[personal, "personal"], [published, "public"]]) button.addEventListener("click", () => {
      if (submit.disabled || !alive(section, account) || mode === next) return;
      saveDraft(draftKey(account, slug, mode), draft);
      mode = next;
      draft = readDraft(draftKey(account, slug, mode));
      restore();
      personal.setAttribute("aria-pressed", String(mode === "personal"));
      published.setAttribute("aria-pressed", String(mode === "public"));
      input.minLength = mode === "personal" ? 1 : 20;
      input.placeholder = mode === "personal" ? "写下仅自己可见的游戏记录（1–500 字）" : "写下公开的游戏评价（20–500 字）";
      notice.textContent = mode === "personal" ? privateNotice : "公开评论经审核后展示给所有人；请勿包含私人信息。";
      submit.textContent = mode === "personal" ? "保存个人评论" : "发表公开评论";
      status.textContent = "";
    });
    const form = el("form", { class: "comment-form" }, segments, notice, input,
      el("div", { class: "comment-form-footer" }, count, submit), status);
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (submit.disabled || !alive(section, account)) return;
      const content = input.value.trim();
      const minimum = mode === "personal" ? 1 : 20;
      if (content.length < minimum || content.length > 500) {
        status.textContent = "评论需要 " + minimum + "–500 字。";
        input.focus();
        return;
      }
      const submittedMode = mode;
      const key = draftKey(account, slug, mode);
      saveDraft(key, draft);
      submit.disabled = true;
      personal.disabled = published.disabled = input.disabled = true;
      status.textContent = "正在提交…";
      try {
        const session = await loadSession();
        if (session?.account_id !== account) {
          if (session?.account_id && section.isConnected) {
            personalDetails.remove();
            privateDetails.remove();
            clear(form);
            form.append(el("p", { class: "status comment-status error", role: "status", text: "账号已切换，原账号草稿已保留。请重新读取当前账号的评论。" }),
              el("button", { type: "button", class: "ghost", text: "重新读取", onclick: () => { clear(node); void renderGameComments(node, slug); } }));
          }
          const error = new Error("账号已切换");
          error.status = session?.account_id ? 409 : 401;
          throw error;
        }
        if (!alive(section, account)) return;
        const result = await api(submittedMode === "personal" ? "/web/personal-comments" : "/web/comments", {
          method: "POST", body: { game: slug, content, operation_id: draft.operation_id },
        });
        if (!alive(section, account)) return;
        const confirmed = submittedMode === "personal" ? !!result.id : ["approved", "pending"].includes(result.status);
        if (confirmed) {
          removeDraft(key);
          draft = { content: "", operation_id: crypto.randomUUID() };
          restore();
        }
        status.className = "status comment-status good";
        status.textContent = submittedMode === "personal" && confirmed ? "个人评论已保存，仅本人可见。"
          : result.status === "approved" ? "评论已公开。"
            : result.status === "pending" ? "评论已提交审核，可在公开投稿中查看。"
              : result.status === "rejected" ? "评论未通过审核，草稿已保留，请修改后再提交。"
                : "提交结果尚未确认，草稿已保留。请先核对已保存的个人评论或公开投稿。";
        if (!confirmed) status.className = "status comment-status error";
        if (submittedMode === "personal") personalDetails.open = true;
        else privateDetails.open = true;
        ownPage = 1;
        personalPage = 1;
        await refresh();
      } catch (error) {
        if (section.isConnected && (!identity() || identity() === account)) showError(status, error);
      }
      finally { if (alive(section, account)) { submit.disabled = personal.disabled = published.disabled = input.disabled = false; } }
    });
    section.append(form);
    await Promise.all([loadOwn(), loadPersonal()]);
  } else section.append(el("p", { class: "muted small" }, el("a", { href: loginHref(), text: "登录" }), "后发表游戏评论。"));
  await loadPublic();

  async function loadOwn() {
    const request = ++ownRequest;
    try {
      const data = await api("/web/account/comments?" + new URLSearchParams({ game: slug, page: String(ownPage), limit: String(PAGE_SIZE) }));
      const items = data.comments || [];
      if (request !== ownRequest || !alive(section, account)) return;
      clear(ownList);
      privateStatus.textContent = "";
      ownList.append(...items.map((item) => commentCard(item, true, refresh)));
      if (!items.length) ownList.append(el("p", { class: "muted small", text: "还没有评论这款游戏。" }));
      pager(ownList, data, (next) => { ownPage = next; void refresh(); });
    } catch (error) { if (request === ownRequest && alive(section, account)) showError(privateStatus, error); }
  }
  async function loadPersonal() {
    const request = ++personalRequest;
    try {
      const data = await api("/web/account/personal-comments?" + new URLSearchParams({ game: slug, page: String(personalPage), limit: String(PAGE_SIZE) }));
      if (request !== personalRequest || !alive(section, account)) return;
      clear(personalList);
      personalStatus.textContent = "";
      personalList.append(...(data.items || []).map(item => personalCard(item, loadPersonal)));
      if (!data.items?.length) personalList.append(el("p", { class: "muted small", text: "还没有个人评论。" }));
      pager(personalList, data, next => { personalPage = next; void loadPersonal(); });
    } catch (error) { if (request === personalRequest && alive(section, account)) showError(personalStatus, error); }
  }
  async function refresh() {
    if (!alive(section, account)) return;
    if (account) await Promise.all([loadOwn(), loadPersonal()]);
    await loadPublic();
  }
  async function loadPublic() {
    const request = ++publicRequest;
    publicStatus.textContent = "正在读取评论…";
    try {
      const data = await api("/comments?" + new URLSearchParams({ game: slug, page: String(page), limit: String(PAGE_SIZE), sort }));
      if (request !== publicRequest || !alive(section, account)) return;
      const items = data.comments || [];
      let ownIds = new Set();
      let allowInteraction = true;
      if (currentAccount() && items.length) {
        try {
          const own = await api("/web/account/comments?" + new URLSearchParams({
            game: slug, ids: items.map((item) => item.id).join(","), limit: "50",
          }));
          ownIds = new Set((own.comments || []).map((item) => item.id));
        } catch {
          // Keep public reading available when private ownership cannot be resolved.
          allowInteraction = false;
        }
      }
      if (request !== publicRequest || !alive(section, account)) return;
      clear(list);
      publicStatus.textContent = "共 " + (data.total_count || 0) + " 条游戏评论";
      list.append(...items.map((item) => commentCard(item, ownIds.has(item.id), refresh, false, allowInteraction)));
      if (!items.length) list.append(el("p", { class: "muted small", text: "暂无公开评论。" }));
      pager(list, data, (next) => { page = next; void loadPublic(); });
    } catch (error) {
      if (request !== publicRequest || !alive(section, account)) return;
      showError(publicStatus, error);
      publicStatus.append(" ", el("button", { class: "ghost", type: "button", text: "重试", onclick: loadPublic }));
    }
  }
}

export async function renderMyComments(node, params = new URLSearchParams()) {
  let page = Math.max(1, Number.parseInt(params.get("page") || "1", 10) || 1);
  const game = params.get("game") || "";
  let mode = params.get("mode") === "public" ? "public" : "personal";
  let requestId = 0;
  const status = statusBox();
  const list = el("div", { class: "comment-list" });
  node.append(el("a", { href: "#/login", text: "返回个人中心" }), el("h2", { text: "我的游戏评论" }), status);
  try { await loadSession(); } catch (error) { showError(status, error); return; }
  if (!node.isConnected) return;
  const account = identity();
  const personal = el("button", { type: "button", class: "ghost", text: "个人（仅本人）" });
  const published = el("button", { type: "button", class: "ghost", text: "公开投稿" });
  const notice = el("p", { class: "muted small comment-privacy" });
  for (const [button, next] of [[personal, "personal"], [published, "public"]]) button.addEventListener("click", () => {
    mode = next;
    page = 1;
    void load();
  });
  node.append(el("div", { class: "comment-mode", role: "group", "aria-label": "评论分类" }, personal, published), notice, list);
  if (!account) { showError(status, { status: 401 }); return; }
  await load();
  async function load() {
    const request = ++requestId;
    const loadingMode = mode;
    personal.setAttribute("aria-pressed", String(mode === "personal"));
    published.setAttribute("aria-pressed", String(mode === "public"));
    notice.textContent = mode === "personal" ? privateNotice : "这里包含你的公开评论及待审核、未通过的投稿。";
    clear(list);
    status.textContent = "正在读取…";
    try {
      const query = new URLSearchParams({ page: String(page), limit: String(PAGE_SIZE) });
      if (game) query.set("game", game);
      const data = await api((mode === "personal" ? "/web/account/personal-comments?" : "/web/account/comments?") + query);
      if (request !== requestId || !alive(node, account)) return;
      clear(list);
      status.textContent = "共 " + (data.total || 0) + " 条评论";
      const items = loadingMode === "personal" ? data.items || [] : data.comments || [];
      list.append(...items.map((item) => loadingMode === "personal" ? personalCard(item, load, true) : commentCard(item, true, load, true)));
      if (!items.length) list.append(el("p", { class: "muted small", text: loadingMode === "personal" ? "还没有个人评论。" : "还没有公开投稿。" }));
      pager(list, data, (next) => {
        query.set("page", String(next));
        query.set("mode", mode);
        location.hash = "#/comments/my?" + query;
      });
    } catch (error) { if (request === requestId && alive(node, account)) showError(status, error); }
  }
}
