// Game detail. Describes the game itself only: the server sends no download,
// mirror or group-join pointer, and this page never invents one (see
// mem-compliance.md — 站内零资源链接).
import { api, el, cover, tagLabel, clientGuide, catalogueReturnHash } from "./app.js";
import { renderGameComments } from "./comments.js";

async function loadTagLabels(catalog) {
  try {
    const data = await api("/catalogue/tags?catalog_id=" + encodeURIComponent(catalog));
    if (!Array.isArray(data.tags)) return new Map();
    return new Map(data.tags
      .filter((item) => item && typeof item.slug === "string" && item.slug)
      .map((item) => [item.slug,
        typeof item.label === "string" && item.label ? item.label : item.slug]));
  } catch {
    return new Map();
  }
}

function safeSource(source, game) {
  const hosts = { dikgames: ["dikgames.com", "www.dikgames.com"],
    f95zone: ["f95zone.to", "www.f95zone.to", "f95zone.to.it"], dlsite: ["www.dlsite.com"] };
  if (!source || typeof source.source_url !== "string" || /[\s\\]/.test(source.source_url)) return false;
  try {
    const url = new URL(source.source_url);
    if (url.protocol !== "https:" || url.username || url.password || (url.port && url.port !== "443")
      || !hosts[source.source_id]?.includes(url.hostname)) return false;
    const path = decodeURIComponent(url.pathname).toLowerCase();
    if (/\.(zip|rar|7z|exe|torrent|iso|apk|msi)$/.test(path)
      || /\/(downloads?|attachments?)(?:\/|$)/.test(path)) return false;
    if (source.source_id === "dlsite") {
      // RJ works live under /maniax/ and VJ works under /pro/, and both pages
      // end in /product_id/<编号>.html. The section is part of the identity, so
      // a mismatched link (RJ on /pro/ or VJ on /maniax/) is rejected instead of
      // accepted on the id alone.
      const match = /^dlsite-((?:rj|vj)\d{6,10})$/i.exec(game.slug);
      if (!match) return false;
      const id = match[1].toLowerCase();
      const section = id.startsWith("rj") ? "maniax" : "pro";
      return new RegExp("^/" + section + "/work/=/product_id/" + id + "\\.html(?:/|$)").test(path);
    }
    return url.pathname !== "/";
  } catch { return false; }
}

export async function renderGame(node, slug, params = new URLSearchParams()) {
  const catalog = params.get("catalog_id");
  const data = await api("/catalogue/game/" + encodeURIComponent(slug)
    + (catalog ? "?catalog_id=" + encodeURIComponent(catalog) : ""));
  const game = data.game;
  const sourceCatalog = game.catalog_id || "western_slg";
  const tagLabels = await loadTagLabels(sourceCatalog);
  node.append(el("a", { class: "detail-back", href: catalogueReturnHash(sourceCatalog), text: "返回目录" }));

  const facts = [];
  if (game.developer) facts.push("开发 · " + game.developer);
  if (game.engine) facts.push("引擎 · " + game.engine);
  if (game.version) facts.push("版本 " + game.version);
  if (game.updated) facts.push("更新 " + game.updated);
  if (game.rating !== null && game.rating !== undefined) facts.push("评分 " + game.rating);
  if (sourceCatalog === "dlsite") {
    const info = game.facts || {};
    for (const [label, key] of [["商品编号", "product_id"], ["发布日期", "release_date"],
      ["作品形式", "work_type"], ["年龄分级", "age_rating"]]) facts.push(label + " · " + (info[key] || "未知"));
    facts.push("语言 · " + (Array.isArray(info.languages) && info.languages.length ? info.languages.join("、") : "未知"));
    facts.push("DLsite 评分 · " + (info.rating_value == null ? "未知"
      : info.rating_value + "/" + (info.rating_scale_max ?? "未知满分")
        + (info.rating_count == null ? "" : "（" + info.rating_count + " 人）")));
  }
  const sources = (game.sources || []).filter(source => safeSource(source, game));
  const sourceNames = { dikgames: "Dikgames 资料页", f95zone: "F95 资料页", dlsite: "DLsite 官方商品页" };

  node.append(el("div", { class: "card" },
    el("div", { class: "hero" },
      cover(game.cover, "shot", game.cover_url),
      el("div", {},
        el("h2", { text: game.title || game.slug }),
        el("div", { class: "muted small", text: game.developer || "未知开发" }),
        el("nav", { class: "detail-links", "aria-label": "原站与官方商品页" },
          sources.length ? sources.map(source => el("a", {
            class: "detail-source-link", href: source.source_url, target: "_blank",
            rel: "noopener noreferrer nofollow", text: sourceNames[source.source_id],
          })) : el("span", { class: "muted small", text: "暂无可靠的原站链接。" })),
        facts.length ? el("div", { class: "kv" }, facts.map((text) => el("span", { text }))) : null,
      ),
    ),
  ));

  if (game.tags && game.tags.length) {
    node.append(el("div", { class: "card" },
      el("h3", { text: "标签" }),
      el("div", { class: "chips" }, game.tags.map((tag) =>
        el("a", {
          class: "chip", text: tagLabel(tag, tagLabels),
          href: "#/search?catalog_id=" + encodeURIComponent(sourceCatalog) + "&tag=" + encodeURIComponent(tag),
        }))),
    ));
  }

  if (game.overview) {
    node.append(el("div", { class: "card" },
      el("h3", { text: "简介" }),
      // textContent via el(): the overview is scraped third-party text.
      el("div", { class: "overview", text: game.overview }),
    ));
  }

  const commentsSlot = el("div", {});
  node.append(commentsSlot);
  node.append(clientGuide("game"));

  node.append(el("div", { class: "card" },
    el("h3", { text: "资料出处" }),
    sources.length
      ? el("div", { class: "sources" }, sources.map(source =>
          el("p", { class: "muted small", text: source.source_title || sourceNames[source.source_id] })))
      : el("p", { class: "muted small", text: "无外部来源记录。" }),
    el("p", { class: "muted small", text: "本页只提供游戏资料检索，不提供任何获取渠道。" }),
  ));
  await renderGameComments(commentsSlot, game.slug);
}
