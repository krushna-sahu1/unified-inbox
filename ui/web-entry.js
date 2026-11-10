/* Hermes web dashboard tab. Generated into dashboard/dist/index.js by scripts/build_ui.py. */
(function () {
  "use strict";
  var SDK = window.__HERMES_PLUGIN_SDK__;
  if (!SDK || !window.__HERMES_PLUGINS__) return;

  /*CORE*/

  if (!document.getElementById("uinbox-style")) {
    var style = document.createElement("style");
    style.id = "uinbox-style";
    style.textContent = INBOX_CSS + "\n.ui-web-host .uinbox{height:calc(100vh - 7rem);min-height:560px;border:1px solid var(--u-border);border-radius:var(--u-radius)}";
    document.head.appendChild(style);
  }

  var BASE = "/api/plugins/unified-inbox";

  // fetchJSON throws Error("<status>: <body>"); keep the API's message, its kind and the status
  function readError(err) {
    var text = (err && err.message) || String(err || "");
    var status = parseInt(text, 10) || undefined;
    var m = text.match(/\{[\s\S]*\}$/);
    if (m) {
      try {
        var parsed = JSON.parse(m[0]);
        if (parsed && parsed.error) {
          var out = new Error(parsed.error);
          out.kind = parsed.kind; out.status = status;
          return out;
        }
      } catch (e) { /* not JSON */ }
    }
    var plain = err instanceof Error ? err : new Error(text || "Request failed");
    if (status && !plain.status) plain.status = status;
    return plain;
  }
  function unsupported() {
    var e = new Error("This version of Hermes can't show files on this page. Open it in the app on your phone.");
    e.kind = "unsupported";
    return e;
  }
  // Files come through the SDK's authenticated fetch (never a bare fetch). Called only after the
  // person opened the file, so it never starts a download from Unipile on its own.
  function fileResponse(path) {
    if (!SDK.authedFetch) return Promise.reject(unsupported());
    return SDK.authedFetch(BASE + "/" + path).then(function (r) {
      if (!r.ok) return r.text().then(function (t) { throw readError(new Error(r.status + ": " + t)); });
      return r.blob();
    });
  }

  var App = createInboxApp({
    React: SDK.React,
    platform: "web",
    api: function (method, path, body) {
      var init = { method: method, headers: { "Accept": "application/json" } };
      if (body !== undefined) {
        init.headers["Content-Type"] = "application/json";
        init.body = JSON.stringify(body);
      }
      return SDK.fetchJSON(BASE + path, init).catch(function (e) { throw readError(e); });
    },
    notify: function (title, body) {
      try {
        if (window.Notification && window.Notification.permission === "granted" && document.hidden) {
          new window.Notification(title, { body: body || "" });
        }
      } catch (e) { /* notifications are optional */ }
    },
    openExternal: function (url) { window.open(url, "_blank", "noopener"); return Promise.resolve(true); },
    copy: function (text) { return navigator.clipboard.writeText(text).then(function () { return true; }); },
    download: function (name, text) {
      var url = URL.createObjectURL(new Blob([text], { type: "application/json" }));
      var a = document.createElement("a");
      a.href = url; a.download = name; document.body.appendChild(a); a.click(); a.remove();
      setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
      return Promise.resolve("");
    },
    redirectUrl: function () { return window.location.href.split("#")[0]; },
    // inline media (photo, audio, video) as a local blob URL the page can put in <img>/<audio>/<video>
    fileUrl: function (path) { return fileResponse(path).then(function (b) { return URL.createObjectURL(b); }); },
    releaseUrl: function (url) { try { URL.revokeObjectURL(url); } catch (e) { /* already gone */ } },
    // a small text file the page made (a calendar .ics), saved by the browser
    saveText: function (name, text, type) {
      var url = URL.createObjectURL(new Blob([text], { type: type || "text/plain" }));
      var a = document.createElement("a");
      a.href = url; a.download = name || "file.txt"; a.rel = "noopener";
      document.body.appendChild(a); a.click(); a.remove();
      setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
      return Promise.resolve("");
    },
    // any file, saved by the browser; it is never rendered in the page
    saveFile: function (path, filename) {
      return fileResponse(path).then(function (b) {
        var url = URL.createObjectURL(b);
        var a = document.createElement("a");
        a.href = url; a.download = filename || "file"; a.rel = "noopener";
        document.body.appendChild(a); a.click(); a.remove();
        setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
        return "";
      });
    }
  });

  function Page() {
    return SDK.React.createElement("div", { className: "uinbox-host ui-web-host" }, SDK.React.createElement(App, null));
  }

  window.__HERMES_PLUGINS__.register("unified-inbox", Page);
})();
