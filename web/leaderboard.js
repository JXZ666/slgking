import { api, el, clear } from "./app.js";
import { publicProfileHref } from "./author.js";

const BOARDS = {
  points: { name: "云端积分榜", unit: "积分", description: "按当前云端积分排序" },
  wardrobe: { name: "装扮榜", unit: "件", description: "按拥有的头衔、头像框和名片框总数排序" },
};

export async function renderLeaderboard(node, params = new URLSearchParams()) {
  const requested = typeof params.get === "function" ? params.get("board") : params.board;
  const board = Object.hasOwn(BOARDS, requested) ? requested : "points";
  const definition = BOARDS[board];
  clear(node);
  let revision = 0, loaded = false;
  const section = el("section", { class: "leaderboard", "aria-labelledby": "leaderboard-heading" });
  const status = el("p", { class: "leaderboard-status muted", role: "status", "aria-live": "polite" });
  const listing = el("div", { class: "leaderboard-list" });
  const refresh = el("button", {
    type: "button", class: "ghost leaderboard-refresh", title: "刷新排行榜",
    "aria-label": "刷新排行榜", text: "↻",
  });
  const tabs = el("nav", { class: "leaderboard-tabs", "aria-label": "排行榜类型" });
  const links = Object.entries(BOARDS).map(([key, value]) => el("a", {
    href: `#/leaderboard?board=${key}`, text: value.name,
    "aria-current": key === board ? "page" : null,
  }));
  tabs.append(...links);
  tabs.addEventListener("keydown", (event) => {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    const index = links.indexOf(document.activeElement);
    if (index < 0) return;
    event.preventDefault();
    const next = event.key === "Home" ? 0 : event.key === "End" ? links.length - 1
      : (index + (event.key === "ArrowRight" ? 1 : -1) + links.length) % links.length;
    links[next].focus();
  });
  section.append(
    el("header", { class: "leaderboard-heading" }, el("h2", { id: "leaderboard-heading", text: "社区排行榜" }), refresh),
    tabs,
    el("p", { class: "leaderboard-description muted", text: definition.description }),
    el("p", { class: "leaderboard-policy muted", text: "前 30 名 · 不含管理员 · 仅展示昵称和榜单数值" }),
    status, listing,
  );
  node.append(section);
  const alive = () => node.isConnected && node.contains(section);

  function rows(entries) {
    clear(listing);
    if (!entries.length) {
      listing.append(el("p", { class: "leaderboard-empty muted", text: "暂时还没有上榜数据。" }));
      return;
    }
    const table = el("table", { class: "leaderboard-table" });
    table.append(el("caption", { class: "leaderboard-sr-only", text: definition.name }),
      el("colgroup", {}, el("col", { class: "leaderboard-rank-column" }), el("col"), el("col", { class: "leaderboard-score-column" })),
      el("thead", {}, el("tr", {}, el("th", { scope: "col", text: "名次" }), el("th", { scope: "col", text: "昵称" }), el("th", { scope: "col", text: board === "points" ? "积分" : "装扮数" }))));
    const body = el("tbody");
    for (const entry of entries) {
      body.append(el("tr", {},
        el("td", { class: `leaderboard-rank${entry.rank <= 3 ? " leaderboard-top" : ""}`, text: String(entry.rank).padStart(2, "0") }),
        el("td", { class: "leaderboard-nickname" }, entry.profileHref
          ? el("a", { class: "author-name", href: entry.profileHref, text: entry.nickname })
          : el("span", { text: entry.nickname })),
        el("td", { class: "leaderboard-score", text: `${entry.score.toLocaleString("zh-CN")} ${definition.unit}` }),
      ));
    }
    table.append(body); listing.append(table);
  }

  async function load() {
    const ticket = ++revision;
    refresh.disabled = true;
    listing.setAttribute("aria-busy", "true");
    status.classList.remove("error");
    status.textContent = loaded ? "正在刷新排行榜…" : "正在读取排行榜…";
    try {
      const data = await api(`/community/leaderboard?board=${board}&limit=30`);
      if (!alive() || ticket !== revision) return;
      if (!data || data.board !== board || !Array.isArray(data.entries)) throw new Error("排行榜数据格式不正确，请重试。");
      const entries = data.entries.slice(0, 30).map((entry) => {
        if (!entry || !Number.isSafeInteger(entry.rank) || entry.rank < 1 || entry.rank > 30
            || !Number.isSafeInteger(entry.score) || entry.score < 0 || typeof entry.nickname !== "string") {
          throw new Error("排行榜数据格式不正确，请重试。");
        }
        return { rank: entry.rank, nickname: entry.nickname || "匿名用户", score: entry.score,
          profileHref: publicProfileHref(entry.public_profile_id) || publicProfileHref(entry.public_id) };
      });
      rows(entries); loaded = true;
      status.textContent = "";
      refresh.title = "刷新排行榜"; refresh.setAttribute("aria-label", "刷新排行榜");
    } catch (error) {
      if (!alive() || ticket !== revision) return;
      status.classList.add("error");
      status.textContent = loaded ? "刷新失败，保留上次已加载的榜单。请稍后重试。"
        : error.message || "排行榜暂时无法加载，请重试。";
      refresh.title = "重试读取排行榜"; refresh.setAttribute("aria-label", "重试读取排行榜");
    } finally {
      if (alive() && ticket === revision) {
        refresh.disabled = false; listing.setAttribute("aria-busy", "false");
      }
    }
  }
  refresh.onclick = () => { if (!refresh.disabled) void load(); };
  await load();
}
