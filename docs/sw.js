/* H bank 서비스 워커: 앱 설치 + 오프라인에서도 마지막으로 받은 화면·데이터를 보여 준다.
   - 화면(index.html)과 데이터(data/*.json): 네트워크 먼저, 실패하면 저장해 둔 것
   - 아이콘 등 나머지 같은 사이트 파일: 저장해 둔 것 먼저
   화면 구조를 바꿔 배포할 때 VERSION을 올리면 예전 저장본이 지워진다. */
const VERSION = "hbank-v4";
const SHELL = ["./", "./manifest.webmanifest", "./icons/icon-192.png", "./icons/icon-512.png"];

self.addEventListener("install", e => {
  e.waitUntil(caches.open(VERSION).then(c => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", e => {
  e.waitUntil(caches.keys()
    .then(keys => Promise.all(keys.filter(k => k !== VERSION).map(k => caches.delete(k))))
    .then(() => self.clients.claim()));
});

self.addEventListener("fetch", e => {
  const req = e.request, url = new URL(req.url);
  if (req.method !== "GET" || url.origin !== location.origin) return;  // 글꼴 CDN 등 외부 요청은 그대로

  const isData = url.pathname.includes("/data/") && url.pathname.endsWith(".json");
  if (req.mode === "navigate" || isData) {
    // ?v=시각 같은 쿼리는 빼고 저장해서 같은 파일로 취급
    const key = isData ? url.origin + url.pathname : "./";
    e.respondWith(fetch(req).then(res => {
      if (res.ok) { const copy = res.clone(); caches.open(VERSION).then(c => c.put(key, copy)); }
      return res;
    }).catch(() => caches.match(key)));
    return;
  }
  e.respondWith(caches.match(req).then(hit => hit || fetch(req).then(res => {
    if (res.ok) { const copy = res.clone(); caches.open(VERSION).then(c => c.put(req, copy)); }
    return res;
  })));
});
