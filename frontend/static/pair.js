(function () {
  "use strict";

  function finishError(message) {
    document.getElementById("pair-status").textContent = "This device was not paired.";
    document.getElementById("pair-error").textContent = message;
    document.getElementById("pair-error").classList.remove("hidden");
    document.getElementById("pair-home").classList.remove("hidden");
  }

  document.addEventListener("DOMContentLoaded", function () {
    var params = new URLSearchParams(window.location.search);
    var token = params.get("token") || "";
    // Strip the credential before fetch so it cannot appear in a Referer.
    window.history.replaceState({}, "", "/pair");
    if (!token) {
      finishError("The pairing link has no token. Generate a fresh QR code on the admin page.");
      return;
    }
    fetch("/api/pairing/consume", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token: token })
    }).then(function (response) {
      if (!response.ok) return response.json().then(function (body) {
        throw new Error(body.detail || "Pairing failed");
      });
      return response.json();
    }).then(function (body) {
      window.Mouseion.setToken(body.api_token);
      document.getElementById("pair-status").textContent = "Paired. Opening your library…";
      setTimeout(function () { window.location.replace("/"); }, 350);
    }).catch(function (error) { finishError(error.message); });
  });
})();
