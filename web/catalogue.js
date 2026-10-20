import { api, el, clear, tile } from "./app.js";
import { renderAnnouncementSummary } from "./announcements.js";

export const CATALOGUES = [["western_slg", "SLG"], ["dlsite", "DLsite"], ["all", "全部"]];
const PREFERENCE_KEY = "slg_web_catalogue_preference";
export function readCataloguePreference() {
  try {
    const value = sessionStorage.getItem(PREFERENCE_KEY);
    return CATALOGUES.some(([id]) => id === value) ? value : "western_slg";
  } catch { return "western_slg"; }
}
export function rememberCatalogue(id) {
  if (!CATALOGUES.some(([value]) => value === id)) return;
  try { sessionStorage.setItem(PREFERENCE_KEY, id); } catch { /* Optional storage. */ }
}
export function catalogueSorts(id) {
  return [["heat", "热度"], ...(id === "dlsite" ? [] : [["rating", "评分"]]), ["updated", "更新"], ["title", "名称"],
    ...(id === "dlsite" ? [["source_rating", "DLsite 评分"]] : [])];
}
export function catalogueState(params) {
  const raw = params.get("catalog_id") || readCataloguePreference();
  const catalog_id = CATALOGUES.some(([id]) => id === raw) ? raw : "western_slg";
  const unique = (key) => [...new Set(params.getAll(key).filter(Boolean))];
  return {
    catalog_id, q: params.get("q") || "",
    sort: catalogueSorts(catalog_id).some(([key]) => key === params.get("sort")) ? params.get("sort") : "heat",
    page: Math.min(1000, Math.max(1, Number.parseInt(params.get("page") || "1", 10) || 1)),
    tag: unique("tag").slice(0, 8),
    language: catalog_id === "dlsite" ? unique("language") : [],
    work_type: catalog_id === "dlsite" ? unique("work_type") : [],
  };
}
export function catalogueQuery(state, apiQuery = false) {
  const query = new URLSearchParams();
  query.set("catalog_id", state.catalog_id);
  query.set("sort", state.sort);
  query.set("page", String(state.page));
  if (apiQuery) query.set("limit", "20");
  if (state.q) query.set("q", state.q);
  for (const key of ["tag", "language", "work_type"]) {
    if (state.catalog_id !== "dlsite" && key !== "tag") continue;
    for (const value of state[key] || []) query.append(key, value);
  }
  return query;
}
export function catalogueNavigation(state, path) {
  return (overrides) => {
    const next = { ...state, page: 1, ...overrides };
    if (next.catalog_id !== state.catalog_id) {
      next.tag = []; next.language = []; next.work_type = [];
      if (!catalogueSorts(next.catalog_id).some(([key]) => key === next.sort)) next.sort = "heat";
    }
    rememberCatalogue(next.catalog_id);
    location.hash = path + "?" + catalogueQuery(next).toString();
  };
}
export function catalogueSources(state, go) {
  return el("div", { class: "catalogue-sources", role: "group", "aria-label": "目录来源" },
    CATALOGUES.map(([id, name]) => el("button", {
      type: "button", class: id === state.catalog_id ? "source-segment selected" : "source-segment ghost",
      text: name, "aria-pressed": id === state.catalog_id ? "true" : "false",
      onclick: () => { if (id !== state.catalog_id) go({ catalog_id: id }); },
    })));
}
export function cataloguePager(data, state, go) {
  const page = Number(data.page) || state.page;
  const last = Math.max(1, Math.min(1000, Math.ceil((Number(data.total) || 0) / (Number(data.limit) || 20))));
  return el("nav", { class: "pager catalogue-pager", "aria-label": "目录分页" },
    el("button", { type: "button", class: "ghost", text: "上一页", disabled: page <= 1,
      onclick: () => go({ page: page - 1 }) }),
    el("span", { class: "muted small", text: page + " / " + last }),
    el("button", { type: "button", class: "ghost", text: "下一页", disabled: !data.has_more || page >= 1000,
      onclick: () => go({ page: page + 1 }) }));
}
export async function catalogueFilters(node, state, go) {
  if (state.catalog_id !== "dlsite") return;
  const status = el("p", { class: "status small", role: "status", text: "加载分类中…" });
  node.append(status);
  try {
    const data = await api("/catalogue/filters?catalog_id=dlsite");
    if (!node.isConnected) return;
    clear(node);
    for (const [key, label, values] of [["language", "语言", data.languages], ["work_type", "作品类型", data.work_types]]) {
      const options = el("div", { class: "catalogue-filter-options" });
      for (const item of Array.isArray(values) ? values : []) {
        if (!item || typeof item.value !== "string") continue;
        const input = el("input", { type: "checkbox", checked: state[key].includes(item.value) });
        input.addEventListener("change", () => go({ [key]: input.checked
          ? [...state[key], item.value] : state[key].filter((value) => value !== item.value) }));
        options.append(el("label", { class: "catalogue-filter-option" }, input,
          el("span", { text: item.label || item.value })));
      }
      if (options.childElementCount) node.append(el("fieldset", { class: "catalogue-filter" },
        el("legend", { text: label }), options));
    }
  } catch (error) {
    if (node.isConnected) status.textContent = "分类暂时不可用，仍可浏览目录。";
  }
}

export async function renderCatalogue(node, params = new URLSearchParams()) {
  const state = catalogueState(params);
  rememberCatalogue(state.catalog_id);
  const go = catalogueNavigation(state, "#/");
  const announcement = el("section", { class: "home-announcement" });
  const shortcuts = el("nav", { class: "catalogue-shortcuts", "aria-label": "目录快捷入口" },
    el("a", { href: "#/search?" + catalogueQuery(state).toString(), text: "搜索与筛选" }),
    el("a", { href: "#/leaderboard", text: "排行榜" }));
  const select = el("select", { "aria-label": "排序方式" }, catalogueSorts(state.catalog_id).map(([key, label]) =>
    el("option", { value: key, text: label, selected: key === state.sort })));
  select.addEventListener("change", () => go({ sort: select.value }));
  const filters = el("div", { class: "catalogue-filters" });
  const status = el("p", { class: "status", role: "status", text: "加载中…" });
  const results = el("div", {}, el("div", { class: "grid" },
    Array.from({ length: 6 }, () => el("div", { class: "skeleton" }))));
  node.append(announcement, shortcuts, catalogueSources(state, go),
    el("div", { class: "catalogue-toolbar" }, el("label", { class: "muted small", text: "排序" }), select),
    filters, status, results);
  void renderAnnouncementSummary(announcement);
  void catalogueFilters(filters, state, go);
  try {
    const data = await api("/catalogue/search?" + catalogueQuery(state, true));
    if (!node.isConnected) return;
    clear(results);
    status.textContent = "共 " + data.total + " 款 · 第 " + data.page + " 页";
    const items = Array.isArray(data.items) ? data.items : [];
    if (!items.length) {
      results.append(el("p", { class: "empty", text: "暂无符合条件的游戏。" }), cataloguePager(data, state, go));
      return;
    }
    results.append(cataloguePager(data, state, go), el("div", { class: "grid" }, items.map(tile)),
      cataloguePager(data, state, go));
  } catch (error) {
    if (!node.isConnected) return;
    clear(results);
    status.className = "status error";
    status.textContent = error.message || "目录暂时不可用";
    results.append(el("button", { class: "ghost", type: "button", text: "重试", onclick: () => {
      clear(node); void renderCatalogue(node, params);
    } }));
  }
}
