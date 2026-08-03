"""
The HTML pages this proxy serves in its own right: the sign-in screen, the
gallery, the admin panel, and the two dead ends. Markup only — every access
decision lives in session.py and allowlist.py, and the design system they all
draw on is theme.py.

The marketing landing page is next door in landing.py, because it is the one page
here aimed at someone who has not signed in and has different content entirely.

All of these are rendered by `.replace()` on `__PLACEHOLDER__` tokens rather than
f-strings or `.format()`, because the bodies are full of CSS and JS braces that
either would try to interpret. Values are injected as `json.dumps(...)`, which is
valid JS literal syntax and quotes anything dangerous.
"""

from .theme import head

# ---------------------------------------------------------------------------
# Sign-in
# ---------------------------------------------------------------------------
# Served ONLY on AUTH_HOST so Firebase needs a single authorized domain (see
# config.AUTH_HOST).
#
# The old version of this page said "Sign in to view agent-built sites" and
# stopped there. It now answers the two questions someone actually has at this
# moment: *what am I about to be let into*, and — when they followed a preview
# link — *where will I land*. `__DEST__` carries that destination when we know it.
_LOGIN_CSS = """
body{min-height:100svh;display:grid;grid-template-rows:auto 1fr auto;
     padding:0 20px;padding-left:max(20px,env(safe-area-inset-left));
     padding-right:max(20px,env(safe-area-inset-right))}
.top{display:flex;align-items:center;height:64px;padding-top:env(safe-area-inset-top)}
.mark{display:flex;align-items:center;gap:9px;font-family:var(--display);
      font-weight:600;font-size:15px;text-decoration:none}
.glyph{width:22px;height:22px}
main{display:grid;place-items:center;padding:20px 0 40px}
.card{width:100%;max-width:400px;padding:30px 26px;position:relative;overflow:hidden}
.card::before{content:"";position:absolute;inset:-60% -20% 60%;pointer-events:none;
  background:radial-gradient(50% 60% at 50% 100%,rgba(255,194,75,.10),transparent 70%)}
.card>*{position:relative}
h1{font-size:25px;margin:16px 0 0}
.sub{color:var(--muted);font-size:14.5px;margin-top:10px}
/* Stacked, not inline: the hostname is the whole point of this chip and a
   preview host is long enough that an inline layout ellipsises the port away. */
.dest{margin-top:20px;padding:11px 13px;border:1px solid var(--line);
      border-radius:var(--r-sm);background:var(--sunk)}
.dest .lab{display:block;font-family:var(--mono);font-size:10px;letter-spacing:.12em;
           text-transform:uppercase;color:var(--faint)}
.dest .val{display:block;margin-top:5px;font-family:var(--mono);font-size:12.5px;
           color:var(--text);word-break:break-all}
#go{width:100%;margin-top:22px;min-height:48px;font-size:15px}
.gl{width:17px;height:17px;flex:0 0 auto}
.err{color:var(--bad);font-size:13px;margin-top:14px;min-height:1em}
.gets{list-style:none;margin:24px 0 0;padding:20px 0 0;border-top:1px solid var(--line)}
.gets li{display:flex;gap:10px;font-size:13.5px;color:var(--muted);padding:5px 0}
.gets li::before{content:"";width:5px;height:5px;border-radius:50%;background:var(--live);
                 margin-top:8px;flex:0 0 auto}
.note{color:var(--faint);font-size:12.5px;margin-top:20px;line-height:1.6}
footer{padding:0 0 30px;padding-bottom:max(30px,env(safe-area-inset-bottom));
       text-align:center;font-size:12.5px;color:var(--faint)}
footer a{color:var(--muted)}
"""

_GOOGLE_G = (
    '<svg class="gl" viewBox="0 0 24 24" aria-hidden="true">'
    '<path fill="#4285F4" d="M23 12.3c0-.8-.1-1.6-.2-2.3H12v4.5h6.2a5.3 5.3 0 0 1-2.3 3.5v2.9h3.7c2.2-2 3.4-5 3.4-8.6z"/>'
    '<path fill="#34A853" d="M12 23.5c3.1 0 5.7-1 7.6-2.8l-3.7-2.9c-1 .7-2.3 1.1-3.9 1.1-3 0-5.5-2-6.4-4.7H1.8v3a11.5 11.5 0 0 0 10.2 6.3z"/>'
    '<path fill="#FBBC05" d="M5.6 14.2a6.9 6.9 0 0 1 0-4.4v-3H1.8a11.5 11.5 0 0 0 0 10.4l3.8-3z"/>'
    '<path fill="#EA4335" d="M12 4.7c1.7 0 3.2.6 4.4 1.7l3.3-3.3A11.5 11.5 0 0 0 1.8 6.8l3.8 3c.9-2.7 3.4-4.7 6.4-4.7z"/>'
    "</svg>"
)

_MARK_GLYPH = (
    '<svg class="glyph" viewBox="0 0 24 24" fill="none" aria-hidden="true">'
    '<rect x="1.5" y="4.5" width="21" height="15" rx="3.5" stroke="#2C3548"/>'
    '<path d="M1.5 9h21" stroke="#2C3548"/>'
    '<circle cx="5" cy="6.75" r="1" fill="#FFC24B"/>'
    '<path d="M7 15.5l2.5-2.5L7 10.5" stroke="#FFC24B" stroke-width="1.6" '
    'stroke-linecap="round" stroke-linejoin="round"/>'
    '<path d="M12 15.5h5" stroke="#7FD8F5" stroke-width="1.6" stroke-linecap="round"/>'
    "</svg>"
)

LOGIN_PAGE = (
    '<!doctype html><html lang="en"><head>'
    + head("Sign in — Preview", _LOGIN_CSS, "__PWA_HEAD__")
    + """</head><body>
<div class="top"><a class="mark" href="/">"""
    + _MARK_GLYPH
    + """Preview</a></div>
<main><div class="card">
  <span class="eyebrow">Sandboxed live previews</span>
  <h1>Sign in to open it.</h1>
  <p class="sub">These sites run inside sealed sandboxes that answer nothing else on
     the internet. Your session is what unlocks them.</p>
  __DEST__
  <button class="btn go" id="go">"""
    + _GOOGLE_G
    + """Sign in with Google</button>
  <div class="err" id="err" role="alert"></div>
  <ul class="gets">
    <li>Every site the agent has served, across sessions</li>
    <li>Suspended sandboxes wake when you open them</li>
    <li>Works as an installed app on your phone</li>
  </ul>
  <p class="note">Invite only. If sign-in is refused, your address hasn't been added
     yet — an admin has to do that.</p>
</div></main>
<footer><a href="/">What is this?</a></footer>

<script type="module">
import { initializeApp } from "https://www.gstatic.com/firebasejs/12.16.0/firebase-app.js";
import { getAuth, GoogleAuthProvider, signInWithPopup, signInWithRedirect, getRedirectResult }
  from "https://www.gstatic.com/firebasejs/12.16.0/firebase-auth.js";

const auth = getAuth(initializeApp(__CONFIG__));
const NEXT = __NEXT__;
const go = document.getElementById("go"), err = document.getElementById("err");

// An installed PWA has no browser chrome to host a popup, and mobile Safari
// blocks them outright, so those paths sign in by full-page redirect instead.
// The destination has to survive that round trip.
const standalone = matchMedia("(display-mode: standalone)").matches || navigator.standalone === true;
const KEY = "preview:next";

async function exchange(user) {
  const res = await fetch("/__auth/session", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ idToken: await user.getIdToken(), next: sessionStorage.getItem(KEY) || NEXT }),
  });
  if (!res.ok) throw new Error(await res.text());
  sessionStorage.removeItem(KEY);
  location.href = (await res.json()).redirect;
}

// Coming back from a redirect sign-in.
getRedirectResult(auth).then((r) => { if (r && r.user) return exchange(r.user); })
  .catch((e) => { err.textContent = e.message || String(e); });

go.onclick = async () => {
  go.disabled = true; err.textContent = "";
  sessionStorage.setItem(KEY, NEXT);
  try {
    if (standalone) {
      await signInWithRedirect(auth, new GoogleAuthProvider());
      return;                                  // navigates away
    }
    await exchange((await signInWithPopup(auth, new GoogleAuthProvider())).user);
  } catch (e) {
    const code = e && e.code || "";
    if (code === "auth/popup-blocked" || code === "auth/operation-not-supported-in-this-environment"
        || code === "auth/popup-closed-by-user" && standalone) {
      await signInWithRedirect(auth, new GoogleAuthProvider());
      return;
    }
    err.textContent = e.message || String(e);
    go.disabled = false;
  }
};
</script></body></html>
"""
)

# The destination chip, rendered into __DEST__ only when `next` names a preview
# host. Landing back on the gallery is the default and needs no announcement;
# being sent to someone else's sandbox does.
LOGIN_DEST = (
    '<div class="dest"><span class="lab">opening</span>'
    '<span class="val">__HOST__</span></div>'
)


# ---------------------------------------------------------------------------
# The gallery
# ---------------------------------------------------------------------------
# Rendered at the root of the login host for any signed-in user; the "Manage
# access" control is emitted only when __IS_ADMIN__ is true, so the admin path
# stops being something you have to memorise.
#
# Tiles are STATIC IMAGES, never iframes — an iframe per app would wake every
# sandbox in the gallery just to look at it, and each wake bills for compute.
#
# Everything that organises the grid (search, sort, group, density) is CLIENT
# SIDE and costs no request. Only the three things that change shared state —
# rename, pin, forget — go to the server, and all three are admin-only, because
# one registry is shared by everyone who signs in.
_HOME_CSS = """
body{padding:0 0 56px;padding-bottom:max(56px,env(safe-area-inset-bottom))}
.wrap{max-width:1240px;margin:0 auto;padding:0 20px;
      padding-left:max(20px,env(safe-area-inset-left));
      padding-right:max(20px,env(safe-area-inset-right))}

/* --- header -------------------------------------------------------------- */
header{position:sticky;top:0;z-index:30;background:rgba(8,10,14,.86);
       backdrop-filter:blur(14px);border-bottom:1px solid var(--line);
       padding-top:env(safe-area-inset-top)}
.bar{display:flex;align-items:center;gap:10px;height:58px}
.mark{display:flex;align-items:center;gap:9px;font-family:var(--display);
      font-weight:600;font-size:15px;margin-right:auto;text-decoration:none}
.glyph{width:22px;height:22px;flex:0 0 auto}
.tools{display:flex;align-items:center;gap:8px;padding-bottom:12px}
.tools select{flex:0 1 auto;min-width:0}
.search{flex:1;min-width:0;position:relative;display:flex;align-items:center}
.search .field{width:100%;padding-left:36px}
.search svg{position:absolute;left:12px;width:15px;height:15px;stroke:var(--faint);
            fill:none;stroke-width:1.8;pointer-events:none}
.search kbd{position:absolute;right:10px;font-family:var(--mono);font-size:11px;
            color:var(--faint);border:1px solid var(--line2);border-radius:4px;
            padding:1px 5px;pointer-events:none}
.search .field:not(:placeholder-shown)+svg+kbd,.search .field:focus+svg+kbd{display:none}
.seg{display:flex;border:1px solid var(--line);border-radius:var(--r-sm);overflow:hidden;
     flex:0 0 auto;background:var(--sunk)}
.seg button{border:0;background:transparent;color:var(--faint);cursor:pointer;
            width:38px;height:42px;display:grid;place-items:center;padding:0}
.seg button[aria-pressed="true"]{background:var(--raise);color:var(--text)}
.seg svg{width:16px;height:16px;fill:currentColor}
@media (max-width:700px){
  .tools{flex-wrap:wrap}
  .search{order:-1;flex:1 0 100%}
  .tools select{flex:1 1 0}
}

/* --- account menu -------------------------------------------------------- */
/* One button instead of a row of them: at 390px "Open agent / Manage access /
   Sign out" wrapped every label onto two lines and ate a third of the screen. */
.acct{position:relative;flex:0 0 auto}
.avatar{width:34px;height:34px;border-radius:50%;border:1px solid var(--line2);
        background:var(--raise);color:var(--text);cursor:pointer;padding:0;
        font:600 13px/1 var(--body);text-transform:uppercase}
.avatar[aria-expanded="true"]{border-color:var(--live);color:var(--live)}
.menu{position:absolute;right:0;top:calc(100% + 8px);min-width:216px;z-index:40;
      background:var(--panel);border:1px solid var(--line2);border-radius:var(--r);
      padding:6px;box-shadow:0 20px 50px -20px rgba(0,0,0,.95)}
.menu[hidden]{display:none}
.menu .em{padding:9px 10px 11px;font-family:var(--mono);font-size:11.5px;
          color:var(--faint);border-bottom:1px solid var(--line);margin-bottom:6px;
          overflow:hidden;text-overflow:ellipsis}
.menu a{display:block;padding:9px 10px;border-radius:6px;font-size:14px;
        color:var(--text);text-decoration:none}
.menu a:hover,.menu a:focus-visible{background:var(--raise)}

/* --- counts -------------------------------------------------------------- */
.counts{display:flex;align-items:center;gap:14px;flex-wrap:wrap;
        padding:22px 0 18px;font-size:13px;color:var(--faint)}
.counts .n{color:var(--text);font-family:var(--mono);font-size:13px}

/* --- grid ---------------------------------------------------------------- */
.group{margin-bottom:34px}
.ghead{display:flex;align-items:center;gap:10px;padding:0 0 12px;
       border-bottom:1px solid var(--line);margin-bottom:18px}
.ghead .id{font-family:var(--mono);font-size:12.5px;color:var(--muted)}
.ghead .ct{font-family:var(--mono);font-size:11px;color:var(--faint)}
.grid{display:grid;gap:18px;grid-template-columns:repeat(auto-fill,minmax(290px,1fr))}
@media (max-width:560px){ .grid{gap:14px;grid-template-columns:1fr} }

.app{background:var(--panel);border:1px solid var(--line);border-radius:var(--r);
     overflow:hidden;display:flex;flex-direction:column;position:relative;
     transition:border-color .15s,transform .15s}
@media (hover:hover){ .app:hover{border-color:var(--line2);transform:translateY(-2px)} }
.app.pin{border-color:var(--live-deep)}
.shotwrap{position:relative;display:block;background:var(--sunk);
          border-bottom:1px solid var(--line);text-decoration:none}
.shot{display:block;width:100%;aspect-ratio:16/10;object-fit:cover;object-position:top center}
.noshot{aspect-ratio:16/10;display:grid;place-items:center;color:var(--faint);font-size:12.5px;
        text-align:center;padding:16px;font-family:var(--mono)}
.app .pill{position:absolute;left:10px;bottom:10px;backdrop-filter:blur(6px)}
.app .pill.inline{display:none}
.star{position:absolute;right:8px;top:8px;width:32px;height:32px;border-radius:8px;
      border:1px solid var(--line);background:rgba(8,10,14,.7);color:var(--faint);
      display:grid;place-items:center;cursor:pointer;padding:0}
.star svg{width:15px;height:15px;fill:none;stroke:currentColor;stroke-width:1.7}
.star[aria-pressed="true"]{color:var(--live);border-color:var(--live-deep)}
.star[aria-pressed="true"] svg{fill:currentColor}

.meta{padding:14px 15px 12px;flex:1;min-width:0}
.name{font-family:var(--display);font-size:15.5px;font-weight:600;margin:0;
      overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.name input{font:inherit;width:100%;background:var(--sunk);color:var(--text);
            border:1px solid var(--live-deep);border-radius:6px;padding:2px 6px}
.host{font-family:var(--mono);font-size:11.5px;color:var(--faint);margin-top:5px;
      overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.when{font-size:12px;color:var(--faint);margin-top:8px}
.acts{display:flex;gap:7px;padding:0 15px 15px}
.acts .btn{flex:0 0 auto}
/* Open stays quiet. Six amber blocks per screen would out-shout the awake/asleep
   pills, and those are the only colour on this page that carries information. */
.acts .open{flex:1;background:var(--raise);border-color:var(--line2);color:var(--text)}
@media (hover:hover){
  .acts .open:hover{border-color:var(--live);color:var(--live);background:var(--live-dim)}
}
.icon{width:38px;padding:0}
.icon svg{width:15px;height:15px;fill:none;stroke:currentColor;stroke-width:1.7;
          stroke-linecap:round;stroke-linejoin:round}

/* --- list density -------------------------------------------------------- */
.grid.list{grid-template-columns:1fr;gap:8px}
.grid.list .app{flex-direction:row;align-items:center;padding:9px 12px 9px 9px;gap:14px}
.grid.list .shotwrap{width:104px;flex:0 0 auto;border:1px solid var(--line);
                     border-radius:8px;overflow:hidden}
.grid.list .shot,.grid.list .noshot{aspect-ratio:16/10}
.grid.list .noshot{font-size:0}
.grid.list .shotwrap .pill{display:none}
.grid.list .star{position:static;order:3}
/* Three rows, not four: the state pill shares the last line with the timestamps,
   which is what keeps the compact density actually compact. */
.grid.list .meta{padding:0;display:grid;align-content:center;gap:0 10px;
                 grid-template-columns:max-content 1fr;
                 grid-template-areas:"name name" "host host" "when pill"}
.grid.list .name{grid-area:name}
.grid.list .host{grid-area:host}
.grid.list .when{grid-area:when;margin-top:5px}
.grid.list .meta .pill.inline{grid-area:pill;display:inline-flex;position:static;
                              justify-self:start;align-self:center;
                              backdrop-filter:none;margin-top:5px}
.grid.list .acts{padding:0}
.grid.list .acts .open{flex:0 0 auto}
@media (max-width:560px){ .grid.list .acts .btn:not(.open){display:none} }

/* --- states -------------------------------------------------------------- */
/* Nothing to search, sort or group: an empty screen should be an invitation to
   act, not a control panel presiding over nothing. */
body.bare .tools,body.bare .counts,body.bare .note{display:none}
body.bare header{padding-bottom:12px}
body.bare #grid{margin-top:60px}
.empty{border:1px dashed var(--line2);border-radius:var(--r);padding:60px 26px;
       text-align:center;color:var(--muted);max-width:520px;margin:8px auto}
.empty h2{font-size:19px;margin-bottom:10px}
.empty p{font-size:14px;margin-top:8px}
.empty .mono{color:var(--faint);font-size:12.5px;margin-top:18px;display:block}
.msg{font-size:13px;margin:18px 0 0;min-height:1em;color:var(--muted)}
.msg.bad{color:var(--bad)}
.note{font-size:12.5px;color:var(--faint);margin-top:34px;line-height:1.7;max-width:70ch;
      border-top:1px solid var(--line);padding-top:18px}
.stale{position:fixed;left:50%;transform:translateX(-50%);z-index:40;
       bottom:max(18px,env(safe-area-inset-bottom));
       background:var(--raise);border:1px solid var(--frozen-deep);color:var(--frozen);
       font-family:var(--mono);font-size:12px;padding:9px 15px;border-radius:999px;display:none}
"""

HOME_PAGE = (
    '<!doctype html><html lang="en"><head>'
    + head("Preview gallery", _HOME_CSS, "__PWA_HEAD__")
    + """</head><body>
<header>
  <div class="wrap">
    <div class="bar">
      <a class="mark" href="/">"""
    + _MARK_GLYPH
    + """Preview</a>
      <span id="agent-slot"></span>
      <div class="acct">
        <button class="avatar" id="acct" aria-expanded="false" aria-haspopup="true"
                aria-controls="menu" aria-label="Account"></button>
        <div class="menu" id="menu" hidden>
          <div class="em">__EMAIL__</div>
          <span id="admin-slot"></span>
          <a href="/__auth/logout">Sign out</a>
        </div>
      </div>
    </div>
    <div class="tools">
      <label class="search">
        <input class="field" id="q" type="search" placeholder="Search sites"
               autocomplete="off" spellcheck="false" aria-label="Search sites">
        <svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="7"/><path d="M20 20l-4-4"/></svg>
        <kbd>/</kbd>
      </label>
      <select class="field" id="sort" aria-label="Sort by">
        <option value="recent">Recent</option>
        <option value="name">Name</option>
        <option value="port">Port</option>
        <option value="oldest">Oldest</option>
      </select>
      <select class="field" id="group" aria-label="Group by">
        <option value="none">Ungrouped</option>
        <option value="sandbox">By sandbox</option>
      </select>
      <div class="seg" role="group" aria-label="Density">
        <button id="v-grid" aria-pressed="true" title="Grid" aria-label="Grid">
          <svg viewBox="0 0 24 24"><rect x="3" y="3" width="8" height="8" rx="1.5"/>
          <rect x="13" y="3" width="8" height="8" rx="1.5"/><rect x="3" y="13" width="8" height="8" rx="1.5"/>
          <rect x="13" y="13" width="8" height="8" rx="1.5"/></svg>
        </button>
        <button id="v-list" aria-pressed="false" title="List" aria-label="List">
          <svg viewBox="0 0 24 24"><rect x="3" y="4" width="18" height="3.4" rx="1.5"/>
          <rect x="3" y="10.3" width="18" height="3.4" rx="1.5"/>
          <rect x="3" y="16.6" width="18" height="3.4" rx="1.5"/></svg>
        </button>
      </div>
    </div>
  </div>
</header>

<div class="wrap">
  <div class="counts">
    <span><span class="n" id="c-sites">0</span> sites</span>
    <span><span class="n" id="c-boxes">0</span> sandboxes</span>
  </div>
  <div id="grid"></div>
  <div class="msg" id="msg" role="status"></div>
  <p class="note">
    Tiles are screenshots, not live pages — loading the real sites here would wake
    every sandbox and bill for compute. Each shot refreshes when the proxy wakes that
    sandbox to serve someone. <strong>Refresh</strong> forces a new one and wakes the
    sandbox if it is asleep. Sleep state is inferred from the last visit, so a
    sandbox the agent is working in may say asleep while it is not.
  </p>
</div>
<div class="stale" id="stale">offline — showing the last loaded list</div>

<script>
const IS_ADMIN = __IS_ADMIN__, BASE = __BASE__, IDLE_MIN = __IDLE_MIN__;
const AGENT_URL = __AGENT_URL__;   // "" when the agent isn't exposed to this user
let APPS = __APPS__;

const grid = document.getElementById("grid"), msg = document.getElementById("msg");
const q = document.getElementById("q");
const sortSel = document.getElementById("sort"), groupSel = document.getElementById("group");

// --- header actions --------------------------------------------------------
// "Open agent" is the one action worth a button of its own; the rest live in the
// account menu, which is also the only place the signed-in address is shown.
if (AGENT_URL) {
  const a = document.createElement("a");
  a.className = "btn sm go"; a.href = AGENT_URL; a.textContent = "Open agent";
  document.getElementById("agent-slot").appendChild(a);
}
if (IS_ADMIN) {
  const a = document.createElement("a");
  a.href = "/__auth/admin"; a.textContent = "Manage access";
  document.getElementById("admin-slot").appendChild(a);
}

const acct = document.getElementById("acct"), menu = document.getElementById("menu");
const emailEl = menu.querySelector(".em");
acct.textContent = (emailEl.textContent.trim()[0] || "?");
function closeMenu() { menu.hidden = true; acct.setAttribute("aria-expanded", "false"); }
acct.onclick = (e) => {
  e.stopPropagation();
  menu.hidden = !menu.hidden;
  acct.setAttribute("aria-expanded", String(!menu.hidden));
};
addEventListener("click", (e) => { if (!menu.contains(e.target)) closeMenu(); });
addEventListener("keydown", (e) => { if (e.key === "Escape") closeMenu(); });

// --- preferences -----------------------------------------------------------
// Kept on the device, not the server: how you like the grid laid out is nobody
// else's business, and this list is shared by everyone who signs in.
const prefs = {
  get(k, fallback) { try { return localStorage.getItem("preview:" + k) || fallback; }
                     catch (e) { return fallback; } },
  set(k, v) { try { localStorage.setItem("preview:" + k, v); } catch (e) {} },
};
let view = prefs.get("view", "grid");
sortSel.value = prefs.get("sort", "recent");
groupSel.value = prefs.get("group", "none");

const vGrid = document.getElementById("v-grid"), vList = document.getElementById("v-list");
function setView(v) {
  view = v; prefs.set("view", v);
  vGrid.setAttribute("aria-pressed", String(v === "grid"));
  vList.setAttribute("aria-pressed", String(v === "list"));
  render();
}
vGrid.onclick = () => setView("grid");
vList.onclick = () => setView("list");
sortSel.onchange = () => { prefs.set("sort", sortSel.value); render(); };
groupSel.onchange = () => { prefs.set("group", groupSel.value); render(); };
q.oninput = () => render();
addEventListener("keydown", (e) => {
  if (e.key === "/" && document.activeElement !== q && !e.metaKey && !e.ctrlKey) {
    e.preventDefault(); q.focus();
  }
  if (e.key === "Escape" && document.activeElement === q) { q.value = ""; q.blur(); render(); }
});

// --- helpers ---------------------------------------------------------------
const name = (a) => a.label || a.title || ("port " + a.port);
const hostOf = (a) => a.key + "." + BASE;
// Awake is INFERRED, never observed: the proxy pauses a sandbox after IDLE_MIN
// with no traffic, so a recent visit means it is almost certainly still up. The
// note under the grid says so, and the tooltip below repeats it where it matters.
const awake = (a) => a.last_seen && (Date.now() / 1000 - a.last_seen) < IDLE_MIN * 60;

function ago(ts) {
  if (!ts) return "never";
  const s = Math.max(0, Math.floor(Date.now() / 1000) - ts);
  if (s < 60) return "just now";
  for (const [label, size] of [["d", 86400], ["h", 3600], ["m", 60]]) {
    if (s >= size) return Math.floor(s / size) + label + " ago";
  }
  return "just now";
}

function icon(path) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.innerHTML = path;                       // literal, never user data
  return svg;
}
const I_REFRESH = '<path d="M20 11a8 8 0 1 0-2.3 5.7"/><path d="M20 4.5V11h-6.2"/>';
const I_RENAME = '<path d="M4 20h16"/><path d="M14.5 4.5l4.2 4.2L9 18.4l-4.6 1 1-4.6z"/>';
const I_FORGET = '<path d="M4 7h16"/><path d="M9.5 7V4.8h5V7"/><path d="M6.5 7l1 12.5h9L17.5 7"/>';
const I_STAR = '<path d="M12 3.6l2.6 5.4 5.9.8-4.3 4.1 1 5.9-5.2-2.8-5.2 2.8 1-5.9L3.5 9.8l5.9-.8z"/>';

// --- one card --------------------------------------------------------------
function card(app) {
  const el = document.createElement("div");
  el.className = "app" + (app.pinned ? " pin" : "");

  const shot = document.createElement("a");
  shot.className = "shotwrap";
  shot.href = "https://" + hostOf(app) + "/";
  shot.target = "_blank"; shot.rel = "noopener";
  shot.setAttribute("aria-label", "Open " + name(app));
  if (app.shot_at) {
    const img = document.createElement("img");
    img.className = "shot"; img.loading = "lazy"; img.alt = "";
    img.src = "/__apps/shot/" + encodeURIComponent(app.key) + "?v=" + app.shot_at;
    shot.appendChild(img);
  } else {
    const ph = document.createElement("div");
    ph.className = "noshot";
    ph.textContent = "no screenshot yet";
    shot.appendChild(ph);
  }
  // Two pills, one shown at a time by CSS: over the thumbnail in the grid, and
  // in the text column in the list, where a 104px thumbnail can't hold one.
  shot.appendChild(statePill(app));
  el.appendChild(shot);

  if (IS_ADMIN) {
    const star = document.createElement("button");
    star.className = "star";
    star.setAttribute("aria-pressed", String(!!app.pinned));
    star.setAttribute("aria-label", app.pinned ? "Unpin " + name(app) : "Pin " + name(app));
    star.title = app.pinned ? "Unpin" : "Pin to the top";
    star.appendChild(icon(I_STAR));
    star.onclick = () => post("/__apps/pin", { key: app.key, pinned: !app.pinned }, star);
    el.appendChild(star);
  }

  const meta = document.createElement("div");
  meta.className = "meta";
  const t = document.createElement("p");
  t.className = "name";
  t.textContent = name(app);          // textContent: titles come from agent-built pages
  const h = document.createElement("div");
  h.className = "host"; h.textContent = hostOf(app);
  const w = document.createElement("div");
  w.className = "when";
  w.textContent = "visited " + ago(app.last_seen) + " · shot " + ago(app.shot_at);
  meta.append(t, h, w, statePill(app, true));
  el.appendChild(meta);

  const acts = document.createElement("div");
  acts.className = "acts";
  const open = document.createElement("a");
  open.className = "btn go sm open"; open.href = "https://" + hostOf(app) + "/";
  open.target = "_blank"; open.rel = "noopener"; open.textContent = "Open";
  acts.appendChild(open);

  acts.appendChild(iconBtn(I_REFRESH, "Refresh screenshot", "",
    (btn) => post("/__apps/refresh", { key: app.key }, btn)));

  if (IS_ADMIN) {
    acts.appendChild(iconBtn(I_RENAME, "Rename", "", () => rename(app, t)));
    acts.appendChild(iconBtn(I_FORGET, "Remove from gallery", "danger", (btn) => {
      if (confirm("Remove " + hostOf(app) + " from the gallery? The sandbox is left alone."))
        post("/__apps/forget", { key: app.key }, btn);
    }));
  }
  el.appendChild(acts);
  return el;
}

function statePill(app, inline) {
  const up = awake(app);
  const pill = document.createElement("span");
  pill.className = "pill " + (up ? "live" : "frozen") + (inline ? " inline" : "");
  pill.textContent = up ? "awake" : "asleep";
  pill.title = up
    ? "Visited within the last " + IDLE_MIN + " minutes, so it is probably still running."
    : "No preview traffic for over " + IDLE_MIN + " minutes, so it has probably been paused. Opening it wakes it.";
  return pill;
}

function iconBtn(path, label, cls, onclick) {
  const b = document.createElement("button");
  b.className = "btn sm icon " + cls;
  b.title = label; b.setAttribute("aria-label", label);
  b.appendChild(icon(path));
  b.onclick = () => onclick(b);
  return b;
}

// Rename in place. Enter commits, Escape abandons, blur commits — the three
// things a text field is expected to do.
function rename(app, titleEl) {
  const input = document.createElement("input");
  input.value = app.label || app.title || "";
  input.placeholder = "port " + app.port;
  input.setAttribute("aria-label", "Name for " + hostOf(app));
  titleEl.textContent = ""; titleEl.appendChild(input);
  input.focus(); input.select();
  let done = false;
  const commit = (save) => {
    if (done) return;
    done = true;
    if (!save) { titleEl.textContent = name(app); return; }
    post("/__apps/label", { key: app.key, label: input.value }, null);
  };
  input.onkeydown = (e) => {
    if (e.key === "Enter") commit(true);
    if (e.key === "Escape") commit(false);
  };
  input.onblur = () => commit(true);
}

// --- rendering -------------------------------------------------------------
function matches(app, needle) {
  if (!needle) return true;
  return (name(app) + " " + hostOf(app) + " " + app.sandbox_id + " " + app.port)
    .toLowerCase().includes(needle);
}

function ordered(apps) {
  const by = sortSel.value;
  const list = apps.slice();
  if (by === "name") list.sort((a, b) => name(a).localeCompare(name(b)));
  else if (by === "port") list.sort((a, b) => a.port - b.port);
  else if (by === "oldest") list.sort((a, b) => (a.last_seen || 0) - (b.last_seen || 0));
  else list.sort((a, b) => (b.last_seen || 0) - (a.last_seen || 0));
  // Pinned always float, whatever the sort — that is what pinning is for.
  return list.sort((a, b) => (b.pinned ? 1 : 0) - (a.pinned ? 1 : 0));
}

function gridOf(apps) {
  const g = document.createElement("div");
  g.className = "grid" + (view === "list" ? " list" : "");
  for (const app of apps) g.appendChild(card(app));
  return g;
}

function render() {
  const needle = q.value.trim().toLowerCase();
  const shown = ordered(APPS.filter((a) => matches(a, needle)));

  document.getElementById("c-sites").textContent = String(APPS.length);
  document.getElementById("c-boxes").textContent =
    String(new Set(APPS.map((a) => a.sandbox_id)).size);

  grid.innerHTML = "";
  document.body.classList.toggle("bare", !APPS.length);
  if (!APPS.length) {
    grid.appendChild(emptyState(
      "Nothing here yet",
      "Ask the agent to build a site and serve it on a port. The first time you open it, it lands here.",
      "https://<sandboxId>-<port>." + BASE + "/"));
    return;
  }
  if (!shown.length) {
    grid.appendChild(emptyState("No matches", "Nothing matches \\u201c" + needle + "\\u201d.", ""));
    return;
  }
  if (groupSel.value !== "sandbox") { grid.appendChild(gridOf(shown)); return; }

  // Grouped: one section per sandbox, i.e. per chat session. Sections keep the
  // order their first app had, so the sort still decides what you see first.
  const boxes = new Map();
  for (const app of shown) {
    if (!boxes.has(app.sandbox_id)) boxes.set(app.sandbox_id, []);
    boxes.get(app.sandbox_id).push(app);
  }
  for (const [id, apps] of boxes) {
    const sec = document.createElement("section");
    sec.className = "group";
    const head = document.createElement("div");
    head.className = "ghead";
    const idEl = document.createElement("span");
    idEl.className = "id"; idEl.textContent = id;
    const ct = document.createElement("span");
    ct.className = "ct"; ct.textContent = apps.length + (apps.length === 1 ? " port" : " ports");
    head.append(idEl, ct);
    sec.append(head, gridOf(apps));
    grid.appendChild(sec);
  }
}

function emptyState(title, body, mono) {
  const d = document.createElement("div");
  d.className = "empty";
  const h = document.createElement("h2"); h.textContent = title;
  const p = document.createElement("p"); p.textContent = body;
  d.append(h, p);
  if (mono) {
    const m = document.createElement("span");
    m.className = "mono"; m.textContent = mono;
    d.appendChild(m);
  }
  return d;
}

// --- server mutations ------------------------------------------------------
async function post(url, body, btn) {
  if (btn) btn.disabled = true;
  msg.className = "msg"; msg.textContent = "";
  try {
    const res = await fetch(url, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.message || res.statusText);
    msg.textContent = data.message || "";
    APPS = data.apps || APPS;
    render();                                 // re-render replaces btn; no re-enable needed
  } catch (e) {
    msg.className = "msg bad"; msg.textContent = e.message || String(e);
    if (btn) btn.disabled = false;
  }
}

render();

// --- PWA -------------------------------------------------------------------
// The page you are reading was fetched network-first, so APPS is whatever the
// server just said. Registering the worker only adds an offline fallback and
// instant screenshots; it never serves you a stale list while online.
if ("serviceWorker" in navigator) {
  addEventListener("load", async () => {
    try {
      const reg = await navigator.serviceWorker.register("/__apps/sw.js", { scope: "/" });
      // Check on every load, so a deploy lands on the next launch rather than
      // whenever the browser next feels like it.
      reg.update();
      // A worker that installs while this tab is open has newer markup than the
      // markup running it; reload once, and only once, to pick it up.
      let reloading = false;
      navigator.serviceWorker.addEventListener("controllerchange", () => {
        if (reloading) return;
        reloading = true;
        location.reload();
      });
    } catch (e) { /* the gallery works fine without it */ }
  });
}
// Tell the user when what they're looking at came off the shelf.
const stale = document.getElementById("stale");
const setStale = (on) => { stale.style.display = on ? "block" : "none"; };
if (!navigator.onLine) setStale(true);
addEventListener("offline", () => setStale(true));
addEventListener("online", () => setStale(false));
</script></body></html>
"""
)


# ---------------------------------------------------------------------------
# Admin panel
# ---------------------------------------------------------------------------
# Reachable only by config.ADMIN_EMAILS; everyone else gets a 404 from the
# handler, so this markup is never sent to a non-admin. It can add and remove
# GUESTS only — there is no "make admin" control, by design (allowlist.py).
_ADMIN_CSS = """
body{padding:0 20px 60px}
.wrap{max-width:640px;margin:0 auto}
.top{display:flex;align-items:center;height:64px}
.mark{display:flex;align-items:center;gap:9px;font-family:var(--display);
      font-weight:600;font-size:15px;text-decoration:none;margin-right:auto}
.glyph{width:22px;height:22px}
h1{font-size:27px;margin:26px 0 0}
.sub{color:var(--muted);font-size:14.5px;margin-top:12px;max-width:60ch}
.warn{margin-top:18px;padding:13px 15px;border-radius:var(--r-sm);
      border:1px solid var(--live-deep);background:var(--live-dim);
      color:#F6DFAE;font-size:13.5px}
form{display:flex;gap:8px;margin:28px 0 20px;flex-wrap:wrap}
form .field{flex:1;min-width:180px}
ul{list-style:none;padding:0;margin:0;border:1px solid var(--line);
   border-radius:var(--r);overflow:hidden}
li{display:flex;align-items:center;gap:12px;padding:11px 14px;background:var(--panel)}
li+li{border-top:1px solid var(--line)}
li .who{flex:1;min-width:0;font-family:var(--mono);font-size:13px;
        overflow:hidden;text-overflow:ellipsis}
li .field{min-height:34px;font-size:13px}
.empty{padding:26px 16px;color:var(--faint);font-size:13.5px;background:var(--panel);
       text-align:center}
.msg{font-size:13px;margin-top:16px;min-height:1em;color:var(--muted)}
.msg.bad{color:var(--bad)}
footer{margin-top:34px;padding-top:18px;border-top:1px solid var(--line);
       font-size:12.5px;color:var(--faint);line-height:1.7}
footer a{color:var(--muted)}
code{font-family:var(--mono);font-size:12px;color:var(--muted)}
"""

ADMIN_PAGE = (
    '<!doctype html><html lang="en"><head>'
    + head("Preview access", _ADMIN_CSS)
    + """</head><body><div class="wrap">
  <div class="top">
    <a class="mark" href="/">"""
    + _MARK_GLYPH
    + """Preview</a>
    <a class="btn sm" href="/">Back to gallery</a>
  </div>
  <span class="eyebrow">Access</span>
  <h1>Who gets in.</h1>
  <p class="sub">Anyone listed here can sign in and open every preview site.
     Roles are checked on each request, so a change lands on their next click.</p>
  <div class="warn">Grant <b>Previews + agent</b> only to people you're happy to have
     spend your model tokens and sandbox compute.</div>
  <form id="add">
    <input class="field" id="email" type="email" placeholder="friend@example.com" required
           autocomplete="off" spellcheck="false" aria-label="Email address">
    <select class="field" id="role" aria-label="Role">
      <option value="preview">Previews only</option>
      <option value="agent">Previews + agent</option>
    </select>
    <button class="btn go" type="submit">Add</button>
  </form>
  <ul id="list"></ul>
  <div class="msg" id="msg" role="status"></div>
  <footer>
    Signed in as __EMAIL__ · <a href="/__auth/logout">Sign out</a><br>
    Admins are set by <code>PREVIEW_ADMIN_EMAILS</code> on the server. They can't be
    granted, demoted or removed from this page — only an admin can hand out agent
    access, and no one can hand out admin.
  </footer>
</div>
<script>
const list = document.getElementById("list"), msg = document.getElementById("msg");
const form = document.getElementById("add"), email = document.getElementById("email");
const ADMINS = __ADMINS__;

function render(members) {
  list.innerHTML = "";
  for (const a of ADMINS) list.appendChild(row({ email: a, role: "admin" }, true));
  for (const m of members) list.appendChild(row(m, false));
  if (!members.length) {
    const d = document.createElement("div");
    d.className = "empty";
    d.textContent = "No guests yet — add someone above.";
    list.appendChild(d);
  }
}
function row(member, admin) {
  const li = document.createElement("li");
  const s = document.createElement("span");
  s.className = "who";
  s.textContent = member.email;              // textContent, never innerHTML
  li.appendChild(s);
  if (admin) {
    // Admins come from PREVIEW_ADMIN_EMAILS and have no controls here on purpose:
    // this page cannot create, demote or remove one.
    const t = document.createElement("span");
    t.className = "pill live"; t.textContent = "admin";
    li.appendChild(t);
    return li;
  }
  const sel = document.createElement("select");
  sel.className = "field";
  sel.setAttribute("aria-label", "Role for " + member.email);
  for (const [value, label] of [["preview", "Previews only"], ["agent", "Previews + agent"]]) {
    const o = document.createElement("option");
    o.value = value; o.textContent = label;
    if (member.role === value) o.selected = true;
    sel.appendChild(o);
  }
  sel.onchange = () => {
    if (sel.value === "agent" &&
        !confirm("Let " + member.email + " run the agent? They'll be able to spend your model tokens and sandbox compute.")) {
      sel.value = member.role; return;
    }
    send("set_role", member.email, sel.value);
  };
  li.appendChild(sel);
  const b = document.createElement("button");
  b.className = "btn sm danger";
  b.textContent = "Remove";
  b.onclick = () => send("remove", member.email);
  li.appendChild(b);
  return li;
}
async function send(action, addr, role) {
  msg.className = "msg"; msg.textContent = "";
  const res = await fetch("/__auth/admin/guests", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ action, email: addr, role }),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) { msg.className = "msg bad"; msg.textContent = data.message || res.statusText; }
  else msg.textContent = data.message || "";
  if (data.members) render(data.members);
}
form.onsubmit = async (e) => {
  e.preventDefault();
  const addr = email.value.trim();
  if (addr) { await send("add", addr, document.getElementById("role").value); email.value = ""; }
};
render(__MEMBERS__);
</script></body></html>
"""
)


# ---------------------------------------------------------------------------
# Dead ends
# ---------------------------------------------------------------------------
# Shown to someone who IS signed in but hasn't been granted agent access.
# Deliberately not a redirect to sign-in: they're already signed in, so that would
# just bounce them back here forever.
_STOP_CSS = """
body{min-height:100svh;display:grid;place-items:center;padding:24px}
.card{max-width:440px;padding:32px 28px}
h1{font-size:23px;margin:16px 0 0}
p{color:var(--muted);font-size:14.5px;margin-top:12px}
.acts{display:flex;gap:10px;margin-top:24px;flex-wrap:wrap}
code{font-family:var(--mono);font-size:12.5px;color:var(--muted)}
"""

DENIED_PAGE = (
    '<!doctype html><html lang="en"><head>'
    + head("Agent access required", _STOP_CSS)
    + """</head><body><div class="card">
  <span class="eyebrow">Not granted</span>
  <h1>Running the agent is a separate grant.</h1>
  <p>You're signed in and every preview site is open to you. Driving the agent
     spends model tokens and sandbox compute, so an admin has to hand that out
     deliberately — ask one to give you the <b>agent</b> role.</p>
  <div class="acts"><a class="btn go" href="/">Back to the gallery</a></div>
</div></body></html>
"""
)

# What the proxy answers on a host that is neither a sandbox nor the login host.
# There is nothing to choose from here — routing is host-in-the-URL by design —
# so this says how to build the URL and stops.
HELP_PAGE = (
    '<!doctype html><html lang="en"><head>'
    + head("Sandbox preview proxy", _STOP_CSS)
    + """</head><body><div class="card">
  <span class="eyebrow">Preview proxy</span>
  <h1>Previews live on their own hostname.</h1>
  <p>Open <code>https://&lt;sandboxId&gt;-&lt;port&gt;.__BASE__/</code>. The chat agent
     prints the full URL after it builds a site — it reads the id from
     <code>$E2B_SANDBOX_ID</code> inside the sandbox.</p>
</div></body></html>
"""
)

