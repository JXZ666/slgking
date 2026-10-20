// Search list. Server does filtering, sorting and paging; this module only
// builds the query string and renders one page.
import { api, el, clear, tile, tagLabel } from "./app.js";
import { reportFeature } from "./usage.js";
import { catalogueState, catalogueSorts, catalogueNavigation, catalogueQuery,
  catalogueSources, cataloguePager, catalogueFilters, rememberCatalogue } from "./catalogue.js";

export async function renderSearch(node, params) {
  const state = catalogueState(params);
  rememberCatalogue(state.catalog_id);

  const input = el("input", {
    type: "search", placeholder: state.catalog_id === "dlsite" ? "搜作品名、社团或 RJ 编号" : "搜游戏名", value: state.q,
    enterkeyhint: "search", autocomplete: "off",
  });
  input.setAttribute("aria-label", input.placeholder);
  const select = el("select", {},
    catalogueSorts(state.catalog_id).map(([key, label]) =>
      el("option", { value: key, text: label, selected: key === state.sort })));
  select.setAttribute("aria-label", "排序方式");

  const results = el("div", {});
  const status = el("div", { class: "status" });
  status.setAttribute("role", "status");
  status.setAttribute("aria-live", "polite");
  const form = el("form", { class: "toolbar" }, input, select);
  form.setAttribute("role", "search");
  form.setAttribute("aria-label", "游戏目录搜索");

  const tagInput = el("input", {
    class: "tag-search", type: "search", placeholder: "搜索中文或英文标签",
    enterkeyhint: "search", autocomplete: "off",
  });
  tagInput.setAttribute("aria-label", "搜索标签");
  const tagStatus = el("div", { class: "status tag-status", text: "加载标签中…" });
  tagStatus.setAttribute("role", "status");
  tagStatus.setAttribute("aria-live", "polite");
  const tagOptions = el("div", { class: "tag-options" });
  tagOptions.setAttribute("aria-label", "可选标签");
  const selectedTags = el("div", { class: "hidden selected-tags" });
  const tagPicker = el("section", { class: "tag-filter card" },
    el("h3", { text: "按标签筛选" }),
    selectedTags,
    tagInput,
    tagStatus,
    tagOptions,
  );

  const go = catalogueNavigation(state, "#/search");

  let tags = [];
  const renderTagPicker = () => {
    clear(selectedTags);
    selectedTags.className = state.tag.length ? "chips selected-tags" : "hidden selected-tags";
    for (const slug of state.tag) {
      const info = tags.find((item) => item.slug === slug);
      const label = tagLabel(info || slug);
      selectedTags.append(el("button", {
        class: "chip chip-button",
        type: "button",
        text: label + " ×",
        "aria-label": "移除标签：" + label,
        onclick: () => go({ tag: state.tag.filter((item) => item !== slug) }),
      }));
    }
    if (state.tag.length) {
      selectedTags.append(el("button", {
        class: "chip chip-button", type: "button", text: "清除全部",
        "aria-label": "清除全部已选标签",
        onclick: () => go({ tag: [] }),
      }));
    }

    const needle = tagInput.value.trim().toLocaleLowerCase();
    const matchingTags = tags.filter((item) =>
      !needle || tagLabel(item).toLocaleLowerCase().includes(needle) ||
        item.slug.toLocaleLowerCase().includes(needle));
    clear(tagOptions);
    if (!matchingTags.length) {
      if (tags.length) tagOptions.append(el("div", {
        class: "muted small tag-empty", text: "没有匹配的标签。",
      }));
    }
    for (const item of matchingTags) {
      const checkbox = el("input", {
        class: "tag-option-input", type: "checkbox",
        checked: state.tag.includes(item.slug),
      });
      checkbox.addEventListener("change", (event) => {
        const selected = event.currentTarget.checked;
        if (selected && state.tag.length >= 8) {
          event.currentTarget.checked = false;
          tagStatus.textContent = "最多同时选择 8 个标签。";
          tagStatus.className = "status tag-status error";
          return;
        }
        go({ tag: selected
          ? [...state.tag, item.slug]
          : state.tag.filter((slug) => slug !== item.slug) });
      });
      tagOptions.append(el("label", {
        class: state.tag.includes(item.slug) ? "tag-option selected" : "tag-option",
      },
        checkbox,
        el("span", { class: "tag-option-label", text: tagLabel(item) }),
        Number.isFinite(item.count)
          ? el("span", { class: "tag-option-count", text: String(item.count) })
          : null,
      ));
    }
  };

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    reportFeature('search');
    go({ q: input.value.trim() });
  });
  select.addEventListener("change", () => go({ sort: select.value }));
  tagInput.addEventListener("input", renderTagPicker);

  const sourceFilters = el("div", { class: "catalogue-filters" });
  node.append(catalogueSources(state, go), form, sourceFilters, tagPicker);
  node.append(status, results);

  const query = catalogueQuery(state, true);

  clear(results);
  results.append(el("div", { class: "grid" },
    Array.from({ length: 6 }, () => el("div", { class: "skeleton" }))));
  status.textContent = "加载中…";
  status.className = "status";

  const [tagResult, dataResult] = await Promise.allSettled([
    api("/catalogue/tags?catalog_id=" + encodeURIComponent(state.catalog_id)),
    api("/catalogue/search?" + query.toString()),
  ]);
  if (!node.isConnected) return;
  void catalogueFilters(sourceFilters, state, go);
  if (tagResult.status === "fulfilled" && Array.isArray(tagResult.value.tags)) {
    tags = tagResult.value.tags
      .filter((item) => item && typeof item.slug === "string" && item.slug)
      .map((item) => ({
        slug: item.slug,
        label: tagLabel(item),
        count: Number.isFinite(item.count) ? item.count : null,
      }));
    tagStatus.textContent = tags.length ? "选择标签后立即筛选（最多 8 个）。" : "暂无可选标签。";
    tagStatus.className = "status tag-status";
  } else {
    tagStatus.textContent = "标签列表暂时不可用，仍可使用关键词搜索。";
    tagStatus.className = "status tag-status error";
  }
  renderTagPicker();

  if (dataResult.status === "rejected") throw dataResult.reason;
  const data = dataResult.value;
  clear(results);

  if (!data.items.length) {
    status.textContent = "";
    results.append(el("div", { class: "empty", text: "没有匹配的结果，换个关键词试试。" }));
    results.append(cataloguePager(data, state, go));
    return;
  }

  status.textContent = "共 " + data.total + " 条 · 第 " + data.page + " 页";
  results.append(cataloguePager(data, state, go), el("div", { class: "grid" }, data.items.map(tile)));

  const lastPage = Math.max(1, Math.ceil(data.total / data.limit));
  if (lastPage > 1) {
    results.append(cataloguePager(data, state, go));
  }
}
