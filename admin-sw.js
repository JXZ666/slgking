"use strict";

var CACHE_NAME = "slgking-admin-shell-v1";
var SHELL_ASSETS = [
  "/admin/",
  "/admin-manifest.json",
  "/assets/admin/admin-icon-192.svg",
  "/assets/admin/admin-icon-512.svg"
];

self.addEventListener("install", function (event) {
  event.waitUntil((async function () {
    var cache = await caches.open(CACHE_NAME);
    for (var i = 0; i < SHELL_ASSETS.length; i++) {
      try {
        var request = new Request(SHELL_ASSETS[i], { credentials: "same-origin", cache: "reload" });
        var response = await fetch(request);
        if (response.ok) await cache.put(SHELL_ASSETS[i], response);
      } catch (_) { }
    }
    await self.skipWaiting();
  })());
});

self.addEventListener("activate", function (event) {
  event.waitUntil((async function () {
    var names = await caches.keys();
    await Promise.all(names.filter(function (name) { return name.indexOf("slgking-admin-shell-") === 0 && name !== CACHE_NAME; }).map(function (name) { return caches.delete(name); }));
    await self.clients.claim();
  })());
});

self.addEventListener("fetch", function (event) {
  var request = event.request;
  if (request.method !== "GET") return;
  var url = new URL(request.url);
  if (url.origin !== self.location.origin || url.search || SHELL_ASSETS.indexOf(url.pathname) === -1) return;

  event.respondWith((async function () {
    var cache = await caches.open(CACHE_NAME);
    var cached = await cache.match(url.pathname);
    if (cached && url.pathname !== "/admin/") return cached;
    try {
      var response = await fetch(request);
      if (response.ok) await cache.put(url.pathname, response.clone());
      return response;
    } catch (error) {
      if (cached) return cached;
      throw error;
    }
  })());
});

self.addEventListener("push", function (event) {
  event.waitUntil((async function () {
    var data = {};
    try { data = event.data ? event.data.json() : {}; } catch (_) {
      try { data = { body: event.data ? event.data.text() : "" }; } catch (_) { data = {}; }
    }
    var title = String(data.title || "SLGking 管理通知");
    var options = {
      body: String(data.body || data.message || "收到一条新的管理通知。"),
      icon: "/assets/admin/admin-icon-192.svg",
      badge: "/assets/admin/admin-icon-192.svg",
      data: { url: "/admin/" }
    };
    await self.registration.showNotification(title, options);
  })());
});

self.addEventListener("notificationclick", function (event) {
  event.notification.close();
  event.waitUntil((async function () {
    var target = "/admin/";
    var windows = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
    for (var i = 0; i < windows.length; i++) {
      if (new URL(windows[i].url).pathname.indexOf("/admin/") === 0 && "focus" in windows[i]) return windows[i].focus();
    }
    return self.clients.openWindow(target);
  })());
});
