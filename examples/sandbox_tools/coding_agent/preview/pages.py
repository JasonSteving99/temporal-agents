"""
The two HTML pages this proxy serves in its own right: the sign-in screen and the
admin panel. Markup only — every access decision lives in session.py and
allowlist.py.

Both are rendered by `.replace()` on `__PLACEHOLDER__` tokens rather than
f-strings or `.format()`, because the bodies are full of CSS and JS braces that
either would try to interpret. Values are injected as `json.dumps(...)`, which is
valid JS literal syntax and quotes anything dangerous.
"""

# The sign-in page. Served ONLY on AUTH_HOST so Firebase needs a single authorized
# domain (see config.AUTH_HOST).
LOGIN_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>Sign in</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
  body{font:16px/1.5 system-ui,sans-serif;display:grid;place-items:center;
       min-height:100vh;margin:0;background:#0b0d10;color:#e6e8eb}
  .card{background:#14171c;border:1px solid #262b33;border-radius:12px;
        padding:32px;max-width:380px;text-align:center}
  h1{font-size:20px;margin:0 0 8px}
  p{color:#98a2b3;margin:0 0 24px;font-size:14px}
  button{font:inherit;padding:10px 20px;border-radius:8px;border:0;cursor:pointer;
         background:#3b82f6;color:#fff}
  button:disabled{opacity:.5;cursor:default}
  .err{color:#f87171;font-size:13px;margin-top:16px;min-height:1em}
</style></head>
<body><div class="card">
  <h1>Sandbox preview</h1>
  <p>Sign in to view agent-built sites.</p>
  <button id="go">Sign in with Google</button>
  <div class="err" id="err"></div>
</div>
<script type="module">
import { initializeApp } from "https://www.gstatic.com/firebasejs/12.16.0/firebase-app.js";
import { getAuth, GoogleAuthProvider, signInWithPopup }
  from "https://www.gstatic.com/firebasejs/12.16.0/firebase-auth.js";

const auth = getAuth(initializeApp(__CONFIG__));
const NEXT = __NEXT__;
const go = document.getElementById("go"), err = document.getElementById("err");

go.onclick = async () => {
  go.disabled = true; err.textContent = "";
  try {
    const cred = await signInWithPopup(auth, new GoogleAuthProvider());
    const res = await fetch("/__auth/session", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ idToken: await cred.user.getIdToken(), next: NEXT }),
    });
    if (!res.ok) throw new Error(await res.text());
    location.href = (await res.json()).redirect;
  } catch (e) {
    err.textContent = e.message || String(e);
    go.disabled = false;
  }
};
</script></body></html>
"""

# The admin panel. Reachable only by config.ADMIN_EMAILS; everyone else gets a 404
# from the handler, so this markup is never sent to a non-admin. It can add and
# remove GUESTS only — there is no "make admin" control, by design (allowlist.py).
ADMIN_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>Preview access</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
  body{font:16px/1.5 system-ui,sans-serif;background:#0b0d10;color:#e6e8eb;margin:0;
       padding:40px 20px}
  .wrap{max-width:560px;margin:0 auto}
  h1{font-size:20px;margin:0 0 4px}
  .sub{color:#98a2b3;font-size:14px;margin:0 0 28px}
  .row{display:flex;gap:8px;margin-bottom:24px}
  input{flex:1;font:inherit;padding:10px 12px;border-radius:8px;background:#14171c;
        color:inherit;border:1px solid #262b33}
  button{font:inherit;padding:10px 18px;border-radius:8px;border:0;cursor:pointer;
         background:#3b82f6;color:#fff}
  button:disabled{opacity:.5;cursor:default}
  ul{list-style:none;padding:0;margin:0;border:1px solid #262b33;border-radius:10px;
     overflow:hidden}
  li{display:flex;align-items:center;gap:12px;padding:12px 16px;background:#14171c}
  li+li{border-top:1px solid #262b33}
  li span{flex:1;font-size:14px;word-break:break-all}
  li button{background:transparent;color:#f87171;padding:4px 8px;font-size:13px}
  .tag{font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:#fbbf24;
       border:1px solid #3f3722;background:#241f12;border-radius:5px;padding:2px 7px}
  .empty{padding:20px 16px;color:#667085;font-size:14px;background:#14171c}
  .msg{font-size:13px;margin-top:16px;min-height:1em;color:#98a2b3}
  .msg.bad{color:#f87171}
  footer{margin-top:32px;font-size:13px;color:#667085}
  a{color:#60a5fa}
</style></head>
<body><div class="wrap">
  <h1>Preview access</h1>
  <p class="sub">Anyone listed here can sign in and view preview sites.</p>
  <form class="row" id="add">
    <input id="email" type="email" placeholder="friend@example.com" required
           autocomplete="off" spellcheck="false">
    <button type="submit">Add</button>
  </form>
  <ul id="list"></ul>
  <div class="msg" id="msg"></div>
  <footer>
    Signed in as __EMAIL__ · <a href="/__auth/logout">Sign out</a><br>
    Admins are set by <code>PREVIEW_ADMIN_EMAILS</code> on the server and can't be
    changed from this page.
  </footer>
</div>
<script>
const list = document.getElementById("list"), msg = document.getElementById("msg");
const form = document.getElementById("add"), email = document.getElementById("email");
const ADMINS = __ADMINS__;

function render(guests) {
  list.innerHTML = "";
  for (const a of ADMINS) list.appendChild(row(a, true));
  for (const g of guests) list.appendChild(row(g, false));
  if (!guests.length) {
    const d = document.createElement("div");
    d.className = "empty";
    d.textContent = "No guests yet — add someone above.";
    list.appendChild(d);
  }
}
function row(addr, admin) {
  const li = document.createElement("li");
  const s = document.createElement("span");
  s.textContent = addr;                      // textContent, never innerHTML
  li.appendChild(s);
  if (admin) {
    const t = document.createElement("span");
    t.className = "tag"; t.textContent = "admin";
    li.appendChild(t);
  } else {
    const b = document.createElement("button");
    b.textContent = "Remove";
    b.onclick = () => send("remove", addr);
    li.appendChild(b);
  }
  return li;
}
async function send(action, addr) {
  msg.className = "msg"; msg.textContent = "";
  const res = await fetch("/__auth/admin/guests", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ action, email: addr }),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) { msg.className = "msg bad"; msg.textContent = data.message || res.statusText; return; }
  msg.textContent = data.message || "";
  render(data.guests || []);
}
form.onsubmit = async (e) => {
  e.preventDefault();
  const addr = email.value.trim();
  if (addr) { await send("add", addr); email.value = ""; }
};
render(__GUESTS__);
</script></body></html>
"""

# The gallery. Rendered at the root of the login host for any signed-in user;
# the "Manage access" control is emitted only when __IS_ADMIN__ is true, so the
# admin path stops being something you have to memorise.
#
# Tiles are STATIC IMAGES, never iframes — an iframe per app would wake every
# sandbox in the gallery just to look at it, and each wake bills for compute.
HOME_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>Preview gallery</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
  *{box-sizing:border-box}
  body{font:16px/1.5 system-ui,sans-serif;background:#0b0d10;color:#e6e8eb;margin:0;
       padding:40px 24px 64px}
  .wrap{max-width:1100px;margin:0 auto}
  header{display:flex;align-items:baseline;gap:16px;flex-wrap:wrap;margin-bottom:6px}
  h1{font-size:22px;margin:0;flex:1}
  .sub{color:#98a2b3;font-size:14px;margin:0 0 28px}
  .btn{font:inherit;font-size:14px;padding:7px 14px;border-radius:8px;border:1px solid #262b33;
       cursor:pointer;background:#14171c;color:#e6e8eb;text-decoration:none;display:inline-block}
  .btn:hover{border-color:#3b4250}
  .btn.primary{background:#3b82f6;border-color:#3b82f6;color:#fff}
  .btn:disabled{opacity:.5;cursor:default}
  .grid{display:grid;gap:20px;grid-template-columns:repeat(auto-fill,minmax(300px,1fr))}
  .card{background:#14171c;border:1px solid #262b33;border-radius:12px;overflow:hidden;
        display:flex;flex-direction:column}
  .shot{display:block;width:100%;aspect-ratio:16/10;object-fit:cover;object-position:top center;
        background:#0f1216;border-bottom:1px solid #262b33}
  .noshot{display:grid;place-items:center;color:#4b5563;font-size:13px;text-align:center;padding:16px}
  .meta{padding:14px 16px;flex:1;min-width:0}
  .title{font-size:15px;font-weight:600;margin:0 0 3px;overflow:hidden;text-overflow:ellipsis;
         white-space:nowrap}
  .host{font-size:12px;color:#667085;font-family:ui-monospace,monospace;overflow:hidden;
        text-overflow:ellipsis;white-space:nowrap}
  .when{font-size:12px;color:#4b5563;margin-top:6px}
  .actions{display:flex;gap:8px;padding:0 16px 14px;flex-wrap:wrap}
  .empty{border:1px dashed #262b33;border-radius:12px;padding:48px 24px;text-align:center;
         color:#667085}
  .msg{font-size:13px;margin:18px 0 0;min-height:1em;color:#98a2b3}
  .msg.bad{color:#f87171}
  .note{font-size:12px;color:#4b5563;margin-top:28px;line-height:1.6}
  a{color:#60a5fa}
</style></head>
<body><div class="wrap">
  <header>
    <h1>Preview gallery</h1>
    <span id="admin-slot"></span>
    <a class="btn" href="/__auth/logout">Sign out</a>
  </header>
  <p class="sub">Every site the agent has served, across sessions. Signed in as __EMAIL__.</p>
  <div class="grid" id="grid"></div>
  <div class="msg" id="msg"></div>
  <p class="note">
    Tiles are screenshots, not live pages — loading the real sites here would wake every
    sandbox and bill for compute. Each shot refreshes automatically when the proxy wakes
    that sandbox to serve someone. <strong>Refresh</strong> forces a new one, and will wake
    the sandbox if it's asleep.
  </p>
</div>
<script>
const APPS = __APPS__, IS_ADMIN = __IS_ADMIN__, BASE = __BASE__;
const grid = document.getElementById("grid"), msg = document.getElementById("msg");

if (IS_ADMIN) {
  const a = document.createElement("a");
  a.className = "btn"; a.href = "/__auth/admin"; a.textContent = "Manage access";
  document.getElementById("admin-slot").appendChild(a);
}

function ago(ts) {
  if (!ts) return "never";
  const s = Math.max(0, Math.floor(Date.now() / 1000) - ts);
  if (s < 60) return "just now";
  const units = [["d", 86400], ["h", 3600], ["m", 60]];
  for (const [label, size] of units) {
    if (s >= size) return Math.floor(s / size) + label + " ago";
  }
  return "just now";
}

function card(app) {
  const host = app.key + "." + BASE;
  const el = document.createElement("div");
  el.className = "card";

  if (app.shot_at) {
    const img = document.createElement("img");
    img.className = "shot";
    img.loading = "lazy";
    img.alt = "";
    img.src = "/__apps/shot/" + encodeURIComponent(app.key) + "?v=" + app.shot_at;
    el.appendChild(img);
  } else {
    const ph = document.createElement("div");
    ph.className = "shot noshot";
    ph.textContent = "No screenshot yet — open the site or press Refresh";
    el.appendChild(ph);
  }

  const meta = document.createElement("div");
  meta.className = "meta";
  const t = document.createElement("p");
  t.className = "title";
  t.textContent = app.title || ("port " + app.port);   // textContent: titles come from agent-built pages
  const h = document.createElement("div");
  h.className = "host"; h.textContent = host;
  const w = document.createElement("div");
  w.className = "when";
  w.textContent = "visited " + ago(app.last_seen) + " · shot " + ago(app.shot_at);
  meta.append(t, h, w);
  el.appendChild(meta);

  const actions = document.createElement("div");
  actions.className = "actions";
  const open = document.createElement("a");
  open.className = "btn primary"; open.href = "https://" + host + "/";
  open.target = "_blank"; open.rel = "noopener"; open.textContent = "Open";
  const refresh = document.createElement("button");
  refresh.className = "btn"; refresh.textContent = "Refresh";
  refresh.title = "Re-screenshot. Wakes the sandbox if it is stopped.";
  refresh.onclick = () => post("/__apps/refresh", app.key, refresh, "Refreshing…");
  actions.append(open, refresh);
  if (IS_ADMIN) {
    const forget = document.createElement("button");
    forget.className = "btn"; forget.textContent = "Forget";
    forget.title = "Remove from this gallery. Does not touch the sandbox.";
    forget.onclick = () => {
      if (confirm("Remove " + host + " from the gallery?")) {
        post("/__apps/forget", app.key, forget, "Removing…");
      }
    };
    actions.appendChild(forget);
  }
  el.appendChild(actions);
  return el;
}

function render(apps) {
  grid.innerHTML = "";
  if (!apps.length) {
    const d = document.createElement("div");
    d.className = "empty";
    d.textContent = "No preview sites yet. Ask the agent to build and serve one, open it, and it shows up here.";
    grid.appendChild(d);
    return;
  }
  for (const app of apps) grid.appendChild(card(app));
}

async function post(url, key, btn, pending) {
  const label = btn.textContent;
  btn.disabled = true; btn.textContent = pending;
  msg.className = "msg"; msg.textContent = "";
  try {
    const res = await fetch(url, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ key }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.message || res.statusText);
    msg.textContent = data.message || "";
    render(data.apps || []);
  } catch (e) {
    msg.className = "msg bad"; msg.textContent = e.message || String(e);
    btn.disabled = false; btn.textContent = label;
  }
}

render(APPS);
</script></body></html>
"""
