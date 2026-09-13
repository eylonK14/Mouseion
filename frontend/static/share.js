(function () {
  "use strict";

  var shared = null;
  var progress = {
    queued: 8,
    downloading: 20,
    extracting: 38,
    indexing: 56,
    tagging: 73,
    embedding: 90,
    done: 100,
    failed: 100
  };

  function $(id) { return document.getElementById(id); }

  function openDb() {
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

  function readShared() {
    return openDb().then(function (db) {
      return new Promise(function (resolve, reject) {
        var transaction = db.transaction("items", "readonly");
        var request = transaction.objectStore("items").get("latest");
        request.onsuccess = function () { db.close(); resolve(request.result || null); };
        request.onerror = function () { db.close(); reject(request.error); };
      });
    });
  }

  function clearShared() {
    return openDb().then(function (db) {
      return new Promise(function (resolve) {
        var transaction = db.transaction("items", "readwrite");
        transaction.objectStore("items").delete("latest");
        transaction.oncomplete = function () { db.close(); resolve(); };
      });
    });
  }

  function fail(message) {
    $("share-error").textContent = message;
    $("share-error").classList.remove("hidden");
    $("share-add").disabled = false;
  }

  function render(value) {
    shared = value;
    $("share-empty").classList.add("hidden");
    if (!value || (!(value.files || []).length && !value.url && !value.text && !value.title)) {
      $("share-empty").textContent = "Nothing is staged. Share a paper or PDF to Mouseion and try again.";
      $("share-empty").classList.remove("hidden");
      return;
    }
    $("share-preview").classList.remove("hidden");
    if ((value.files || []).length) {
      $("share-file-row").classList.remove("hidden");
      $("share-file").textContent = value.files.map(function (file) {
        return file.name + " · " + Math.max(1, Math.round(file.size / 1024)) + " KB";
      }).join("\n");
    }
    var text = [value.title, value.text, value.url].filter(Boolean).join("\n");
    if (text) {
      $("share-url-row").classList.remove("hidden");
      $("share-url").textContent = text;
    }
    $("share-add").disabled = false;
  }

  function update(job) {
    $("share-progress").classList.remove("hidden");
    $("share-progress-label").textContent = job.state === "done" ? "Paper added" : "Adding paper…";
    $("share-progress-state").textContent = job.state;
    $("share-progress-bar").style.width = (progress[job.state] || 8) + "%";
  }

  function follow(accepted, label) {
    $("share-progress").classList.remove("hidden");
    window.Mouseion.watchJob(accepted.job_id, label, {
      onUpdate: update,
      onDone: function (job) {
        clearShared().then(function () {
          if (job.paper_id) window.location.replace("/papers/" + job.paper_id);
          else window.location.replace("/");
        });
      },
      onFailed: function (job) { fail(job.error || "Ingest failed. You can try again."); },
      onError: function (error) { fail(error.message); }
    });
  }

  function submit() {
    if (!shared || !window.Mouseion.token()) {
      window.Mouseion.openGate("Pair this device or enter the API token before adding a paper.");
      return;
    }
    $("share-add").disabled = true;
    $("share-error").classList.add("hidden");
    var files = shared.files || [];
    if (files.length) {
      var form = new FormData();
      form.append("file", files[0], files[0].name);
      window.Mouseion.api("/api/papers", { method: "POST", body: form }).then(function (accepted) {
        follow(accepted, files[0].name);
      }).catch(function (error) { fail(error.message); });
      return;
    }
    window.Mouseion.api("/api/share/extract", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url: shared.url || "", text: shared.text || "", title: shared.title || "" })
    }).then(function (resolved) {
      return window.Mouseion.api("/api/papers", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url: resolved.url })
      }).then(function (accepted) { follow(accepted, resolved.url); });
    }).catch(function (error) { fail(error.message); });
  }

  document.addEventListener("DOMContentLoaded", function () {
    $("share-add").addEventListener("click", submit);
    readShared().then(render).catch(function (error) { fail("Could not read the shared item: " + error.message); });
    if (navigator.storage && navigator.storage.persist) navigator.storage.persist();
  });
})();
