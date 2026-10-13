// Web-only forum UI. User supplied text is inserted with textContent through
// app.js's el() helper and is never parsed as HTML.
import { api, el, clear } from "./app.js";
import { renderAuthor, applyAuthorAppearance } from "./author.js";

const PAGE_SIZE = 20;
const REPLY_SIZE = 30;
const DRAFT_PREFIX = "slgking:forum-draft:";
const memoryDrafts = new Map();

export function clearForumDrafts(accountId) {
  const prefix = DRAFT_PREFIX + encodeURIComponent(accountId || "") + ":";
  for (const key of memoryDrafts.keys()) if (key.startsWith(prefix)) memoryDrafts.delete(key);
  try {
    for (const key of Object.keys(sessionStorage)) {
      if (key.startsWith(prefix)) sessionStorage.removeItem(key);
    }
  } catch { /* Private browsing may disable storage. */ }
}

function draftKey(accountId, kind, topic) {
  return DRAFT_PREFIX + encodeURIComponent(accountId) + ":" + kind + ":" + encodeURIComponent(topic || "general");
}

function readDraft(key) {
  try {
    const value = JSON.parse(sessionStorage.getItem(key) || "null");
    if (value && typeof value === "object" && !Array.isArray(value)) return value;
  } catch { /* The in-memory draft still works in this page. */ }
  return memoryDrafts.get(key) || {};
}

function saveDraft(key, value) {
  memoryDrafts.set(key, value);
  try { sessionStorage.setItem(key, JSON.stringify(value)); } catch { /* optional */ }
}

function removeDraft(key) {
  memoryDrafts.delete(key);
  try { sessionStorage.removeItem(key); } catch { /* optional */ }
}

async function writerAccount(host) {
  const status = statusBox();
  status.textContent = "正在确认登录状态…";
  host.append(status);
  try {
    const account = await api("/account/session");
    if (!host.isConnected) return null;
    if (!account.account_id) throw new Error("登录状态暂时无法确认。");
    status.remove();
    return account.account_id;
  } catch (error) {
    if (!host.isConnected) return null;
    status.className = "status forum-status error";
    status.textContent = error.status === 401 ? "登录后即可参与讨论。" : friendlyError(error, "登录状态暂时无法确认，请稍后重试。");
    if (error.status === 401) status.append(" ", el("a", { href: "#/login", text: "前往登录" }));
    else status.append(" ", el("button", { class: "ghost forum-button", type: "button", text: "重试", onclick: () => {
      clear(host);
      host._retryWriter?.();
    } }));
    return null;
  }
}

async function verifyWriter(accountId) {
  const account = await api("/account/session");
  if (account.account_id !== accountId) throw new Error("账号已切换，请重新打开写作表单。原账号草稿已保留。");
}

function submissionMessage(record, kind) {
  if (record?.status === "approved") return kind + "已公开。";
  if (record?.status === "pending") return kind + "已提交审核，可在“我的内容”中查看。";
  if (record?.status === "rejected") return "内容未通过审核，请修改后再提交。草稿已保留。";
  return "提交结果暂时无法确认，草稿已保留。请先查看“我的内容”，避免重复提交。";
}

function writeError(status, error, fallback) {
  status.className = "status forum-status error";
  status.textContent = error.outcomeUnknown
    ? "提交结果暂时无法确认，草稿已保留。请先查看“我的内容”，避免重复提交。"
    : friendlyError(error, fallback);
  if (error.status === 401) status.append(" ", el("a", { href: "#/login", text: "前往登录" }));
  if (error.outcomeUnknown) status.append(" ", el("a", { href: "#/forum/my", text: "查看我的内容" }));
}

export async function renderForum(node, params = new URLSearchParams()) {
  const page = pageNumber(params.get("page"));
  const game = params.get("game") || "";
  const status = statusBox();
  const list = el("div", { class: "forum-list" });
  const composeSlot = el("div", { class: "forum-composer-slot" });
  const heading = el("div", { class: "forum-heading" },
    el("div", {},
      el("p", { class: "forum-kicker", text: "网页专属讨论区" }),
      el("h2", { text: game ? "论坛 · 关联游戏话题" : "论坛" }),
      el("p", { class: "muted small", text: "自由讨论、交流想法与游玩心得。普通内容可直接公开，风险内容需人工审核。" }),
      game ? el("a", { href: "#/forum", text: "查看全部论坛话题" }) : null,
      game ? el("a", { href: "#/game/" + encodeURIComponent(game), text: "查看这款游戏的评论" }) : null,
    ),
    el("div", { class: "forum-actions" },
      el("a", { class: "ghost forum-button", href: "#/forum/my", text: "我的内容" }),
      el("button", { class: "forum-button", type: "button", text: "发起讨论" }),
    ),
  );
  const newButton = heading.querySelector("button");
  newButton.addEventListener("click", () => {
    if (composeSlot.childElementCount) {
      clear(composeSlot);
      newButton.textContent = "发起讨论";
    } else {
      void renderThreadComposer(composeSlot, game, async (record) => {
        clear(composeSlot);
        newButton.textContent = "发起讨论";
        status.replaceChildren(el("span", { text: submissionMessage(record, "帖子") }));
        status.className = "status forum-status good";
        if (record.status === "approved" && record.id) {
          const exists = [...list.querySelectorAll("[data-forum-thread-id]")]
            .some((card) => card.dataset.forumThreadId === record.id);
          if (page === 1 && (!game || record.game_slug === game) && !exists) {
            list.querySelector(".forum-empty")?.remove();
            list.prepend(threadCard(record, false));
            const cards = list.querySelectorAll("[data-forum-thread-id]");
            if (cards.length > PAGE_SIZE) cards[cards.length - 1].remove();
          }
          status.append(" ", el("a", { href: "#/forum/" + encodeURIComponent(record.id), text: "查看新讨论" }));
        }
      });
      newButton.textContent = "收起表单";
      composeSlot.scrollIntoView({ behavior: "smooth", block: "start" });
    }
  });
  node.append(heading, composeSlot, status, list);
  await load();

  async function load() {
    status.textContent = "正在载入讨论…";
    status.className = "status forum-status";
    clear(list);
    try {
      const query = new URLSearchParams({ page: String(page), limit: String(PAGE_SIZE) });
      if (game) query.set("game", game);
      const data = await api("/web/forum/threads?" + query.toString());
      const items = Array.isArray(data.items) ? data.items : [];
      status.textContent = data.total === undefined ? "" : "共 " + data.total + " 个讨论 · 第 " + (data.page || page) + " 页";
      if (!items.length) list.append(el("div", { class: "forum-empty", text: "这里还没有公开讨论，来发起第一个话题吧。" }));
      else list.append(...items.map((item) => threadCard(item, false)));
      appendPager(list, data, page, PAGE_SIZE, (next) => changeHashPage("/forum", next, game));
    } catch (error) {
      status.textContent = friendlyError(error, "讨论暂时无法载入，请稍后重试。");
      status.className = "status forum-status error";
      list.append(el("button", { class: "ghost forum-button", type: "button", text: "重试", onclick: load }));
    }
  }
}

export async function renderForumThread(node, threadId, params = new URLSearchParams()) {
  const status = statusBox();
  const content = el("div", { class: "forum-detail-slot" });
  node.append(el("a", { class: "forum-back", href: "#/forum", text: "← 返回讨论列表" }), status, content);
  await load();

  async function load() {
    status.textContent = "正在载入讨论…";
    status.className = "status forum-status";
    clear(content);
    try {
      const replyPage = pageNumber(params.get("page"));
      const query = new URLSearchParams({ page: String(replyPage), limit: String(REPLY_SIZE) });
      const data = await api("/web/forum/threads/" + encodeURIComponent(threadId) + "?" + query.toString());
      status.textContent = "";
      const thread = data.thread || {};
      const own = thread.is_mine === true || thread.can_delete === true;
      const article = el("article", { class: "forum-card forum-thread" },
        el("h2", { class: "forum-title", text: textValue(thread.title, "无标题讨论") }),
        renderAuthor(thread),
        el("div", { class: "forum-meta" },
          el("span", { text: formatDate(thread.created) }),
          thread.game_slug ? el("a", { href: "#/game/" + encodeURIComponent(thread.game_slug), text: "相关游戏：" + thread.game_slug }) : null,
          statusBadge(thread.status),
        ),
        el("p", { class: "forum-body", text: textValue(thread.content) }),
        thread.status && thread.status !== "approved" ? el("p", { class: "forum-review-note", text: reviewMessage(thread.status) }) : null,
      );
      applyAuthorAppearance(article, thread);
      const threadActions = el("div", { class: "forum-item-actions" });
      if (!own && thread.status === "approved") threadActions.append(reportControl("thread", thread.id || threadId));
      if (own && !isDeleted(thread.status)) {
        threadActions.append(deleteButton("thread", thread.id || threadId, () => { location.hash = "#/forum"; }));
      }
      article.append(threadActions);

      const replyData = data.replies || {};
      const replies = Array.isArray(replyData.items) ? replyData.items : [];
      let replyTotal = Number(replyData.total) || replies.length;
      const replySection = el("section", { class: "forum-replies" },
        el("div", { class: "forum-section-heading" },
          el("h3", { text: "回复" + (replyData.total === undefined ? "" : " · " + replyData.total) }),
          el("span", { class: "muted small", text: "风险内容需人工审核" }),
        ),
      );
      if (!replies.length) replySection.append(el("div", { class: "forum-empty", text: "还没有公开回复。" }));
      else replySection.append(...replies.map((reply) => replyCard(reply)));
      appendPager(replySection, replyData, pageNumber(replyData.page), REPLY_SIZE, (next) => {
        params.set("page", String(next));
        location.hash = "#/forum/" + encodeURIComponent(threadId) + "?" + params.toString();
      });

      const composer = el("div", { class: "forum-card forum-reply-composer" });
      if (thread.status === "approved") {
        void renderReplyComposer(composer, threadId, (record, submissionStatus) => {
          const exists = [...replySection.querySelectorAll("[data-forum-reply-id]")]
            .some((card) => card.dataset.forumReplyId === record.id);
          if (!exists) replyTotal += 1;
          const latestPage = Math.max(1, Math.ceil(replyTotal / REPLY_SIZE));
          if (!exists && replyPage === latestPage && replies.length < REPLY_SIZE) {
            replySection.querySelector(".forum-empty")?.remove();
            const card = replyCard({ ...record, is_mine: true, can_delete: true });
            const pager = replySection.querySelector(".forum-pager");
            if (pager) pager.before(card);
            else replySection.append(card);
            replies.push(record);
          }
          replySection.querySelector("h3").textContent = "回复 · " + replyTotal;
          const query = new URLSearchParams(params);
          if (latestPage > 1) query.set("page", String(latestPage));
          else query.delete("page");
          const tail = query.toString();
          submissionStatus.append(" ", el("a", {
            href: "#/forum/" + encodeURIComponent(threadId) + (tail ? "?" + tail : ""),
            text: "查看新回复",
          }));
        });
      } else {
        composer.append(el("p", {
          class: "forum-review-note",
          text: own ? "主题通过审核后即可回复。" : "主题审核通过后才会开放回复。",
        }));
      }
      content.append(article, replySection, composer);
    } catch (error) {
      status.textContent = friendlyError(error, "讨论暂时无法载入，请稍后重试。");
      status.className = "status forum-status error";
      content.append(el("button", { class: "ghost forum-button", type: "button", text: "重试", onclick: load }));
    }
  }
}

export async function renderMyForum(node, params = new URLSearchParams()) {
  const kind = params.get("kind") === "replies" ? "replies" : "threads";
  const page = pageNumber(params.get("page"));
  const status = statusBox();
  const list = el("div", { class: "forum-list" });
  node.append(
    el("a", { class: "forum-back", href: "#/forum", text: "← 返回讨论区" }),
    el("div", { class: "forum-heading forum-my-heading" },
      el("div", {}, el("p", { class: "forum-kicker", text: "个人主页" }), el("h2", { text: "我的论坛内容" })),
      el("div", { class: "forum-switch" },
        el("a", { class: kind === "threads" ? "forum-button" : "ghost forum-button", href: "#/forum/my?kind=threads", text: "我的帖子" }),
        el("a", { class: kind === "replies" ? "forum-button" : "ghost forum-button", href: "#/forum/my?kind=replies", text: "我的回复" }),
      ),
    ),
    status, list,
  );
  status.textContent = "正在载入你的内容…";
  try {
    const data = await api("/web/forum/my?kind=" + kind + "&page=" + page + "&limit=" + PAGE_SIZE);
    const items = Array.isArray(data.items) ? data.items : [];
    status.textContent = data.total === undefined ? "" : "共 " + data.total + " 条 · 第 " + (data.page || page) + " 页";
    if (!items.length) list.append(el("div", { class: "forum-empty", text: kind === "threads" ? "你还没有发起讨论。" : "你还没有发表回复。" }));
    else if (kind === "threads") list.append(...items.map((item) => threadCard(item, true)));
    else list.append(...items.map(replyCardForMy));
    appendPager(list, data, page, PAGE_SIZE, (next) => {
      location.hash = "#/forum/my?kind=" + kind + "&page=" + next;
    });
  } catch (error) {
    status.textContent = friendlyError(error, "你的论坛内容暂时无法载入。");
    status.className = "status forum-status error";
    list.append(el("a", { class: "ghost forum-button", href: "#/login", text: "前往登录" }));
  }
}

async function renderThreadComposer(host, gameSlug, onSuccess) {
  host._retryWriter = () => void renderThreadComposer(host, gameSlug, onSuccess);
  const accountId = await writerAccount(host);
  if (!accountId) return;
  const key = draftKey(accountId, "thread", gameSlug);
  const draft = readDraft(key);
  let operationId = typeof draft.operation_id === "string" ? draft.operation_id : "";
  const title = el("input", { name: "title", type: "text", required: true, maxlength: "120", placeholder: "写一个清楚的讨论标题" });
  const body = el("textarea", { name: "content", required: true, minlength: "20", maxlength: "10000", rows: "7", placeholder: "分享你的想法（至少 20 个字）" });
  const gameSearch = el("input", { type: "search", maxlength: "100", placeholder: "搜索要关联的游戏" });
  const gameSelect = el("select", {}, el("option", { value: "", text: "不关联游戏" }),
    gameSlug ? el("option", { value: gameSlug, text: "当前关联游戏", selected: true }) : null);
  const gameStatus = statusBox();
  title.value = typeof draft.title === "string" ? draft.title : "";
  body.value = typeof draft.content === "string" ? draft.content : "";
  if (typeof draft.game_slug === "string" && draft.game_slug && draft.game_slug !== gameSlug) {
    gameSelect.append(el("option", { value: draft.game_slug, text: "草稿关联游戏", selected: true }));
  }
  if (draft.game_slug === "") gameSelect.value = "";
  let searchTimer;
  let searchRequest = 0;
  gameSearch.addEventListener("input", () => {
    clearTimeout(searchTimer);
    const request = ++searchRequest;
    const query = gameSearch.value.trim();
    if (!query) { gameStatus.textContent = ""; return; }
    searchTimer = setTimeout(async () => {
      try {
        const data = await api("/catalogue/search?" + new URLSearchParams({ q: query, limit: "10" }));
        if (request !== searchRequest || !gameSearch.isConnected) return;
        const selected = gameSelect.value;
        const selectedText = gameSelect.selectedOptions[0]?.textContent;
        clear(gameSelect);
        gameSelect.append(el("option", { value: "", text: "不关联游戏" }));
        if (selected && !data.items.some((game) => game.slug === selected)) {
          gameSelect.append(el("option", { value: selected, text: selectedText, selected: true }));
        }
        gameSelect.append(...data.items.map((game) => el("option", {
          value: game.slug, text: game.title || game.slug, selected: game.slug === selected,
        })));
        gameStatus.textContent = data.items.length ? "" : "没有找到匹配游戏。";
      } catch {
        if (request === searchRequest) gameStatus.textContent = "游戏搜索暂时不可用，可直接发布无关联话题。";
      }
    }, 250);
  });
  const status = statusBox();
  const submit = el("button", { class: "forum-button", type: "submit", text: "发表讨论" });
  const form = el("form", { class: "forum-card forum-form" },
    el("h3", { text: "发起讨论" }),
    labelled("讨论标题", title), labelled("正文", body),
    el("details", { class: "forum-game-picker", open: Boolean(gameSlug) },
      el("summary", { text: "关联游戏（可选）" }),
      labelled("搜索游戏", gameSearch), labelled("关联游戏", gameSelect), gameStatus),
    el("p", { class: "forum-review-note", text: "普通内容直接公开，风险内容待人工审核；违规内容不会公开。" }),
    status, submit,
  );
  const persist = () => {
    const previous = readDraft(key);
    if (previous.title !== title.value || previous.content !== body.value || previous.game_slug !== gameSelect.value) operationId = "";
    saveDraft(key, { title: title.value, content: body.value, game_slug: gameSelect.value, operation_id: operationId });
  };
  form.addEventListener("input", persist);
  form.addEventListener("change", persist);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!title.value.trim() || !body.value.trim()) return;
    if (body.value.trim().length < 20) {
      status.textContent = "正文至少需要 20 个字。";
      status.className = "status forum-status error";
      body.focus();
      return;
    }
    submit.disabled = true;
    persist();
    status.textContent = "正在提交…";
    try {
      await verifyWriter(accountId);
      operationId ||= crypto.randomUUID();
      persist();
      const result = await api("/web/forum/threads", {
        method: "POST",
        body: { title: title.value.trim(), content: body.value.trim(), operation_id: operationId,
          ...(gameSelect.value ? { game_slug: gameSelect.value } : {}) },
      });
      const record = result.thread;
      status.textContent = submissionMessage(record, "帖子");
      const accepted = ["approved", "pending"].includes(record?.status);
      status.className = "status forum-status " + (accepted ? "good" : "error");
      if (accepted) {
        removeDraft(key);
        form.reset();
        await onSuccess(record);
      }
    } catch (error) {
      writeError(status, error, "提交失败，草稿已保留，请稍后重试。");
    } finally {
      submit.disabled = false;
    }
  });
  host.append(form);
}

async function renderReplyComposer(host, threadId, onPublished) {
  host._retryWriter = () => void renderReplyComposer(host, threadId, onPublished);
  const accountId = await writerAccount(host);
  if (!accountId) return;
  const key = draftKey(accountId, "reply", threadId);
  const draft = readDraft(key);
  let operationId = typeof draft.operation_id === "string" ? draft.operation_id : "";
  const body = el("textarea", { name: "content", required: true, minlength: "20", maxlength: "5000", rows: "4", placeholder: "写下你的回复（至少 20 个字）" });
  body.value = typeof draft.content === "string" ? draft.content : "";
  const status = statusBox();
  const submit = el("button", { class: "forum-button", type: "submit", text: "提交回复" });
  const form = el("form", { class: "forum-form" },
    el("h3", { text: "参与讨论" }),
    labelled("回复内容", body),
    el("p", { class: "forum-review-note", text: "普通回复直接公开，风险内容待人工审核。" }),
    status, submit,
  );
  const persist = () => {
    if (readDraft(key).content !== body.value) operationId = "";
    saveDraft(key, { content: body.value, operation_id: operationId });
  };
  form.addEventListener("input", persist);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!body.value.trim()) return;
    if (body.value.trim().length < 20) {
      status.textContent = "回复至少需要 20 个字。";
      status.className = "status forum-status error";
      body.focus();
      return;
    }
    submit.disabled = true;
    persist();
    status.textContent = "正在提交…";
    try {
      await verifyWriter(accountId);
      operationId ||= crypto.randomUUID();
      persist();
      const result = await api("/web/forum/threads/" + encodeURIComponent(threadId) + "/replies", {
        method: "POST", body: { content: body.value.trim(), operation_id: operationId },
      });
      const accepted = ["approved", "pending"].includes(result.reply?.status);
      status.replaceChildren(el("span", { text: submissionMessage(result.reply, "回复") }));
      status.className = "status forum-status " + (accepted ? "good" : "error");
      if (accepted) {
        removeDraft(key);
        body.value = "";
        if (result.reply.status === "approved" && result.reply.id) onPublished?.(result.reply, status);
      }
    } catch (error) {
      writeError(status, error, "提交回复失败，草稿已保留，请稍后重试。");
    } finally {
      submit.disabled = false;
    }
  });
  host.append(form);
}

function threadCard(thread, mine) {
  const link = "#/forum/" + encodeURIComponent(thread.id);
  const side = el("div", { class: "forum-list-side" });
  if (mine) {
    const badge = statusBadge(thread.status);
    if (badge) side.append(badge);
    if (thread.status !== "approved") side.append(el("p", { class: "forum-review-note small", text: reviewMessage(thread.status) }));
    if (!isDeleted(thread.status)) {
      side.append(deleteButton("thread", thread.id, (button) => { button.closest(".forum-card")?.remove(); }));
    }
  }
  side.append(el("a", { class: "ghost forum-button", href: link, text: "查看讨论" }));
  return applyAuthorAppearance(el("article", { class: "forum-card forum-list-card", "data-forum-thread-id": thread.id },
    el("div", { class: "forum-list-main" },
      renderAuthor(thread),
      el("a", { class: "forum-list-title", href: link, text: textValue(thread.title, "无标题讨论") }),
      el("p", { class: "forum-excerpt", text: excerpt(thread.excerpt ?? thread.content) }),
      el("div", { class: "forum-meta" },
        el("span", { text: formatDate(thread.created) }),
        thread.game_slug ? el("a", { href: "#/game/" + encodeURIComponent(thread.game_slug), text: "游戏：" + thread.game_slug }) : null,
      ),
    ), side,
  ), thread);
}

function replyCard(reply) {
  const own = reply.is_mine === true || reply.can_delete === true;
  const card = el("article", { class: "forum-card forum-reply-card", "data-forum-reply-id": reply.id },
    renderAuthor(reply),
    el("div", { class: "forum-meta" },
      el("span", { text: formatDate(reply.created) }),
      statusBadge(reply.status),
    ),
    el("p", { class: "forum-body", text: textValue(reply.content) }),
  );
  const actions = el("div", { class: "forum-item-actions" });
  if (!own && reply.status === "approved") actions.append(reportControl("reply", reply.id));
  if (own && !isDeleted(reply.status)) actions.append(deleteButton("reply", reply.id, (button) => { button.closest(".forum-card")?.remove(); }));
  card.append(actions);
  return applyAuthorAppearance(card, reply);
}

function replyCardForMy(reply) {
  const target = reply.thread_id || reply.threadId;
  return applyAuthorAppearance(el("article", { class: "forum-card forum-reply-card" },
    renderAuthor(reply),
    el("div", { class: "forum-meta" },
      el("span", { text: "回复了：" + textValue(reply.thread_title, "讨论") }),
      el("span", { text: formatDate(reply.created) }),
      statusBadge(reply.status),
    ),
    el("p", { class: "forum-body", text: textValue(reply.content) }),
    reply.status !== "approved" ? el("p", { class: "forum-review-note", text: reviewMessage(reply.status) }) : null,
    target ? el("a", { class: "ghost forum-button", href: "#/forum/" + encodeURIComponent(target), text: "打开原讨论" }) : null,
    reply.id && !isDeleted(reply.status)
      ? deleteButton("reply", reply.id, (button) => { button.closest(".forum-card")?.remove(); }) : null,
  ), reply);
}

function reportControl(targetType, targetId) {
  const details = el("details", { class: "forum-report" });
  details.append(el("summary", { text: "举报" }));
  const reason = el("select", {},
    el("option", { value: "spam", text: "垃圾内容" }),
    el("option", { value: "abuse", text: "骚扰或辱骂" }),
    el("option", { value: "illegal", text: "违法或不适内容" }),
    el("option", { value: "other", text: "其他" }),
  );
  const extra = el("textarea", { rows: "2", maxlength: "1000", placeholder: "补充说明（选填）" });
  const status = statusBox();
  const submit = el("button", { class: "ghost forum-button forum-small-button", type: "button", text: "提交举报" });
  submit.addEventListener("click", async () => {
    submit.disabled = true;
    status.textContent = "正在提交…";
    try {
      await api("/web/forum/reports", {
        method: "POST",
        body: { target_type: targetType, target_id: targetId, reason: reason.value, ...(extra.value.trim() ? { details: extra.value.trim() } : {}) },
      });
      status.textContent = "举报已提交，感谢反馈。";
      status.className = "status forum-status good";
      reason.disabled = true;
      extra.disabled = true;
      submit.textContent = "已提交";
    } catch (error) {
      status.textContent = friendlyError(error, "举报提交失败，请稍后重试。");
      status.className = "status forum-status error";
      submit.disabled = false;
    }
  });
  details.append(el("div", { class: "forum-report-fields" }, reason, extra, status, submit));
  return details;
}

function deleteButton(type, id, onSuccess) {
  const button = el("button", { class: "ghost forum-button forum-small-button forum-danger", type: "button", text: "删除" });
  button.addEventListener("click", async () => {
    if (!window.confirm("确定删除这条内容？删除后将不再显示。")) return;
    button.disabled = true;
    try {
      await api("/web/forum/" + (type === "thread" ? "threads" : "replies") + "/" + encodeURIComponent(id), { method: "DELETE" });
      button.textContent = "已删除";
      await onSuccess(button);
    } catch (error) {
      button.disabled = false;
      window.alert(friendlyError(error, "删除失败，请稍后重试。"));
    }
  });
  return button;
}

function appendPager(host, data, page, pageSize, go) {
  const total = Number(data.total);
  if (!Number.isFinite(total) || total <= pageSize) return;
  const current = Number(data.page) || page;
  const size = Number(data.page_size || data.limit) || pageSize;
  const last = Math.max(1, Math.ceil(total / size));
  host.append(el("nav", { class: "forum-pager", "aria-label": "讨论分页" },
    el("button", { class: "ghost forum-button", type: "button", text: "上一页", disabled: current <= 1, onclick: () => go(current - 1) }),
    el("span", { class: "muted small", text: current + " / " + last }),
    el("button", { class: "ghost forum-button", type: "button", text: "下一页", disabled: current >= last, onclick: () => go(current + 1) }),
  ));
}

function labelled(label, control) {
  const id = "forum-field-" + Math.random().toString(36).slice(2);
  control.id = id;
  return el("label", { class: "forum-field", for: id }, el("span", { text: label }), control);
}

function statusBox() {
  return el("div", { class: "status forum-status", role: "status", "aria-live": "polite" });
}

function statusBadge(status) {
  if (!status) return null;
  const names = { pending: "待审核", approved: "已公开", rejected: "未通过", hidden: "已隐藏", deleted: "已删除" };
  return el("span", { class: "forum-badge forum-badge-" + String(status).replace(/[^a-z0-9_-]/gi, ""), text: names[status] || String(status) });
}

function reviewMessage(status) {
  if (status === "pending") return "待审核：目前只有你能看到这条内容。";
  if (status === "rejected") return "审核未通过：这条内容不会公开显示。";
  if (status === "hidden") return "这条内容已被隐藏。";
  if (status === "deleted") return "这条内容已删除。";
  return "审核通过后会在讨论区公开显示。";
}

function textValue(value, fallback = "") {
  return value === null || value === undefined || value === "" ? fallback : String(value);
}

function excerpt(value) {
  const text = textValue(value).replace(/\s+/g, " ").trim();
  return text.length > 180 ? text.slice(0, 180) + "…" : (text || "（无正文）");
}

function formatDate(value) {
  if (!value) return "时间未知";
  const number = Number(value);
  const date = Number.isFinite(number) && number > 0
    ? new Date(number < 1e12 ? number * 1000 : number)
    : new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString();
}

function pageNumber(value) {
  const n = Number.parseInt(value || "1", 10);
  return Number.isFinite(n) && n > 0 ? n : 1;
}

function changeHashPage(path, page, game) {
  const query = new URLSearchParams();
  if (page > 1) query.set("page", String(page));
  if (game) query.set("game", game);
  location.hash = path + (query.toString() ? "?" + query.toString() : "");
}

function isDeleted(status) {
  return status === "deleted" || status === "removed";
}

function friendlyError(error, fallback) {
  if (error && error.status === 401) return "请先登录网页账号，再进行这项操作。";
  if (error && error.status === 0) return "网络暂时不可用，请检查连接后重试。";
  const message = error && typeof error.message === "string" ? error.message : "";
  return message && !/^[a-z][a-z _-]*$/i.test(message) ? message : fallback;
}
