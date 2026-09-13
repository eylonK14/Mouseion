/* Mouseion PWA shell and Web Share Target staging. */
"use strict";

var CACHE = "mouseion-shell-v2";
var API_CACHE = "mouseion-api-v1";
var SHELL = [
  "/",
  "/share",
  "/offline",
  "/manifest.webmanifest",
  "/static/tailwind.css",
  "/static/vendor/htmx.min.js",
  "/static/app.css",
  "/static/app.js",
  "/static/share.js",
  "/static/pair.js",
  "/static/icon-192.png",
  "/static/icon-512.png",
  "/static/icon.svg"
];

self.addEventListener("install", function (event) {
  event.waitUntil(caches.open(CACHE).then(function (cache) { return cache.addAll(SHELL); }));
  self.skipWaiting();
});

self.addEventListener("activate", function (event) {
  event.waitUntil(
    caches.keys().then(function (keys) {
      return Promise.all(keys.filter(function (key) {
        return key !== CACHE && key !== API_CACHE;
      }).map(function (key) { return caches.delete(key); }));
    }).then(function () { return self.clients.claim(); })
  );
});

function openShareDb() {
  return new Promise(function (resolve, reject) {
    var request = indexedDB.open("mouseion-share", 1);
    request.onupgradeneeded = function () {
      if (!request.result.objectStoreNames.contains("items")) {
        request.result.createObjectStore("items");
      }
    };
    request.onsuccess = function () { resolve(request.result); };
    request.onerror = function () { reject(request.error); };
  });
}

function storeShare(value) {
  return openShareDb().then(function (db) {
    return new Promise(function (resolve, reject) {
      var transaction = db.transaction("items", "readwrite");
      transaction.objectStore("items").put(value, "latest");
      transaction.oncomplete = function () { db.close(); resolve(); };
      transaction.onerror = function () { db.close(); reject(transaction.error); };
    });
  });
}

function receiveShare(request) {
  return request.formData().then(function (form) {
    var files = form.getAll("file").filter(function (item) {
      return item instanceof File && (item.type === "application/pdf" || /\.pdf$/i.test(item.name));
    });
    return storeShare({
      title: String(form.get("title") || ""),
      text: String(form.get("text") || ""),
      url: String(form.get("url") || ""),
      files: files,
      receivedAt: Date.now()
    });
  }).then(function () {
    return Response.redirect(new URL("/share?shared=1", self.location.origin).href, 303);
  });
}

function networkFirst(request) {
  return fetch(request).then(function (response) {
    if (request.method === "GET" && response.ok) {
      caches.open(API_CACHE).then(function (cache) { cache.put(request, response.clone()); });
    }
    return response;
  }).catch(function () {
    return caches.match(request).then(function (cached) {
      return cached || new Response(JSON.stringify({ detail: "Mouseion is offline" }), {
        status: 503,
        headers: { "Content-Type": "application/json" }
      });
    });
  });
}

self.addEventListener("fetch", function (event) {
  var url = new URL(event.request.url);
  if (url.origin !== self.location.origin) return;

  if (event.request.method === "POST" && url.pathname === "/share") {
    event.respondWith(receiveShare(event.request));
    return;
  }
  if (url.pathname.indexOf("/api/") === 0) {
    event.respondWith(networkFirst(event.request));
    return;
  }
  if (event.request.mode === "navigate") {
    event.respondWith(fetch(event.request).catch(function () { return caches.match("/offline"); }));
    return;
  }
  if (event.request.method === "GET") {
    event.respondWith(caches.match(event.request).then(function (cached) {
      return cached || fetch(event.request);
    }));
  }
});
