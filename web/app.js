import { allowUsage, reportFeature } from "./usage.js";
// Hash router + shared helpers. Page modules export render(view, params).
// No bundler, no CDN: every asset is served from /m/ on the same origin.
import { renderSearch } from "./search.js";
import { renderGame } from "./game.js";
import { renderLogin, loadSession, currentAccount } from "./login.js";
import { renderForum, renderForumThread, renderMyForum } from "./forum.js";
import { renderMyComments } from "./comments.js";
import { renderShop } from "./shop.js";
import { renderAnnouncements, renderAnnouncementSummary } from "./announcements.js";
import { renderAppearance } from "./author.js";
import { renderCatalogue } from "./catalogue.js";
import { renderLeaderboard } from "./leaderboard.js";
import { WEB_VERSION } from "./version.js";

const view = document.getElementById("view");
const titleEl = document.getElementById("topbar-title");
const userEl = document.getElementById("topbar-user");
const versionEl = document.getElementById("web-version");
if (versionEl) versionEl.textContent = "v" + WEB_VERSION;

/* ---------- DOM ---------- */

// Build nodes with textContent instead of innerHTML: game titles and developer
// names are third-party strings, so never interpolate them into markup.
export function el(tag, props, ...kids) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value === true ? "" : String(value));
  }
  for (const kid of kids.flat()) {
    if (kid === null || kid === undefined || kid === false) continue;
    node.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return node;
}

export function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
}

// Reviewed DLsite display names; identifiers are never translated in requests.
const DLSITE_TAG_LABELS = Object.freeze({
  "dlsite:genre:432": "女主人公",
  "dlsite:genre:504": "姐姐×正太",
  "dlsite:genre:534": "正太×姐姐",
  "dlsite:genre:501": "潮吹",
  "dlsite:genre:523": "乳头刺激",
  "dlsite:genre:128": "内射/中出",
  "dlsite:genre:138": "口交",
  "dlsite:genre:182": "巨乳/爆乳",
  "dlsite:genre:240": "魔法少女",
  "dlsite:genre:019": "恐怖",
  "dlsite:genre:324": "异种X",
  "dlsite:genre:131": "挤奶",
  "dlsite:genre:134": "羞辱",
  "dlsite:genre:317": "人外娘/魔物娘",
  "dlsite:genre:448": "色诱",
  "dlsite:genre:536": "女性主导",
  "dlsite:genre:125": "足交",
  "dlsite:genre:156": "男性受",
  "dlsite:genre:065": "胸部/奶子",
  "dlsite:genre:533": "战斗H",
  "dlsite:genre:446": "卖春/援交",
  "dlsite:genre:016": "奇幻",
  "dlsite:genre:142": "淫乱",
  "dlsite:genre:049": "着衣/穿衣",
  "dlsite:genre:070": "连续高潮",
  "dlsite:genre:146": "拘束",
  "dlsite:genre:144": "言语刺激",
  "dlsite:genre:118": "蕾丝/女同",
  "dlsite:genre:190": "扶她",
  "dlsite:genre:311": "触摸/抚摸",
  "dlsite:genre:071": "断面图",
  "dlsite:genre:263": "玩具",
  "dlsite:genre:207": "萝",
  "dlsite:genre:302": "NTR(黄毛视角)",
  "dlsite:genre:527": "阴蒂刺激",
  "dlsite:genre:031": "同居",
  "dlsite:genre:013": "温馨",
  "dlsite:genre:004": "亲热/甜蜜",
  "dlsite:genre:495": "睡奸/迷奸",
  "dlsite:genre:082": "内衣",
  "dlsite:genre:524": "哦吼淫叫",
  "dlsite:genre:159": "放尿/小便",
  "dlsite:genre:189": "阴毛/腋毛",
  "dlsite:genre:006": "颓废/背德",
  "dlsite:genre:115": "反向奸",
  "dlsite:genre:124": "手交",
  "dlsite:genre:122": "乳交",
  "dlsite:genre:114": "命令/强迫",
  "dlsite:genre:129": "怀孕",
  "dlsite:genre:509": "3D作品",
  "dlsite:genre:505": "女忍者",
  "dlsite:genre:487": "深喉口交(男主导)",
  "dlsite:genre:113": "强X",
  "dlsite:genre:186": "西瓜肚/孕妇",
  "dlsite:genre:063": "臀部/屁股",
  "dlsite:genre:074": "制服",
  "dlsite:genre:637": "亲吻",
  "dlsite:genre:526": "沉迷快乐/快乐堕落",
  "dlsite:genre:048": "被NTR(苦主视角)",
  "dlsite:genre:017": "历史/古风",
  "dlsite:genre:193": "处女",
  "dlsite:genre:060": "女性视角",
  "dlsite:genre:183": "贫乳/微乳",
  "dlsite:genre:319": "医生",
  "dlsite:genre:149": "羞耻/耻辱",
  "dlsite:genre:235": "精灵/妖精",
  "dlsite:genre:197": "男性自称少女/仆娘",
  "dlsite:genre:254": "机器人/仿生人",
  "dlsite:genre:078": "女仆",
  "dlsite:genre:010": "科幻",
  "dlsite:genre:455": "爱情喜剧",
  "dlsite:genre:187": "平胸/飞机场",
  "dlsite:genre:313": "虐待/ryona",
  "dlsite:genre:068": "淫语",
  "dlsite:genre:502": "男主人公",
  "dlsite:genre:433": "无逆转",
  "dlsite:genre:510": "RPG制作大师",
  "dlsite:genre:506": "魅魔/淫魔",
  "dlsite:genre:212": "妹妹",
  "dlsite:genre:008": "日常/生活",
  "dlsite:genre:219": "人妻",
  "dlsite:genre:160": "肛交",
  "dlsite:genre:171": "黑发",
  "dlsite:genre:414": "僵尸/丧尸",
  "dlsite:genre:162": "触手",
  "dlsite:genre:204": "血液/流血",
  "dlsite:genre:300": "丸吞",
  "dlsite:genre:222": "青梅竹马",
  "dlsite:genre:321": "褐皮/晒痕",
  "dlsite:genre:256": "项圈/锁链/拘束道具",
  "dlsite:genre:257": "道具/异物",
  "dlsite:genre:136": "SM",
  "dlsite:genre:164": "机械X",
  "dlsite:genre:201": "拷打",
  "dlsite:genre:241": "魔法师/魔女",
  "dlsite:genre:140": "教育",
  "dlsite:genre:176": "兽耳",
  "dlsite:genre:116": "多P/乱交",
  "dlsite:genre:046": "后宫",
  "dlsite:genre:312": "换装",
  "dlsite:genre:220": "姐姐",
  "dlsite:genre:323": "高潮脸/阿黑颜",
  "dlsite:genre:464": "打屁股",
  "dlsite:genre:179": "丰满",
  "dlsite:genre:050": "走光",
  "dlsite:genre:083": "内裤",
  "dlsite:genre:135": "自慰",
  "dlsite:genre:519": "异世界转生",
  "dlsite:genre:007": "喜剧",
  "dlsite:genre:659": "按摩",
  "dlsite:genre:489": "吞精/食精",
  "dlsite:genre:157": "暗示",
  "dlsite:genre:400": "学生",
  "dlsite:genre:661": "常识改变",
  "dlsite:genre:326": "精神支配",
  "dlsite:genre:266": "润滑液",
  "dlsite:genre:227": "教师",
  "dlsite:genre:143": "露出",
  "dlsite:genre:206": "少女",
  "dlsite:genre:237": "天使/恶魔",
  "dlsite:genre:215": "女儿",
  "dlsite:genre:191": "巨根",
  "dlsite:genre:170": "金发",
  "dlsite:genre:541": "兽人",
  "dlsite:genre:091": "浴衣/和服",
  "dlsite:genre:123": "通奸/和奸",
  "dlsite:genre:066": "特殊癖好/变态",
  "dlsite:genre:130": "母乳",
  "dlsite:genre:057": "平淡/冷淡",
  "dlsite:genre:132": "分娩",
  "dlsite:genre:069": "大量汁/液",
  "dlsite:genre:647": "乳房膨胀",
  "dlsite:genre:151": "限制自由",
  "dlsite:genre:154": "造孽",
  "dlsite:genre:121": "多人运动",
  "dlsite:genre:484": "腹击",
  "dlsite:genre:255": "眼镜",
  "dlsite:genre:285": "前辈/后辈",
  "dlsite:genre:648": "淫纹",
  "dlsite:genre:120": "骨科",
  "dlsite:genre:251": "野兽/兽化",
  "dlsite:genre:032": "纯爱",
  "dlsite:genre:096": "修女",
  "dlsite:genre:325": "恶堕",
  "dlsite:genre:058": "皆大欢喜",
  "dlsite:genre:051": "萌",
  "dlsite:genre:014": "情侣",
  "dlsite:genre:269": "变身女主",
  "dlsite:genre:301": "电车",
  "dlsite:genre:139": "偷偷摸摸",
  "dlsite:genre:086": "Cosplay/角色扮演",
  "dlsite:genre:147": "奴仆",
  "dlsite:genre:153": "挠痒痒",
  "dlsite:genre:073": "动画",
  "dlsite:genre:155": "偷拍/盗摄",
  "dlsite:genre:538": "妈妈",
  "dlsite:genre:440": "出轨",
  "dlsite:genre:444": "时间停止",
  "dlsite:genre:465": "路人大叔",
  "dlsite:genre:188": "白虎/无阴毛",
  "dlsite:genre:431": "像素",
  "dlsite:genre:211": "正太",
  "dlsite:genre:438": "婚前同居",
  "dlsite:genre:540": "婴儿PLAY",
  "dlsite:genre:662": "人外",
  "dlsite:genre:200": "人体改造",
  "dlsite:genre:203": "乳头交",
  "dlsite:genre:099": "兔女郎",
  "dlsite:genre:001": "学校/学园",
  "dlsite:genre:447": "风俗/泡泡浴",
  "dlsite:genre:245": "性转换(TS)",
  "dlsite:genre:163": "兽X",
  "dlsite:genre:265": "药物",
  "dlsite:genre:415": "辣妹",
  "dlsite:genre:126": "外射",
  "dlsite:genre:005": "搞笑",
  "dlsite:genre:061": "致郁",
  "dlsite:genre:260": "耳环/装饰品",
  "dlsite:genre:281": "同学/同事",
  "dlsite:genre:166": "短发",
  "dlsite:genre:284": "警察/刑警",
  "dlsite:genre:488": "口内射精/口爆",
  "dlsite:genre:660": "阴郁系",
  "dlsite:genre:497": "ASMR",
  "dlsite:genre:649": "模仿娇喘",
  "dlsite:genre:514": "自慰辅助",
  "dlsite:genre:112": "正常玩法",
  "dlsite:genre:003": "室外",
  "dlsite:genre:529": "故意被NTR/绿奴",
  "dlsite:genre:133": "产卵",
  "dlsite:genre:490": "虫X",
  "dlsite:genre:213": "母亲",
  "dlsite:genre:486": "寡妇",
  "dlsite:genre:145": "野外/野战",
  "dlsite:genre:137": "捆绑/紧缚",
  "dlsite:genre:202": "拳交",
  "dlsite:genre:411": "体育系/运动员",
  "dlsite:genre:077": "泳装",
  "dlsite:genre:642": "感官屏蔽",
  "dlsite:genre:079": "护士",
  "dlsite:genre:409": "艺人/偶像/模特",
  "dlsite:genre:080": "巫女",
  "dlsite:genre:102": "丝袜",
  "dlsite:genre:026": "超自然现象",
  "dlsite:genre:208": "娃娃脸/婴儿肥",
  "dlsite:genre:158": "百合",
  "dlsite:genre:173": "单马尾",
  "dlsite:genre:512": "东方Project",
  "dlsite:genre:148": "灌肠",
  "dlsite:genre:214": "义妹",
  "dlsite:genre:243": "妖怪",
  "dlsite:genre:064": "癖好/性趣",
  "dlsite:genre:152": "挑逗/调情",
  "dlsite:genre:525": "雌小鬼"
});

export function tagLabel(item, labels) {
  const object = item && typeof item === "object";
  const slug = String(object ? item.slug || "" : item || "");
  if (Object.prototype.hasOwnProperty.call(DLSITE_TAG_LABELS, slug)) {
    return DLSITE_TAG_LABELS[slug];
  }
  const candidates = [object ? item.label : null, labels?.get(slug)];
  const dlsite = slug.startsWith("dlsite:genre:");
  for (const value of candidates) {
    if (typeof value !== "string" || !value.trim()) continue;
    const label = value.trim();
    if (dlsite && (label === slug || label.startsWith("dlsite:genre:") || /^\d+$/.test(label))) continue;
    return label;
  }
  return dlsite ? "未收录标签" : slug;
}

const CLIENT_RELEASE = "https://github.com/JXZ666/slgking/releases/latest";

export function clientGuide(context = "home") {
  const copy = {
    home: "手机网页随时查资料、聊游戏。想扫描和管理电脑里的游戏，可使用 Windows 客户端。",
    game: "用 Windows 客户端整理收藏、记录本机评分，并管理和启动已经安装的游戏。",
    account: "网页方便随时浏览和讨论；Windows 客户端还支持本地游戏库、私人记录和按偏好推荐。账号积分、签到和公开评论共用，本机评分、收藏和游戏目录不会随登录自动同步，需要自行备份迁移。",
  };
  const guide = el("aside", { class: "client-guide", "aria-label": "Windows 客户端" },
    el("div", {},
      el("p", { text: copy[context] || copy.home }),
      el("a", { class: "client-download", href: CLIENT_RELEASE, target: "_blank",
        rel: "noopener noreferrer", text: "下载 Windows 客户端" })),
  );
  if (context === "home") {
    try {
      if (sessionStorage.getItem("slg_web_client_guide_dismissed") === "1") guide.hidden = true;
    } catch { /* Storage may be unavailable in private browsing. */ }
    guide.append(el("button", { class: "ghost guide-dismiss", type: "button",
      text: "×", "aria-label": "关闭客户端提示", title: "关闭提示", onclick: () => {
        guide.hidden = true;
        try { sessionStorage.setItem("slg_web_client_guide_dismissed", "1"); } catch { /* optional */ }
      } }));
  }
  return guide;
}

/* ---------- API ---------- */

export class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.status = status;
  }
}

// Same-origin cookie auth: the browser attaches slg_web_session itself, and the
// server rejects writes without a matching Origin header.
export async function api(pathname, { method = "GET", body } = {}) {
  const writing = !["GET", "HEAD"].includes(method.toUpperCase());
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), writing ? 30000 : 20000);
  let response;
  let payload = null;
  try {
    response = await fetch(pathname, {
      method,
      signal: controller.signal,
      credentials: "same-origin",
      headers: body ? { "Content-Type": "application/json" } : undefined,
      body: body ? JSON.stringify(body) : undefined,
    });
    try {
      payload = await response.json();
    } catch (error) {
      if (controller.signal.aborted) throw error;
      // A proxy error page is handled below like any non-JSON response.
    }
  } catch {
    const error = new ApiError(writing
      ? "连接中断或等待超时，提交结果尚未确认。请先刷新查看，不要重复提交。"
      : controller.signal.aborted ? "请求超时，请稍后重试" : "网络不可用，请检查连接", 0);
    error.outcomeUnknown = writing;
    throw error;
  } finally {
    clearTimeout(timer);
  }
  if (!response.ok || !payload || payload.ok === false) {
    const phrase = (payload && payload.error) || "HTTP " + response.status;
    const error = new ApiError(messageFor(phrase), response.status);
    if (writing && !payload) {
      error.outcomeUnknown = true;
      error.message = "服务器响应未能确认提交结果，请先刷新查看，不要重复提交。";
    }
    throw error;
  }
  return payload;
}

// The server deliberately returns short English phrases; map the ones a visitor
// can actually hit rather than showing raw codes.
function messageFor(phrase) {
  switch (phrase) {
    case "unauthorized": return "登录已失效，请重新登录";
    case "recovery required": return "新设备登录需要恢复码";
    case "rate limited": return "服务繁忙或请求过于频繁，请稍后再试";
    case "bad body": return "提交内容不合法";
    case "bad query": return "搜索条件不合法";
    case "not found": return "没有找到这款游戏";
    case "catalogue unavailable": return "目录暂时不可用，请稍后再试";
    case "cross-origin request blocked": return "请求被拒绝";
    case "storage unavailable": return "服务暂时不可用";
    default: return phrase;
  }
}

/* ---------- covers ---------- */

export function cover(src, cls) {
  if (!src) return el("div", { class: (cls || "") + " miss", text: "无封面" });
  const img = el("img", { class: cls, src, alt: "", loading: "lazy", decoding: "async" });
  // A missing cover file must not leave a broken-image glyph in the grid.
  img.addEventListener("error", () => {
    const fallback = el("div", { class: (cls || "") + " miss", text: "无封面" });
    img.replaceWith(fallback);
  });
  return img;
}

/* ---------- pages ---------- */

async function renderHome(node, params = new URLSearchParams()) {
  titleEl.textContent = "游戏目录";
  await renderCatalogue(node, params);
}

export function tile(item) {
  const catalog = item.catalog_id || "western_slg";
  const nativeRating = catalog === "dlsite" ? item.facts?.rating_value : null;
  const score = nativeRating != null
    ? "DLsite " + nativeRating + "/" + (item.facts.rating_scale_max || "?")
    : item.rating == null ? "" : "★ " + item.rating;
  return el("a", { class: "tile", href: "#/game/" + encodeURIComponent(item.slug)
    + "?catalog_id=" + encodeURIComponent(catalog) },
    cover(item.cover, "shot"),
    el("div", { class: "body" },
      el("div", { class: "name", text: item.title || item.slug }),
      el("div", { class: "meta" },
        el("span", { text: item.version || "" }),
        el("span", { class: "score", text: score }),
      ),
    ),
  );
}

export function errorView(node, error, retry) {
  clear(node);
  node.append(el("div", { class: "card" },
    el("div", { class: "status error", text: error.message || String(error) }),
    retry ? el("button", { class: "ghost block", text: "重试", onclick: retry }) : null,
  ));
}

/* ---------- router ---------- */

const routes = [
  { test: /^\/?$/, run: renderHome },
  { test: /^\/search$/, title: "搜索", run: renderSearch },
  { test: /^\/forum$/, title: "论坛", run: renderForum },
  { test: /^\/forum\/my$/, title: "我的论坛", run: renderMyForum },
  { test: /^\/forum\/([^/]+)$/, title: "讨论详情", thread: true, run: renderForumThread },
  { test: /^\/game\/(.+)$/, title: "游戏详情", run: renderGame },
  { test: /^\/login$/, title: "个人中心", run: renderLogin },
  { test: /^\/comments\/my$/, title: "我的游戏评论", run: renderMyComments },
  { test: /^\/shop$/, title: "积分商城", run: renderShop },
  { test: /^\/appearance$/, title: "个人装扮", run: renderAppearance },
  { test: /^\/announcements$/, title: "公告", run: renderAnnouncements },
  { test: /^\/leaderboard$/, title: "社区排行榜", run: renderLeaderboard },
];

const listPositions = new Map();
let activeHash = null;
let routeRevision = 0;
let lastListHash = null;
if ("scrollRestoration" in history) history.scrollRestoration = "manual";
export function catalogueReturnHash(catalog = "western_slg") {
  return lastListHash || "#/?catalog_id=" + encodeURIComponent(catalog);
}
function isListHash(hash) {
  return /^#\/(?:\?|$)/.test(hash) || /^#\/search(?:\?|$)/.test(hash);
}

function parseHash() {
  const raw = location.hash.replace(/^#/, "") || "/";
  const cut = raw.indexOf("?");
  return {
    path: cut < 0 ? raw : raw.slice(0, cut),
    params: new URLSearchParams(cut < 0 ? "" : raw.slice(cut + 1)),
  };
}

function reportRouteFeature(path) {
  const usageFeature = path.startsWith('/game/') ? 'detail' : path.startsWith('/forum') ? 'forum' : path.startsWith('/shop') ? 'shop' : path === '/' ? 'catalogue' : null;
  if (usageFeature) reportFeature(usageFeature);
}

async function route() {
  const revision = ++routeRevision;
  const nextHash = location.hash || "#/";
  const returning = activeHash?.startsWith("#/game/") && isListHash(nextHash);
  if (activeHash && isListHash(activeHash)) {
    lastListHash = activeHash;
    listPositions.delete(activeHash);
    listPositions.set(activeHash, window.scrollY);
    if (listPositions.size > 30) listPositions.delete(listPositions.keys().next().value);
  }
  activeHash = nextHash;
  const { path, params } = parseHash();
  reportRouteFeature(path);
  const match = routes.map((r) => [r, r.test.exec(path)]).find(([, m]) => m);
  const routeView = el("div", { class: "route-view" });
  clear(view);
  view.append(routeView);
  window.scrollTo(0, 0);
  markTab(path);
  if (!match) {
    renderHome(routeView).catch((error) => errorView(routeView, error, route));
    return;
  }
  const [entry, found] = match;
  try {
    if (entry.title) titleEl.textContent = entry.title;
    const args = entry.thread
      ? [decodeURIComponent(found[1]), params]
      : path.startsWith("/game/")
        ? [decodeURIComponent(found[1]), params]
        : [params];
    await entry.run(routeView, ...args);
    if (revision === routeRevision && returning && listPositions.has(nextHash)) {
      requestAnimationFrame(() => {
        if (revision === routeRevision && routeView.isConnected) window.scrollTo(0, listPositions.get(nextHash));
      });
    }
  } catch (error) {
    errorView(routeView, error, route);
  }
}

function markTab(path) {
  const name = path.startsWith("/forum") ? "forum"
    : path.startsWith("/search") ? "search"
    : path.startsWith("/login") || path.startsWith("/comments/my")
      || path.startsWith("/shop") || path.startsWith("/appearance") ? "me" : "home";
  for (const link of document.querySelectorAll(".tabbar a")) {
    if (link.dataset.tab === name) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }
}

function paintAccount() {
  const account = currentAccount();
  userEl.textContent = account ? "我的账号" : "登录";
}

window.addEventListener("hashchange", route);
window.addEventListener("slg-account-updated", paintAccount);

/* ---------- age gate ---------- */

const AGE_KEY = "slg_web_age_ok";

function ageGate() {
  let acknowledged = false;
  try { acknowledged = localStorage.getItem(AGE_KEY) === "1"; } catch { acknowledged = false; }
  if (acknowledged) { allowUsage(); return; }
  const gate = document.getElementById("age-gate");
  const background = document.querySelectorAll(".topbar, #view, .tabbar");
  background.forEach((node) => { node.inert = true; });
  gate.classList.remove("hidden");
  const enter = document.getElementById("age-enter");
  const leave = document.getElementById("age-leave");
  enter.focus();
  gate.addEventListener("keydown", (event) => {
    if (event.key !== "Tab") return;
    if (event.shiftKey && document.activeElement === enter) {
      event.preventDefault();
      leave.focus();
    } else if (!event.shiftKey && document.activeElement === leave) {
      event.preventDefault();
      enter.focus();
    }
  });
  enter.addEventListener("click", () => {
    try { localStorage.setItem(AGE_KEY, "1"); } catch { /* private mode */ }
    allowUsage();
    reportRouteFeature(parseHash().path);
    gate.classList.add("hidden");
    background.forEach((node) => { node.inert = false; });
    document.querySelector(".brand").focus();
  });
  leave.addEventListener("click", () => {
    location.replace("https://slg-king.com/");
  });
}

/* ---------- boot ---------- */

export { route, paintAccount };

ageGate();
paintAccount();
loadSession().then(paintAccount).catch(() => {});
route();
