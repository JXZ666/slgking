// Browser login and web-only account actions. Credentials are used only for
// login; the server keeps the session in an HttpOnly cookie.
import { api, el, clear, ApiError, paintAccount, clientGuide } from "./app.js";
import { clearForumDrafts } from "./forum.js";
import { clearCommentDrafts } from "./comments.js";
import { renderAuthor, applyAuthorAppearance, TITLE_NAMES } from "./author.js";
import { clearReplyDrafts } from "./replies.js";
import { clearMessageState } from "./messages.js";

let session = null;
let sessionRevision = 0;
let identityRevision = 0;

export function sessionIdentityVersion() { return identityRevision; }

export function currentAccount() {
  return session;
}

function setSession(value) {
  const previous = session?.account_id || "";
  session = value;
  if (previous !== (session?.account_id || "")) {
    identityRevision++;
    clearMessageState();
    window.dispatchEvent(new CustomEvent("slg-session-changed", { detail: { previous, current: session?.account_id || "" } }));
    window.dispatchEvent(new CustomEvent("slg-account-updated"));
  }
}

export function invalidateSession() {
  sessionRevision++;
  setSession(null);
}

export async function loadSession() {
  const revision = ++sessionRevision;
  try {
    const loaded = await api("/account/session");
    if (revision === sessionRevision) setSession(loaded);
  } catch (error) {
    if (!(error instanceof ApiError) || error.status !== 401) throw error;
    if (revision === sessionRevision) setSession(null);
  }
  return session;
}

export async function signOut() {
  const accountId = session && session.account_id;
  sessionRevision += 1;
  try {
    await api("/account/session", { method: "DELETE" });
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) {
      clearForumDrafts(accountId);
      clearCommentDrafts(accountId);
      clearReplyDrafts(accountId);
      sessionRevision += 1;
      setSession(null);
    }
    throw error;
  }
  clearForumDrafts(accountId);
  clearCommentDrafts(accountId);
  clearReplyDrafts(accountId);
  sessionRevision += 1;
  setSession(null);
}

export async function renderLogin(node) {
  await loadSession();
  clear(node);
  if (session) {
    renderAccount(node);
    void renderPendingRegistration(node);
  } else renderForm(node);
  paintAccount();
}

function renderAccount(node) {
  const accountId = session.account_id;
  node.dataset.accountId = accountId;
  const active = () => node.isConnected && session?.account_id === accountId;
  const profile = el("div", { class: "account-summary muted small", text: "正在读取账号信息…" });
  const accountDetails = el("div", { class: "account-details", text: "正在读取账号详情…" });
  const profileStatus = statusBox("login-status");
  const profileEditStatus = statusBox("profile-edit-status");
  const nicknameInput = el("input", {
    name: "nickname",
    autocomplete: "nickname",
    maxlength: "40",
    placeholder: "设置一个昵称",
  });
  const profileInput = el("textarea", {
    name: "profile_message",
    maxlength: "20",
    rows: "3",
    placeholder: "介绍一下自己（最多 20 个字）",
    "aria-label": "个人简介",
  });
  profileInput.style.cssText = "display:block;width:100%;min-height:88px;resize:vertical;border:1px solid #49364c;border-radius:10px;background:#130e17;color:var(--text);padding:11px 12px;font:inherit";
  const nicknameLabel = el("label", { class: "muted small", text: "昵称（最多 40 个字）", for: "profile-nickname" });
  const profileLabel = el("label", { class: "muted small", text: "个人简介（最多 20 个字）", for: "profile-message" });
  nicknameInput.id = "profile-nickname";
  profileInput.id = "profile-message";
  nicknameInput.disabled = true;
  profileInput.disabled = true;
  const visibilityInput = el("input", { type: "checkbox", name: "profile_visible", id: "profile-visible", disabled: true });
  const saveProfile = el("button", { class: "block", type: "submit", text: "保存个人资料" });
  saveProfile.disabled = true;
  const profileForm = el("form", {},
    nicknameLabel,
    nicknameInput,
    profileLabel,
    profileInput,
    el("label", { class: "profile-visibility", for: "profile-visible" }, visibilityInput, "公开我的装扮和寄语"),
    saveProfile,
  );
  let profileFormDirty = false;
  let latestAccount = null;
  profileForm.addEventListener("input", () => { profileFormDirty = true; });
  profileForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const requested = {};
    const currentNickname = latestAccount && typeof latestAccount.nickname === "string"
      ? latestAccount.nickname : "";
    const currentMessage = latestAccount && typeof latestAccount.profile_message === "string"
      ? latestAccount.profile_message : "";
    if (nicknameInput.value.trim() !== currentNickname) {
      requested.nickname = nicknameInput.value.trim();
    }
    if (profileInput.value.trim() !== currentMessage) {
      requested.profile_message = profileInput.value.trim();
    }
    if (visibilityInput.checked !== (latestAccount?.profile_visible === true)) requested.profile_visible = visibilityInput.checked;
    if (!Object.keys(requested).length) {
      setStatus(profileEditStatus, "个人资料没有变化。", false, true);
      return;
    }
    saveProfile.disabled = true;
    nicknameInput.disabled = true;
    profileInput.disabled = true;
    visibilityInput.disabled = true;
    saveProfile.textContent = "正在保存…";
    setStatus(profileEditStatus, "正在保存个人资料…");
    try {
      const result = await api("/web/account/profile", { method: "POST", body: requested });
      if (!active()) return;
      profileFormDirty = false;
      latestAccount = {
        ...(latestAccount || {}),
        nickname: typeof result.nickname === "string" ? result.nickname : requested.nickname,
        profile_message: typeof result.profile_message === "string"
          ? result.profile_message : requested.profile_message,
        profile_visible: typeof result.profile_visible === "boolean" ? result.profile_visible
          : requested.profile_visible ?? latestAccount?.profile_visible,
      };
      renderAccountSummary(profile, latestAccount);
      renderAccountDetails(accountDetails, latestAccount, session && session.account_id);
      nicknameInput.value = latestAccount.nickname || "";
      profileInput.value = latestAccount.profile_message || "";
      visibilityInput.checked = latestAccount.profile_visible === true;
      window.dispatchEvent(new CustomEvent("slg-account-updated"));
      const refreshed = await refreshAccountData();
      if (refreshed && refreshed.expired) return;
      if (refreshed && refreshed.accountOk) {
        setStatus(profileEditStatus, "个人资料已保存并刷新。", false, true);
      } else {
        setStatus(profileEditStatus, "个人资料已保存，但最新资料刷新失败；可点击上方刷新重试。", true);
      }
    } catch (error) {
      if (!active()) return;
      if (handleAccountError(error, node)) return;
      setStatus(profileEditStatus, friendlyError(error, "个人资料保存失败，请检查内容后重试。"), true);
    } finally {
      if (saveProfile.isConnected) {
        saveProfile.disabled = false;
        nicknameInput.disabled = false;
        profileInput.disabled = false;
        visibilityInput.disabled = false;
        saveProfile.textContent = "保存个人资料";
      }
    }
  });
  const questList = el("div", { class: "quest-list" },
    el("p", { class: "muted small", text: "正在读取任务…" }));
  const questStatus = statusBox("quest-status");
  const questBadge = el("span", { class: "account-fold-badge", hidden: true });
  const signin = el("button", { class: "block", type: "button", text: "签到", disabled: true });
  const refresh = el("button", { class: "ghost block", type: "button", text: "刷新账号与任务" });
  const logout = el("button", { class: "ghost block", type: "button", text: "退出登录" });

  node.append(
    el("section", { class: "account-header" },
      el("h2", { text: "我的账号" }),
      profile,
      profileStatus,
      el("div", { class: "account-actions" }, signin, refresh),
      el("div", { class: "account-actions" },
        forumLink("#/shop", "积分商城"), forumLink("#/appearance", "个人装扮")),
      forumLink("#/leaderboard", "社区排行榜"),
    ),
    accountFold("details", "账号详情与签到日历", accountDetails),
    accountFold("profile", "个人资料",
      el("p", { class: "muted small", text: "寄语需要先拥有名片框。关闭公开后，装扮和寄语不再展示；昵称、公开评论和榜单分数保留。" }),
      profileForm,
      profileEditStatus,
    ),
    accountFold("quests", el("span", { class: "account-fold-heading" }, "每日与每周任务", questBadge),
      el("p", { class: "muted small", text: "任务进度来自账号已有记录；网页不会自行补记进度。" }),
      questList,
      questStatus,
    ),
    accountFold("comments", "我的游戏评论",
      el("p", { class: "muted small", text: "查看自己针对具体游戏发表的评论。" }),
      forumLink("#/comments/my", "查看我的游戏评论"),
      forumLink("#/messages?tab=replies", "查看回复消息"),
    ),
    accountFold("forum", "论坛内容",
      el("p", { class: "muted small", text: "自由讨论、发布主题，查看自己的帖子与回复。" }),
      forumLink("#/forum", "进入论坛"),
      forumLink("#/forum/my", "我的论坛内容"),
    ),
    accountFold("client", "网页与客户端", clientGuide("account")),
    accountFold("session", "会话管理",
      el("p", { class: "muted small", text: "网页会话到期 · " + formatExpiry(session.expires_at) }),
      el("p", { class: "muted small", text: "退出登录会结束此浏览器的会话；账号封禁或更换密钥也会让会话失效。" }),
      logout,
    ),
  );

  signin.addEventListener("click", async () => {
    signin.disabled = true;
    signin.textContent = "签到中…";
    setStatus(profileStatus, "正在提交签到…");
    try {
      const result = await api("/web/account/signin", { method: "POST", body: {} });
      if (!active()) return;
      const refreshed = await refreshAccountData();
      if (refreshed && refreshed.expired) return;
      const refreshFailed = refreshed && (!refreshed.accountOk || !refreshed.questsOk);
      setStatus(profileStatus,
        signinMessage(result) + (refreshFailed ? " 账号或任务刷新失败，请手动刷新。" : ""),
        Boolean(refreshFailed), !refreshFailed);
    } catch (error) {
      if (handleAccountError(error, node)) return;
      setStatus(profileStatus, friendlyError(error, "签到暂时未能完成，请稍后重试。"), true);
    } finally {
      if (node.isConnected && session) {
        signin.textContent = profile._signedToday ? "今日已签到" : "签到";
        signin.disabled = Boolean(profile._signedToday);
      }
    }
  });

  refresh.addEventListener("click", async () => {
    refresh.disabled = true;
    refresh.textContent = "刷新中…";
    const refreshed = await refreshAccountData();
    if (node.isConnected && session) {
      refresh.disabled = false;
      refresh.textContent = "刷新账号与任务";
      if (refreshed && refreshed.accountOk && refreshed.questsOk) {
        setStatus(profileStatus, "账号与任务已刷新。", false, true);
      } else {
        setStatus(profileStatus, "部分内容刷新失败，请查看上方错误并重试。", true);
      }
    }
  });

  logout.addEventListener("click", async () => {
    logout.disabled = true;
    logout.textContent = "正在退出…";
    setStatus(profileStatus, "正在结束网页会话…");
    try {
      await signOut();
      clear(node);
      renderForm(node);
      paintAccount();
    } catch (error) {
      if (!currentAccount()) {
        clear(node);
        renderForm(node);
        paintAccount();
        showStatus(node, "网页登录状态已失效，请重新登录。", true);
      } else {
        logout.disabled = false;
        logout.textContent = "退出登录";
        setStatus(profileStatus, friendlyError(error, "退出登录暂时失败，请重试。"), true);
      }
    }
  });

  function refreshAccountData() {
    if (!active()) return Promise.resolve(null);
    setStatus(profileStatus, "正在读取账号信息…");
    setStatus(questStatus, "正在读取任务…");
    signin.disabled = true;

    return Promise.allSettled([
      api("/web/account/me"),
      api("/web/account/quests"),
    ]).then((results) => {
      if (!active()) return null;
      const expired = results.find((result) => result.status === "rejected"
        && result.reason instanceof ApiError && result.reason.status === 401);
      if (expired) {
        handleAccountError(expired.reason, node);
        return { expired: true };
      }

      const [accountResult, questsResult] = results;
      if (accountResult.status === "fulfilled") {
        const account = accountResult.value.account || {};
        latestAccount = account;
        profile._signedToday = account.signed_today === true;
        renderAccountSummary(profile, account);
        renderAccountDetails(accountDetails, account, session.account_id);
        if (!profileFormDirty) {
          nicknameInput.value = typeof account.nickname === "string" ? account.nickname : "";
          profileInput.value = typeof account.profile_message === "string" ? account.profile_message : "";
          visibilityInput.checked = account.profile_visible === true;
        }
        saveProfile.disabled = false;
        nicknameInput.disabled = false;
        profileInput.disabled = false;
        visibilityInput.disabled = false;
        setStatus(profileStatus, "账号信息已载入。", false, true);
        signin.disabled = profile._signedToday;
        signin.textContent = profile._signedToday ? "今日已签到" : "签到";
      } else {
        profile.textContent = "账号资料暂时无法读取。";
        profile._signedToday = false;
        signin.disabled = false;
        signin.textContent = "签到";
        saveProfile.disabled = true;
        nicknameInput.disabled = true;
        profileInput.disabled = true;
        visibilityInput.disabled = true;
        setStatus(profileStatus, friendlyError(accountResult.reason, "账号资料暂时无法读取，请稍后刷新。"), true);
      }

      if (questsResult.status === "fulfilled") {
        const tasks = questsResult.value.tasks;
        const claimableCount = Array.isArray(tasks) ? tasks.filter((task) =>
          task && task.claimable === true && task.claimed !== true && typeof task.id === "string" && task.id).length : 0;
        questBadge.hidden = claimableCount === 0;
        questBadge.textContent = claimableCount ? claimableCount + " 项待领取" : "";
        renderQuests(questList, questsResult.value.tasks, async (task, button) => {
          button.disabled = true;
          button.textContent = "领取中…";
          setStatus(questStatus, "正在领取任务奖励…");
          try {
            await api("/web/account/quests/claim", {
              method: "POST",
              body: { quest_id: task.id },
            });
            const refreshed = await refreshAccountData();
            if (refreshed && refreshed.expired) return;
            if (refreshed && refreshed.accountOk && refreshed.questsOk) {
              setStatus(questStatus, "奖励已领取，任务列表已刷新。", false, true);
            } else {
              setStatus(questStatus, "奖励已领取，但任务刷新失败，请手动刷新。", true);
            }
          } catch (error) {
            if (handleAccountError(error, node)) return;
            setStatus(questStatus, friendlyError(error, "领取暂时未能完成，请刷新任务后重试。"), true);
            button.disabled = false;
            button.textContent = "领取奖励";
          }
        });
        setStatus(questStatus, "任务已载入。", false, true);
      } else {
        questBadge.hidden = true;
        questList.replaceChildren(el("p", { class: "muted small", text: "任务暂时无法载入。" }));
        setStatus(questStatus, friendlyError(questsResult.reason, "任务暂时无法载入，请稍后刷新。"), true);
      }
      return {
        accountOk: accountResult.status === "fulfilled",
        questsOk: questsResult.status === "fulfilled",
      };
    });
  }

  const resume = () => {
    if (document.visibilityState === "visible" && active()) void refreshAccountData();
  };
  document.addEventListener("visibilitychange", resume);
  window.addEventListener("slg-account-updated", resume);
  const cleanup = new MutationObserver(() => {
    if (!node.isConnected) {
      document.removeEventListener("visibilitychange", resume);
      window.removeEventListener("slg-account-updated", resume);
      cleanup.disconnect();
    }
  });
  cleanup.observe(document.getElementById("view") || document.body, { childList: true, subtree: true });

  void refreshAccountData();
}

function forumLink(href, label) {
  const link = el("a", { href, text: label });
  link.style.cssText = "display:flex;align-items:center;justify-content:center;min-height:44px;margin-top:9px;padding:10px 14px;border:1px solid #4a354a;border-radius:10px;text-align:center";
  return link;
}

function renderAccountSummary(node, account) {
  clear(node);
  const card = el("div", { class: "account-namecard" });
  const author = { nickname: account.nickname,
    author_title_name: titleText(account.equipped_title),
    author_title_id: account.equipped_title?.id,
    author_appearances: account.equipped_appearances };
  card.append(renderAuthor(author, { privatePreview: true }));
  applyAuthorAppearance(card, author, { privatePreview: true });
  node.append(
    card,
    el("p", { text: "积分余额 · " + displayValue(account.balance) }),
    el("p", { text: "当前头衔 · " + (titleText(account.equipped_title) || "暂无") }),
  );
}

function accountFold(id, heading, ...content) {
  const fold = el("details", { class: "account-fold", "data-account-fold": id },
    el("summary", {}, heading),
    el("div", { class: "account-fold-content" }, ...content),
  );
  const key = "slgking:account-folds:" + session.account_id;
  const readState = () => {
    try {
      const value = JSON.parse(sessionStorage.getItem(key) || "{}");
      return value && typeof value === "object" && !Array.isArray(value) ? value : {};
    } catch {
      return {};
    }
  };
  fold.open = readState()[id] === true;
  fold.addEventListener("toggle", () => {
    try {
      sessionStorage.setItem(key, JSON.stringify({ ...readState(), [id]: fold.open }));
    } catch {
      // Native disclosure still works when browser storage is unavailable.
    }
  });
  return fold;
}

function renderAccountDetails(node, account, fallbackId) {
  clear(node);
  const ownedTitles = Array.isArray(account.titles)
    ? account.titles
    : (Array.isArray(account.owned_titles) ? account.owned_titles : []);
  const equippedTitle = titleText(account.equipped_title);
  const details = [
    ["昵称", displayValue(account.nickname, "未设置")],
    ["积分余额", displayValue(account.balance)],
    ["注册时间", formatCreated(account.created)],
    ["本月签到", displayValue(account.signin_month_count, "0") + " 天"],
    ["连续签到", displayValue(account.signin_streak, "0") + " 天"],
    ["已装备头衔", equippedTitle || "暂无"],
    ["账号 ID", displayValue(account.account_id, fallbackId || "未知")],
  ];
  node.append(...details.map(([label, value]) =>
    el("p", { class: "muted small", text: label + " · " + value })));
  node.append(el("p", {
    class: "muted small",
    text: "个人简介 · " + displayValue(account.profile_message, "尚未填写"),
  }));

  const titleList = el("div", { class: "muted small" },
    el("p", { text: "已拥有头衔" }));
  if (ownedTitles.length) {
    const list = el("ul", { style: "margin:4px 0 0;padding-left:20px;overflow-wrap:anywhere" });
    for (const title of ownedTitles) list.append(el("li", { text: titleText(title) || "未知头衔" }));
    titleList.append(list);
  } else {
    titleList.append(el("p", { text: "暂未获得头衔。" }));
  }
  node.append(titleList, renderSigninCalendar(account.signin_month_days, account));
}

function titleText(value) {
  if (typeof value === "string") return TITLE_NAMES[value] || "";
  if (!value || typeof value !== "object" || Array.isArray(value)) return "";
  for (const key of ["name", "title", "label"]) {
    if (typeof value[key] === "string" && value[key]) return value[key];
  }
  return TITLE_NAMES[value.id] || "";
}

function signinDayNumbers(value) {
  if (!Array.isArray(value)) return [];
  const days = new Set();
  for (const item of value) {
    const raw = typeof item === "string" && item.includes("-")
      ? item.slice(-2) : item;
    const day = Number(raw);
    if (Number.isInteger(day) && day >= 1 && day <= 31) days.add(day);
  }
  return [...days].sort((a, b) => a - b);
}

function renderSigninCalendar(rawDays, account) {
  const day = /^\d{4}-\d{2}-\d{2}$/.test(account.day || "") ? account.day
    : new Intl.DateTimeFormat("sv-SE", { timeZone: "Asia/Shanghai" }).format(new Date());
  const year = Number(day.slice(0, 4));
  const month = Number(day.slice(5, 7)) - 1;
  const monthName = year + "年" + (month + 1) + "月";
  const days = signinDayNumbers(rawDays);
  const signed = new Set(days);
  const calendar = el("div", { class: "signin-calendar" });
  const heading = el("p", {
    class: "muted small",
    text: monthName + "签到记录",
  });
  const weekdays = el("div", {
    "aria-hidden": "true",
    style: "display:grid;grid-template-columns:repeat(7,minmax(0,1fr));gap:4px;text-align:center",
  });
  for (const weekday of ["一", "二", "三", "四", "五", "六", "日"]) {
    const cell = el("span", { text: weekday });
    cell.style.cssText = "color:var(--muted);font-size:11px;padding:3px 0";
    weekdays.append(cell);
  }
  const grid = el("div", {
    role: "list",
    "aria-label": monthName + "签到日历",
    style: "display:grid;grid-template-columns:repeat(7,minmax(0,1fr));gap:4px;text-align:center",
  });
  const firstWeekday = (new Date(Date.UTC(year, month, 1)).getUTCDay() + 6) % 7;
  const monthLength = new Date(Date.UTC(year, month + 1, 0)).getUTCDate();
  for (let index = 0; index < firstWeekday; index += 1) {
    grid.append(el("span", { "aria-hidden": "true" }));
  }
  for (let day = 1; day <= monthLength; day += 1) {
    const didSign = signed.has(day);
    const dayDate = new Date(Date.UTC(year, month, day));
    const weekday = new Intl.DateTimeFormat("zh-CN", { weekday: "long", timeZone: "UTC" }).format(dayDate);
    const cell = el("span", {
      role: "listitem",
      "aria-label": monthName + day + "日，" + weekday + (didSign ? "，已签到" : "，未签到"),
      text: String(day) + (didSign ? " ✓" : ""),
    });
    cell.style.cssText = "display:grid;place-items:center;min-height:32px;border:1px solid "
      + (didSign ? "#83d8b1" : "#38283b") + ";border-radius:8px;color:"
      + (didSign ? "#83d8b1" : "var(--muted)") + ";background:"
      + (didSign ? "#83d8b118" : "transparent") + ";font-size:12px";
    grid.append(cell);
  }
  const summary = Array.isArray(rawDays)
    ? days.length + " 天已签到" + (account.signed_today === true ? " · 今天已签到" : "")
    : "本月签到日期暂不可用";
  calendar.append(heading, weekdays, grid, el("p", { class: "muted small", text: summary }));
  return calendar;
}

function renderQuests(node, tasks, onClaim) {
  clear(node);
  if (!Array.isArray(tasks) || tasks.length === 0) {
    node.append(el("p", { class: "empty", text: "目前没有可展示的任务。" }));
    return;
  }

  for (const task of tasks) {
    const item = task && typeof task === "object" ? task : {};
    const id = typeof item.id === "string" ? item.id : "";
    const claimed = item.claimed === true;
    const claimable = item.claimable === true && !claimed && Boolean(id);
    const button = el("button", {
      class: "ghost block",
      type: "button",
      text: claimed ? "已领取" : (claimable ? "领取奖励" : "暂不可领取"),
      disabled: !claimable,
    });
    if (claimable) button.addEventListener("click", () => onClaim(item, button));

    const progress = Number.isFinite(Number(item.progress)) ? Number(item.progress) : null;
    const target = Number.isFinite(Number(item.target)) ? Number(item.target) : null;
    const progressText = progress === null || target === null
      ? "进度 · —"
      : "进度 · " + progress + " / " + target;
    node.append(el("div", { class: "card" },
      el("h3", { text: displayValue(item.label, "任务") }),
      el("p", { class: "muted small", text: displayValue(item.period, "周期未知") + " · " + progressText }),
      el("p", { class: "muted small", text: "奖励 · " + displayValue(item.reward) }),
      button,
    ));
  }
}

function renderForm(node) {
  delete node.dataset.accountId;
  const accountId = el("input", { name: "account_id", autocomplete: "off", placeholder: "u_…（可留空）" });
  const loginKey = el("input", { name: "login_key", autocomplete: "off", placeholder: "sk_u_…", required: true });
  const recovery = el("input", { name: "recovery_code", autocomplete: "off", placeholder: "sr_…" });

  loginKey.type = "password";
  loginKey.autocapitalize = "none";
  loginKey.spellcheck = false;
  recovery.type = "password";
  recovery.required = true;
  recovery.autocapitalize = "none";
  recovery.spellcheck = false;

  const submit = el("button", { class: "block", type: "submit", text: "登录" });
  const form = el("form", {},
    el("label", { class: "muted small", text: "账号 ID" }), accountId,
    el("label", { class: "muted small", text: "登录密钥" }), loginKey,
    el("label", { class: "muted small", text: "恢复码" }), recovery,
    submit,
  );

  const labels = form.querySelectorAll("label");
  [accountId, loginKey, recovery].forEach((input, index) => {
    input.id = "web-login-field-" + index;
    labels[index].htmlFor = input.id;
  });

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    submit.disabled = true;
    showStatus(node, "正在登录…");
    try {
      await api("/account/session", {
        method: "POST",
        body: {
          account_id: accountId.value.trim(),
          login_key: loginKey.value.trim(),
          recovery_code: recovery.value.trim(),
          device_label: "网页端",
        },
      });
      loginKey.value = "";
      recovery.value = "";
      const query = new URLSearchParams(location.hash.split("?")[1] || "");
      const destination = query.get("return") || query.get("next");
      if (destination && /^#\/(shop|appearance|announcements|messages|discussion\/[^?]+|profile\/[^?]+|comments\/my|game\/[^?]+|forum(?:\/[^?]+)?)(?:\?.*)?$/.test(destination)) {
        await loadSession();
        location.hash = destination;
        return;
      }
      await renderLogin(node);
    } catch (error) {
      submit.disabled = false;
      showStatus(node, error.status === 401
        ? "账号 ID、登录密钥或恢复码不正确，请检查后重试。"
        : friendlyError(error, "登录暂时未能完成，请检查信息后重试。"), true);
      if (error.status === 401 && /恢复码/.test(error.message)) recovery.focus();
    }
  });

  node.append(el("div", { class: "card" },
    el("h2", { text: "登录网页端" }),
    el("p", { class: "muted small", text: "已有客户端账号可直接登录，无需重新注册。账号资料与积分共用。" }),
    form,
    statusBox("login-status"),
  ));

  node.append(el("div", { class: "card" },
    el("h3", { text: "恢复码是必需的" }),
    el("p", { class: "muted small", text: "网页登录会创建独立的浏览器会话，不占用桌面端设备。每次重新登录都需要携带恢复码。" }),
    el("p", { class: "muted small", text: "桌面端登录后可在账号页查看恢复码。网页不会保存你的密钥，登录后只保留一个 HttpOnly 会话 Cookie。" }),
  ));

  node.append(el("div", { class: "card" },
    el("h3", { text: "安全提示" }),
    el("p", { class: "muted small", text: "请勿在公共电脑上登录。退出登录会撤销当前浏览器会话；账号封禁或更换密钥会撤销该账号的网页会话。" }),
  ));
  renderRegistrationEntry(node);
}

const WEB_REGISTRATION_OPERATION_KEY = "slgking:web-registration-operation";
let memoryRegistrationOperation = null;

function pendingWebRegistrationOperation() {
  if (memoryRegistrationOperation) return memoryRegistrationOperation;
  try {
    memoryRegistrationOperation = sessionStorage.getItem(WEB_REGISTRATION_OPERATION_KEY);
  } catch {
    // Keep the retry key in memory if browser storage is unavailable.
  }
  return memoryRegistrationOperation;
}

function saveWebRegistrationOperation(operationId) {
  memoryRegistrationOperation = operationId;
  try {
    sessionStorage.setItem(WEB_REGISTRATION_OPERATION_KEY, operationId);
  } catch {
    // The operation key is not a credential; memory still supports retries in
    // this page, while sessionStorage normally survives a reload in this tab.
  }
}

function clearWebRegistrationOperation() {
  memoryRegistrationOperation = null;
  try {
    sessionStorage.removeItem(WEB_REGISTRATION_OPERATION_KEY);
  } catch {
    // Nothing else is stored in browser storage.
  }
}

function newWebRegistrationOperation() {
  if (!globalThis.crypto || typeof globalThis.crypto.getRandomValues !== "function") {
    throw new Error("secure random generation unavailable");
  }
  const bytes = new Uint8Array(16);
  globalThis.crypto.getRandomValues(bytes);
  return Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
}

function renderRegistrationEntry(node) {
  const card = el("div", { class: "card" });
  const pending = Boolean(pendingWebRegistrationOperation());
  const button = el("button", {
    class: "ghost block",
    type: "button",
    text: pending ? "继续未完成的网页注册" : "创建网页账号",
  });
  const status = statusBox("registration-status");
  const result = el("div", {});

  card.append(
    el("h3", { text: "还没有账号？" }),
    el("p", {
      class: "muted small",
      text: "可以直接创建网页账号。登录密钥和恢复码只在你确认保存前显示，请妥善保存；关闭页面前完成保存确认。",
    }),
    button,
    status,
    result,
  );

  button.addEventListener("click", async () => {
    let operationId = pendingWebRegistrationOperation();
    if (!operationId) {
      try {
        operationId = newWebRegistrationOperation();
        saveWebRegistrationOperation(operationId);
      } catch {
        setStatus(status, "当前浏览器无法安全生成注册请求，请更新浏览器后重试。", true);
        return;
      }
    }

    button.disabled = true;
    button.textContent = "正在创建网页账号…";
    setStatus(status, "正在提交注册请求…");
    try {
      const response = await api("/web/account/register", {
        method: "POST",
        body: { operation_id: operationId },
      });
      if (response.credentials_available !== true
          || typeof response.account_id !== "string"
          || typeof response.login_key !== "string"
          || typeof response.recovery_code !== "string") {
        clearWebRegistrationOperation();
        button.disabled = false;
        button.textContent = "创建网页账号";
        setStatus(status,
          "这次注册的凭据已确认保存。请使用已保存的账号 ID、登录密钥和恢复码登录。",
          true);
        return;
      }
      if (!card.isConnected) return;

      button.hidden = true;
      clear(result);
      renderRegistrationCredentials(node, result, status, response);
      setStatus(status, "账号已创建，凭据尚未确认保存。", false, true);
    } catch (error) {
      button.disabled = false;
      button.textContent = "重试网页注册";
      if (error instanceof ApiError && error.status === 409) {
        clearWebRegistrationOperation();
        button.textContent = "创建网页账号";
        setStatus(status,
          "该注册请求的凭据已确认保存。请用已保存的凭据登录。", true);
      } else {
        setStatus(status,
          friendlyError(error, "注册暂时失败。可重试同一请求，不会重复创建账号。"),
          true);
      }
    }
  });

  node.append(card);
}

function renderPendingRegistration(node) {
  const card = el("div", { class: "card account-registration" });
  const status = statusBox("registration-pending-status");
  const result = el("div", {});
  card.append(
    el("h3", { text: "待保存的注册凭据" }),
    status,
    result,
  );
  node.prepend(card);

  api("/web/account/register/pending").then((response) => {
    if (!card.isConnected) return;
    if (response.credentials_available === true
        && typeof response.account_id === "string"
        && typeof response.login_key === "string"
        && typeof response.recovery_code === "string") {
      renderRegistrationCredentials(node, result, status, response);
      setStatus(status, "发现尚未确认保存的注册凭据。", false, true);
    } else {
      card.remove();
    }
  }).catch((error) => {
    if (!card.isConnected || (error instanceof ApiError && error.status === 401)) {
      card.remove();
      return;
    }
    const retry = el("button", {
      class: "ghost block",
      type: "button",
      text: "重新检查",
    });
    retry.addEventListener("click", () => {
      card.remove();
      renderPendingRegistration(node);
    });
    card.append(retry);
    setStatus(status, friendlyError(error, "暂时无法检查待保存的凭据。"), true);
  });
}

function renderRegistrationCredentials(node, target, status, response) {
  clear(target);
  target.append(
    el("p", { class: "muted small", text: "请立即保存以下凭据；离开此页前需要确认已保存。" }),
    el("p", { class: "muted small" },
      "账号 ID：", el("code", { text: response.account_id })),
    el("p", { class: "muted small" },
      "登录密钥：", el("code", { text: response.login_key })),
    el("p", { class: "muted small" },
      "恢复码：", el("code", { text: response.recovery_code })),
  );

  const acknowledge = el("button", {
    class: "block",
    type: "button",
    text: "我已安全保存，进入账号主页",
  });
  target.append(
    el("p", {
      class: "muted small",
      text: "恢复码用于之后的网页登录和账号恢复。服务器不会保存这些凭据的明文。",
    }),
    acknowledge,
  );
  acknowledge.addEventListener("click", async () => {
    acknowledge.disabled = true;
    setStatus(status, "正在确认凭据已保存…");
    try {
      await api("/web/account/register/ack", {
        method: "POST",
        body: {},
      });
      clearWebRegistrationOperation();
      if (!target.isConnected) return;
      clear(target);
      await renderLogin(node);
    } catch (error) {
      if (!target.isConnected) return;
      acknowledge.disabled = false;
      setStatus(status,
        friendlyError(error, "确认暂时失败。凭据仍会显示，请保存后重试确认。"),
        true);
    }
  });
}

function statusBox(id) {
  return el("div", { class: "status", id, role: "status", "aria-live": "polite" });
}

function setStatus(box, text, isError = false, isGood = false) {
  box.textContent = text;
  box.className = "status" + (isError ? " error" : (isGood ? " good" : ""));
}

function showStatus(node, text, isError) {
  const box = node.querySelector("#login-status");
  if (box) setStatus(box, text, Boolean(isError));
}

function handleAccountError(error, node) {
  if (!(error instanceof ApiError) || error.status !== 401 || !node.isConnected) return false;
  if (node.dataset.accountId && node.dataset.accountId !== session?.account_id) return false;
  sessionRevision += 1;
  setSession(null);
  clear(node);
  renderForm(node);
  showStatus(node, "网页登录状态已失效，请重新登录。", true);
  paintAccount();
  return true;
}

function friendlyError(error, fallback) {
  if (error instanceof ApiError && error.status === 401) return "网页登录状态已失效，请重新登录。";
  if (error instanceof ApiError && error.status === 0) return "网络暂时不可用，请检查连接后重试。";
  const message = error && typeof error.message === "string" ? error.message : "";
  const knownEnglish = {
    "signin disabled": "签到功能当前不可用，请稍后再试。",
    "quest not claimable": "任务尚未达成或已领取，请刷新任务列表。",
    "signin unavailable": "签到服务暂时不可用，请稍后重试。",
    "storage unavailable": "服务暂时不可用，请稍后重试。",
    "rate limited": "操作过于频繁，请稍后再试。",
  };
  if (knownEnglish[message.toLowerCase()]) return knownEnglish[message.toLowerCase()];
  if (/[A-Za-z]/.test(message)) return fallback;
  return message || fallback;
}

function signinMessage(result) {
  if (result && result.already === true) return "今天已经签到过了。";
  const gained = optionalNumber(result && result.gained);
  const bonus = optionalNumber(result && result.bonus);
  const balance = optionalNumber(result && result.balance);
  const parts = [];
  if (gained !== null) parts.push("获得 " + gained + " 积分");
  if (bonus !== null && bonus > 0) parts.push("额外奖励 " + bonus + " 积分");
  if (balance !== null) parts.push("当前余额 " + balance);
  return "签到成功" + (parts.length ? "，" + parts.join("；") : "") + "。";
}

function optionalNumber(value) {
  if (value === null || value === undefined || value === "") return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

function displayValue(value, fallback = "—") {
  return value === null || value === undefined || value === "" ? fallback : String(value);
}

function formatCreated(value) {
  if (value === null || value === undefined || value === "") return "—";
  const numeric = Number(value);
  const date = Number.isFinite(numeric)
    ? new Date(numeric < 100000000000 ? numeric * 1000 : numeric)
    : new Date(value);
  return Number.isNaN(date.getTime()) ? "—" : date.toLocaleDateString();
}

function formatExpiry(seconds) {
  if (!seconds) return "未知";
  const days = Math.round((seconds * 1000 - Date.now()) / 86400000);
  return days > 0 ? days + " 天后" : "即将到期";
}
