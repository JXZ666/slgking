import { api, el, clear } from "./app.js";
import { currentAccount, loadSession } from "./login.js";

// Only these built-in IDs may affect classes; server supplied assets are never URLs.
export const APPEARANCE_ITEMS = Object.freeze({
  neon_comment_frame: { slot: "comment_frame", name: "霓虹名片框", style: "neon" },
  comment_frame_black_gold_member: { slot: "comment_frame", name: "黑金会员", style: "black-gold" },
  comment_frame_sakura: { slot: "comment_frame", name: "樱花", style: "sakura" },
  comment_frame_mint: { slot: "comment_frame", name: "薄荷", style: "mint" },
  comment_frame_deep_red_velvet: { slot: "comment_frame", name: "深红丝绒", style: "velvet" },
  avatar_frame_cyber_neon: { slot: "avatar_frame", name: "赛博霓虹", style: "cyber" },
  avatar_frame_dark_rose: { slot: "avatar_frame", name: "暗夜黑蔷薇", style: "rose" },
  avatar_frame_gilded_laurel: { slot: "avatar_frame", name: "鎏金桂冠", style: "laurel" },
  avatar_frame_glitch: { slot: "avatar_frame", name: "故障 Glitch", style: "glitch" },
  avatar_frame_startrail: { slot: "avatar_frame", name: "星轨", style: "startrail" },
  avatar_frame_pixel_8bit: { slot: "avatar_frame", name: "像素 8-bit", style: "pixel" },
  avatar_frame_blood_moon: { slot: "avatar_frame", name: "血月", style: "blood-moon" },
});

function known(id, slot) {
  return Object.hasOwn(APPEARANCE_ITEMS, id || "") && APPEARANCE_ITEMS[id].slot === slot
    ? APPEARANCE_ITEMS[id] : null;
}

// Display names from the production slg_titles.TITLES; unknown IDs stay unnamed.
export const TITLE_NAMES = Object.freeze({
  referral_1: "呼朋引伴", referral_3: "引路人", referral_5: "人生导师",
  normal_user: "普通用户", group_friend: "群友", senior_user: "资深用户",
  first_release: "首发用户", mid_autumn_happy: "中秋快乐", national_day_happy: "国庆快乐",
  collector: "收藏家", connoisseur: "鉴赏家", feedback_contributor: "共创者",
  lucky_star: "幸运星", king_of_luck: "幸运之王", butter_king: "黄油之王",
});

function text(value, maximum = 40) {
  return typeof value === "string" ? Array.from(value).slice(0, maximum).join("") : "";
}

export function validPublicProfileId(value) {
  return typeof value === "string" && /^pp_[0-9a-f]{32}$/.test(value);
}

export function publicProfileHref(value) {
  return validPublicProfileId(value) ? "#/profile/" + value : null;
}

export function normalizePublicAuthor(item = {}, { privatePreview = false } = {}) {
  const publicId = [item.author_public_id, item.public_profile_id, item.public_id].find(validPublicProfileId) || "";
  const account = currentAccount();
  const locallyHidden = publicId && publicId === account?.public_profile_id && account?.profile_visible === false;
  const visible = privatePreview || (!locallyHidden && (Object.hasOwn(item, "author_profile_visible")
    ? item.author_profile_visible === true : item.profile_visible === true));
  const title = item.equipped_title;
  const titleId = text(item.author_title_id || (typeof title === "string" ? title : title?.id), 128);
  const titleName = visible ? text(item.author_title_name || title?.name || TITLE_NAMES[titleId], 80) : "";
  const source = item.author_appearances || item.equipped_appearances;
  const appearances = {};
  if (visible && source && typeof source === "object" && !Array.isArray(source)) {
    for (const slot of ["avatar_frame", "comment_frame"]) {
      if (known(source[slot], slot)) appearances[slot] = source[slot];
    }
  }
  return {
    name: text(item.author_name || item.nickname) || "网页用户", publicId, visible,
    titleName, appearances, message: visible ? text(item.profile_message, 20) : "",
  };
}

function removeFrame(card) {
  for (const definition of Object.values(APPEARANCE_ITEMS)) {
    if (definition.slot === "comment_frame") card.classList.remove("author-card-" + definition.style);
  }
}

// Account/privacy changes invalidate already-rendered public snapshots, not private previews.
window.addEventListener("slg-account-updated", () => {
  for (const plate of document.querySelectorAll('[data-public-author="true"]')) {
    plate.querySelectorAll(".author-title,.author-message").forEach((child) => child.remove());
    const avatar = plate.querySelector(".author-avatar");
    if (avatar) {
      avatar.className = "author-avatar";
      avatar.removeAttribute("title");
    }
  }
  document.querySelectorAll('[data-public-author-card="true"]').forEach(removeFrame);
});

export function renderAuthor(item = {}, options = {}) {
  const { name, publicId, titleName, appearances, message } = normalizePublicAuthor(item, options);
  const avatar = el("span", { class: "author-avatar", "aria-hidden": "true", text: Array.from(name)[0] || "用" });
  const decoration = known(appearances.avatar_frame, "avatar_frame");
  if (decoration) {
    avatar.classList.add("author-avatar-" + decoration.style);
    avatar.title = decoration.name;
  }
  const href = publicProfileHref(publicId);
  return el("div", { class: "author-nameplate", "data-public-author": !options.privatePreview ? "true" : null }, avatar,
    el("div", { class: "author-identity" },
      el(href && !options.privatePreview ? "a" : "span", { class: "author-name", href: !options.privatePreview ? href : null, text: name }),
      titleName ? el("span", { class: "author-title", "aria-label": "头衔：" + titleName, text: titleName }) : null,
      message ? el("span", { class: "author-message muted", text: message }) : null));
}

export function applyAuthorAppearance(card, item = {}, options = {}) {
  removeFrame(card);
  const frame = known(normalizePublicAuthor(item, options).appearances.comment_frame, "comment_frame");
  card.classList.add("author-card");
  if (!options.privatePreview) card.setAttribute("data-public-author-card", "true");
  else card.removeAttribute("data-public-author-card");
  if (frame) card.classList.add("author-card-" + frame.style);
  return card;
}

export function renderAppearancePreview(account = {}, item = null) {
  const appearances = { ...(account.author_appearances || account.equipped_appearances || {}) };
  let title = account.equipped_title;
  const definition = item && known(item.id, item.appearance || APPEARANCE_ITEMS[item.id]?.slot);
  if (definition) appearances[definition.slot] = item.id;
  else if (item?.kind === "title") title = { id: item.id, name: item.name };
  const record = { ...account, equipped_title: title, author_appearances: appearances };
  return applyAuthorAppearance(el("article", { class: "appearance-preview", "aria-label": "装扮预览" },
    renderAuthor(record, { privatePreview: true }), el("p", { class: "small muted", text: "这是一条游戏讨论。" })), record, { privatePreview: true });
}

export async function renderAppearance(node) {
  clear(node);
  const page = el("section", { class: "appearance-page" });
  const status = el("div", { class: "status", role: "status", "aria-live": "polite" });
  const content = el("div", { class: "appearance-content" });
  page.append(el("a", { href: "#/login", text: "返回个人中心" }), el("h2", { text: "我的装扮" }), status, content);
  node.append(page);
  status.textContent = "正在确认账号状态…";
  try {
    await loadSession();
  } catch (error) {
    if (!page.isConnected || !node.contains(page)) return;
    clear(content);
    status.replaceChildren(el("span", { text: error.status === 401 ? "登录已过期，请重新登录。" : "账号状态暂时无法确认，请重试。" }),
      el("button", { type: "button", class: "ghost", text: "重试", onclick: () => { void renderAppearance(node); } }));
    return;
  }
  if (!page.isConnected || !node.contains(page)) return;
  const accountId = currentAccount()?.account_id;
  status.textContent = "";
  if (!accountId) {
    status.append("登录后查看你的装扮。", el("a", { href: "#/login", text: "前往登录" }));
    return;
  }
  const active = () => page.isConnected && currentAccount()?.account_id === accountId;
  let account = null;
  let shop = null;
  let loading = 0;
  let writing = false;
  let uncertain = false;
  await refresh();

  async function refresh() {
    const request = ++loading;
    status.textContent = "正在读取装扮…";
    try {
      const [data, catalogue] = await Promise.all([api("/web/account/me"), api("/web/shop")]);
      if (!active() || request !== loading) return false;
      account = data.account || data;
      shop = catalogue;
      uncertain = false;
      status.textContent = "";
      draw();
      return true;
    } catch (error) {
      if (!active() || request !== loading) return false;
      status.replaceChildren(el("span", { text: error.status === 401 ? "登录已过期，请重新登录。" : "装扮暂时无法读取。" }),
        el("button", { class: "ghost", type: "button", text: "重试", onclick: refresh }));
      return false;
    }
  }

  function draw() {
    clear(content);
    content.append(renderAppearancePreview(account));
    const owned = new Set(Array.isArray(account.owned_item_ids) ? account.owned_item_ids : []);
    const titles = Array.isArray(account.titles) ? account.titles.filter((item) => item && typeof item.id === "string") : [];
    const sections = [
      ["title", "头衔", titles],
      ["avatar_frame", "头像框", shop.items?.filter((item) => owned.has(item.id) && known(item.id, "avatar_frame")) || []],
      ["comment_frame", "名片框", shop.items?.filter((item) => owned.has(item.id) && known(item.id, "comment_frame")) || []],
    ];
    for (const [slot, label, items] of sections) {
      const equipped = slot === "title" ? (typeof account.equipped_title === "string" ? account.equipped_title : account.equipped_title?.id) : account.equipped_appearances?.[slot];
      const section = el("section", { class: "appearance-section" }, el("h3", { text: label }));
      const remove = el("button", { class: "ghost", type: "button", text: slot === "title" ? "恢复默认头衔" : "卸下" });
      remove.disabled = writing || uncertain || !equipped || (slot === "title" && equipped === "normal_user");
      remove.onclick = () => equip(slot, "");
      section.append(remove);
      const list = el("div", { class: "appearance-inventory" });
      if (!items.length) list.append(el("p", { class: "muted", text: "暂无可装备的" + label + "。" }));
      for (const item of items) {
        const button = el("button", { type: "button", text: item.id === equipped ? "已装备" : "装备", disabled: writing || uncertain || item.id === equipped });
        button.onclick = () => equip(slot, item.id);
        list.append(el("article", { class: "appearance-item" },
          el("strong", { text: item.name || APPEARANCE_ITEMS[item.id]?.name || "头衔" }),
          el("div", { class: "appearance-actions" },
            el("button", { class: "ghost", type: "button", text: "试搭", onclick: () => content.querySelector(".appearance-preview").replaceWith(renderAppearancePreview(account, { ...item, kind: slot === "title" ? "title" : "decoration", appearance: slot })) }), button)));
      }
      section.append(list);
      content.append(section);
    }
    content.append(el("a", { class: "appearance-shop-link", href: "#/shop", text: "前往积分商城" }));
  }

  async function equip(slot, id) {
    if (writing || uncertain || !active()) return;
    writing = true;
    draw();
    status.textContent = "正在保存装扮…";
    try {
      await api("/web/account/equip", { method: "POST", body: slot === "title" ? { title_id: id } : { appearance_slot: slot, item_id: id } });
      if (!active()) return;
      await loadSession(true);
      if (!active()) return;
      window.dispatchEvent(new CustomEvent("slg-account-updated"));
      const refreshed = await refresh();
      if (active() && refreshed) status.textContent = "装扮已更新。";
      else if (active()) uncertain = true;
    } catch (error) {
      if (!active()) return;
      uncertain = Boolean(error.outcomeUnknown);
      status.replaceChildren(el("span", { text: uncertain ? "保存结果尚未确认，请刷新库存后再操作。" : error.status === 401 ? "登录已过期，请重新登录。" : "装扮保存失败，请稍后重试。" }),
        el("button", { class: "ghost", type: "button", text: "刷新库存", onclick: refresh }));
    } finally {
      writing = false;
      if (active() && account) draw();
    }
  }
}
