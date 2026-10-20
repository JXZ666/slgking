import { api, el, clear } from "./app.js";
import { currentAccount } from "./login.js";
import { renderAuthor, applyAuthorAppearance, normalizePublicAuthor, validPublicProfileId } from "./author.js";

let currentView = null;

window.addEventListener("slg-account-updated", () => {
  if (currentView?.active()) currentView.reload();
  else currentView = null;
});

export async function renderPublicProfile(node, publicId) {
  clear(node);
  const page = el("section", { class: "public-profile", "aria-labelledby": "public-profile-title" });
  const status = el("p", { class: "public-profile-status muted", role: "status", "aria-live": "polite" });
  const content = el("div", { class: "public-profile-content" });
  const retry = el("button", { class: "ghost", type: "button", text: "重新读取" });
  page.append(el("a", { class: "public-profile-back", href: "#/", text: "返回首页" }),
    el("header", { class: "public-profile-heading" }, el("h2", { id: "public-profile-title", text: "公开名片" }), retry), status, content);
  node.append(page);
  let revision = 0;
  const active = () => page.isConnected && node.contains(page);
  const identity = () => currentAccount()?.account_id || null;
  const reload = () => { void load(); };
  retry.onclick = reload;
  if (!validPublicProfileId(publicId)) {
    currentView = null;
    status.textContent = "公开名片链接无效。";
    retry.hidden = true;
    return;
  }
  currentView = { active, reload };
  await load();

  async function load() {
    const ticket = ++revision, accountId = identity();
    clear(content);
    retry.disabled = true;
    content.setAttribute("aria-busy", "true");
    status.textContent = "正在读取公开名片…";
    const applicable = () => active() && ticket === revision && accountId === identity();
    try {
      const data = await api("/community/profile?public_id=" + encodeURIComponent(publicId));
      if (!applicable()) return;
      const profile = data?.profile;
      if (!profile || profile.public_id !== publicId || typeof profile.profile_visible !== "boolean") {
        throw new Error("公开名片数据尚未确认，请重新读取。");
      }
      // Explicit projection: unrelated server fields never enter the public DOM.
      const record = { author_public_id: publicId,
        nickname: typeof profile.nickname === "string" ? profile.nickname : "匿名用户",
        author_profile_visible: profile.profile_visible };
      if (profile.profile_visible) {
        record.equipped_title = typeof profile.equipped_title === "string" ? profile.equipped_title : "";
        record.equipped_appearances = profile.equipped_appearances;
        record.profile_message = profile.profile_message;
      }
      const author = normalizePublicAuthor(record);
      const card = applyAuthorAppearance(el("article", { class: "public-profile-card" }, renderAuthor(record)), record);
      if (!profile.profile_visible) {
        card.append(el("p", { class: "muted", text: "对方已隐藏装扮和寄语。" }));
      } else if (!author.message && !author.titleName && !Object.keys(author.appearances).length) {
        card.append(el("p", { class: "muted", text: "暂无公开装扮或寄语。" }));
      }
      content.append(card);
      status.textContent = "";
    } catch (error) {
      if (!applicable()) return;
      clear(content);
      status.textContent = error.status === 404 ? "这张公开名片暂时不可用。"
        : error.status === 429 ? "访问较多，请稍后重新读取。"
          : error.message || "公开名片暂时无法读取，请重试。";
    } finally {
      if (applicable()) {
        content.setAttribute("aria-busy", "false");
        retry.disabled = false;
      }
    }
  }
}
