/* Hermes Desktop plugin. Generated into desktop/plugin.js by scripts/build_ui.py. */
import { host as hermes, ROUTES_AREA, SIDEBAR_NAV_AREA } from '@hermes/plugin-sdk'
import * as React from 'react'

/*CORE*/

export default {
  id: 'unified-inbox',
  name: 'Inbox',
  register(ctx) {
    var style = document.createElement('style')
    style.id = 'uinbox-style'
    style.textContent = INBOX_CSS
    document.head.appendChild(style)
    ctx.onDispose(function () { style.remove() })

    // keep the API's message, its kind (too_large, unavailable, ...) and the status when there is one
    function readError(err) {
      var text = (err && err.message) || String(err || '')
      var out = err instanceof Error ? err : new Error(text || 'Request failed')
      var m = text.match(/\{[\s\S]*\}/)
      if (m) {
        try {
          var parsed = JSON.parse(m[0])
          if (parsed && parsed.error) { out = new Error(parsed.error); out.kind = parsed.kind }
        } catch (e) { /* not JSON */ }
      }
      var st = (err && (err.status || err.statusCode)) || (text.match(/\b([45]\d\d)\b/) || [])[1]
      if (st) out.status = Number(st)
      return out
    }
    function rest(method, path, body) {
      return ctx.rest(path, body === undefined ? { method: method } : { method: method, body: body })
        .catch(function (e) { throw readError(e) })
    }
    // Desktop's plugin REST returns JSON only, so files come as base64 JSON (inline media) or are
    // saved by the backend and shown in the file manager. Older backends without these: say so.
    function unsupported(e) {
      if (e && e.status === 404 && !e.kind) {
        var u = new Error('Open this file in the Hermes web dashboard, or in the app on your phone.')
        u.kind = 'unsupported'
        return u
      }
      return e
    }

    var App = createInboxApp({
      React: React,
      platform: 'desktop',
      api: rest,
      notify: function () { /* the background poll below notifies, page open or not */ },
      openExternal: function (url) { return ctx.os.openExternal(url) },
      copy: function (text) { return ctx.os.writeClipboard(text) },
      download: function (name, text) {
        // the backend writes the file next to the plugin's data; show it in the file manager
        return rest('POST', '/privacy/export-file', {}).then(function (res) {
          ctx.os.revealPath(res.path)
          return res.path
        })
      },
      redirectUrl: function () { return 'https://dashboard.unipile.com' },
      fileUrl: function (path) {
        return rest('GET', '/' + path + '/data').then(function (res) {
          var bin = atob(res.data || '')
          var bytes = new Uint8Array(bin.length)
          for (var i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i)
          return URL.createObjectURL(new Blob([bytes], { type: res.mimetype || 'application/octet-stream' }))
        }, function (e) { throw unsupported(e) })
      },
      releaseUrl: function (url) { try { URL.revokeObjectURL(url) } catch (e) { /* already gone */ } },
      // a small text file the page made (a calendar .ics): a Blob download, which Desktop saves
      saveText: function (name, text, type) {
        var url = URL.createObjectURL(new Blob([text], { type: type || 'text/plain' }))
        var a = document.createElement('a')
        a.href = url; a.download = name || 'file.txt'; a.rel = 'noopener'
        a.click()
        setTimeout(function () { URL.revokeObjectURL(url) }, 1000)
        return Promise.resolve('')
      },
      saveFile: function (path) {
        return rest('POST', '/' + path + '/save', {}).then(function (res) {
          ctx.os.revealPath(res.path)
          return res.path
        }, function (e) { throw unsupported(e) })
      }
    })

    ctx.registerMany([
      { id: 'page', area: ROUTES_AREA, data: { path: '/inbox' }, render: function () { return React.createElement(App, null) } },
      { id: 'nav', area: SIDEBAR_NAV_AREA, data: { path: '/inbox', label: 'Inbox', codicon: 'inbox' } }
    ])

    // New alerts become a native notification (when the app is in the background) and a toast.
    var lastAlert = -1
    function poll() {
      rest('GET', '/changes?alerts_since=' + Math.max(0, lastAlert)).then(function (c) {
        var first = lastAlert < 0
        ;(c.alerts || []).forEach(function (a) {
          if (!first && a.id > lastAlert) {
            try { ctx.os.notify({ title: a.title, body: a.body || '', onActivate: function () { hermes.navigate('/inbox') } }) } catch (e) { /* optional */ }
            try { hermes.notify({ kind: 'info', title: 'Inbox', message: a.title }) } catch (e) { /* optional */ }
          }
          lastAlert = Math.max(lastAlert, a.id)
        })
        if (lastAlert < 0) lastAlert = 0
      }, function () { /* not connected yet, or the backend is restarting */ })
    }
    poll()
    ctx.setInterval(poll, 30000)
  }
}
