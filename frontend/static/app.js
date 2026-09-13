/* Mouseion — Phase 2 client.
 *
 * HTMX does the fetching and swapping. This file owns the four things it
 * cannot: the bearer token, ingest jobs (a poll loop, not a swap), the PDF
 * viewer (which needs an authenticated fetch), and drag-to-merge.
 *
 * Loaded synchronously in <head> so the htmx:configRequest hook is registered
 * before htmx issues its first `load`-triggered request.
 */
(function () {
  "use strict";

  var TOKEN_KEY = "mouseion.token";

  var M = {
    token: function () {
      return localStorage.getItem(TOKEN_KEY) || "";
    },
    setToken: function (value) {
      localStorage.setItem(TOKEN_KEY, (value || "").trim());
    },
  };
  window.Mouseion = M;

  if ("serviceWorker" in navigator) {
    window.addEventListener("load", function () {
      navigator.serviceWorker.register("/sw.js", { scope: "/" }).catch(function (err) {
        console.warn("Mouseion service worker registration failed", err);
      });
    });
  }

  function $(id) {
    return document.getElementById(id);
  }
  function on(el, event, fn) {
    if (el) el.addEventListener(event, fn);
  }

  // ---------------------------------------------------------------- auth
  // Every /ui/* fragment and /api/* call is protected; the shells are not.
  document.addEventListener("htmx:configRequest", function (evt) {
    var token = M.token();
    if (token) evt.detail.headers["Authorization"] = "Bearer " + token;
  });

  // With no token, cancel rather than firing a request we know returns 401 —
  // otherwise the first paint is a wall of error fragments.
  document.addEventListener("htmx:beforeRequest", function (evt) {
    if (!M.token()) {
      evt.preventDefault();
      M.openGate();
    }
  });

  document.addEventListener("htmx:responseError", function (evt) {
    var status = evt.detail.xhr.status;
    if (status === 401 || status === 503) {
      M.openGate(status === 503 ? "The server has no API_TOKEN configured." : "That token was rejected.");
      return;
    }
    var detail = "";
    try {
      detail = JSON.parse(evt.detail.xhr.responseText).detail || "";
    } catch (e) {
      detail = evt.detail.xhr.statusText || "";
    }
    M.toast(status + " — " + detail, "error");
  });

  document.addEventListener("htmx:sendError", function () {
    M.toast("Could not reach the server.", "error");
  });

  M.openGate = function (message) {
    var gate = $("token-gate");
    if (!gate) return;
    if (message) {
      var note = $("token-gate-note");
      if (note) {
        note.textContent = message;
        note.classList.remove("hidden");
      }
    }
    if (!gate.open) gate.showModal();
    var input = $("token-input");
    if (input) {
      input.value = M.token();
      input.focus();
    }
  };

  /** Re-run every fragment that waits on `mouseion:auth`. */
  M.authChanged = function () {
    if (window.htmx) {
      htmx.trigger(document.body, "mouseion:auth");
      htmx.trigger(document.body, "mouseion:tree");
    }
  };

  /** Re-run the result list (filters changed, or an ingest finished). */
  M.refresh = function () {
    if (window.htmx) htmx.trigger(document.body, "mouseion:refresh");
  };

  /** Re-run the topic sidebar (counts changed). */
  M.refreshTree = function () {
    if (window.htmx) htmx.trigger(document.body, "mouseion:tree");
  };

  // --------------------------------------------------------------- toasts
  M.toast = function (message, kind, opts) {
    var host = $("toasts");
    if (!host) return null;
    var node = document.createElement("div");
    var tone =
      kind === "error"
        ? "border-red-500/40 text-red-200"
        : kind === "ok"
          ? "border-emerald-500/40 text-emerald-200"
          : "border-neutral-700 text-neutral-200";
    node.className =
      "panel px-3 py-2 text-[13px] shadow-lg border " + tone + " flex items-start gap-2";
    node.innerHTML = '<span class="flex-1"></span>';
    node.firstChild.textContent = message;
    host.appendChild(node);
    if (!opts || !opts.sticky) {
      setTimeout(function () {
        node.remove();
      }, (opts && opts.ms) || 4500);
    }
    return node;
  };

  function setToastText(node, message, kind) {
    if (!node) return;
    node.firstChild.textContent = message;
    node.className = node.className.replace(
      /border-(red|emerald|neutral)-\S+ text-\S+/,
      kind === "error"
        ? "border-red-500/40 text-red-200"
        : kind === "ok"
          ? "border-emerald-500/40 text-emerald-200"
          : "border-neutral-700 text-neutral-200"
    );
  }

  // ------------------------------------------------------------------ api
  function api(path, options) {
    options = options || {};
    var raw = Boolean(options.raw);
    var headers = Object.assign({}, options.headers || {}, {
      Authorization: "Bearer " + M.token(),
    });
    var fetchOptions = Object.assign({}, options, { headers: headers });
    delete fetchOptions.raw;
    return fetch(path, fetchOptions).then(function (res) {
      if (!res.ok) {
        return res.text().then(function (body) {
          var detail = body;
          try {
            detail = JSON.parse(body).detail || body;
          } catch (e) {
            /* keep the raw body */
          }
          throw new Error(res.status + " — " + String(detail).slice(0, 200));
        });
      }
      if (raw) return res;
      return res.status === 204 ? null : res.json();
    });
  }
  M.api = api;

  // ----------------------------------------------------------- grounded QA
  M.openPaperChat = function (button) {
    try {
      var url = new URL(button.dataset.openwebuiUrl, window.location.href);
      url.searchParams.set("model", "single_paper");
      // Open WebUI auto-submits `q`; send only the scope lock so the pipe can
      // select the paper without inventing the user's first real question.
      url.searchParams.set("q", button.dataset.chatPrefix);
      window.open(url.toString(), "_blank", "noopener,noreferrer");
    } catch (err) {
      M.toast("Open WebUI URL is invalid: " + err.message, "error");
    }
  };

  M.openTestChat = function (button) {
    try {
      var url = new URL(button.dataset.openwebuiUrl, window.location.href);
      url.searchParams.set("model", "test_me");
      url.searchParams.set("q", button.dataset.chatPrefix);
      window.open(url.toString(), "_blank", "noopener,noreferrer");
    } catch (err) {
      M.toast("Open WebUI URL is invalid: " + err.message, "error");
    }
  };

  function parseSseBlock(block) {
    var event = "message";
    var data = "";
    block.split(/\r?\n/).forEach(function (line) {
      if (line.indexOf("event:") === 0) event = line.slice(6).trim();
      if (line.indexOf("data:") === 0) data += line.slice(5).trim();
    });
    if (!data) return null;
    try {
      return { event: event, data: JSON.parse(data) };
    } catch (err) {
      return null;
    }
  }

  async function streamQuickAnswer(form) {
    var input = form.elements.question;
    var button = form.querySelector('button[type="submit"]');
    var answer = form.parentElement.querySelector("[data-quick-qa-answer]");
    var sources = form.parentElement.querySelector("[data-quick-qa-sources]");
    var question = (input.value || "").trim();
    if (!question) return;

    button.disabled = true;
    answer.classList.remove("hidden");
    answer.textContent = "Looking through the paper…";
    sources.classList.add("hidden");
    var accumulated = "";
    try {
      var response = await api("/api/qa/paper/" + form.dataset.paperId, {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
        body: JSON.stringify({ question: question }),
        raw: true,
      });
      if (!response.body) throw new Error("The browser did not provide a response stream.");
      var reader = response.body.getReader();
      var decoder = new TextDecoder();
      var buffer = "";
      while (true) {
        var chunk = await reader.read();
        buffer += decoder.decode(chunk.value || new Uint8Array(), { stream: !chunk.done });
        var blocks = buffer.split(/\r?\n\r?\n/);
        buffer = blocks.pop() || "";
        blocks.forEach(function (block) {
          var parsed = parseSseBlock(block);
          if (!parsed) return;
          if (parsed.event === "token") {
            accumulated += parsed.data.text || "";
            answer.innerHTML = M.renderMarkdown(accumulated);
          } else if (parsed.event === "done") {
            var papers = parsed.data.consulted_papers || [];
            var labels = papers.map(function (paper) {
              return paper.title + " — " + (paper.sections || []).join(", ");
            });
            if (parsed.data.citation_warnings && parsed.data.citation_warnings.length) {
              labels.push("Citation warning: " + parsed.data.citation_warnings.join(", "));
            }
            sources.textContent = labels.join(" · ");
            sources.classList.toggle("hidden", !labels.length);
          } else if (parsed.event === "error") {
            throw new Error(parsed.data.detail || "QA stream failed");
          }
        });
        if (chunk.done) break;
      }
      if (!accumulated) answer.textContent = "No answer was returned.";
    } catch (err) {
      answer.textContent = err.message;
      M.toast("Quick question failed: " + err.message, "error");
    } finally {
      button.disabled = false;
    }
  }

  document.addEventListener("click", function (evt) {
    var button = evt.target.closest("[data-open-paper-chat]");
    if (button) M.openPaperChat(button);
    var testButton = evt.target.closest("[data-open-test-chat]");
    if (testButton) M.openTestChat(testButton);
    var pageLink = evt.target.closest("[data-pdf-page]");
    if (pageLink) M.jumpToPdf(pageLink.dataset.paperId, pageLink.dataset.pdfPage);
  });

  document.addEventListener("submit", function (evt) {
    var form = evt.target.closest("[data-quick-qa-form]");
    if (!form) return;
    evt.preventDefault();
    streamQuickAnswer(form);
  });

  // --------------------------------------------------------------- ingest
  // A job is a poll loop with a terminal state, not something to swap into the
  // DOM, so it is the one flow that talks to the JSON API directly.
  //
  // Each submitted item owns one toast for its whole life — created *before*
  // the POST, because that request can block (the API waits on the queue) and
  // silence there reads as "the button did nothing".
  function watchJob(jobId, label, node, options) {
    var delay = 700;
    options = options || {};

    function tick() {
      api("/api/jobs/" + jobId)
        .then(function (job) {
          if (options.onUpdate) options.onUpdate(job);
          if (job.state === "done") {
            setToastText(
              node,
              label + " — done" + (job.duplicate ? " (already in the library)" : ""),
              "ok"
            );
            setTimeout(function () {
              node.remove();
            }, 4000);
            M.refresh();
            M.refreshTree();
            if (options.onDone) options.onDone(job);
            return;
          }
          if (job.state === "failed") {
            setToastText(node, label + " — failed: " + (job.error || "unknown error"), "error");
            if (options.onFailed) options.onFailed(job);
            return;
          }
          setToastText(node, label + " — " + job.state + "…", "info");
          delay = Math.min(delay * 1.25, 4000);
          setTimeout(tick, delay);
        })
        .catch(function (err) {
          setToastText(node, label + " — " + err.message, "error");
          if (options.onError) options.onError(err);
        });
    }
    tick();
  }

  /** Follow an already-submitted job to its terminal state. Phase 5's share
   *  target hands off here after posting from the service worker. */
  M.watchJob = function (jobId, label, options) {
    watchJob(
      jobId,
      label,
      M.toast(label + " — queued…", "info", { sticky: true }),
      options
    );
  };

  function submitIngest(label, options) {
    var node = M.toast(label + " — sending…", "info", { sticky: true });
    return api("/api/papers", options).then(
      function (accepted) {
        watchJob(accepted.job_id, label, node);
      },
      function (err) {
        setToastText(node, label + " — " + err.message, "error");
        setTimeout(function () {
          node.remove();
        }, 12000);
      }
    );
  }

  function ingestFile(file) {
    var form = new FormData();
    form.append("file", file);
    return submitIngest(file.name, { method: "POST", body: form });
  }

  function ingestUrl(url) {
    return submitIngest(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url: url }),
    });
  }

  M.submitUpload = function () {
    var fileInput = $("upload-file");
    var urlInput = $("upload-url");
    var files = fileInput && fileInput.files ? Array.prototype.slice.call(fileInput.files) : [];
    var urls = urlInput
      ? urlInput.value
          .split(/\r?\n/)
          .map(function (url) {
            return url.trim();
          })
          .filter(Boolean)
          .filter(function (url, index, all) {
            return all.indexOf(url) === index;
          })
      : [];

    if (!files.length && !urls.length) {
      M.toast("Choose one or more PDFs, or paste one URL per line.", "error");
      return;
    }

    // Start every request before awaiting anything: each item gets its own job
    // and toast, and one rejected item does not hold up the rest of the batch.
    files.forEach(ingestFile);
    urls.forEach(ingestUrl);

    if (fileInput) fileInput.value = "";
    if (urlInput) urlInput.value = "";
    var summary = $("upload-file-summary");
    if (summary) {
      summary.textContent = "";
      summary.classList.add("hidden");
    }
    var dialog = $("upload-dialog");
    if (dialog && dialog.open) dialog.close();
  };

  // ----------------------------------------------------------- topic filter
  // Delegated, not inline onclick: a topic name is user data, and Jinja's
  // `tojson` does not escape the double quote that would end an HTML
  // attribute. Reading it back out of a data-* attribute sidesteps that
  // entirely, and it keeps working across HTMX swaps with no re-binding.
  document.addEventListener("click", function (evt) {
    if (evt.target.closest("[data-caret]")) {
      // The caret inside a <summary> expands the branch instead of filtering.
      var details = evt.target.closest("details");
      if (details) {
        evt.preventDefault();
        evt.stopPropagation();
        details.open = !details.open;
      }
      return;
    }
    var el = evt.target.closest("[data-topic-filter]");
    if (!el) return;
    evt.preventDefault();
    evt.stopPropagation(); // a <summary> would otherwise toggle as well
    M.setTopic(el.dataset.topicId ? Number(el.dataset.topicId) : null, el.dataset.topicName || "");
  });

  document.addEventListener("keydown", function (evt) {
    if (evt.key !== "Enter" && evt.key !== " ") return;
    var el = evt.target.closest && evt.target.closest('[data-topic-filter][role="button"]');
    if (!el) return;
    evt.preventDefault();
    M.setTopic(el.dataset.topicId ? Number(el.dataset.topicId) : null, el.dataset.topicName || "");
  });

  /** Point the library view at a topic (or clear it) and re-run the search. */
  M.setTopic = function (topicId, name) {
    var field = $("topic_id");
    if (!field) {
      // No filter form on this page (paper detail, taxonomy) — hand the topic
      // to the library view instead, so a chip is clickable everywhere.
      window.location.href = topicId ? "/?topic_id=" + encodeURIComponent(topicId) : "/";
      return;
    }
    field.value = topicId === null || topicId === undefined ? "" : String(topicId);

    document.querySelectorAll("[data-topic-id]").forEach(function (el) {
      el.setAttribute("aria-current", String(el.dataset.topicId) === field.value ? "true" : "false");
    });

    var label = $("topic-filter-label");
    if (label) {
      label.textContent = name || "";
      label.parentElement.classList.toggle("hidden", !field.value);
    }
    // On a phone the drawer covers the results it just changed.
    if (window.matchMedia("(max-width: 1023px)").matches) M.toggleSidebar(false);
    M.refresh();
  };

  M.clearTopic = function () {
    M.setTopic(null, "");
  };

  /** Open/close the mobile topic drawer. `force` sets it explicitly. */
  M.toggleSidebar = function (force) {
    var sidebar = $("sidebar");
    var backdrop = $("sidebar-backdrop");
    if (!sidebar) return;
    var open = force === undefined ? sidebar.classList.contains("hidden") : force;
    sidebar.classList.toggle("hidden", !open);
    if (backdrop) backdrop.classList.toggle("hidden", !open);
  };

  M.resetFilters = function () {
    var form = $("filters");
    var q = $("q");
    if (q) q.value = "";
    if (form) {
      form.reset();
      var topic = $("topic_id");
      if (topic) topic.value = ""; // form.reset() restores the server-rendered value
      document.querySelectorAll("[data-topic-id]").forEach(function (el) {
        el.setAttribute("aria-current", el.dataset.topicId ? "false" : "true");
      });
      var label = $("topic-filter-label");
      if (label) label.parentElement.classList.add("hidden");
    }
    M.refresh();
  };

  // ------------------------------------------------------------- markdown
  /**
   * A deliberately small Markdown subset — headings, emphasis, code, links,
   * lists, quotes, rules. Escaping happens first and the output is assembled
   * from escaped pieces, so note text can never inject markup. A full parser
   * would be another CDN dependency for a preview pane.
   */
  function escapeHtml(text) {
    return String(text == null ? "" : text).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  M.escapeHtml = escapeHtml;

  function inline(text) {
    return escapeHtml(text)
      .replace(/`([^`]+)`/g, "<code>$1</code>")
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
      .replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>")
      .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, function (_, label, href) {
        return '<a href="' + href + '" target="_blank" rel="noreferrer noopener">' + label + "</a>";
      });
  }

  M.renderMarkdown = function (source) {
    var lines = String(source || "").split(/\r?\n/);
    var out = [];
    var list = null; // "ul" | "ol" | null
    var fence = null;
    var paragraph = [];

    function closeParagraph() {
      if (paragraph.length) {
        out.push("<p>" + inline(paragraph.join(" ")) + "</p>");
        paragraph = [];
      }
    }
    function closeList() {
      if (list) {
        out.push("</" + list + ">");
        list = null;
      }
    }

    lines.forEach(function (line) {
      if (/^\s*```/.test(line)) {
        if (fence === null) {
          closeParagraph();
          closeList();
          fence = [];
        } else {
          out.push("<pre><code>" + escapeHtml(fence.join("\n")) + "</code></pre>");
          fence = null;
        }
        return;
      }
      if (fence !== null) {
        fence.push(line);
        return;
      }

      var heading = /^(#{1,3})\s+(.*)$/.exec(line);
      if (heading) {
        closeParagraph();
        closeList();
        var level = heading[1].length;
        out.push("<h" + level + ">" + inline(heading[2]) + "</h" + level + ">");
        return;
      }

      if (/^\s*([-*_])\s*\1\s*\1[\s-*_]*$/.test(line)) {
        closeParagraph();
        closeList();
        out.push("<hr>");
        return;
      }

      var bullet = /^\s*[-*+]\s+(.*)$/.exec(line);
      var numbered = /^\s*\d+[.)]\s+(.*)$/.exec(line);
      if (bullet || numbered) {
        closeParagraph();
        var want = bullet ? "ul" : "ol";
        if (list !== want) {
          closeList();
          out.push("<" + want + ">");
          list = want;
        }
        out.push("<li>" + inline((bullet || numbered)[1]) + "</li>");
        return;
      }

      var quote = /^\s*>\s?(.*)$/.exec(line);
      if (quote) {
        closeParagraph();
        closeList();
        out.push("<blockquote>" + inline(quote[1]) + "</blockquote>");
        return;
      }

      if (!line.trim()) {
        closeParagraph();
        closeList();
        return;
      }
      paragraph.push(line.trim());
    });

    if (fence !== null) out.push("<pre><code>" + escapeHtml(fence.join("\n")) + "</code></pre>");
    closeParagraph();
    closeList();
    return out.join("\n");
  };

  /** Toggle a note between its textarea and a rendered preview. */
  M.togglePreview = function (noteId) {
    var editor = $("note-editor-" + noteId);
    var preview = $("note-preview-" + noteId);
    var button = $("note-toggle-" + noteId);
    if (!editor || !preview) return;
    var showPreview = preview.classList.contains("hidden");
    if (showPreview) preview.innerHTML = M.renderMarkdown(editor.value);
    preview.classList.toggle("hidden", !showPreview);
    editor.classList.toggle("hidden", showPreview);
    if (button) button.textContent = showPreview ? "Edit" : "Preview";
  };

  // ------------------------------------------------------------ pdf viewer
  // The PDF route is behind the bearer token, and an <iframe> cannot send a
  // header — so fetch it here and hand the iframe a blob URL instead.
  M.loadPdf = function (paperId, page) {
    var frame = $("pdf-frame");
    var statusEl = $("pdf-status");
    var button = $("pdf-load");
    if (!frame) return Promise.resolve();
    if (frame.dataset.blobUrl) {
      frame.src = frame.dataset.blobUrl + (page ? "#page=" + page : "");
      frame.classList.remove("hidden");
      frame.scrollIntoView({ behavior: "smooth", block: "start" });
      return Promise.resolve();
    }
    if (button) button.disabled = true;
    if (statusEl) statusEl.textContent = "Loading…";

    return fetch("/api/papers/" + paperId + "/pdf", {
      headers: { Authorization: "Bearer " + M.token() },
    })
      .then(function (res) {
        if (!res.ok) throw new Error(res.status === 404 ? "No stored PDF for this paper." : "HTTP " + res.status);
        return res.blob();
      })
      .then(function (blob) {
        var url = URL.createObjectURL(blob);
        frame.dataset.blobUrl = url;
        frame.src = url + (page ? "#page=" + page : "");
        frame.classList.remove("hidden");
        if (page) frame.scrollIntoView({ behavior: "smooth", block: "start" });
        if (statusEl) statusEl.textContent = "";
        if (button) button.classList.add("hidden");
        // The blob outlives the iframe otherwise; a 64MB paper is worth freeing.
        window.addEventListener("pagehide", function () {
          URL.revokeObjectURL(url);
        });
      })
      .catch(function (err) {
        if (statusEl) statusEl.textContent = err.message;
        if (button) button.disabled = false;
      });
  };

  M.jumpToPdf = function (paperId, page) {
    M.loadPdf(paperId, page);
  };

  // ----------------------------------------------------- drag-to-merge
  // Taxonomy page: drag topic A onto topic B to merge A into B.
  var dragged = null;

  M.bindMerge = function (root) {
    (root || document).querySelectorAll("[data-merge-handle]").forEach(function (el) {
      if (el.dataset.mergeBound) return;
      el.dataset.mergeBound = "1";

      el.addEventListener("dragstart", function (evt) {
        dragged = { id: el.dataset.topicId, name: el.dataset.topicName };
        el.classList.add("drag-source");
        evt.dataTransfer.effectAllowed = "move";
        // Firefox refuses to start a drag without payload.
        evt.dataTransfer.setData("text/plain", el.dataset.topicId);
      });
      el.addEventListener("dragend", function () {
        el.classList.remove("drag-source");
        dragged = null;
      });
      el.addEventListener("dragover", function (evt) {
        if (!dragged || dragged.id === el.dataset.topicId) return;
        evt.preventDefault();
        el.classList.add("drop-target");
      });
      el.addEventListener("dragleave", function () {
        el.classList.remove("drop-target");
      });
      el.addEventListener("drop", function (evt) {
        evt.preventDefault();
        el.classList.remove("drop-target");
        if (!dragged || dragged.id === el.dataset.topicId) return;
        M.confirmMerge(dragged.id, dragged.name, el.dataset.topicId, el.dataset.topicName);
      });
    });
  };

  M.confirmMerge = function (sourceId, sourceName, targetId, targetName) {
    var dialog = $("merge-dialog");
    if (!dialog) return;
    $("merge-source").textContent = sourceName;
    $("merge-target").textContent = targetName;
    dialog.dataset.sourceId = sourceId;
    dialog.dataset.targetId = targetId;
    dialog.showModal();
  };

  M.doMerge = function () {
    var dialog = $("merge-dialog");
    if (!dialog) return;
    var source = dialog.dataset.sourceId;
    var target = dialog.dataset.targetId;
    dialog.close();
    htmx.ajax("POST", "/ui/taxonomy/topics/" + source + "/merge", {
      target: "#taxonomy",
      swap: "innerHTML",
      values: { into_topic_id: target },
    });
  };

  // --------------------------------------------------------------- wiring
  document.addEventListener("DOMContentLoaded", function () {
    // Token gate.
    on($("token-form"), "submit", function (evt) {
      evt.preventDefault();
      M.setToken($("token-input").value);
      $("token-gate").close();
      M.authChanged();
      M.refresh();
    });
    on($("token-open"), "click", function () {
      M.openGate();
    });
    if (!M.token() && !document.body.dataset.pairingPage) M.openGate();

    // Upload dialog + drag-and-drop.
    on($("upload-open"), "click", function () {
      $("upload-dialog").showModal();
    });
    on($("upload-form"), "submit", function (evt) {
      evt.preventDefault();
      M.submitUpload();
    });

    on($("upload-file"), "change", function (evt) {
      var count = (evt.target.files || []).length;
      var summary = $("upload-file-summary");
      if (!summary) return;
      summary.textContent = count ? count + " PDF" + (count === 1 ? "" : "s") + " selected" : "";
      summary.classList.toggle("hidden", !count);
    });

    var zone = $("dropzone");
    if (zone) {
      ["dragenter", "dragover"].forEach(function (name) {
        zone.addEventListener(name, function (evt) {
          evt.preventDefault();
          zone.classList.add("is-over");
        });
      });
      ["dragleave", "drop"].forEach(function (name) {
        zone.addEventListener(name, function (evt) {
          evt.preventDefault();
          zone.classList.remove("is-over");
        });
      });
      // Files are ingested by the window-level handler below — a drop here
      // bubbles to it. Doing it in both places would queue the same PDF twice.
      zone.addEventListener("drop", function () {
        var dialog = $("upload-dialog");
        if (dialog && dialog.open) dialog.close();
      });
    }

    // Whole-window drop, so a PDF can be dragged onto the library directly.
    ["dragover", "drop"].forEach(function (name) {
      window.addEventListener(name, function (evt) {
        if (evt.dataTransfer && Array.prototype.indexOf.call(evt.dataTransfer.types || [], "Files") >= 0) {
          evt.preventDefault();
        }
      });
    });
    window.addEventListener("drop", function (evt) {
      Array.prototype.slice
        .call((evt.dataTransfer && evt.dataTransfer.files) || [])
        .filter(function (f) {
          return /\.pdf$/i.test(f.name) || f.type === "application/pdf";
        })
        .forEach(ingestFile);
    });

    // Sidebar drawer (mobile).
    on($("sidebar-toggle"), "click", function () {
      M.toggleSidebar();
    });
    on($("sidebar-backdrop"), "click", function () {
      M.toggleSidebar(false);
    });

    on($("merge-confirm"), "click", M.doMerge);

    document.addEventListener("click", function (evt) {
      var pairingButton = evt.target.closest("#pairing-create");
      if (pairingButton) {
        pairingButton.disabled = true;
        pairingButton.textContent = "Creating…";
        api("/api/pairing", { method: "POST" }).then(function (pairing) {
          $("pairing-qr").src = pairing.qr_data_url;
          $("pairing-link").href = pairing.pair_url;
          $("pairing-link").textContent = pairing.pair_url;
          $("pairing-expiry").textContent = "Expires " + pairing.expires_at;
          $("pairing-result").classList.remove("hidden");
          pairingButton.textContent = "Create a new QR";
          pairingButton.disabled = false;
        }).catch(function (error) {
          M.toast(error.message, "error");
          pairingButton.textContent = "Create pairing QR";
          pairingButton.disabled = false;
        });
        return;
      }
      var refresh = evt.target.closest("[data-admin-refresh]");
      if (refresh) {
        htmx.trigger(document.body, "mouseion:admin");
        return;
      }
      var retry = evt.target.closest("[data-retry-job]");
      if (!retry) return;
      retry.disabled = true;
      api("/api/jobs/" + retry.dataset.retryJob + "/retry", { method: "POST" })
        .then(function () {
          M.toast("Job queued for retry.", "ok");
          htmx.trigger(document.body, "mouseion:admin");
        })
        .catch(function (error) {
          retry.disabled = false;
          M.toast(error.message, "error");
        });
    });

    M.bindMerge(document);
  });

  // Newly swapped fragments need their drag handles bound, and the topic tree
  // is where the filter chip's label comes from (the public shell only knows
  // the id — see api/ui.py::library_shell).
  document.addEventListener("htmx:afterSwap", function (evt) {
    M.bindMerge(evt.detail.target);

    if (evt.detail.target.id === "topic-tree") {
      var field = $("topic_id");
      var label = $("topic-filter-label");
      if (!field || !label) return;
      var row = field.value
        ? evt.detail.target.querySelector('[data-topic-id="' + field.value + '"] .topic-name')
        : null;
      if (row) label.textContent = row.textContent.trim();
      label.parentElement.classList.toggle("hidden", !field.value);
    }
  });
})();
