/* Linkarr web app. Plain JS, no build step. Talks only to its own server. */
(function () {
  "use strict";

  // ---------- helpers ----------
  const $ = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const store = {
    get(k, d) { try { const v = localStorage.getItem("la." + k); return v == null ? d : JSON.parse(v); } catch (e) { return d; } },
    set(k, v) { try { localStorage.setItem("la." + k, JSON.stringify(v)); } catch (e) { /* private mode */ } },
  };
  const I = (d) => `<svg viewBox="0 0 24 24" aria-hidden="true">${d}</svg>`;
  const ICON = {
    activity: I('<path d="M3 12h4l3-8 4 16 3-8h4"/>'),
    search: I('<circle cx="11" cy="11" r="7"/><path d="M20 20l-4-4"/>'),
    tv: I('<rect x="3" y="6" width="18" height="13" rx="2"/><path d="M8 3l4 3 4-3"/>'),
    history: I('<path d="M3 12a9 9 0 1 0 3-6.7L3 8"/><path d="M3 3v5h5M12 7v5l3 2"/>'),
    settings: I('<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/>'),
    check: I('<path d="M9 12l2 2 4-4"/><circle cx="12" cy="12" r="9"/>'),
    logs: I('<path d="M4 6h16M4 12h16M4 18h10"/>'),
    warn: I('<path d="M12 3l10 18H2z"/><path d="M12 10v4M12 17.5v.5"/>'),
    info: I('<circle cx="12" cy="12" r="9"/><path d="M12 11v6M12 7.5v.5"/>'),
    menu: I('<path d="M4 6h16M4 12h16M4 18h16"/>'),
    link: I('<path d="M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1"/><path d="M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1"/>'),
    plus: I('<path d="M12 5v14M5 12h14"/>'),
    key: I('<circle cx="8" cy="15" r="4"/><path d="M11 12l9-9M17 6l3 3M15 8l2 2"/>'),
    lock: I('<rect x="5" y="11" width="14" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 8 0v4"/>'),
    ext: I('<path d="M14 4h6v6M20 4l-9 9"/><path d="M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5"/>'),
  };
  // Your download site's icon: its 1-3 letter badge drawn like the line icons.
  const badgeIcon = (b) => `<span class="badge-ico" aria-hidden="true">${esc(b)}</span>`;

  // Updates el to match html, touching only the parts that changed. Replacing the whole page every
  // few seconds made it flicker, jump, restart animations and swallow clicks mid-redraw.
  function morph(el, html) {
    const next = document.createElement(el.tagName);
    next.innerHTML = html;
    morphChildren(el, next);
  }
  function morphChildren(a, b) {
    const ac = Array.from(a.childNodes), bc = Array.from(b.childNodes);
    for (let i = 0; i < bc.length; i++) {
      const o = ac[i], n = bc[i];
      if (!o) { a.appendChild(n); continue; }
      if (o.isEqualNode(n)) continue;
      if (o.nodeType !== n.nodeType || o.nodeName !== n.nodeName || o.nodeType !== 1 || o.id !== n.id ||
          /^(INPUT|SELECT|TEXTAREA|DETAILS)$/.test(o.nodeName) && o.dataset.key !== n.dataset.key) {
        if (o.nodeType === 3 && n.nodeType === 3) o.nodeValue = n.nodeValue; else a.replaceChild(n, o);
        continue;
      }
      for (const at of Array.from(o.attributes)) if (!n.hasAttribute(at.name) && at.name !== "data-bound") o.removeAttribute(at.name);
      for (const at of Array.from(n.attributes)) if (o.getAttribute(at.name) !== at.value) o.setAttribute(at.name, at.value);
      if (/^(INPUT|SELECT|TEXTAREA)$/.test(o.nodeName)) continue;  // keep what's being typed
      morphChildren(o, n);
    }
    for (let i = ac.length - 1; i >= bc.length; i--) a.removeChild(ac[i]);
  }

  function toast(text) {
    const t = $("#toast");
    t.textContent = text;
    t.hidden = false;
    clearTimeout(toast.timer);
    toast.timer = setTimeout(() => { t.hidden = true; }, 3800);
  }

  async function api(method, path, body, isForm) {
    const opts = { method, headers: { "X-Linkarr": "1" }, credentials: "same-origin" };
    if (body !== undefined) {
      if (isForm) opts.body = body;
      else { opts.headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(body); }
    }
    const r = await fetch(path, opts);
    if (r.status === 401 && !path.startsWith("/api/auth/")) {
      state.auth = null;
      await boot();
      throw new Error("Signed out.");
    }
    const data = (r.headers.get("content-type") || "").includes("json") ? await r.json() : null;
    if (!r.ok) {
      let msg = data && data.detail;
      if (Array.isArray(msg)) msg = msg.map((d) => d.msg).join("; ");
      throw new Error(msg || "Something went wrong (" + r.status + ").");
    }
    return data;
  }

  const fmtBytes = (n) => {
    n = n || 0;
    if (n >= 1024 ** 4) return (n / 1024 ** 4).toFixed(2) + " TB";
    if (n >= 1024 ** 3) return (n / 1024 ** 3).toFixed(1) + " GB";
    if (n >= 1024 ** 2) return Math.round(n / 1024 ** 2) + " MB";
    return Math.max(1, Math.round(n / 1024)) + " KB";
  };
  const fmtAgo = (ts) => {
    const s = Math.floor(Date.now() / 1000 - ts);
    if (s < 60) return "just now";
    if (s < 3600) return Math.floor(s / 60) + " min ago";
    if (s < 86400) return Math.floor(s / 3600) + " h ago";
    return new Date(ts * 1000).toLocaleDateString(undefined, { day: "numeric", month: "short" });
  };
  const fmtWhen = (ts) => new Date(ts * 1000).toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
  const fmtTime = (ts) => new Date(ts * 1000).toLocaleString(undefined, { weekday: "short", hour: "2-digit", minute: "2-digit" });
  const posterUrl = (p) => (p ? "/api/poster" + p : "");
  const posterEl = (p, title, cls) => {
    const u = posterUrl(p);
    return u ? `<div class="poster ${cls || ""}" style="background-image:url('${esc(u)}')"></div>`
      : `<div class="poster ${cls || ""}">${esc((title || "?").trim().charAt(0).toUpperCase())}</div>`;
  };
  const titleYear = (j) => `${esc(j.title)}${j.year ? ` <span class="yr">(${esc(j.year)})</span>` : ""}`;
  const STATUS = {
    draft: ["Waiting for you", "acc"], queued: ["Queued", ""], downloading: ["Downloading", "acc"], processing: ["Unpacking", "acc"],
    waiting: ["Waiting for all links", ""], needs_answer: ["Needs an answer", "warn"], needs_captcha: ["Needs captcha", "warn"], moving: ["Moving", "acc"],
    done: ["Done", "ok"], canceled: ["Canceled", ""], failed: ["Failed", "bad"],
  };
  const statusTag = (s) => { const [t, c] = STATUS[s] || [s, ""]; return `<span class="tag ${c}">${esc(t)}</span>`; };
  const LINK_STATUS = { queued: "queued", downloading: "downloading", downloaded: "unpacking", ready: "ready", moved: "moved", error: "needs you", skipped: "skipped" };
  const LINK_ALERTS = ["integrity", "dead", "error", "unpack", "novideo"];

  // ---------- state ----------
  const state = { auth: null, overview: null, settings: null, route: "", timer: null, matchInfo: {}, history: [], openDetails: new Set(), openRecent: new Set(), trackerTab: "series" };

  function setTheme(t) {
    document.documentElement.dataset.theme = t || "dark";
    const bg = getComputedStyle(document.documentElement).getPropertyValue("--bg").trim();
    const meta = $('meta[name="theme-color"]');
    if (meta && bg) meta.setAttribute("content", bg);
    store.set("theme", t);
  }
  setTheme(store.get("theme", "dark"));

  // ---------- boot and sign-in ----------
  async function boot() {
    try { state.auth = await api("GET", "/api/auth/state"); }
    catch (e) { $("#app").innerHTML = `<div class="auth"><div class="card">Can't reach Linkarr: ${esc(e.message)}</div></div>`; return; }
    if (state.auth.theme) setTheme(state.auth.theme);
    if (state.auth.setup_needed) return renderSetup();
    if (!state.auth.signed_in) return renderLogin();
    if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => {});
    renderShell();
    route();
    // The menu's site link, status footer and badges come from the overview; Activity refreshes it, other pages need it once.
    if (state.route !== "") api("GET", "/api/overview").then((ov) => { state.overview = ov; sideInfo(ov); }).catch(() => {});
  }

  const brand = () => `<div class="brand"><img src="/static/mark.svg" alt=""><span><span class="word">link</span><span class="arr">arr</span></span></div>`;

  // The sign-in cookie is HTTPS-only unless LA_COOKIE_SECURE=0, so over plain http the browser
  // drops it and you land back on this form. Say so instead of failing silently.
  const HTTP_HINT = "This address is plain http, so your browser won't keep the sign-in. Open Linkarr over HTTPS " +
    "(for example through Tailscale Serve), or add the container variable LA_COOKIE_SECURE = 0 and restart it.";
  const httpBlocked = () => state.auth && state.auth.cookie_secure && location.protocol === "http:" &&
    !["localhost", "127.0.0.1"].includes(location.hostname);
  const httpHint = () => (httpBlocked() ? `<p class="err">${HTTP_HINT}</p>` : "");
  async function afterSignIn() {
    await boot();
    if (state.auth && !state.auth.signed_in && !httpBlocked() && $("#auth-err")) $("#auth-err").textContent = HTTP_HINT;
  }

  function renderSetup() {
    $("#app").innerHTML = `<div class="auth"><form class="card stack" id="setup-form">${brand()}
      <p class="muted small">First run. The setup code is in the container log (Unraid: the Docker tab, click the icon, Logs).</p>${httpHint()}
      <label class="f"><span>Setup code</span><input type="text" name="code" autocomplete="off" required></label>
      <label class="f"><span>Username</span><input type="text" name="username" autocomplete="username" required></label>
      <label class="f"><span>Password (10 or more characters)</span><input type="password" name="password" autocomplete="new-password" minlength="10" required></label>
      <div class="err" id="auth-err"></div>
      <button class="btn primary" style="width:100%">Create account</button></form></div>`;
    $("#setup-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      const f = new FormData(e.target);
      try { await api("POST", "/api/auth/setup", Object.fromEntries(f)); afterSignIn(); }
      catch (err) { $("#auth-err").textContent = err.message; }
    });
  }

  function renderLogin() {
    $("#app").innerHTML = `<div class="auth"><form class="card stack" id="login-form">${brand()}${httpHint()}
      <label class="f"><span>Username</span><input type="text" name="username" autocomplete="username" required></label>
      <label class="f"><span>Password</span><input type="password" name="password" autocomplete="current-password" required></label>
      <div class="err" id="auth-err">${state.auth.locked_for ? "Too many wrong passwords. Wait a few minutes." : ""}</div>
      <button class="btn primary" style="width:100%">Sign in</button></form></div>`;
    $("#login-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      try { await api("POST", "/api/auth/login", Object.fromEntries(new FormData(e.target))); afterSignIn(); }
      catch (err) { $("#auth-err").textContent = err.message; }
    });
  }

  // ---------- shell ----------
  // Setup check, Logs and the Decoder live in Settings; on a phone History is reached from Activity and Settings.
  const NAV = [["", "Activity", "activity"], ["library", "Library", "search"], ["tracker", "Tracker", "tv"], ["history", "History", "history"], ["settings", "Settings", "settings"]];
  const navKey = (path) => ({ shows: "tracker", check: "settings", logs: "settings", decode: "settings" }[path] || path);

  // The Terminal theme's prompt: linkarr@<server name>, or just linkarr when the address is a number.
  const promptHost = () => (/^[\d.:[\]]+$/.test(location.hostname) || !location.hostname ? "linkarr" : "linkarr@" + location.hostname.split(".")[0]);

  function renderShell() {
    $("#app").innerHTML = `<div class="shell">
      <aside class="side" id="side">${brand()}
        <nav class="nav" id="nav">${NAV.map(([r, t, i]) => `<a href="#/${r}" data-route="${r}">${ICON[i]}<span>${t}</span>${navBadge(r)}</a>`).join("")}</nav>
        <div id="side-site"></div>
        <div class="side-foot" id="side-foot"></div>
      </aside>
      <main class="main"><div class="top"><img class="top-mark" src="/static/mark.svg" alt=""><h1 id="page-title"></h1><div class="grow"></div><div id="top-extra"></div></div>
        <form class="paste" id="paste"><span class="paste-ico">${ICON.link}</span><span class="prompt" aria-hidden="true"><span class="p-host">${esc(promptHost())}:</span>~$</span><input type="text" id="paste-input" aria-label="Add a MEGA link" placeholder="Paste MEGA link or code" autocomplete="off" spellcheck="false"><button class="btn primary">${ICON.plus}Add</button></form>
        <div id="page"></div></main>
      <nav class="tabbar" id="tabbar" aria-label="Pages"></nav></div>`;
    $("#paste").addEventListener("submit", (e) => { e.preventDefault(); addLinks($("#paste-input").value); });
    tabbar(null);
    const shared = new URLSearchParams(location.search).get("link");
    if (shared) {
      history.replaceState(null, "", "/" + location.hash);
      $("#paste-input").value = shared;
      addLinks(shared);
    }
  }

  const navBadge = (r) => (r === "" ? '<span class="count hidden" data-alerts></span>' : r === "settings" ? '<span class="dotw hidden" data-issues title="A setup check needs a look"></span>' : "");

  // The phone's bottom tabs: Activity, Library, Tracker, your download site (History when none is set), Settings.
  function tabbar(ov) {
    const bar = $("#tabbar");
    if (!bar) return;
    const site = ov && ov.site;
    const items = NAV.filter(([r]) => r !== "history" || !site);
    morph(bar, items.map(([r, t, i]) => `<a href="#/${r}" data-route="${r}" class="${navKey(state.route.split("/")[0]) === r ? "on" : ""}">${ICON[i]}<span>${t}</span>${navBadge(r)}</a>`)
      .concat(site ? [`<a href="${esc(site.url)}" target="_blank" rel="noopener noreferrer" class="site-tab">${badgeIcon(site.badge)}<span class="ellipsis">${esc(site.name)}</span></a>`] : [])
      .sort((a, b) => (a.includes("#/settings") ? 1 : 0) - (b.includes("#/settings") ? 1 : 0)).join(""));
  }

  function sideInfo(ov) {
    const site = ov.site;
    morph($("#side-site"), site ? `<a class="site-btn" href="${esc(site.url)}" target="_blank" rel="noopener noreferrer">${badgeIcon(site.badge)}<span class="ellipsis">${esc(site.name)}</span>${ICON.ext}</a>` : "");
    $("#side-foot").innerHTML = `<div><span class="dot ${ov.mega ? "ok" : ""}"></span>${ov.mega ? "MEGA: " + esc(ov.mega) : "MEGA not signed in"}</div>
      ${quotaMeter(ov.quota)}
      ${ov.free != null ? `<div>Disk: ${fmtBytes(ov.free)} free</div>` : ""}<div>Linkarr ${esc(ov.version)}</div>`;
    tabbar(ov);
    const n = (ov.alerts || []).length;
    $$("[data-alerts]").forEach((c) => { c.textContent = n; c.classList.toggle("hidden", !n); });
    $$("[data-issues]").forEach((c) => c.classList.toggle("hidden", !ov.setup_issues));
  }

  function quotaMeter(q) {
    if (!q || !q.max) return "";
    const left = Math.max(0, q.max - q.used);
    const pct = Math.min(100, Math.round((100 * q.used) / q.max));
    return `<div class="quota" title="MEGA transfer allowance: ${fmtBytes(q.used)} used of ${fmtBytes(q.max)}"><div>Transfer: ${fmtBytes(left)} left</div>
      <div class="bar${pct >= 90 ? " low" : ""}"><i style="width:${pct}%"></i></div></div>`;
  }

  const findUrls = (text) => (text || "").match(/(https?:\/\/(?:www\.)?mega\.(?:nz|co\.nz|io)\/[^\s<>"']+|mega:\/\/[^\s<>"']+)/gi) || [];

  async function addLinks(text) {
    let urls = findUrls(text);
    if (!urls.length) {
      // Not a link: maybe a Base64 code with one inside (sometimes encoded several times).
      let r;
      try { r = await api("POST", "/api/decode", { text: text || "" }); } catch (e) { toast(e.message); return; }
      if (!r.links.length && r.captcha && r.captcha.length) return holdCaptcha(r.captcha);
      if (!r.links.length) {
        toast(r.layers.length ? `Decoded ${r.layers.length} time${r.layers.length > 1 ? "s" : ""} but found no MEGA link. Open Settings, Tools to see it.`
          : "That isn't a MEGA link or a Base64 code.");
        return;
      }
      if (r.layers.length && !(await askAdd(r))) return;
      urls = r.links;
    }
    let last = null, auto = 0;
    for (const u of urls) {
      try { last = await api("POST", "/api/jobs", { url: u }); if (last.status !== "draft") auto++; }
      catch (e) { toast(e.message); }
    }
    $("#paste-input").value = "";
    if (!last) return;
    if (urls.length === 1 && last.status !== "draft") {
      toast(`Found ${last.title}. Linkarr is sure it's right, so it started.`);
      if (state.route === "") refreshActivity(); else location.hash = "#/";
    } else if (urls.length === 1) {
      toast(`Found ${last.title}.`);
      location.hash = "#/job/" + last.id;
    } else {
      toast(`Added ${urls.length} links${auto ? `, ${auto} started automatically` : ""}.`);
      if (state.route === "") refreshActivity(); else location.hash = "#/";
    }
  }

  // A captcha link (a site listed in Settings → Extras): held as "Needs captcha" until you paste back the MEGA link.
  async function holdCaptcha(urls) {
    let last = null;
    for (const u of urls) {
      try { last = await api("POST", "/api/captcha", { url: u }); } catch (e) { toast(e.message); }
    }
    $("#paste-input").value = "";
    if (!last) return;
    toast("Needs captcha. Open the link, solve it, then paste the MEGA link.");
    if (urls.length === 1) location.hash = "#/job/" + last.id;
    else if (state.route === "") refreshActivity(); else location.hash = "#/";
  }

  async function solveCaptcha(id, text) {
    if (!(text || "").trim()) { toast("Paste the MEGA link first."); return; }
    let r;
    try { r = await api("POST", `/api/jobs/${id}/captcha`, { text }); } catch (e) { toast(e.message); return; }
    const first = r.jobs[0];
    if (r.jobs.length === 1) { toast(`Found ${first.title}.`); location.hash = "#/job/" + first.id; return; }
    toast(`Added ${r.jobs.length} links.`);
    if (state.route === "") refreshActivity(); else location.hash = "#/";
  }

  // The link, two steps, Open link, and a box for the MEGA link. Linkarr never tries the captcha itself.
  function captchaCard(j) {
    const host = (j.hold_url.split("/")[2] || "").toLowerCase();
    return `<div class="card job captcha"><div class="poster lock">${ICON.lock}</div><div class="job-body">
      <div class="spread"><div class="job-title">Captcha link</div>${statusTag(j.status)}</div>
      <div class="meta"><span>${esc(host)}</span><span>Added ${fmtAgo(j.created_at)}</span></div>
      <div class="path">${esc(j.hold_url)}</div>
      <ol class="cap-steps small"><li>Open the link and solve the captcha.</li><li>Paste the MEGA link it shows below.</li></ol>
      <div class="row"><a class="btn primary" href="${esc(j.hold_url)}" target="_blank" rel="noopener noreferrer">${ICON.ext}Open link</a></div>
      <form class="row" data-captcha-form="${j.id}"><input type="text" id="cap-${j.id}" data-key="cap-${j.id}" class="grow" style="flex:1" placeholder="Paste the MEGA link here" autocomplete="off" spellcheck="false"><button class="btn">Continue</button></form>
      <div class="row"><span class="muted small">You get one reminder if it's still here tomorrow.</span><div class="grow"></div><button class="btn sm ghost danger" data-action="drop-captcha" data-job="${j.id}">Drop it</button></div>
    </div></div>`;
  }

  // "Found a MEGA link in that code. Add it?" Resolves true or false.
  function askAdd(r) {
    return new Promise((resolve) => {
      const d = document.createElement("dialog");
      const n = r.layers.length;
      d.innerHTML = `<h2>Found a MEGA link</h2><p class="muted small">Decoded ${n} time${n > 1 ? "s" : ""}.</p>
        <div class="path" style="margin:10px 0">${r.links.map((u) => esc(u.split("#")[0]) + "#…").join("<br>")}</div>
        <p>Add ${r.links.length > 1 ? "them" : "it"}?</p><div class="row" style="justify-content:flex-end">
        <button class="btn" value="no">No</button><button class="btn primary" value="yes">Add ${r.links.length > 1 ? "them" : "it"}</button></div>`;
      document.body.appendChild(d);
      d.addEventListener("click", (e) => {
        const b = e.target.closest("button");
        if (!b && e.target !== d) return;
        d.close();
        d.remove();
        resolve(!!b && b.value === "yes");
      });
      d.addEventListener("cancel", () => { d.remove(); resolve(false); });
      d.showModal();
    });
  }

  // ---------- decoder ----------
  function renderDecoder() {
    title("Settings");
    page().innerHTML = settingsTabs("tools") + `<div class="card stack"><h2>Decoder</h2><p class="muted small">Paste a Base64 code. Linkarr decodes it up to 4 times, stopping as soon as a MEGA link appears. The box at the top does this too: paste a code there and it asks before adding the link.</p>
      <textarea id="dec-in" rows="5" class="mono" placeholder="aHR0cHM6Ly9tZWdhLm56L..." spellcheck="false"></textarea>
      <div class="row"><button class="btn primary" data-action="decode">Decode</button></div><div id="dec-out"></div></div>`;
  }

  async function runDecoder() {
    const out = $("#dec-out");
    const r = await api("POST", "/api/decode", { text: $("#dec-in").value });
    const caps = r.captcha || [];
    if (!r.layers.length && !r.links.length && !caps.length) { out.innerHTML = `<div class="err">That isn't Base64.</div>`; return; }
    out.innerHTML = r.layers.map((t, i) => `<div class="stack" style="margin-top:12px"><div class="spread"><b class="small">Layer ${i + 1}</b>
        <button class="btn sm ghost" data-action="copy" data-i="${i}">Copy</button></div><div class="logs" id="dec-${i}">${esc(t)}</div></div>`).join("") +
      (r.links.length ? `<div class="question" style="margin-top:12px"><b>Found ${r.links.length} MEGA link${r.links.length > 1 ? "s" : ""}.</b>
        <div class="row" style="margin-top:8px">${r.links.map((u, i) => `<button class="btn sm primary" data-action="add-decoded" data-url="${esc(u)}">Add${r.links.length > 1 ? " " + (i + 1) : ""}</button>`).join("")}</div></div>`
        : caps.length ? `<div class="question" style="margin-top:12px"><b>Found a captcha link.</b><div class="small">Linkarr can't solve captchas. It holds the link as "Needs captcha" for you.</div>
        <div class="row" style="margin-top:8px"><button class="btn sm primary" data-action="hold-captcha" data-url="${esc(caps[0])}">Hold it</button></div></div>`
        : `<div class="muted small" style="margin-top:12px">${r.done === "max_layers" ? "Stopped after 4 decodes without finding a MEGA link." : "No MEGA link in it."}</div>`);
  }

  // ---------- routing ----------
  function route() {
    clearInterval(state.timer);
    state.timer = null;
    const h = location.hash.replace(/^#\/?/, "");
    const [path, qs] = h.split("?");
    const q = new URLSearchParams(qs || "");
    state.route = path;
    $$("#nav a, #tabbar a[data-route]").forEach((a) => a.classList.toggle("on", a.dataset.route === navKey(path.split("/")[0])));
    $("#top-extra").innerHTML = "";
    const pages = { "": renderActivity, library: renderLibrary, tracker: renderTracker, shows: renderTracker, history: renderHistory, settings: renderSettings,
      decode: () => renderSettings(new URLSearchParams("tab=tools")), check: () => renderSettings(new URLSearchParams("tab=check")), logs: () => renderSettings(new URLSearchParams("tab=logs")) };
    if (path.startsWith("job/")) return renderJob(parseInt(path.slice(4), 10));
    (pages[path] || renderActivity)(q);
  }
  window.addEventListener("hashchange", route);

  // One refresh timer for the whole app. Each page used to start its own and some were never
  // stopped, so a page you'd left kept redrawing itself over the one you were on.
  function every(fn, ms) {
    clearInterval(state.timer);
    state.timer = setInterval(fn, ms);
  }

  function title(t) { $("#page-title").textContent = t; document.title = t + " · Linkarr"; }
  const page = () => $("#page");
  const busy = () => { const a = document.activeElement; return a && /INPUT|SELECT|TEXTAREA/.test(a.tagName) && a.id !== "paste-input"; };

  // ---------- activity ----------
  async function renderActivity() {
    title("Activity");
    if (!state.overview) page().innerHTML = `<div class="muted">Loading…</div>`;
    await refreshActivity();
    if (state.route === "") every(() => { if (!busy() && !document.hidden) refreshActivity(); }, 3000);
  }

  async function refreshActivity() {
    const seq = (refreshActivity.seq = (refreshActivity.seq || 0) + 1);
    let ov;
    try { ov = await api("GET", "/api/overview"); } catch (e) { return; }
    if (seq !== refreshActivity.seq) return;  // a newer refresh already started
    state.overview = ov;
    sideInfo(ov);
    if (state.route !== "") return;
    const caps = ov.active.filter((j) => j.status === "needs_captcha");
    const drafts = ov.active.filter((j) => j.status === "draft" || j.status === "needs_answer");
    const running = ov.active.filter((j) => !["draft", "needs_answer", "needs_captcha"].includes(j.status));
    const jobAlerts = new Set(drafts.map((j) => j.id));
    const alerts = ov.alerts.filter((a) => !(["match", "conflict", "inbox", "recheck"].includes(a.kind) && jobAlerts.has(a.job_id)));
    let html = "";
    if (!ov.libraries.length) {
      html += `<div class="card alert info section"><span class="ic">${ICON.info}</span><div class="grow"><h3>Add your library folders</h3>
        <div class="muted small">Tell Linkarr where your Movies, TV Shows and other folders are. Then paste a link.</div></div>
        <a class="btn sm primary" href="#/settings?tab=libraries">Set up</a></div>`;
    }
    if (alerts.length) html += `<div class="section"><div class="spread"><h2>Needs you</h2></div><div class="cards">${alerts.map(alertCard).join("")}</div></div>`;
    if (caps.length) html += `<div class="section"><div class="spread"><h2>Needs captcha</h2></div><div class="cards">${caps.map(captchaCard).join("")}</div></div>`;
    const ready = drafts.filter((j) => j.status === "draft").length;
    if (drafts.length) html += `<div class="section"><div class="spread"><h2>Check and start</h2>${ready > 1 ? `<div class="row">
      <button class="btn sm primary" data-action="start-all" data-when="now">Start all ${ready}</button><button class="btn sm" data-action="start-all" data-when="tonight">All tonight</button></div>` : ""}</div><div class="cards">${drafts.map((j) => jobCard(j, ov)).join("")}</div></div>`;
    html += `<div class="section"><div class="spread"><h2>Downloading</h2>${ov.speed ? `<span class="muted small">${fmtBytes(ov.speed)}/s</span>` : ""}</div>`;
    html += running.length ? `<div class="cards">${running.map((j) => jobCard(j, ov)).join("")}</div>`
      : `<div class="idle">No downloads pending</div>`;
    html += `</div>`;
    if (ov.recent.length) {
      html += `<div class="section"><div class="spread"><h2>Recently finished</h2><a href="#/history" class="small">History</a></div>
        <div class="card list recent">${ov.recent.map(recentRow).join("")}</div></div>`;
    }
    morph(page(), html);
  }

  // A finished download: title, year and library, and where it went when you tap it.
  function recentRow(j) {
    const open = state.openRecent.has(j.id);
    const tag = plexTag(j) || statusTag(j.status);
    const m = j.moved || {};
    const libPath = j.library ? j.library.path : "";
    const folder = m.where ? m.where.split("/" + libPath + "/").pop() : "";
    return `<div class="li${open ? " open" : ""}" data-action="toggle-recent" data-job="${j.id}" role="button" tabindex="0" aria-expanded="${open}">
      ${posterEl(j.poster, j.title, "sm")}<div class="grow"><div class="r-title">${titleYear(j)}</div>
      <div class="muted small">${j.library ? esc(j.library.name) : ""}${j.finished_at ? " · " + fmtAgo(j.finished_at) : ""}</div><div class="r-tag">${tag}</div>
      ${open ? `<div class="det">
        ${j.library ? `<div class="kv"><span>Library</span><b>${esc(j.library.name)}</b></div>` : ""}
        ${folder ? `<div class="kv"><span>Folder</span><b>${esc(folder.split("/").join(" / "))}</b></div>` : ""}
        ${m.file ? `<div class="kv"><span>File</span><b>${esc(m.file)}${m.files > 1 ? ` <span class="muted">+${m.files - 1} more</span>` : ""}</b></div>` : ""}
        ${m.ts ? `<div class="kv"><span>Moved</span><b>${esc(fmtWhen(m.ts))}</b></div>` : ""}
        <div class="row">${j.plex === "missing" || j.plex === "checking" ? `<button class="btn sm primary" data-action="plex-check" data-job="${j.id}">Check Plex again</button>` : ""}
          ${m.history_id ? `<a class="btn sm" href="#/history?refile=${m.history_id}">Move to…</a>` : ""}
          ${m.where ? `<button class="btn sm" data-action="copy-path" data-path="${esc(m.where + "/" + (m.file || ""))}">Copy path</button>` : ""}
          <a class="btn sm ghost" href="#/job/${j.id}">Details</a></div></div>` : ""}</div>
      <div class="r-tag-wide">${tag}</div></div>`;
  }

  // "Moved 8 files to /media/TV Shows/Severance (2022)/Season 2", so you know it landed, not just downloaded.
  function movedLine(j) {
    const m = j.moved;
    if (!m) return "";
    return `Moved ${m.files} file${m.files === 1 ? "" : "s"} to <span class="mono">${esc(m.where)}</span>`;
  }
  const plexButtons = (id) => `<button class="btn sm primary" data-action="plex-check" data-job="${id}">Check Plex again</button>
    <button class="btn sm" data-action="plex-found" data-job="${id}">It's in Plex</button>`;
  const plexTag = (j) => ({ found: '<span class="tag ok" title="Plex has it">In Plex</span>', checking: '<span class="tag" title="Waiting for Plex to scan it">Checking Plex</span>',
    missing: '<span class="tag warn" title="Plex hasn\'t picked it up">Not in Plex yet</span>' }[j.plex] || "");

  function alertCard(a) {
    const cls = ["integrity", "dead", "error", "auth", "space", "unpack", "novideo"].includes(a.kind) ? "bad" : a.kind === "inbox" ? "info" : "";
    let buttons = "";
    if (a.link_id && LINK_ALERTS.includes(a.kind)) {
      buttons = `<button class="btn sm primary" data-action="fix" data-link="${a.link_id}">Fix it</button><button class="btn sm" data-action="skip-link" data-link="${a.link_id}">Skip file</button>`;
    } else if (a.kind === "auth") {
      buttons = `<a class="btn sm primary" href="#/settings?tab=mega">Sign in</a>`;
    } else if (a.kind === "plex" && a.job_id) {
      buttons = plexButtons(a.job_id);
    } else if (a.job_id) {
      buttons = `<a class="btn sm primary" href="#/job/${a.job_id}">Open</a>`;
    }
    buttons += `<button class="btn sm ghost" data-action="dismiss" data-alert="${a.id}">Dismiss</button>`;
    return `<div class="card alert ${cls}"><span class="ic">${cls === "info" ? ICON.info : ICON.warn}</span><div class="grow"><h3>${esc(a.title)}</h3>
      ${a.message ? `<div class="muted small">${esc(a.message)}</div>` : ""}<div class="row" style="margin-top:8px">${buttons}</div></div>
      <span class="muted tiny">${fmtAgo(a.created_at)}</span></div>`;
  }

  function libChips(j, ov) {
    const libs = (ov && ov.libraries) || [];
    if (!libs.length) return `<a href="#/settings?tab=libraries" class="small">Add a library first</a>`;
    return `<div class="chips">${libs.map((l) => `<button class="chip ${j.library_id === l.id ? "on" : ""}" data-action="set-lib" data-job="${j.id}" data-lib="${l.id}">${esc(l.name)}</button>`).join("")}</div>`;
  }

  function targetLine(j) {
    if (!j.library || j.show_unknown) return "";
    const folder = ["exact", "confirmed"].includes(j.match_state) ? j.match_folder : j.folder_name;
    const note = j.match_state === "exact" || j.match_state === "confirmed" ? ' <span class="tag ok">already in your library</span>' : "";
    return `<div class="path">${esc(j.library.path)}/<b>${esc(folder)}</b>${j.kind === "series" ? "/" + esc(seasonsOf(j)) : ""}${note}</div>`;
  }
  function seasonsOf(j) {
    const s = [...new Set(j.links.map((l) => l.season).filter((x) => x != null))].sort((a, b) => a - b);
    return s.length === 1 ? `Season ${s[0]}` : s.length ? `Season ${s[0]}–${s[s.length - 1]}` : "Season …";
  }

  const noteBlock = (j) => (j.note && j.status === "draft" && !j.recheck && !j.show_unknown ? `<div class="question small">${esc(j.note)} <a href="#/job/${j.id}">Pick the right one</a></div>` : "");

  // What the folder already in your library is (title, year, poster), fetched once per job and folder.
  function matchInfo(j) {
    const key = j.id + "|" + j.match_folder;
    if (!(key in state.matchInfo)) {
      state.matchInfo[key] = null;
      api("GET", `/api/jobs/${j.id}/match-info`).then((r) => {
        state.matchInfo[key] = r;
        if (state.route === "") refreshActivity(); else if (state.route === "job/" + j.id) renderJob(j.id, true);
      }).catch(() => {});
    }
    return state.matchInfo[key];
  }

  function questionBlock(j) {
    let html = "";
    if (j.show_unknown) {
      html += `<div class="question"><div>Which show is this?</div>
        <div class="muted small">The link name has a season but no show name.</div>
        <div class="muted small">It waits here until you pick the show.</div>
        ${state.route === "job/" + j.id ? "" : `<div class="row" style="margin-top:8px"><a class="btn sm primary" href="#/job/${j.id}">Match to show</a></div>`}</div>`;
    }
    if (j.plex === "missing") {
      html += `<div class="question"><div>Plex doesn't show this yet.</div>
        <div class="muted small">Check Plex again asks Plex to scan the library and looks for it a few more times.</div>
        <div class="muted small">If you can already see it in Plex, mark it.</div>
        <div class="row" style="margin-top:8px">${plexButtons(j.id)}</div></div>`;
    }
    if (j.recheck) {
      html += `<div class="question"><div>${esc(j.note || "Check the title before it's filed.")}</div>
        <div class="row" style="margin-top:8px"><button class="btn sm primary" data-action="recheck-ok" data-job="${j.id}">Looks right, file it</button>
        <a class="btn sm" href="#/job/${j.id}">Pick the right one</a></div></div>`;
    }
    if (j.match_state === "close") {
      const mi = matchInfo(j) || {};
      const lib = j.library ? j.library.name : "your library";
      const yrs = j.year && mi.year && Math.abs(j.year - mi.year) > 1;
      const side = (label, poster, t, y, extra) => `<div class="cmp">${posterEl(poster, t, "sm")}<div class="grow"><div class="muted tiny">${label}</div>
        <b>${esc(t)}</b>${y ? ` <span class="yr">(${esc(y)})</span>` : ""}${extra || ""}</div></div>`;
      html += `<div class="question"><div>Is this the same ${j.kind === "series" ? "show" : "movie"} as one in ${esc(lib)}?</div>
        <div class="compare">${side("This link", j.poster, j.title, j.year)}${side("Already in " + lib, mi.poster, mi.title || j.match_folder, mi.year,
          `<div class="muted tiny mono ellipsis">${esc(j.match_folder)}</div>`)}</div>
        ${yrs ? `<div class="small" style="margin-top:6px">Different years, so probably not the same.</div>` : ""}
        <div class="row" style="margin-top:8px"><button class="btn sm${yrs ? "" : " primary"}" data-action="match" data-job="${j.id}" data-choice="use">Yes, add to it</button>
        <button class="btn sm${yrs ? " primary" : ""}" data-action="match" data-job="${j.id}" data-choice="new">No, new folder</button></div></div>`;
    }
    if (j.kind === "movie" && j.conflict === "exists" && !j.conflict_choice) {
      // An upgrade puts Replace first; a worse or equal copy puts Skip first.
      const up = !j.quality || ["better", "bigger", "unknown"].includes(j.quality.verdict);
      const b = (choice, label, main) => `<button class="btn sm${main ? " primary" : ""}" data-action="conflict" data-job="${j.id}" data-choice="${choice}">${label}</button>`;
      html += `<div class="question${j.quality && j.quality.verdict === "better" ? " upgrade" : ""}"><div>${esc(j.quality_text || "You already have this movie.")} What should happen to the copy you have?</div>
        <div class="row" style="margin-top:8px">${up ? b("replace", "Replace it", true) + b("keep", "Keep both") + b("skip", "Skip this one")
          : b("skip", "Skip this one", true) + b("keep", "Keep both") + b("replace", "Replace it")}</div></div>`;
    } else if (j.conflict_choice && j.conflict === "exists") {
      html += `<div class="muted small">Already have it: ${{ replace: "the old copy goes to the recycle folder", keep: "both copies are kept", skip: "skipping" }[j.conflict_choice]}.</div>`;
    }
    return html;
  }

  // Where a download is: 1 Download, 2 Unpack and check, 3 Move to the library, 4 Done.
  // With several links, the slowest link decides (a show isn't moved until its links are ready).
  const STEPS = ["Downloading", "Unpacking and checking", "Moving to your library", "Done"];
  function stepOf(j) {
    if (j.status === "done") return 4;
    if (j.status === "moving") return 3;
    const live = j.links.filter((l) => !["skipped", "moved"].includes(l.status));
    if (live.some((l) => ["queued", "downloading", "error"].includes(l.status))) return 1;
    if (live.some((l) => l.status === "downloaded")) return 2;
    return 3;
  }
  function steps(j) {
    const at = stepOf(j);
    const stuck = j.status === "failed" || j.links.some((l) => l.status === "error") ? "bad" : j.status === "needs_answer" ? "warn" : "";
    return `<ol class="steps">${STEPS.map((t, i) => {
      const n = i + 1;
      const cls = n < at ? "done" : n === at ? "now " + stuck : "";
      return `<li class="${cls}"><span class="dot">${n < at ? "✓" : n}</span><span class="lbl">${t}</span></li>`;
    }).join("")}</ol>`;
  }

  function jobCard(j, ov) {
    const pct = j.size ? Math.min(100, Math.round((100 * j.done_bytes) / j.size)) : 0;
    const speed = ov && ov.speed && j.status === "downloading" ? ` · ${fmtBytes(ov.speed)}/s` : "";
    const nightLink = j.links.find((l) => l.not_before && l.status === "queued" && l.not_before * 1000 > Date.now());
    const windowLink = !nightLink && j.links.find((l) => l.window_at && l.status === "queued");
    const errs = j.links.filter((l) => l.status === "error").length;
    let body = `<div class="spread"><div class="job-title ellipsis"><a href="#/job/${j.id}" style="color:inherit">${titleYear(j)}</a></div>${statusTag(j.status)}</div>
      <div class="meta"><span>${j.kind === "movie" ? "Movie" : "Series"}</span><span>${fmtBytes(j.size)}</span><span>${j.links.length} link${j.links.length === 1 ? "" : "s"}</span>
      ${j.genres.includes("Documentary") ? '<span class="tag acc">Documentary</span>' : ""}${j.source === "inbox" ? '<span class="tag">From the Inbox</span>' : ""}</div>`;
    if (j.status === "draft" || j.status === "needs_answer") {
      body += libChips(j, ov) + targetLine(j) + noteBlock(j) + questionBlock(j);
      if (j.status === "draft") {
        body += `<div class="row"><button class="btn primary" data-action="start" data-job="${j.id}">Start</button>
          <button class="btn" data-action="tonight" data-job="${j.id}">Tonight</button>
          <a class="btn ghost" href="#/job/${j.id}">Edit details</a><div class="grow"></div>
          <button class="btn ghost danger" data-action="cancel" data-job="${j.id}">Cancel</button></div>`;
      }
    } else {
      body += targetLine(j) + steps(j);
      body += `<div class="bar"><i style="width:${pct}%"></i></div><div class="spread small muted"><span>${pct}% of ${fmtBytes(j.size)}${speed}</span>
        <span>${nightLink ? "Starts " + fmtTime(nightLink.not_before) : windowLink ? "Waits for the download window, " + fmtTime(windowLink.window_at) : errs ? `${errs} link${errs > 1 ? "s" : ""} need${errs > 1 ? "" : "s"} you` : j.wait_all && j.status === "waiting" ? "Waiting for the other links" : ""}</span></div>
        <div class="row"><a class="btn sm ghost" href="#/job/${j.id}">Details</a>${j.status !== "moving" ? `<button class="btn sm ghost danger" data-action="cancel" data-job="${j.id}">Cancel</button>` : ""}</div>`;
    }
    return `<div class="card job">${posterEl(j.poster, j.title)}<div class="job-body">${body}</div></div>`;
  }

  // ---------- one job ----------
  async function renderJob(id, refresh) {
    if (!refresh) title("Details");
    let j;
    try { j = await api("GET", "/api/jobs/" + id); } catch (e) { page().innerHTML = `<div class="empty"><b>Not found</b>${esc(e.message)}</div>`; return; }
    if (state.route !== "job/" + id) return;  // moved to another page meanwhile
    if (!state.overview) { try { state.overview = await api("GET", "/api/overview"); sideInfo(state.overview); } catch (e) { /* shown later */ } }
    const ov = state.overview || { libraries: [] };
    if (j.status === "needs_captcha") { title("Needs captcha"); page().innerHTML = `<div class="section">${captchaCard(j)}</div>`; return; }
    title(j.title);
    const editable = !["moving", "done", "canceled"].includes(j.status);
    const pct = j.size ? Math.min(100, Math.round((100 * j.done_bytes) / j.size)) : 0;
    let html = `<div class="card job section">${posterEl(j.poster, j.title)}<div class="job-body">
      <div class="spread"><div class="job-title">${titleYear(j)}</div>${statusTag(j.status)}</div>
      <div class="meta"><span>${j.kind === "movie" ? "Movie" : "Series"}</span><span>${fmtBytes(j.size)}</span>${j.genres.map((g) => `<span>${esc(g)}</span>`).join("")}</div>
      ${j.target && !j.show_unknown ? `<div class="path">${esc(j.target)}${j.kind === "series" ? "/" + esc(seasonsOf(j)) : ""}</div>` : ""}
      ${j.error ? `<div class="err">${esc(j.error)}</div>` : ""}
      ${!["draft", "canceled"].includes(j.status) ? steps(j) : ""}
      ${!["draft", "done", "canceled"].includes(j.status) ? `<div class="bar"><i style="width:${pct}%"></i></div><div class="muted small">${pct}% of ${fmtBytes(j.size)}</div>` : ""}
      ${j.note && j.status === "draft" && !j.recheck && !j.show_unknown ? `<div class="question small">${esc(j.note)} Search below to pick the right one, or press Start if it is.</div>` : ""}
      ${j.moved ? `<div class="small">${movedLine(j)} ${plexTag(j)} <a href="#/history">History</a></div>` : ""}
      ${j.moved && j.kind === "series" && j.moved.history_id ? `<div class="row"><a class="btn sm" href="#/history?fixshow=${j.moved.history_id}">Fix show</a><span class="muted small">Filed under the wrong show or season?</span></div>` : ""}
      ${questionBlock(j)}</div></div>`;
    if (editable && (j.kind === "series" || j.show_unknown)) {
      html += `<details class="section card stack match-box"${j.show_unknown ? " open" : ""} data-key="mb-${j.id}"><summary><b>Match to show</b>
        <span class="muted small">Put this in a show you pick.</span></summary><div id="mb-slot-job-${j.id}"></div></details>`;
    }
    // A draft shows its details to check. Once it's saved and started they fold away until you ask for them.
    const showDetails = j.status === "draft" || state.openDetails.has(j.id);
    if (editable && !showDetails) {
      html += `<div class="section row"><button class="btn sm" data-action="show-details" data-job="${j.id}">Edit details</button>
        <span class="muted small">Title, folder, library and archive password.</span></div>`;
    }
    if (editable && showDetails) {
      const draft = j.status === "draft";
      html += `<div class="section card stack"><div class="spread"><h2>Details</h2>${draft ? "" : `<button class="btn sm ghost" data-action="hide-details" data-job="${j.id}">Hide</button>`}</div>
        <div class="grid3"><label class="f"><span>Title</span><input type="text" id="j-title" value="${esc(j.title)}"></label>
        <label class="f"><span>Year</span><input type="number" id="j-year" value="${esc(j.year || "")}"></label>
        <label class="f"><span>Type</span><select id="j-kind"><option value="movie"${j.kind === "movie" ? " selected" : ""}>Movie</option><option value="series"${j.kind === "series" ? " selected" : ""}>Series</option></select></label></div>
        <label class="f"><span>Folder name</span><input type="text" id="j-folder" value="${esc(j.folder_name)}"></label>
        <div><div class="muted small" style="margin-bottom:6px">Library</div>${libChips(j, ov)}</div>
        ${j.kind === "series" ? `<label class="check"><input type="checkbox" id="j-wait" ${j.wait_all ? "checked" : ""}><span>Wait until every link has downloaded before moving anything<br><span class="muted small">Use this when one season is split across several links.</span></span></label>` : ""}
        <label class="f"><span>Archive password (only if the download is a locked zip or rar)</span><input type="password" id="j-pass" placeholder="${j.has_password ? "Saved. Type to change it." : ""}" autocomplete="off"></label>
        <div class="row"><button class="btn primary" data-action="save-job" data-job="${j.id}" data-start="${draft ? 1 : ""}">${draft ? "Save and start" : "Save"}</button>
        ${draft ? `<button class="btn ghost" data-action="save-job" data-job="${j.id}">Save only</button>` : ""}<span class="muted small">Changing the title or year recalculates the folder.</span></div></div>`;
      html += `<div class="section card stack"><h2>Wrong title?</h2><form class="row" id="tmdb-form"><input type="search" id="tmdb-q" class="grow" placeholder="Search TMDB" value="${esc(j.title)}" style="flex:1"><button class="btn">Search</button></form><div id="tmdb-results"></div></div>`;
    }
    html += `<div class="section"><div class="spread"><h2>Links</h2>${editable ? `<form class="row more-link" id="more-link"><input type="text" id="more-url" placeholder="Add another link to this ${j.kind === "series" ? "show" : "job"}"><button class="btn sm">Add</button></form>` : ""}</div><div class="cards">`;
    for (const l of j.links) {
      const lpct = l.size ? Math.min(100, Math.round((100 * l.bytes_done) / l.size)) : 0;
      const nSel = l.files.filter((f) => f.selected).length;
      html += `<div class="card stack"><div class="spread"><div class="grow"><div class="ellipsis"><b>${esc(l.name)}</b></div>
        <div class="muted small">${fmtBytes(l.size)} · ${l.files.length} file${l.files.length === 1 ? "" : "s"}${l.season != null ? " · Season " + l.season : ""} · ${esc(LINK_STATUS[l.status] || l.status)}${l.attempts > 1 ? ` · try ${l.attempts}` : ""}</div></div>
        <div class="row">${watchButton(j, l)}${l.status === "error" ? `<button class="btn sm primary" data-action="fix" data-link="${l.id}">Fix it</button>` : ""}
        ${["queued", "error"].includes(l.status) && editable ? `<button class="btn sm ghost" data-action="skip-link" data-link="${l.id}">Skip</button>` : ""}</div></div>
        ${l.status === "downloading" ? `<div class="bar"><i style="width:${lpct}%"></i></div>` : ""}
        ${l.error ? `<div class="err">${esc(l.error)}</div>` : ""}
        ${l.files.length > 1 && editable && ["queued", "downloading", "error"].includes(l.status) ? `<details><summary class="small">Choose files (${nSel} of ${l.files.length})</summary>
          <p class="muted tiny">MEGA sends the whole link; files you untick are deleted after download.</p>
          <div class="files" data-link="${l.id}">${l.files.map((f) => `<label class="check ${f.junk ? "junk" : ""}"><input type="checkbox" value="${esc(f.path)}" ${f.selected ? "checked" : ""}><span class="grow ellipsis">${esc(f.path)}</span><span class="muted">${fmtBytes(f.size)}</span></label>`).join("")}</div>
          <button class="btn sm" style="margin-top:8px" data-action="save-files" data-link="${l.id}">Save choice</button></details>` : ""}</div>`;
    }
    html += `</div></div><div class="row">`;
    if (j.status === "draft") html += `<button class="btn primary" data-action="start" data-job="${j.id}">Start now</button><button class="btn" data-action="tonight" data-job="${j.id}">Start tonight</button>`;
    if (j.status === "failed" || j.status === "canceled") html += `<button class="btn primary" data-action="start" data-job="${j.id}">Try again</button>`;
    if (editable) html += `<div class="grow"></div><button class="btn ghost danger" data-action="cancel" data-job="${j.id}">Cancel</button>`;
    if (["done", "canceled", "failed"].includes(j.status)) html += `<div class="grow"></div><button class="btn ghost danger" data-action="delete-job" data-job="${j.id}">Remove from list</button>`;
    html += `</div>`;
    if (refresh) morph(page(), html); else page().innerHTML = html;

    const slot = $(`#mb-slot-job-${j.id}`);
    if (slot && !slot.innerHTML) matchBox(slot, "job", j.id, j.library_id, j.show_unknown ? "" : j.title);
    const tf = $("#tmdb-form");
    if (tf && !tf.dataset.bound) tf.addEventListener("submit", (e) => { e.preventDefault(); tmdbSearch(j); });
    const ml = $("#more-link");
    if (tf) tf.dataset.bound = "1";
    if (ml && !ml.dataset.bound) ml.dataset.bound = "1", ml.addEventListener("submit", async (e) => {
      e.preventDefault();
      try { await api("POST", "/api/jobs", { url: $("#more-url").value, job_id: j.id }); toast("Link added."); renderJob(j.id); }
      catch (err) { toast(err.message); }
    });
    if (!refresh && ["queued", "downloading", "processing", "waiting", "moving"].includes(j.status)) {
      every(() => { if (!busy() && !document.hidden && !$("details[open]")) renderJob(id, true); }, 4000);
    }
  }

  // Folder watch: a show's folder link can be watched for new episodes (only with folder watch on).
  const isFolderLink = (u) => /\/folder\/[^#]+#[^/]+(\/folder\/[^/]+)?$|#F!/.test(u || "");
  function watchButton(j, l) {
    if (j.kind !== "series" || !isFolderLink(l.url)) return "";
    if (l.watch === "active") return `<a class="tag ok" href="#/tracker?tab=monitored">Monitored</a>`;
    if (!watchOn()) return "";
    return `<button class="btn sm" data-action="watch-add" data-link="${l.id}">Monitor this link</button>`;
  }

  async function tmdbSearch(j) {
    const box = $("#tmdb-results");
    box.innerHTML = `<div class="muted small">Searching…</div>`;
    try {
      const hits = await api("GET", "/api/tmdb/search?q=" + encodeURIComponent($("#tmdb-q").value));
      box.innerHTML = hits.length ? `<div class="list">${hits.slice(0, 8).map((h) => `<div class="li">${posterEl(h.poster, h.title, "sm")}
        <div class="grow"><div><b>${esc(h.title)}</b> ${h.year ? `<span class="muted">(${h.year})</span>` : ""} <span class="tag">${h.kind === "movie" ? "Movie" : "Series"}</span></div>
        <div class="muted tiny ellipsis">${esc(h.overview)}</div></div><button class="btn sm" data-action="use-tmdb" data-job="${j.id}" data-id="${h.id}" data-kind="${h.kind}">Use this</button></div>`).join("")}</div>`
        : `<div class="muted small">Nothing found.</div>`;
    } catch (e) { box.innerHTML = `<div class="err">${esc(e.message)}</div>`; }
  }

  // ---------- Match to show / Fix show ----------
  // For links like "Season 200 - The Science of Connection" that name a season but not the show. The same box
  // is Match to show on a job and Fix show on something already filed (History).
  async function matchBox(slot, target, id, libId, q) {
    const ov = state.overview || (state.overview = await api("GET", "/api/overview"));
    const libs = ov.libraries.filter((l) => l.kind === "series");
    if (!libs.length) { slot.innerHTML = `<div class="muted small">Add a TV library in Settings first.</div>`; return; }
    const cur = libs.find((l) => l.id === libId) || libs[0];
    const p = target + "-" + id;
    slot.innerHTML = `<div class="stack mb">
      <label class="f"><span>Library</span><select id="mb-lib-${p}">${libs.map((l) => `<option value="${l.id}"${l.id === cur.id ? " selected" : ""}>${esc(l.name)}</option>`).join("")}</select></label>
      <form class="row" id="mb-form-${p}"><input type="search" id="mb-q-${p}" placeholder="Search TMDB for the show" value="${esc(q || "")}" style="flex:1"><button class="btn">Search</button></form>
      <div id="mb-hits-${p}"></div>
      <label class="f"><span>Or use a folder</span><input type="text" id="mb-folder-${p}" list="mb-list-${p}" placeholder="Pick a show you have, or type a new name" autocomplete="off"><datalist id="mb-list-${p}"></datalist></label>
      <div class="muted small">Use a folder when TMDB doesn't list the show.</div>
      <div class="muted small">Plex tags like {tvdb-362158} stay in the folder name.</div>
      <div class="row"><button class="btn sm primary" data-action="mb-use" data-target="${target}" data-id="${id}">Use this folder</button></div>
      <label class="check" id="mb-alike-${p}" hidden><input type="checkbox" id="mb-rem-${p}"><span>Do the same for every link named like this<br><span class="muted small" id="mb-count-${p}"></span></span></label>
      ${target === "history" ? `<div class="muted small">Files move to the right season folder.</div><div class="muted small">Old folders are deleted once they're empty.</div>` : ""}</div>`;
    const fill = async () => {
      try { const fs = await api("GET", `/api/libraries/${$("#mb-lib-" + p).value}/folders`); $("#mb-list-" + p).innerHTML = fs.map((f) => `<option value="${esc(f.name || f)}">`).join(""); } catch (e) { /* typing still works */ }
    };
    $("#mb-lib-" + p).addEventListener("change", fill);
    fill();
    $("#mb-form-" + p).addEventListener("submit", async (e) => {
      e.preventDefault();
      const box = $("#mb-hits-" + p);
      box.innerHTML = `<div class="muted small">Searching…</div>`;
      try {
        const hits = await api("GET", "/api/tmdb/search?kind=series&q=" + encodeURIComponent($("#mb-q-" + p).value));
        box.innerHTML = hits.length ? `<div class="list">${hits.slice(0, 8).map((h) => `<div class="li">${posterEl(h.poster, h.title, "sm")}
          <div class="grow"><div><b>${esc(h.title)}</b> ${h.year ? `<span class="muted">(${h.year})</span>` : ""}</div>
          <div class="muted tiny ellipsis">${esc(h.overview)}</div></div><button class="btn sm" data-action="mb-use" data-target="${target}" data-id="${id}" data-tmdb="${h.id}">Use this</button></div>`).join("")}</div>`
          : `<div class="muted small">Nothing found. Use a folder instead.</div>`;
      } catch (err) { box.innerHTML = `<div class="err">${esc(err.message)}</div>`; }
    });
    try {
      const a = await api("GET", `/api/${target === "job" ? "jobs" : "history"}/${id}/alike`);
      if (a.key !== null) {
        const bits = [];
        if (a.filed) bits.push(`Also fixes ${a.filed} already filed.`);
        if (a.waiting) bits.push(`Also matches ${a.waiting} still waiting.`);
        bits.push("New links named like this go there by themselves.");
        $("#mb-count-" + p).innerHTML = bits.map(esc).join("<br>");
        $("#mb-rem-" + p).checked = a.key === "";
        $("#mb-alike-" + p).hidden = false;
      }
    } catch (e) { /* the box works without it */ }
  }

  async function useShow(d) {
    const p = d.target + "-" + d.id;
    const body = { library_id: parseInt($("#mb-lib-" + p).value, 10) };
    if (d.tmdb) body.tmdb_id = parseInt(d.tmdb, 10);
    else body.folder = $("#mb-folder-" + p).value.trim();
    if (!body.tmdb_id && !body.folder) { toast("Pick a show or type a name."); return; }
    const rem = $("#mb-rem-" + p);
    body.remember = !!(rem && rem.checked && !$("#mb-alike-" + p).hidden);
    const r = await api("POST", `/api/${d.target === "job" ? "jobs" : "history"}/${d.id}/show`, body);
    const bits = [d.target === "job" ? "Matched." : `Moved ${r.moved} file${r.moved === 1 ? "" : "s"}.`];
    if (r.filed) bits.push(`Fixed ${r.filed} more.`);
    if (r.waiting) bits.push(`Matched ${r.waiting} waiting.`);
    if (r.problems && r.problems.length) bits.push(`${r.problems.length} couldn't be moved.`);
    toast(bits.join(" "));
    state.overview = null;
    if (d.target === "job") return renderJob(parseInt(d.id, 10));
    return renderHistory();
  }

  // ---------- library ----------
  async function renderLibrary(q) {
    title("Library");
    const lib = q.get("lib");
    if (!state.overview) { try { state.overview = await api("GET", "/api/overview"); sideInfo(state.overview); } catch (e) { /* chips are optional */ } }
    const libs = (state.overview && state.overview.libraries) || [];
    page().innerHTML = (libs.length ? `<div class="chips section">${libs.map((l) => `<a class="chip ${String(l.id) === lib ? "on" : ""}" href="#/library?lib=${l.id}">${esc(l.name)} <span class="n">${l.count}</span></a>`).join("")}</div>` : "") +
      `<form class="row section lib-search" id="lib-form"><span class="lib-ico">${ICON.search}</span><input type="search" id="lib-q" placeholder="Search for a movie or show before you download it" style="flex:1" value="${esc(q.get("q") || "")}"><button class="btn">Search</button></form><div id="lib-out"></div>`;
    $("#lib-form").addEventListener("submit", (e) => { e.preventDefault(); libSearch($("#lib-q").value, $("#lib-q").value === q.get("q") ? q.get("tmdb") : ""); });
    if (q.get("q")) libSearch(q.get("q"), q.get("tmdb"));
    else if (lib) libFolders(parseInt(lib, 10));
    else $("#lib-out").innerHTML = `<div class="empty"><b>Do I already have it?</b>Search here before you grab a link. You'll see which library has it and, for shows, which seasons.</div>`;
  }

  async function libFolders(id) {
    const out = $("#lib-out");
    const l = ((state.overview && state.overview.libraries) || []).find((x) => x.id === id);
    try {
      const folders = await api("GET", `/api/libraries/${id}/folders`);
      title(l ? l.name : "Library");
      out.innerHTML = `<div class="muted small section">${folders.length} folder${folders.length === 1 ? "" : "s"}${l ? " in " + esc(l.path) : ""}</div>
        <div class="card list">${folders.map((f) => `<div class="li"><span class="grow ellipsis">${esc(f)}</span><a class="small" href="#/library?q=${encodeURIComponent(f.replace(/\s*\(\d{4}\)\s*$/, ""))}">Look up</a></div>`).join("") || '<div class="li muted">Empty.</div>'}</div>`;
    } catch (e) { out.innerHTML = `<div class="err">${esc(e.message)}</div>`; }
  }

  // Yours first (the show you came from in the Tracker on top, with what's missing), then other titles by
  // that name you don't have, like a show's spinoffs.
  async function libSearch(text, tmdb) {
    const out = $("#lib-out");
    if (!text.trim()) return;
    const tq = tmdb ? "&tmdb=" + encodeURIComponent(tmdb) : "";
    history.replaceState(null, "", "#/library?q=" + encodeURIComponent(text) + tq);
    out.innerHTML = `<div class="muted">Searching…</div>`;
    try {
      const rows = await api("GET", "/api/library/search?q=" + encodeURIComponent(text) + tq);
      if (!rows.length) { out.innerHTML = `<div class="empty"><b>Nothing found</b>Try a shorter name.</div>`; return; }
      const mine = rows.filter((r) => r.group !== "similar"), similar = rows.filter((r) => r.group === "similar");
      const group = (head, list) => list.length ? `<h2 class="section-head">${head}</h2><div class="results section">${list.map(resultCard).join("")}</div>` : "";
      out.innerHTML = similar.length && mine.length ? group("In your library", mine) + group("Similar names you don't have", similar)
        : `<div class="results">${rows.map(resultCard).join("")}</div>`;
    } catch (e) { out.innerHTML = `<div class="err">${esc(e.message)}</div>`; }
  }

  function resultCard(r) {
    const have = r.have ? `<span class="tag ${r.have.score >= 0.97 ? "ok" : "warn"}">${r.have.score >= 0.97 ? "In" : "Maybe in"} ${esc(r.have.library)}</span><div class="path">${esc(r.have.folder)}</div>` : `<span class="tag">Not in your library</span>`;
    const disk = Object.entries(r.seasons_on_disk || {});
    const seasons = disk.length ? `<div class="chips">${disk.map(([n, c]) => `<span class="chip static have" title="${c} files">S${n}</span>`).join("")}</div>` : "";
    const more = r.id && r.kind === "series" ? `<button class="btn sm" data-action="show" data-id="${r.id}" data-name="${esc(r.title)}" data-folder="${esc(r.have ? r.have.folder : "")}" data-lib="${r.have ? r.have.library_id : ""}">Seasons and news</button>`
      : r.id && r.kind === "movie" && !r.have ? `<button class="btn sm" data-action="track-movie" data-id="${r.id}">Track release</button>` : "";
    const missing = r.missing ? `<div class="small gap">${esc(r.missing)}</div>` : "";
    return `<div class="card result${r.pinned ? " pinned" : ""}">${posterEl(r.poster, r.title)}<div class="job-body"><div><b>${esc(r.title)}</b> ${r.year ? `<span class="muted">(${r.year})</span>` : ""}</div>
      <div class="meta">${r.kind ? `<span>${r.kind === "movie" ? "Movie" : "Series"}</span>` : ""}${r.documentary ? '<span class="tag acc">Documentary</span>' : ""}</div>
      ${have}${seasons}${missing}<div class="row">${more}</div><div class="show-box" id="show-${r.id}"></div></div></div>`;
  }

  async function showBox(btn) {
    const id = btn.dataset.id;
    const box = $("#show-" + id);
    box.innerHTML = `<div class="muted small">Loading…</div>`;
    try {
      const d = await api("GET", "/api/shows/" + id);
      const card = btn.closest(".result");
      const onDisk = new Set($$(".chip.have", card).map((c) => parseInt(c.textContent.slice(1), 10)));
      box.innerHTML = `<div class="small"><span class="tag ${d.status === "Ended" || d.status === "Canceled" ? "" : "ok"}">${esc(d.status || "Unknown")}</span>
        ${d.next_air ? ` Next episode ${esc(d.next_air)}` : ""}</div>
        <div class="chips" style="margin-top:6px">${d.seasons.map((s) => `<span class="chip static ${onDisk.has(s.season) ? "have" : "miss"}" title="${s.episodes} episodes">S${s.season}</span>`).join("")}</div>
        <label class="check small"><input type="checkbox" data-action="watch" data-id="${id}" data-name="${esc(btn.dataset.name)}" data-folder="${esc(btn.dataset.folder)}" data-lib="${esc(btn.dataset.lib)}" ${d.watching ? "checked" : ""}>
        <span>Tell me when a season starts, a finale airs, or the show ends</span></label>`;
      btn.remove();
    } catch (e) { box.innerHTML = `<div class="err">${esc(e.message)}</div>`; }
  }

  // ---------- tracker: series and movies ----------
  const onTracker = () => ["tracker", "shows"].includes(state.route);
  // The Watched tab is there once folder watch is on (Settings → Extras).
  const watchOn = () => !!(state.overview && state.overview.extras && state.overview.extras.watch);
  const trackerTabs = (tab) => `<div class="tabs">${[["series", "Series"], ["movies", "Movies"]].concat(watchOn() || tab === "monitored" ? [["monitored", "Monitored links"]] : []).map(([k, t]) =>
    `<a href="#/tracker?tab=${k}" class="${k === tab ? "on" : ""}">${t}</a>`).join("")}</div>`;

  async function renderTracker(q) {
    const tab = (q && q.get("tab")) || state.trackerTab;
    state.trackerTab = tab === "watched" ? "monitored" : ["movies", "monitored"].includes(tab) ? tab : "series";
    title("Tracker");
    if (!state.overview) { try { state.overview = await api("GET", "/api/overview"); sideInfo(state.overview); } catch (e) { /* tabs only */ } }
    return state.trackerTab === "movies" ? renderMovies() : state.trackerTab === "monitored" ? renderWatched() : renderShows();
  }

  // ---------- watched folders ----------
  const WATCH_TAG = { active: ["Monitoring", "ok"], stopped: ["Stopped", ""], dead: ["Link gone", "bad"] };
  const everyText = (h) => (h === 24 ? "once a day" : h === 48 ? "every 2 days" : `every ${h} hours`);
  async function renderWatched() {
    try {
      const r = await api("GET", "/api/watches");
      if (!onTracker()) return;
      const intro = r.enabled
        ? `<p class="muted small section">Linkarr opens these MEGA folder links ${everyText(r.every)} and downloads only the new episodes.${r.stop_days ? ` A link with nothing new for ${r.stop_days} days stops by itself.` : ""} Add one with Monitor this link on a show's Details page, or send <span class="mono">/monitor</span> and the link to the bot.</p>`
        : `<div class="card alert info section"><span class="ic">${ICON.info}</span><div class="grow"><h3>Monitored links are off</h3><div class="muted small">Nothing here is checked until you turn it on.</div></div><a class="btn sm primary" href="#/settings?tab=extras">Turn on</a></div>`;
      page().innerHTML = trackerTabs("monitored") + intro + (r.watches.length
        ? `<div class="card list watched">${r.watches.map((w) => {
            const [t, c] = WATCH_TAG[w.status] || [w.status, ""];
            const why = w.status === "stopped" ? (w.stop_reason === "idle" ? "Nothing new lately" : "Stopped by you") : w.status === "dead" ? "Folder removed" : "";
            return `<div class="li">${posterEl(w.poster, w.title, "sm")}<div class="grow">
              <div class="r-title"><b>${esc(w.title)}</b>${w.year ? ` <span class="yr">(${esc(w.year)})</span>` : ""}</div>
              <div class="kv"><span>Last checked</span><b>${w.last_check ? fmtAgo(w.last_check) : "Not yet"}</b></div>
              <div class="kv"><span>Episodes</span><b>${w.episodes}</b></div>
              <div class="kv"><span>Last new</span><b>${w.last_new ? fmtAgo(w.last_new) : "Nothing yet"}</b></div>
              ${why ? `<div class="kv"><span>Why</span><b>${esc(why)}</b></div>` : ""}
              ${w.error && w.status === "active" ? `<div class="small gap">${esc(w.error)}</div>` : ""}
              <div class="row">${w.status === "active" ? `<button class="btn sm primary" data-action="watch-check" data-id="${w.id}" ${r.enabled ? "" : "disabled"}>Check now</button>
                <button class="btn sm" data-action="watch-set" data-id="${w.id}" data-on="0">Stop</button>`
                : w.status === "stopped" ? `<button class="btn sm primary" data-action="watch-set" data-id="${w.id}" data-on="1">Resume</button>` : ""}
                <button class="btn sm ghost danger" data-action="watch-del" data-id="${w.id}">Remove</button></div></div>
              <span class="tag ${c}">${t}</span></div>`;
          }).join("")}</div>`
        : `<div class="empty"><b>No monitored links yet</b>Open a show you've added, then press Monitor this link next to its folder link.</div>`);
    } catch (e) { page().innerHTML = trackerTabs("monitored") + `<div class="err">${esc(e.message)}</div>`; }
  }

  async function renderMovies() {
    try {
      const rows = await api("GET", "/api/movies");
      if (!onTracker()) return;
      page().innerHTML = trackerTabs("movies") + `<p class="muted small section">Add a movie you're waiting for. Linkarr checks once a day and tells you when it's out on Blu-ray, to buy or rent, or on a streaming service. It drops off this list once it's in your library.</p>
        <form class="row section lib-search" id="mv-form"><span class="lib-ico">${ICON.search}</span><input type="search" id="mv-q" placeholder="Find a movie to track" style="flex:1"><button class="btn">Search</button></form>
        <div id="mv-results" class="section"></div>` + (rows.length
        ? `<div class="card list movies">${rows.map((m) => `<div class="li">${posterEl(m.poster, m.title, "sm")}<div class="grow">
            <div class="r-title"><b>${esc(m.title)}</b>${m.year ? ` <span class="yr">(${esc(m.year)})</span>` : ""}</div>
            ${m.lines.length ? m.lines.map((x) => `<div class="kv"><span>${esc(x.label)}</span><b class="${x.out ? "is-out" : ""}">${esc(x.value)}</b></div>`).join("")
              : `<div class="muted small">${m.checked ? "No home release dates yet." : "Not checked yet."}</div>`}</div>
            ${m.out ? '<span class="tag ok">Out</span>' : ""}<button class="btn sm ghost" data-action="untrack-movie" data-id="${m.tmdb_id}">Stop</button></div>`).join("")}</div>`
        : `<div class="empty"><b>No movies yet</b>Search above, or use “Track release” on a movie in Library.</div>`);
      $("#mv-form").addEventListener("submit", (e) => { e.preventDefault(); movieSearch($("#mv-q").value); });
    } catch (e) { page().innerHTML = trackerTabs("movies") + `<div class="err">${esc(e.message)}</div>`; }
  }

  async function movieSearch(text) {
    const box = $("#mv-results");
    if (!text.trim()) return;
    box.innerHTML = `<div class="muted small">Searching…</div>`;
    try {
      const hits = (await api("GET", "/api/tmdb/search?kind=movie&q=" + encodeURIComponent(text))).slice(0, 6);
      box.innerHTML = hits.length ? `<div class="card list">${hits.map((h) => `<div class="li">${posterEl(h.poster, h.title, "sm")}<div class="grow">
          <div><b>${esc(h.title)}</b> ${h.year ? `<span class="muted">(${h.year})</span>` : ""}</div><div class="muted tiny ellipsis">${esc(h.overview)}</div></div>
          <button class="btn sm primary" data-action="track-movie" data-id="${h.id}">Track</button></div>`).join("")}</div>` : `<div class="muted small">Nothing found.</div>`;
    } catch (e) { box.innerHTML = `<div class="err">${esc(e.message)}</div>`; }
  }

  async function renderShows() {
    try {
      const [rows, imp] = await Promise.all([api("GET", "/api/shows"), api("GET", "/api/shows/track-all")]);
      if (!onTracker()) return;
      page().innerHTML = trackerTabs("series") + `<div class="row section" style="align-items:flex-start;gap:12px;flex-wrap:wrap"><p class="muted small" style="margin:0;flex:1 1 280px">Linkarr checks these once a day and tells you when a season starts, a finale airs, or a show ends. Anything it files into a series library is added; switch one off any time.</p>
        <button class="btn primary" data-action="track-all" ${imp.running ? "disabled" : ""}>${ICON.plus}Track all my series</button></div>
        <div id="track-all">${trackAllBox(imp)}</div>` + (rows.length
        ? showFilter(rows) + `<div class="card list">${rows.filter((s) => !state.gapsOnly || s.missing).map((s) => `<div class="li"><div class="grow"><b>${esc(s.name)}</b>
          <div class="muted small">${esc(s.status || "Not checked yet")}${s.next_air ? " · next episode " + esc(s.next_air) : ""}${s.seasons ? ` · ${s.seasons} season${s.seasons === 1 ? "" : "s"}` : ""}</div>
          ${s.missing ? `<div class="small gap">${esc(s.missing)}</div>` : s.checked ? `<div class="small have-all">You have every episode</div>` : ""}</div>
          ${s.missing ? `<a class="btn sm" href="#/library?q=${encodeURIComponent(s.name)}&tmdb=${s.tmdb_id}">Find</a>` : ""}
          <button class="btn sm ghost" data-action="unwatch" data-id="${s.tmdb_id}">Stop</button></div>`).join("")}</div>`
        : `<div class="empty"><b>No series yet</b>Press “Track all my series” to add the shows you already have, or turn it on from a search result in Library.</div>`);
      if (imp.running) every(pollTrackAll, 1500);
    } catch (e) { page().innerHTML = trackerTabs("series") + `<div class="err">${esc(e.message)}</div>`; }
  }

  function showFilter(rows) {
    const gaps = rows.filter((r) => r.missing).length;
    if (!gaps && !state.gapsOnly) return "";
    return `<div class="chips section"><button class="chip ${state.gapsOnly ? "" : "on"}" data-action="gaps" data-v="0">All ${rows.length}</button>
      <button class="chip ${state.gapsOnly ? "on" : ""}" data-action="gaps" data-v="1">Missing episodes ${gaps}</button></div>`;
  }

  function trackAllBox(imp) {
    if (!imp || (!imp.running && !imp.total && !imp.error)) return "";
    if (imp.error) return `<div class="err section">${esc(imp.error)}</div>`;
    if (imp.running) {
      const pct = imp.total ? Math.round((imp.done / imp.total) * 100) : 0;
      return `<div class="card section stack"><div class="small">Matching your shows on TMDB… ${imp.done} of ${imp.total || "?"}</div><div class="bar"><i style="width:${pct}%"></i></div></div>`;
    }
    const miss = imp.unmatched || [];
    return `<div class="card section stack"><div class="small"><b>Added ${imp.added} series.</b> ${miss.length ? `${miss.length} folder${miss.length === 1 ? "" : "s"} didn't match for sure. Look each one up in Library and turn it on there.` : "Every folder matched."}</div>
      ${miss.length ? `<div class="list">${miss.map((m) => `<div class="li"><div class="grow ellipsis">${esc(m.folder)} <span class="muted small">${esc(m.library)}</span></div><a class="btn sm" href="#/library?q=${encodeURIComponent(m.folder.replace(/\s*[\[(].*$/, ""))}">Find</a></div>`).join("")}</div>` : ""}</div>`;
  }

  async function pollTrackAll() {
    if (!onTracker()) return;
    const imp = await api("GET", "/api/shows/track-all").catch(() => null);
    if (!imp) return;
    if (imp.running) { const box = $("#track-all"); if (box) morph(box, trackAllBox(imp)); return; }
    clearInterval(state.timer);
    renderShows();
  }

  // ---------- history ----------
  async function renderHistory(q) {
    title("History");
    try {
      const rows = state.history = await api("GET", "/api/history");
      page().innerHTML = rows.length ? `<div class="card list">${rows.map((h) => `<div class="li">${posterEl(h.poster, h.title || h.summary, "sm")}
        <div class="grow"><div class="ellipsis"><b>${esc(h.title || h.summary)}</b></div><div class="muted small">${esc(h.summary)} · ${h.moves.length} file${h.moves.length === 1 ? "" : "s"}${h.recycled.length ? ` · ${h.recycled.length} replaced` : ""} · ${fmtAgo(h.ts)}</div>
        <details class="tiny"><summary class="muted">Files</summary><div class="path">${h.moves.map((m) => esc(m.to || "")).join("<br>")}</div></details>
        ${h.undone_at ? '<span class="tag">Undone</span>' : `<div class="row" style="margin-top:6px">${!h.kind || h.kind === "series" ? `<button class="btn sm" data-action="fixshow" data-id="${h.id}">Fix show</button>` : ""}<button class="btn sm" data-action="refile" data-id="${h.id}">Move to…</button><button class="btn sm ghost" data-action="undo" data-id="${h.id}">Undo</button></div>`}</div></div>
        <div id="refile-${h.id}"></div>`).join("")}</div>`
        : `<div class="empty"><b>Nothing moved yet</b>Every move is listed here and can be undone.</div>`;
      const want = q && q.get && q.get("refile");
      const btn = want && $(`[data-action="refile"][data-id="${CSS.escape(want)}"]`);
      if (btn) { await refileBox(btn); btn.scrollIntoView({ block: "center" }); }
      const fix = q && q.get && q.get("fixshow");
      const fb = fix && $(`[data-action="fixshow"][data-id="${CSS.escape(fix)}"]`);
      if (fb) { await fixShowBox(fb); fb.scrollIntoView({ block: "center" }); }
    } catch (e) { page().innerHTML = `<div class="err">${esc(e.message)}</div>`; }
  }

  // "Fix show": something filed under the wrong show (like "Season 200 - …/Season 20") goes into the right one.
  async function fixShowBox(btn) {
    const id = btn.dataset.id;
    const box = $("#refile-" + id);
    if (box.dataset.mode === "fix" && box.innerHTML) { box.innerHTML = ""; return; }
    box.dataset.mode = "fix";
    const h = state.history.find((x) => String(x.id) === id) || {};
    box.innerHTML = `<div class="card stack refile"><div class="muted small">Now in <span class="mono">${esc(h.where || "")}</span></div><div id="mb-slot-h-${id}"></div></div>`;
    await matchBox($("#mb-slot-h-" + id), "history", id, null, "");
  }

  // "Move to…": re-file something that went to the wrong folder or library.
  async function refileBox(btn) {
    const id = btn.dataset.id;
    const box = $("#refile-" + id);
    if (box.dataset.mode !== "fix" && box.innerHTML) { box.innerHTML = ""; return; }
    box.dataset.mode = "move";
    const h = state.history.find((x) => String(x.id) === id) || {};
    const ov = state.overview || (state.overview = await api("GET", "/api/overview"));
    const kind = h.kind || "movie";
    const libs = ov.libraries.filter((l) => l.kind === kind);
    const now = h.where || "";
    const cur = libs.find((l) => now.includes("/" + l.path + "/")) || libs[0];
    box.innerHTML = `<div class="card stack refile"><div class="muted small">Now in <span class="mono">${esc(now)}</span></div>
      <div class="grid2"><label class="f"><span>Library</span><select id="rf-lib-${id}">${libs.map((l) => `<option value="${l.id}"${cur && l.id === cur.id ? " selected" : ""}>${esc(l.name)}</option>`).join("")}</select></label>
      <label class="f"><span>Folder (pick one or type a new name)</span><input type="text" id="rf-folder-${id}" list="rf-list-${id}" value="${esc(now.split("/").pop())}" autocomplete="off"><datalist id="rf-list-${id}"></datalist></label></div>
      <div class="row"><button class="btn sm primary" data-action="refile-go" data-id="${id}">Move</button><span class="muted small">Subtitles next to the files move too. Undo still works afterwards.</span></div></div>`;
    const fill = async () => {
      const lib = $("#rf-lib-" + id).value;
      try { const fs = await api("GET", `/api/libraries/${lib}/folders`); $("#rf-list-" + id).innerHTML = fs.map((f) => `<option value="${esc(f.name || f)}">`).join(""); } catch (e) { /* typing still works */ }
    };
    $("#rf-lib-" + id).addEventListener("change", fill);
    fill();
  }

  // ---------- settings ----------
  const TABS = [["check", "Setup check"], ["libraries", "Libraries"], ["mega", "MEGA"], ["connections", "Connections"], ["naming", "Naming"], ["behaviour", "Behaviour"], ["extras", "Extras"],
    ["look", "Look"], ["tools", "Tools"], ["logs", "Logs"], ["account", "Account and backup"]];
  const THEMES = [["dark", "Dark Utility", ["#0e1115", "#151a20", "#f2a93b"]], ["light", "Light", ["#f4f5f8", "#ffffff", "#4f46e5"]],
    ["poster", "Poster", ["#0b0c10", "#1a1d26", "#7dd3fc"]], ["postermono", "Poster Mono", ["#0a0b0f", "#1a1d27", "#7dd3fc"]],
    ["mono", "Mono", ["#fafafa", "#e4e4e4", "#111111"]], ["terminal", "Terminal", ["#000000", "#121813", "#3ddc71"]], ["auto", "Match device", ["#0e1115", "#f4f5f8", "#f2a93b"]]];
  const settingsTabs = (tab) => `<div class="tabs">${TABS.map(([k, t]) => `<a href="#/settings?tab=${k}" class="${k === tab ? "on" : ""}">${t}</a>`).join("")}
    <a href="#/history" class="tab-link">History</a></div>`;

  async function renderSettings(q) {
    title("Settings");
    const tab = q.get("tab") || "check";
    if (tab === "check") return renderCheck();
    if (tab === "logs") return renderLogs();
    if (tab === "tools") return renderDecoder();
    try { state.settings = await api("GET", "/api/settings"); } catch (e) { page().innerHTML = `<div class="err">${esc(e.message)}</div>`; return; }
    const s = state.settings;
    const p = s.prefs;
    let html = settingsTabs(tab);
    const field = (k, label, type, extra) => `<label class="f"><span>${label}</span><input type="${type || "text"}" data-pref="${k}" value="${esc(Array.isArray(p[k]) ? p[k].join(", ") : p[k])}" ${extra || ""}></label>`;
    const box = (k, label, hint) => `<label class="check"><input type="checkbox" data-pref="${k}" ${p[k] ? "checked" : ""}><span>${label}${hint ? `<br><span class="muted small">${hint}</span>` : ""}</span></label>`;
    const save = `<div class="row"><button class="btn primary" data-action="save-prefs">Save</button></div>`;

    if (tab === "libraries") {
      html += `<div class="card stack"><p class="muted small">Folders are inside the media folder mapped into the container (<span class="mono">${esc(s.media_dir)}</span>). Mark a library as Documentary and TMDB's Documentary genre picks it automatically.</p>
        <div class="lib-row head"><span>Name</span><span>Type</span><span>Folder</span><span>Plex library</span><span></span></div>
        ${s.libraries.map((l, i) => libRow(l, i, s.libraries.length)).join("")}
        <div style="border-top:1px solid var(--line);padding-top:12px"><h3 style="margin-bottom:8px">Add a library</h3>${libRow({ id: "", name: "", kind: "movie", path: "", documentary: 0, plex_section: "" }, -1, 0)}</div>
        <div class="row"><button class="btn sm ghost" data-action="plex-sections">Load Plex libraries</button><span class="muted small" id="plex-list"></span></div></div>`;
    } else if (tab === "mega") {
      html += `<div class="card stack">` + (s.mega.email
        ? `<div class="spread"><div><h3>Signed in</h3><div class="muted">${esc(s.mega.email)}</div></div><button class="btn" data-action="mega-logout">Sign out</button></div>
           <p class="muted small">Linkarr downloads with your account so your Pro transfer quota applies. Your password isn't stored; MEGA's own tool keeps the session.</p>
           ${s.quota && s.quota.max ? `<div class="quota big"><div class="spread"><span>Transfer allowance</span><span>${fmtBytes(Math.max(0, s.quota.max - s.quota.used))} left of ${fmtBytes(s.quota.max)}</span></div>
             <div class="bar${s.quota.used / s.quota.max >= 0.9 ? " low" : ""}"><i style="width:${Math.min(100, Math.round((100 * s.quota.used) / s.quota.max))}%"></i></div></div>` : ""}
           ${box("quota_guard", "Hold a download that would go over what's left", "It waits and starts by itself once your allowance has room again, instead of stopping halfway.")}
           <div class="row"><button class="btn primary" data-action="save-prefs">Save</button><button class="btn" data-action="quota">Check the allowance now</button></div>`
        : `${s.mega.installed ? "" : `<div class="err">MEGAcmd isn't installed in this container. Use the official Linkarr image.</div>`}
           <p class="muted small">Sign in so downloads use your account's transfer quota. Your password goes straight to MEGA's own tool and isn't saved by Linkarr.</p>
           <form class="stack" id="mega-form"><div class="grid2"><label class="f"><span>Email</span><input type="text" name="email" autocomplete="off" required></label>
           <label class="f"><span>Password</span><input type="password" name="password" autocomplete="off" required></label></div>
           <label class="f"><span>Two-factor code (if you use it)</span><input type="text" name="code" inputmode="numeric" autocomplete="one-time-code"></label>
           <div class="row"><button class="btn primary">Sign in to MEGA</button></div></form>`) + `</div>`;
    } else if (tab === "connections") {
      const set = (n) => (s.secrets[n] ? '<span class="tag ok">saved</span>' : '<span class="tag">not set</span>');
      html += `<div class="card stack section"><h2>Download site</h2>
          <p class="muted small">The site you get links from. It shows in the menu (and the phone's bottom bar) and opens in a new tab. Change it here whenever the address moves.</p>
          <div class="grid3">${field("site_name", "Name", "text", 'placeholder="My site" maxlength="40"')}${field("site_url", "Address", "url", 'placeholder="https://…"')}${field("site_badge", "Icon letters (up to 3)", "text", 'placeholder="ML" maxlength="3"')}</div>
          <div class="muted small">Leave the icon letters blank to use the capitals in the name.</div>
          <div class="row"><button class="btn primary" data-action="save-prefs">Save</button></div></div>
        <div class="card stack section"><div class="spread"><h2>TMDB</h2>${set("tmdb_key")}</div>
          <p class="muted small">Finds the right title, year and poster. Free key at themoviedb.org → Settings → API. The API key or the read access token both work.</p>
          <div class="row"><input type="password" id="sec-tmdb_key" placeholder="${s.secrets.tmdb_key ? "Saved. Paste a new one to change it." : "TMDB key"}" autocomplete="off" style="flex:1"><button class="btn" data-action="save-secret" data-name="tmdb_key">Save</button></div></div>
        <div class="card stack section"><div class="spread"><h2>Plex</h2>${set("plex_token")}</div>
          <p class="muted small">Optional. Plex rescans the right library when something lands.</p>
          ${field("plex_url", "Plex address", "url", 'placeholder="http://192.168.0.2:32400"')}
          <div class="row"><input type="password" id="sec-plex_token" placeholder="${s.secrets.plex_token ? "Saved. Paste a new one to change it." : "Plex token"}" autocomplete="off" style="flex:1"><button class="btn" data-action="save-secret" data-name="plex_token">Save token</button></div>
          ${save}</div>
        <div class="card stack"><div class="spread"><h2>Telegram</h2>${set("telegram_token")}</div>
          <p class="muted small">Optional. Send links to your bot, answer questions with buttons, get told when things land. Make a bot with @BotFather, paste its token, then send /start to it to get your chat id.</p>
          <div class="row"><input type="password" id="sec-telegram_token" placeholder="${s.secrets.telegram_token ? "Saved. Paste a new one to change it." : "Bot token"}" autocomplete="off" style="flex:1"><button class="btn" data-action="save-secret" data-name="telegram_token">Save token</button></div>
          ${field("telegram_chats", "Allowed chat ids (comma separated)")}
          ${box("notify_done", "Tell me when something lands")}${box("notify_series", "Show news: new season dates, finales, ended or canceled")}
          ${box("notify_movies", "Movie Tracker news: Blu-ray, digital and streaming dates")}
          <div class="row">${box("roundup_enabled", "Weekly roundup", "What was added, which shows have new episodes in the next two weeks, and your MEGA allowance. Sent at 10:00.")}
          <label class="f" style="width:auto"><span>On</span><select data-pref="roundup_day">${["mon", "tue", "wed", "thu", "fri", "sat", "sun"].map((x) => `<option value="${x}"${p.roundup_day === x ? " selected" : ""}>${{ mon: "Monday", tue: "Tuesday", wed: "Wednesday", thu: "Thursday", fri: "Friday", sat: "Saturday", sun: "Sunday" }[x]}</option>`).join("")}</select></label></div>
          ${box("chat_clear", "Clear the chat every night", "Each day starts with an empty chat. Pinned messages stay. Telegram only lets a bot delete messages from the last 48 hours. In a group, the bot must be an admin to delete your messages.")}
          <div class="grid3">${field("chat_clear_time", "Clear at", "time")}</div>
          ${box("chat_clear_keep_open", "Keep cards that still need an answer", "Captcha links, drafts and questions stay until you answer them.")}
          <div class="row"><button class="btn primary" data-action="save-prefs">Save</button><button class="btn" data-action="tg-test">Send a test message</button><button class="btn ghost" data-action="roundup">Send the roundup now</button><button class="btn ghost" data-action="tg-clear">Clear the chat now</button></div></div>
        <div class="card stack" style="margin-top:12px"><div class="spread"><h2>Subtitles</h2>${set("opensubtitles_key")}</div>
          <p class="muted small">Optional. After a movie or episode is filed, Linkarr saves a subtitle next to it from OpenSubtitles.com, unless one is already there. Get a free API key at opensubtitles.com → your profile → API consumers. Adding your OpenSubtitles login raises the daily allowance (20 a day free, more with VIP).</p>
          ${box("subs_enabled", "Fetch subtitles after filing")}
          <div class="grid2">${field("subs_languages", "Languages (up to 3, like en, it)")}${field("opensubtitles_user", "OpenSubtitles username (optional)")}</div>
          <div class="row"><input type="password" id="sec-opensubtitles_key" placeholder="${s.secrets.opensubtitles_key ? "Saved. Paste a new one to change it." : "API key"}" autocomplete="off" style="flex:1"><button class="btn" data-action="save-secret" data-name="opensubtitles_key">Save key</button></div>
          <div class="row"><input type="password" id="sec-opensubtitles_password" placeholder="${s.secrets.opensubtitles_password ? "Saved. Type a new one to change it." : "OpenSubtitles password (optional)"}" autocomplete="off" style="flex:1"><button class="btn" data-action="save-secret" data-name="opensubtitles_password">Save password</button></div>
          <div class="row"><button class="btn primary" data-action="save-prefs">Save</button><button class="btn" data-action="subs-test">Test</button></div></div>`;
    } else if (tab === "naming") {
      html += `<div class="card stack"><p class="muted small">Use {title}, {year}, {show}, {season} and {episode}. Add :00 for two digits, like {season:00}.</p>
        <div class="grid2">${field("movie_pattern", "Movie folder")}${field("show_pattern", "Show folder")}${field("season_pattern", "Season folder")}${field("episode_pattern", "Episode file")}</div>
        ${box("rename_episodes", "Rename episode files", "Off keeps the original file names inside the season folder.")}
        <div class="muted small">Example: <span class="mono">Dune Part Two (2024)/Dune Part Two (2024).mkv</span> and <span class="mono">Severance (2022)/Season 2/Severance - S02E01.mkv</span></div>${save}</div>`;
      const rem = await api("GET", "/api/remembered-shows").catch(() => []);
      html += `<div class="card stack" style="margin-top:12px"><h2>Remembered shows</h2>
        <p class="muted small">Set with Match to show or Fix show. New links named like this go to that show by themselves.</p>
        ${rem.length ? `<div class="list">${rem.map((r) => `<div class="li"><div class="grow"><div><b>${esc(r.folder || r.title)}</b></div>
          <div class="muted small">${esc(r.label)}</div><div class="muted small">${esc(r.library)}</div></div>
          <button class="btn sm ghost" data-action="forget-show" data-key="${esc(r.key)}">Forget</button></div>`).join("")}</div>` : `<div class="muted small">None yet.</div>`}</div>`;
    } else if (tab === "behaviour") {
      html += `<div class="card stack section"><h2>Starting</h2>${box("auto_start", "Start new links automatically when Linkarr is sure",
        "Sure means TMDB found the same name and year, there's only one title by that name, it has a library, and there's nothing to ask. Remakes, same-name titles and close matches still wait for you.")}${save}</div>
        <div class="card stack"><h2>After download</h2>${box("unpack", "Unpack zip, rar and 7z files")}${box("clean_junk", "Delete junk", "Samples, .nfo, .txt, .url, screenshots and similar.")}
        ${box("check_video", "Check that videos play before moving them")}
        <div class="grid3">${field("recycle_days", "Keep replaced files for (days)", "number", 'min="1" max="90"')}${field("max_parallel", "Downloads at once", "number", 'min="1" max="4"')}${field("night_start", '"Tonight" starts at', "time")}</div></div>
        <div class="card stack" style="margin-top:12px"><h2>Inbox folder</h2><p class="muted small">Drop finished downloads (from anywhere) into this folder inside the media folder and Linkarr names and files them like a link.</p>
        ${box("inbox_enabled", "Watch the Inbox folder")}${field("inbox_dir", "Inbox folder")}${box("inbox_auto_start", "Start Inbox items without asking", "Only when Linkarr is sure, as above. Otherwise it still asks.")}
        ${field("work_dir", "Work folder (downloads in progress)")}${save}</div>`;
    } else if (tab === "extras") {
      const sel = (k, opts) => `<select data-pref="${k}">${opts.map(([v, t]) => `<option value="${v}"${String(p[k]) === String(v) ? " selected" : ""}>${t}</option>`).join("")}</select>`;
      html += `<p class="muted small section">Captcha links are on from the start. Everything else here is off until you turn it on.</p>
        <div class="card stack section"><h2>Captcha links</h2>
          ${box("captcha_enabled", "Hold captcha links for me", "Some links (link-protector pages) only show the MEGA link after you solve a captcha. Linkarr doesn't try to get past it. It holds the job as Needs captcha, sends you the link on Telegram and shows it here. Solve it, then paste the MEGA link back: reply to the bot's message, or use the box on the job. One reminder if it's still waiting a day later.")}
          ${field("captcha_hosts", "Captcha sites (comma separated)")}${save}</div>
        <div class="card stack section"><h2>Monitored links</h2>
          ${box("watch_enabled", "Monitor show links for new episodes", "For a MEGA folder link the uploader keeps adding to. Linkarr checks it on a schedule, downloads only the new files and files them in the right season. Press Monitor this link on the show's Details page, or send /monitor and the link to the bot.")}
          <div class="grid2"><label class="f"><span>Check each link</span>${sel("watch_every", [[6, "Every 6 hours"], [12, "Every 12 hours"], [24, "Once a day"], [48, "Every 2 days"]])}</label>
          ${field("watch_stop_days", "Stop after this many days with nothing new (0 = never)", "number", 'min="0" max="365"')}</div>
          <div class="muted small">Once a day is safest. Checking often can hit MEGA's limits. If the link dies, monitoring stops and you're told.</div>${save}</div>
        <div class="card stack section"><h2>Episodes and copies</h2>
          ${box("gap_check", "Missing episode check", "After a show is filed, tell me about gaps between the episodes I have, like S02E03 missing between E02 and E04.")}
          ${box("dup_guard", "Duplicate guard", "Skip episodes and movies already in the library, even from a different link. A link with nothing new isn't downloaded at all.")}
          ${box("auto_upgrade", "Quality upgrade", "When a link has a higher resolution than the copy I have (1080p over 720p), swap it in without asking. The old copy goes to the recycle folder.")}${save}</div>
        <div class="card stack section"><h2>Plex</h2>
          ${box("plex_partial", "Scan only the folder that changed", "After a move Plex scans just that show or movie folder, so it shows up sooner. Falls back to the whole library if Linkarr can't work out Plex's folder.")}${save}</div>
        <div class="card stack section"><h2>Download window</h2>
          ${box("window_enabled", "Big downloads wait for the window", "So they don't slow the internet during the day. A download that already started keeps going.")}
          <div class="grid3">${field("window_start", "Window opens", "time")}${field("window_end", "Window closes", "time")}${field("window_min_gb", "Big means at least (GB, 0 = all)", "number", 'min="0" max="500"')}</div>${save}</div>
        <div class="card stack"><h2>Morning summary</h2>
          ${box("morning_summary", "One Telegram message in the morning", "What came in overnight, one title per line, plus what's still downloading and what needs you. Nothing is sent after a quiet night.")}
          <div class="grid3">${field("morning_time", "Send at", "time")}</div>
          <div class="row"><button class="btn primary" data-action="save-prefs">Save</button><button class="btn ghost" data-action="morning">Send it now</button></div></div>`;
    } else if (tab === "look") {
      html += `<div class="themes">${THEMES.map(([k, t, c]) => `<button class="theme ${p.theme === k ? "on" : ""}" data-action="theme" data-theme="${k}"><div class="sw">${c.map((x) => `<i style="background:${x}"></i>`).join("")}</div><b>${t}</b></button>`).join("")}</div>`;
    } else if (tab === "account") {
      html += `<div class="card stack section"><h2>Password</h2><form class="stack" id="pw-form"><div class="grid2"><label class="f"><span>Current password</span><input type="password" name="current" autocomplete="current-password" required></label>
        <label class="f"><span>New password</span><input type="password" name="password" minlength="10" autocomplete="new-password" required></label></div><div class="row"><button class="btn">Change password</button>
        <div class="grow"></div><button type="button" class="btn ghost" data-action="logout">Sign out of Linkarr</button></div></form></div>
        <div class="card stack"><h2>Backup</h2><p class="muted small">Settings, libraries, watched shows and history. Linkarr also keeps a daily copy of its database in the data folder.</p>
        <div class="row"><a class="btn" href="/api/backup" download>Download backup</a><a class="btn ghost" href="/api/backup?secrets=1" download>Include keys and tokens</a></div>
        <form class="row" id="restore-form"><input type="file" name="file" accept="application/json" required><button class="btn">Restore</button></form></div>`;
    }
    page().innerHTML = html;
    const mf = $("#mega-form");
    if (mf) mf.addEventListener("submit", async (e) => {
      e.preventDefault();
      const b = $("button", mf); b.disabled = true; b.textContent = "Signing in…";
      try { const r = await api("POST", "/api/mega/login", Object.fromEntries(new FormData(mf))); toast("Signed in as " + r.email); renderSettings(q); }
      catch (err) { toast(err.message); b.disabled = false; b.textContent = "Sign in to MEGA"; }
    });
    const pf = $("#pw-form");
    if (pf) pf.addEventListener("submit", async (e) => {
      e.preventDefault();
      try { await api("POST", "/api/auth/password", Object.fromEntries(new FormData(pf))); pf.reset(); toast("Password changed."); }
      catch (err) { toast(err.message); }
    });
    const rf = $("#restore-form");
    if (rf) rf.addEventListener("submit", async (e) => {
      e.preventDefault();
      try { const r = await api("POST", "/api/restore", new FormData(rf), true); toast(`Restored. ${r.libraries_added} libraries added.`); }
      catch (err) { toast(err.message); }
    });
  }

  function libRow(l, i, n) {
    const id = l.id;
    return `<div class="lib-row" data-lib-row="${id}">
      <input type="text" data-k="name" value="${esc(l.name)}" placeholder="Movies">
      <select data-k="kind"><option value="movie"${l.kind === "movie" ? " selected" : ""}>Movies</option><option value="series"${l.kind === "series" ? " selected" : ""}>Shows</option></select>
      <input type="text" data-k="path" value="${esc(l.path)}" placeholder="Movies">
      <input type="text" data-k="plex_section" value="${esc(l.plex_section)}" placeholder="Section id" list="plex-sections">
      <div class="row" style="flex-wrap:nowrap"><label class="check small" title="Documentary library"><input type="checkbox" data-k="documentary" ${l.documentary ? "checked" : ""}><span>Docs</span></label>
      ${id ? `<button class="btn sm" data-action="save-lib" data-id="${id}">Save</button>
        <button class="btn sm ghost" data-action="move-lib" data-id="${id}" data-dir="-1" ${i === 0 ? "disabled" : ""} aria-label="Up">↑</button>
        <button class="btn sm ghost" data-action="move-lib" data-id="${id}" data-dir="1" ${i === n - 1 ? "disabled" : ""} aria-label="Down">↓</button>
        <button class="btn sm ghost danger" data-action="del-lib" data-id="${id}" aria-label="Remove">✕</button>`
        : `<button class="btn sm primary" data-action="save-lib" data-id="">Add</button>`}</div></div>`;
  }

  function readPrefs() {
    const out = {};
    $$("[data-pref]").forEach((el) => { out[el.dataset.pref] = el.type === "checkbox" ? el.checked : el.value; });
    return out;
  }

  // ---------- setup check and logs ----------
  async function renderCheck() {
    title("Settings");
    $("#top-extra").innerHTML = `<button class="btn sm" data-action="recheck">Check again</button>`;
    page().innerHTML = settingsTabs("check") + `<div class="muted">Checking…</div>`;
    try {
      const r = await api("GET", "/api/check");
      page().innerHTML = settingsTabs("check") + `<div class="card section">${r.ok ? "<b>Everything looks good.</b>" : "<b>Some things need a look.</b>"}</div>
        <div class="card list checks">${r.items.map((i) => `<div class="li ${i.ok ? "ok" : "no"}"><span class="st">${i.ok ? "✓" : "!"}</span><div class="grow"><b>${esc(i.name)}</b>
        <div class="muted small">${esc(i.detail)}</div>${i.fix ? `<div class="small">${esc(i.fix)}</div>` : ""}</div>${!i.ok && fixTab(i.name) ? `<a class="btn sm" href="#/settings?tab=${fixTab(i.name)}">Fix</a>` : ""}</div>`).join("")}</div>`;
    } catch (e) { page().innerHTML = `<div class="err">${esc(e.message)}</div>`; }
  }

  // Which Settings tab fixes a failed check.
  const fixTab = (name) => (/^Librar/.test(name) ? "libraries" : /^MEGA/.test(name) ? "mega" : /TMDB|Plex|Telegram|Subtitles|OpenSubtitles/.test(name) ? "connections" : "");

  async function renderLogs() {
    title("Settings");
    $("#top-extra").innerHTML = `<div class="row"><button class="btn sm" data-action="copy-logs">Copy</button><button class="btn sm" data-action="reload-logs">Refresh</button></div>`;
    try {
      const r = await api("GET", "/api/logs");
      page().innerHTML = settingsTabs("logs") + `<p class="muted small">MEGA link keys and tokens are hidden, so it's safe to paste these when asking for help.</p><div class="logs" id="log-box">${esc(r.lines.join("\n")) || "Nothing logged yet."}</div>`;
      const b = $("#log-box"); b.scrollTop = b.scrollHeight;
    } catch (e) { page().innerHTML = `<div class="err">${esc(e.message)}</div>`; }
  }

  // ---------- actions ----------
  async function act(el) {
    const d = el.dataset;
    const job = d.job ? parseInt(d.job, 10) : null;
    const after = () => (state.route.startsWith("job/") ? renderJob(parseInt(state.route.slice(4), 10)) : state.route === "" ? refreshActivity() : route());
    switch (d.action) {
      case "toggle-recent": {
        if (state.openRecent.has(job)) state.openRecent.delete(job); else state.openRecent.add(job);
        return refreshActivity();
      }
      case "copy-path": {
        try { await navigator.clipboard.writeText(d.path); toast("Copied."); } catch (e) { toast(d.path); }
        return;
      }
      case "set-lib": await api("PATCH", `/api/jobs/${job}`, { library_id: parseInt(d.lib, 10) }); return after();
      case "start": await api("POST", `/api/jobs/${job}/start`, { when: "now" }); toast("Started."); return after();
      case "tonight": await api("POST", `/api/jobs/${job}/start`, { when: "tonight" }); toast("It'll start tonight."); return after();
      case "cancel": if (!confirm("Cancel this download? Anything half-downloaded is deleted.")) return; await api("POST", `/api/jobs/${job}/cancel`); return after();
      case "delete-job": await api("DELETE", `/api/jobs/${job}`); location.hash = "#/"; return;
      case "match": await api("POST", `/api/jobs/${job}/match`, { choice: d.choice }); return after();
      case "conflict": await api("PATCH", `/api/jobs/${job}`, { conflict_choice: d.choice }); if (d.choice === "skip") await api("POST", `/api/jobs/${job}/cancel`); return after();
      case "fix": { const r = await api("POST", `/api/links/${d.link}/retry`); toast(r && r.mode === "unpack" ? "Unpacking it again. No new download needed." : "Downloading it again from scratch."); return after(); }
      case "skip-link": await api("POST", `/api/links/${d.link}/skip`); toast("Skipped."); return after();
      case "dismiss": await api("POST", `/api/alerts/${d.alert}/dismiss`); return after();
      case "save-job": {
        const body = { title: $("#j-title").value, year: parseInt($("#j-year").value, 10) || null, kind: $("#j-kind").value, folder_name: $("#j-folder").value };
        const w = $("#j-wait"); if (w) body.wait_all = w.checked ? 1 : 0;
        const pw = $("#j-pass").value; if (pw) body.archive_password = pw;
        const saved = await api("PATCH", `/api/jobs/${job}`, body);
        if (d.start && saved.status === "draft") {
          if (!saved.library_id) { toast("Saved. Pick a library, then it can start."); return after(); }
          await api("POST", `/api/jobs/${job}/start`, { when: "now" });
          state.openDetails.delete(job);
          toast("Saved. Downloading now.");
        } else {
          state.openDetails.delete(job);
          toast("Saved.");
        }
        return after();
      }
      case "show-details": state.openDetails.add(job); return after();
      case "hide-details": state.openDetails.delete(job); return after();
      case "use-tmdb": await api("PATCH", `/api/jobs/${job}`, { tmdb_id: parseInt(d.id, 10), kind: d.kind }); toast("Updated."); return after();
      case "save-files": {
        const sel = $$(`.files[data-link="${d.link}"] input:checked`).map((i) => i.value);
        await api("POST", `/api/links/${d.link}/files`, { selected: sel }); toast("Saved."); return;
      }
      case "show": return showBox(el);
      case "start-all": {
        const r = await api("POST", "/api/jobs/start-all", { when: d.when });
        toast(`${d.when === "tonight" ? "Scheduled" : "Started"} ${r.started}.${r.skipped ? ` ${r.skipped} still need a library.` : ""}`);
        return after();
      }
      case "decode": return runDecoder();
      case "add-decoded": return addLinks(d.url);
      case "hold-captcha": return holdCaptcha([d.url]);
      case "drop-captcha": {
        if (!confirm("Drop this captcha link? Nothing was downloaded from it.")) return;
        await api("DELETE", `/api/jobs/${job}`); toast("Dropped.");
        if (state.route === "") return refreshActivity();
        location.hash = "#/"; return;
      }
      case "copy": {
        const t = $("#dec-" + d.i).textContent;
        try { await navigator.clipboard.writeText(t); toast("Copied."); } catch (e) { toast("Couldn't copy. Select it and copy."); }
        return;
      }
      case "watch": await api("POST", `/api/shows/${d.id}/watch`, { watching: el.checked, name: d.name, folder: d.folder, library_id: d.lib ? parseInt(d.lib, 10) : null }); toast(el.checked ? "You'll get news about this show." : "Stopped."); return;
      case "gaps": state.gapsOnly = d.v === "1"; return renderShows();
      case "track-movie": {
        const m = await api("POST", "/api/movies", { tmdb_id: parseInt(d.id, 10) });
        toast(`Tracking ${m.title}.`);
        return onTracker() ? renderMovies() : (el.outerHTML = '<span class="tag ok">Tracking</span>');
      }
      case "untrack-movie": await api("DELETE", `/api/movies/${d.id}`); return renderMovies();
      case "watch-add": { const w = await api("POST", "/api/watches", { link_id: parseInt(d.link, 10) }); toast(`Monitoring ${w.title} for new episodes.`); return after(); }
      case "watch-check": {
        const r = await api("POST", `/api/watches/${d.id}/check`);
        toast(r.watch.status === "dead" ? "That link is gone, so monitoring stopped." : r.new ? `Found ${r.new} new episode${r.new === 1 ? "" : "s"}.` : "Nothing new yet.");
        return renderWatched();
      }
      case "watch-set": await api("POST", `/api/watches/${d.id}`, { active: d.on === "1" }); toast(d.on === "1" ? "Monitoring again." : "Stopped monitoring."); return renderWatched();
      case "watch-del": if (!confirm("Stop monitoring this link and remove it from the list? Nothing in your library changes.")) return; await api("DELETE", `/api/watches/${d.id}`); return renderWatched();
      case "morning": { await api("PUT", "/api/settings", readPrefs()); const r = await api("POST", "/api/morning/send"); toast(r.sent ? "Sent the morning summary." : "Nothing came in since the last one, so nothing was sent."); return; }
      case "track-all": el.disabled = true; await api("POST", "/api/shows/track-all"); return renderShows();
      case "unwatch": await api("POST", `/api/shows/${d.id}/watch`, { watching: false }); return renderShows();
      case "refile": return refileBox(el);
      case "fixshow": return fixShowBox(el);
      case "mb-use": return useShow(d);
      case "forget-show": await api("DELETE", "/api/remembered-shows?key=" + encodeURIComponent(d.key)); toast("Forgotten."); return route();
      case "refile-go": {
        const id = d.id;
        const r = await api("POST", `/api/history/${id}/move`, { library_id: parseInt($("#rf-lib-" + id).value, 10), folder: $("#rf-folder-" + id).value });
        toast(`Moved ${r.moved} file${r.moved === 1 ? "" : "s"}.${r.problems.length ? ` ${r.problems.length} couldn't be moved.` : ""}`);
        state.overview = null;
        return renderHistory();
      }
      case "plex-check": {
        el.disabled = true;
        const r = await api("POST", `/api/jobs/${job}/plex`, { action: "check" });
        toast(r.plex === "found" ? "Plex has it." : "Plex is scanning. Linkarr will keep looking.");
        return after();
      }
      case "plex-found": await api("POST", `/api/jobs/${job}/plex`, { action: "found" }); toast("Marked as in Plex."); return after();
      case "recheck-ok": await api("PATCH", `/api/jobs/${job}`, { recheck: 0 }); toast("Filing it."); return after();
      case "roundup": await api("PUT", "/api/settings", readPrefs()); await api("POST", "/api/roundup/send"); toast("Sent the roundup."); return;
      case "quota": { const r = await api("POST", "/api/mega/quota"); toast(r.quota ? `${fmtBytes(Math.max(0, r.quota.max - r.quota.used))} of transfer left.` : "MEGA didn't say. Sign in first."); return route(); }
      case "undo": {
        if (!confirm("Undo this move? Files go back to the work folder and anything replaced comes back from the recycle folder.")) return;
        const r = await api("POST", `/api/history/${d.id}/undo`);
        toast(r.problems.length ? `Undone, with ${r.problems.length} problem(s). See Logs.` : "Undone.");
        return renderHistory();
      }
      case "save-prefs": state.settings.prefs = await api("PUT", "/api/settings", readPrefs()); toast("Saved."); return refreshActivity();
      case "save-secret": {
        const input = $("#sec-" + d.name);
        await api("PUT", "/api/secrets", { [d.name]: input.value }); input.value = ""; toast("Saved."); return route();
      }
      case "tg-clear": { await api("PUT", "/api/settings", readPrefs()); const r = await api("POST", "/api/telegram/clear"); toast(r.deleted ? `Cleared ${r.deleted} message${r.deleted === 1 ? "" : "s"}.${r.kept ? ` Kept ${r.kept}.` : ""}` : r.kept ? `Nothing to clear. Kept ${r.kept}.` : "Nothing to clear."); return; }
      case "tg-test": { await api("PUT", "/api/settings", readPrefs()); const r = await api("POST", "/api/telegram/test"); toast(r.sent ? `@${r.bot} sent you a message.` : `@${r.bot} works. Add your chat id to get messages.`); return; }
      case "subs-test": await api("PUT", "/api/settings", readPrefs()); await api("POST", "/api/subs/test"); toast("OpenSubtitles works."); return;
      case "theme": await api("PUT", "/api/settings", { theme: d.theme }); setTheme(d.theme); return route();
      case "mega-logout": await api("POST", "/api/mega/logout"); toast("Signed out of MEGA."); return route();
      case "save-lib": {
        const row = el.closest("[data-lib-row]");
        const body = {};
        $$("[data-k]", row).forEach((i) => { body[i.dataset.k] = i.type === "checkbox" ? i.checked : i.value; });
        if (d.id) await api("PUT", `/api/libraries/${d.id}`, body); else await api("POST", "/api/libraries", body);
        toast("Saved."); state.overview = null; return route();
      }
      case "del-lib": if (!confirm("Remove this library from Linkarr? Your files aren't touched.")) return; await api("DELETE", `/api/libraries/${d.id}`); return route();
      case "move-lib": {
        const ids = state.settings.libraries.map((l) => l.id);
        const i = ids.indexOf(parseInt(d.id, 10)); const k = i + parseInt(d.dir, 10);
        [ids[i], ids[k]] = [ids[k], ids[i]];
        await api("POST", "/api/libraries/order", ids); return route();
      }
      case "plex-sections": {
        const secs = await api("GET", "/api/plex/sections");
        $("#plex-list").innerHTML = secs.map((s) => `${esc(s.title)} = <b>${esc(s.id)}</b>`).join(" · ") +
          `<datalist id="plex-sections">${secs.map((s) => `<option value="${esc(s.id)}">${esc(s.title)}</option>`).join("")}</datalist>`;
        return;
      }
      case "logout": await api("POST", "/api/auth/logout"); state.auth = null; return boot();
      case "recheck": return renderCheck();
      case "reload-logs": return renderLogs();
      case "copy-logs": {
        const text = $("#log-box").textContent;
        try { await navigator.clipboard.writeText(text); toast("Copied."); }
        catch (e) { const r = document.createRange(); r.selectNodeContents($("#log-box")); getSelection().removeAllRanges(); getSelection().addRange(r); toast("Selected. Press copy."); }
        return;
      }
      default:
    }
  }

  document.addEventListener("click", async (e) => {
    const el = e.target.closest("[data-action]");
    if (!el || (el.tagName === "INPUT" && el.type === "checkbox" && e.type === "click" && el.dataset.action !== "watch")) return;
    if (el.tagName === "A") return;
    const a = e.target.closest("a");
    if (a && el.contains(a)) return;  // a link inside a tappable row goes where it says
    if (el.tagName !== "INPUT") e.preventDefault();
    el.disabled = true;
    try { await act(el); }
    catch (err) { if (err.message !== "Signed out.") toast(err.message); }
    finally { el.disabled = false; }
  });

  // The MEGA link box on a captcha card.
  document.addEventListener("submit", (e) => {
    const f = e.target.closest && e.target.closest("[data-captcha-form]");
    if (!f) return;
    e.preventDefault();
    const b = $("button", f); b.disabled = true;
    solveCaptcha(f.dataset.captchaForm, $("input", f).value).finally(() => { b.disabled = false; });
  });

  // Rows you tap (Recently finished) open with Enter or Space too.
  document.addEventListener("keydown", (e) => {
    if ((e.key === "Enter" || e.key === " ") && e.target.matches && e.target.matches('[role="button"][data-action]')) { e.preventDefault(); e.target.click(); }
  });

  boot();
})();
