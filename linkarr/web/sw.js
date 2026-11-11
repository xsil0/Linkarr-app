/* Linkarr service worker: just enough for "install app" and the Share menu. No offline cache:
   everything Linkarr shows is live, and nothing private should sit in a browser cache. */
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (e) => e.waitUntil(self.clients.claim()));
self.addEventListener("fetch", () => {});
