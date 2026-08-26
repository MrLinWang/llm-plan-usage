/* LLM 用量 PWA service worker —— 修改本文件时必须递增 CACHE 版本号 */
var CACHE = "llm-usage-v1";
var SHELL = ["/", "/login", "/manifest.webmanifest",
             "/icons/icon-192.png", "/icons/icon-512.png"];

self.addEventListener("install", function (e) {
  e.waitUntil(caches.open(CACHE).then(function (c) { return c.addAll(SHELL); })
    .then(function () { return self.skipWaiting(); }));
});

self.addEventListener("activate", function (e) {
  e.waitUntil(caches.keys().then(function (keys) {
    return Promise.all(keys.map(function (k) {
      return k !== CACHE ? caches.delete(k) : null;
    }));
  }).then(function () { return self.clients.claim(); }));
});

self.addEventListener("fetch", function (e) {
  var req = e.request;
  if (req.method !== "GET") return;
  var url = new URL(req.url);
  if (url.origin !== location.origin) return;

  if (req.mode === "navigate") {
    // 页面导航:网络优先,成功且非重定向才入缓存;
    // 未登录访问 "/" 会 302 到 /login(redirected=true),不能缓存到 "/" 键下。
    e.respondWith(fetch(req).then(function (resp) {
      if (resp.ok && !resp.redirected) {
        var copy = resp.clone();
        caches.open(CACHE).then(function (c) { return c.put(req, copy); });
      }
      return resp;
    }).catch(function () {
      return caches.match(req).then(function (hit) {
        return hit || caches.match("/") || new Response("离线", { status: 503 });
      });
    }));
    return;
  }

  if (url.pathname.startsWith("/api/")) {
    // API GET:网络优先,失败回退最近一次成功响应(离线显示上次数据)。
    // 只缓存 resp.ok 的 GET;POST(/api/refresh 等)在上方直接放行。
    e.respondWith(fetch(req).then(function (resp) {
      if (resp.ok) {
        var copy = resp.clone();
        caches.open(CACHE).then(function (c) { return c.put(req, copy); });
      }
      return resp;
    }).catch(function () {
      return caches.match(req).then(function (hit) {
        return hit || Response.error();
      });
    }));
  }
  // 其余同源 GET(无此类请求时为前向空档):直连不放缓存。
});
