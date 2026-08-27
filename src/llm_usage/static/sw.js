var CACHE_PREFIX = "llm-usage-";
var CACHE = "llm-usage-v2";
var SHELL = ["/", "/login", "/manifest.webmanifest",
             "/icons/icon-192.png", "/icons/icon-512.png"];

self.addEventListener("install", function (e) {
  e.waitUntil(caches.open(CACHE).then(function (c) { return c.addAll(SHELL); })
    .then(function () { return self.skipWaiting(); }));
});

self.addEventListener("activate", function (e) {
  e.waitUntil(caches.keys().then(function (keys) {
    return Promise.all(keys.map(function (k) {
      // 只清理本应用前缀的旧缓存(llm-usage-v1 可能已含认证 API 响应),
      // 不动同源其它应用的 Cache Storage 条目。
      return k !== CACHE && k.indexOf(CACHE_PREFIX) === 0 ? caches.delete(k) : null;
    }));
  }).then(function () { return self.clients.claim(); }));
});

self.addEventListener("fetch", function (e) {
  var req = e.request;
  if (req.method !== "GET") return;
  var url = new URL(req.url);
  if (url.origin !== location.origin) return;

  if (url.pathname.startsWith("/api/")) {
    // API 响应是用户私有的:网络直连,绝不入缓存、绝不回退缓存,
    // 离线时读取失败而不是返回其它会话的数据。
    // 先于导航分支判断:直接导航到 /api/* 也不得进入导航缓存。
    e.respondWith(fetch(req));
    return;
  }

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
  // 其余同源 GET(无此类请求时为前向空档):直连不放缓存。
});
