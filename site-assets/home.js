/* Core content, FAQ and downloads work without JavaScript. */
(() => {
  "use strict";
  const root = document.documentElement;
  const button = document.querySelector(".theme-toggle");
  const system = window.matchMedia("(prefers-color-scheme: dark)");
  const themes = ["system", "light", "dark"];
  const labels = { system: "跟随系统", light: "浅色", dark: "暗色" };
  let selected = "system";
  try {
    const stored = localStorage.getItem("slgking-home-theme");
    if (themes.includes(stored)) selected = stored;
  } catch (_) {}
  function applyTheme() {
    if (selected === "system") root.removeAttribute("data-theme");
    else root.dataset.theme = selected;
    document.querySelector('meta[name="theme-color"]').content =
      selected === "dark" || (selected === "system" && system.matches)
        ? "#151219"
        : "#f7f7f9";
    button.textContent = `主题：${labels[selected]}`;
    button.setAttribute(
      "aria-label",
      `当前主题：${labels[selected]}；切换为${labels[themes[(themes.indexOf(selected) + 1) % themes.length]]}`,
    );
  }
  applyTheme();
  button.hidden = false;
  button.addEventListener("click", () => {
    selected = themes[(themes.indexOf(selected) + 1) % themes.length];
    applyTheme();
    try {
      localStorage.setItem("slgking-home-theme", selected);
    } catch (_) {}
  });
  if (system.addEventListener) system.addEventListener("change", applyTheme);
  for (const image of document.querySelectorAll(".image-frame img")) {
    function failure() {
      const frame = image.closest(".image-frame");
      frame.classList.add("failed");
      frame.querySelector(".image-fallback").hidden = false;
    }
    image.addEventListener("error", failure);
    if (image.complete && image.naturalWidth === 0) failure();
  }
  const copy = document.querySelector("[data-copy]");
  const status = document.querySelector("#copy-status");
  copy.hidden = false;
  copy.addEventListener("click", async () => {
    copy.disabled = true;
    status.textContent = "";
    try {
      if (!navigator.clipboard || !window.isSecureContext)
        throw new Error("Clipboard unavailable");
      await navigator.clipboard.writeText(copy.dataset.copy);
      status.textContent = "群号已复制，可在 QQ 中搜索加入。";
    } catch (_) {
      status.textContent = `复制失败，请手动选择并复制群号 ${copy.dataset.copy}。`;
    } finally {
      copy.disabled = false;
    }
  });
})();
