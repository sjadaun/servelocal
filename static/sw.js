// sw.js -- ServeLocal service worker.
// Required for Web Push on any platform, and required by iOS Safari
// specifically before it will even allow "Add to Home Screen" to behave
// like an installable app capable of receiving push notifications.

self.addEventListener("install", (event) => {
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(self.clients.claim());
});

self.addEventListener("push", (event) => {
  let payload = { title: "ServeLocal", body: "" };
  try {
    payload = event.data.json();
  } catch (e) {
    if (event.data) payload.body = event.data.text();
  }

  const options = {
    body: payload.body || "",
    tag: payload.tag || undefined,
    // renotify only makes sense when tag is set; avoids silently replacing
    // a still-relevant earlier notification without alerting the user
    renotify: !!payload.tag,
    data: { url: payload.url || "/" },
    icon: "/static/icon-192.png",
    badge: "/static/icon-192.png",
  };

  event.waitUntil(self.registration.showNotification(payload.title || "ServeLocal", options));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const url = (event.notification.data && event.notification.data.url) || "/";

  event.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((clients) => {
      for (const client of clients) {
        if (client.url.includes(url) && "focus" in client) {
          return client.focus();
        }
      }
      if (self.clients.openWindow) {
        return self.clients.openWindow(url);
      }
    })
  );
});
