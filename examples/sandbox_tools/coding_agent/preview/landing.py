"""
The logged-out landing page — what someone sees at the root of AUTH_HOST before
they sign in.

It exists because the old behaviour was a bounce: `/` redirected straight to a
sign-in card that said "Sign in to view agent-built sites" and nothing else. Two
kinds of people hit that and both were stuck. Someone who was sent a preview link
had no idea what they were about to be given access to, and someone who is not on
the allowlist got no hint that being added by an admin is the missing step rather
than a bug in the button.

So this page has one job: explain what signing in gets you, honestly, before you
sign in. Every claim on it is something the proxy actually does — the state
model (proxy.py's idle pause), the hostname scheme (parse_preview_host), the
403-to-everyone-but-us traffic token, the roles (allowlist.py). If one of those
changes, the copy here is wrong and should change with it.

MEDIA SLOTS. Two places render a real recording if one has been dropped into
`preview/static/media/`, and a hand-built CSS mock otherwise (see pwa.media and
the README section "Landing-page demo clips"). The mock is the default on
purpose: it is always accurate, it costs no bytes, and nothing here degrades if
the files are never added.
"""

import json

from .pwa import head_tags, media_tag
from .theme import head, mark_svg

# --------------------------------------------------------------------------
# Page-specific CSS. The shared layer (tokens, buttons, pills) is in theme.py.
# --------------------------------------------------------------------------
_CSS = """
.wrap{max-width:1080px;margin:0 auto;padding:0 22px}
section{padding:74px 0;position:relative}
@media (max-width:720px){ section{padding:60px 0} }

/* --- nav ----------------------------------------------------------------- */
nav{position:sticky;top:0;z-index:20;background:rgba(8,10,14,.78);
    backdrop-filter:blur(14px);border-bottom:1px solid var(--line)}
nav .wrap{display:flex;align-items:center;gap:14px;height:60px}
.mark{display:flex;align-items:center;gap:9px;font-family:var(--display);
      font-weight:600;font-size:15px;letter-spacing:-.01em;margin-right:auto}
.glyph{width:22px;height:22px;flex:0 0 auto}
nav .host{font-family:var(--mono);font-size:12px;color:var(--faint)}
@media (max-width:620px){ nav .host{display:none} }

/* --- hero ---------------------------------------------------------------- */
.hero{padding-top:76px;padding-bottom:0}
.hero h1{font-size:clamp(38px,7vw,66px);font-weight:700;max-width:15ch;margin:18px 0 0}
.hero h1 em{font-style:normal;color:var(--live)}
.lead{color:var(--muted);font-size:clamp(16px,2.2vw,19px);max-width:56ch;margin-top:20px}
.cta{display:flex;flex-wrap:wrap;gap:12px;margin-top:30px}
.cta .btn{min-height:48px;padding:0 22px;font-size:15px}
.invite{margin-top:18px;font-family:var(--mono);font-size:12.5px;color:var(--faint)}
.invite b{color:var(--muted);font-weight:500}

/* --- signature: the wake sequence --------------------------------------- */
.stage{margin-top:56px;position:relative}
/* A single soft bloom behind the window — the one gradient on the page. */
.stage::before{
  content:"";position:absolute;inset:-14% -6% 26%;z-index:0;pointer-events:none;
  background:radial-gradient(60% 60% at 50% 40%,rgba(255,194,75,.13),transparent 70%);
}
.win{position:relative;z-index:1;border:1px solid var(--line2);border-radius:var(--r-lg);
     background:var(--panel);overflow:hidden;box-shadow:0 30px 80px -30px rgba(0,0,0,.9)}
.bar{display:flex;align-items:center;gap:12px;padding:11px 14px;border-bottom:1px solid var(--line);
     background:var(--raise)}
.dots{display:flex;gap:6px;flex:0 0 auto}
.dots i{width:9px;height:9px;border-radius:50%;background:#2A3242;display:block}
.url{flex:1;min-width:0;display:flex;align-items:center;gap:0;background:var(--sunk);
     border:1px solid var(--line);border-radius:999px;padding:6px 13px;
     font-family:var(--mono);font-size:12.5px;color:var(--muted);
     overflow:hidden;white-space:nowrap}
.url .sbx{color:var(--text)}
.url .port{color:var(--live)}
.url .caret{width:1px;height:13px;margin-left:5px;flex:0 0 auto;background:var(--live);animation:blink 1.1s steps(1) infinite}
@keyframes blink{50%{opacity:0}}
@media (max-width:560px){ .dots{display:none} .url{font-size:11px} }

.screen{position:relative;aspect-ratio:16/8;background:var(--sunk)}
.screen video,.screen img{width:100%;height:100%;object-fit:cover;object-position:top center;display:block}
/* A phone gets a taller frame: at 16/8 the mock is 170px high and unreadable. */
@media (max-width:640px){ .screen{aspect-ratio:4/3.4} }

/* The thaw, on a 9s loop that STARTS LIVE — most people see one still frame of
   this page, and the frame worth showing them is the thawed one.
     0–38%  live      38–60%  suspended      60–76%  resuming      76–100%  live
   Under prefers-reduced-motion the animations are clamped to nothing and every
   element falls back to its BASE style, so those bases spell out the live state:
   frost transparent, live pill opaque. Same picture, no movement. */
.frost{position:absolute;inset:0;z-index:3;pointer-events:none;opacity:0;
       background:
         repeating-linear-gradient(115deg,rgba(127,216,245,.09) 0 2px,transparent 2px 7px),
         radial-gradient(120% 90% at 50% 0%,rgba(127,216,245,.18),rgba(8,10,14,.70));
       animation:thaw 9s cubic-bezier(.4,0,.2,1) infinite}
@keyframes thaw{
  0%,36%{opacity:0}
  42%,62%{opacity:1}
  78%,100%{opacity:0}
}
.state{position:absolute;top:14px;right:14px;z-index:4;height:22px}
.state .pill{position:absolute;top:0;right:0;opacity:0;animation:9s steps(1) infinite}
/* The BASE opacity is what a reduced-motion visitor sees, not the last keyframe:
   a finished animation with no fill-mode reverts to the computed style. So the
   live pill is visible by default and the animation hides it when its turn ends,
   which is also why `live` is the state the still frame shows. */
.state .s-live{opacity:1;animation-name:pLive}
.state .s-susp{animation-name:pSusp}
.state .s-res{animation-name:pRes}
@keyframes pLive{0%,37%{opacity:1}38%,77%{opacity:0}78%,100%{opacity:1}}
@keyframes pSusp{0%,39%{opacity:0}40%,59%{opacity:1}60%,100%{opacity:0}}
@keyframes pRes{0%,61%{opacity:0}62%,76%{opacity:1}77%,100%{opacity:0}}

/* The mock app under the frost: a small dashboard the agent might have built.
   Deliberately a real-looking layout — sidebar, stats, chart — because a vague
   grey wireframe would say nothing about what a preview actually is. */
.mock{position:absolute;inset:0;z-index:1;display:flex}
.mock .m-side{width:132px;flex:0 0 auto;border-right:1px solid var(--line);
              padding:18px 14px;display:flex;flex-direction:column;gap:9px;background:var(--sunk)}
.mock .m-brand{display:flex;align-items:center;gap:7px;font-family:var(--display);
               font-weight:600;font-size:13px;margin-bottom:7px}
.mock .m-brand em{width:14px;height:14px;border-radius:4px;background:var(--live);
                  display:block;font-style:normal;flex:0 0 auto}
.mock .m-nav{font-size:11.5px;color:var(--faint);padding:5px 8px;border-radius:6px}
.mock .m-nav.on{background:var(--raise);color:var(--text)}
.mock .m-main{flex:1;min-width:0;padding:18px 20px;display:flex;flex-direction:column;gap:12px}
/* Room for the state pill floating over this row — sized for the widest of the
   three labels ("suspended"), not the one on screen right now. */
.mock .m-h{display:flex;align-items:center;gap:10px;padding-right:124px}
.mock .m-t{font-family:var(--display);font-weight:600;font-size:16px}
.mock .m-s{font-size:11px;color:var(--faint);margin-left:auto;font-family:var(--mono)}
.mock .m-row{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}
.mock .m-c{background:var(--panel);border:1px solid var(--line);border-radius:9px;padding:10px 12px}
.mock .m-n{font-family:var(--display);font-size:19px;font-weight:600}
.mock .m-l{font-size:10.5px;color:var(--faint);margin-top:1px}
.mock .m-chart{flex:2 1 0;min-height:64px;display:flex;align-items:flex-end;gap:5px;
               background:var(--panel);border:1px solid var(--line);border-radius:9px;padding:12px}
.mock .m-chart i{flex:1;background:linear-gradient(180deg,rgba(255,194,75,.85),rgba(255,194,75,.10));
                 border-radius:3px 3px 0 0;display:block}
.mock .m-list{flex:1 1 0;min-height:0;overflow:hidden;background:var(--panel);
              border:1px solid var(--line);border-radius:9px}
.mock .m-list div{display:flex;align-items:center;gap:10px;padding:7px 12px;font-size:11.5px}
.mock .m-list div+div{border-top:1px solid var(--line)}
.mock .m-list .k{color:var(--muted);flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.mock .m-list .v{font-family:var(--mono);color:var(--faint);font-size:11px}
@media (max-width:640px){
  .mock .m-side{display:none}
  .mock .m-main{padding:14px}
  .mock .m-n{font-size:16px}
}

.caption{margin-top:18px;color:var(--faint);font-size:13.5px;max-width:62ch}
.caption b{color:var(--muted);font-weight:500}

/* --- section heads ------------------------------------------------------- */
.head{max-width:60ch}
.head h2{font-size:clamp(26px,4vw,38px);margin:14px 0 0}
.head p{color:var(--muted);margin-top:14px}

/* --- states -------------------------------------------------------------- */
.states{display:grid;gap:16px;grid-template-columns:repeat(3,1fr);margin-top:38px}
@media (max-width:820px){ .states{grid-template-columns:1fr} }
.st{padding:22px;position:relative;overflow:hidden;display:flex;flex-direction:column}
.st::after{content:"";position:absolute;left:0;top:0;bottom:0;width:2px;background:var(--line2)}
.st.a::after{background:var(--live)}
.st.b::after{background:var(--frozen)}
.st h3{font-size:17px;margin:16px 0 8px}
.st p{color:var(--muted);font-size:14px}
/* auto, so the cost line sits on the floor of every card whatever the copy above
   it does — three cards of different lengths otherwise stagger their rules. */
.st .cost{margin-top:auto;padding-top:16px;font-family:var(--mono);font-size:11.5px;
          color:var(--faint)}
.st .cost span{display:block;border-top:1px solid var(--line);padding-top:12px}
.st .pill{align-self:flex-start}

/* --- what you get -------------------------------------------------------- */
.gets{display:grid;gap:18px;grid-template-columns:1fr 1fr;margin-top:38px}
@media (max-width:820px){ .gets{grid-template-columns:1fr} }
.get{padding:24px;display:flex;flex-direction:column}
.get h3{font-size:18px;margin:14px 0 8px}
.get p{color:var(--muted);font-size:14.5px}
.get .vis{margin-top:20px;border-radius:10px;border:1px solid var(--line);background:var(--sunk);
          overflow:hidden;flex:1;min-height:150px;display:flex;align-items:center;justify-content:center}
/* A dropped-in clip needs a box to fill; the CSS mocks size themselves and must
   NOT get one, hence :has() rather than a blanket rule on .vis. */
.get .vis:has(video),.get .vis:has(img){display:block;aspect-ratio:16/10;flex:0 0 auto}
.get .vis video,.get .vis img{width:100%;height:100%;object-fit:cover;display:block}
.get.wide{grid-column:1/-1}

/* Anatomy of a preview hostname — the second signature. */
.anat{font-family:var(--mono);font-size:clamp(11px,2.6vw,15px);padding:26px 18px;text-align:center;
      line-height:2.4;width:100%}
.anat .seg{position:relative;white-space:nowrap}
.anat .seg .t{font-weight:500}
.anat .s-id .t{color:var(--live)}
.anat .s-pt .t{color:var(--frozen)}
.anat .s-bd .t{color:var(--muted)}
.anat .seg .rule{display:block;height:1px;margin-top:7px}
.anat .s-id .rule{background:var(--live-deep)}
.anat .s-pt .rule{background:var(--frozen-deep)}
.anat .s-bd .rule{background:var(--line2)}
.anat .seg .lab{display:block;font-size:10px;letter-spacing:.1em;line-height:1.6;
                text-transform:uppercase;color:var(--faint);margin-top:6px}
.anat .dim{color:var(--faint)}
.anat .row{display:flex;justify-content:center;align-items:flex-start;flex-wrap:wrap;line-height:1.4}
/* Below this the three parts no longer fit on one line, and a diagram that wraps
   mid-hostname explains nothing — so stack them and label each on its own row. */
@media (max-width:620px){
  .anat{text-align:left;padding:20px 16px;line-height:1.5}
  .anat .row{flex-direction:column;align-items:stretch;gap:14px}
  .anat .seg{white-space:normal;word-break:break-all}
  .anat .seg .lab{margin-top:4px}
}

/* Gallery thumbnail mocks (the plain grid, and the one with meta rows). */
.tiles{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;padding:16px;width:100%}
.tiles span{display:block;aspect-ratio:16/10;border-radius:6px;border:1px solid var(--line);
            background:linear-gradient(160deg,#1B2230,#11141B)}
.tiles span:nth-child(2){background:linear-gradient(160deg,#25201A,#14110C)}
.tiles span:nth-child(5){background:linear-gradient(160deg,#16232A,#0D1519)}
.gtiles{display:grid;grid-template-columns:1fr 1fr;gap:10px;padding:16px;width:100%}
.gtiles .g{border:1px solid var(--line);border-radius:8px;overflow:hidden;background:var(--panel)}
.gtiles .g .sh{display:block;aspect-ratio:16/10;background:linear-gradient(160deg,#1B2230,#11141B)}
.gtiles .g:nth-child(2) .sh{background:linear-gradient(160deg,#25201A,#14110C)}
.gtiles .g:nth-child(3) .sh{background:linear-gradient(160deg,#16232A,#0D1519)}
.gtiles .g .mt{padding:8px 10px;display:flex;align-items:center;gap:6px}
.gtiles .g .mt em{display:block;height:5px;border-radius:3px;background:var(--line2);
                  flex:1;font-style:normal}
.gtiles .g .mt .dot{width:5px;height:5px;border-radius:50%;background:var(--frozen);flex:0 0 auto}
.gtiles .g:nth-child(1) .mt .dot{background:var(--live)}

/* Gate diagram. */
.gate{display:flex;flex-direction:column;gap:10px;padding:20px;width:100%;font-family:var(--mono);font-size:12px}
.gate div{display:flex;align-items:center;gap:10px;padding:10px 12px;border-radius:8px;
          border:1px solid var(--line);background:var(--panel);color:var(--muted)}
.gate .no{color:var(--faint)}
.gate .no .code{color:var(--bad)}
.gate .yes .code{color:var(--live)}
.gate .code{font-weight:600;margin-left:auto}

/* --- roles table --------------------------------------------------------- */
.roles{margin-top:38px;border:1px solid var(--line);border-radius:var(--r);overflow-x:auto}
table{border-collapse:collapse;width:100%;min-width:520px}
th,td{text-align:left;padding:14px 18px;border-bottom:1px solid var(--line);font-size:14px}
thead th{font-family:var(--mono);font-size:11px;letter-spacing:.12em;text-transform:uppercase;
         color:var(--faint);font-weight:500;background:var(--sunk)}
tbody tr:last-child td{border-bottom:0}
td.who{font-family:var(--mono);font-size:13px;color:var(--text);white-space:nowrap}
td.no{color:var(--faint)}
.tick{color:var(--live);font-weight:600}

/* --- close --------------------------------------------------------------- */
.close{text-align:center;border-top:1px solid var(--line)}
.close h2{font-size:clamp(26px,4.4vw,40px)}
.close .cta{justify-content:center}
footer{border-top:1px solid var(--line);padding:28px 0 44px;color:var(--faint);font-size:13px}
footer .wrap{display:flex;gap:14px;flex-wrap:wrap;align-items:center}
footer a{color:var(--muted)}
footer .mono{margin-left:auto}
@media (max-width:620px){ footer .mono{margin-left:0} }
"""


# --------------------------------------------------------------------------
# The two visuals, each with a real-recording slot and a hand-built fallback.
# --------------------------------------------------------------------------
# The wake sequence: a mock dashboard under a sheet of frost that dissolves while
# the state pill flips suspended -> resuming -> live. It is CSS rather than a
# recording because it can never go out of date, weighs nothing, and holds still
# for anyone who asked their OS for reduced motion.
_HERO_MOCK = """
<div class="mock">
  <div class="m-side">
    <div class="m-brand"><em></em>Signups</div>
    <div class="m-nav on">Overview</div>
    <div class="m-nav">Sources</div>
    <div class="m-nav">Cohorts</div>
    <div class="m-nav">Settings</div>
  </div>
  <div class="m-main">
    <div class="m-h">
      <span class="m-t">Overview</span>
      <span class="m-s">last 30 days</span>
    </div>
    <div class="m-row">
      <div class="m-c"><div class="m-n">1,284</div><div class="m-l">signups</div></div>
      <div class="m-c"><div class="m-n">63%</div><div class="m-l">activated</div></div>
      <div class="m-c"><div class="m-n">4.1s</div><div class="m-l">median build</div></div>
    </div>
    <div class="m-chart">
      <i style="height:34%"></i><i style="height:52%"></i><i style="height:41%"></i>
      <i style="height:68%"></i><i style="height:57%"></i><i style="height:80%"></i>
      <i style="height:70%"></i><i style="height:92%"></i><i style="height:76%"></i>
      <i style="height:88%"></i>
    </div>
    <div class="m-list">
      <div><span class="k">Organic search</span><span class="v">412</span></div>
      <div><span class="k">Product Hunt</span><span class="v">268</span></div>
      <div><span class="k">Referral</span><span class="v">191</span></div>
    </div>
  </div>
</div>
<div class="frost"></div>
<div class="state">
  <span class="pill live s-live">live</span>
  <span class="pill frozen s-susp">suspended</span>
  <span class="pill frozen s-res">resuming</span>
</div>
"""

_GALLERY_MOCK = """
<div class="gtiles">
  <div class="g"><span class="sh"></span><span class="mt"><span class="dot"></span><em></em></span></div>
  <div class="g"><span class="sh"></span><span class="mt"><span class="dot"></span><em></em></span></div>
  <div class="g"><span class="sh"></span><span class="mt"><span class="dot"></span><em></em></span></div>
  <div class="g"><span class="sh"></span><span class="mt"><span class="dot"></span><em></em></span></div>
</div>
"""


def render(base_domain: str) -> str:
    """The landing page, with the base domain and any dropped-in media filled in."""
    base = base_domain or "preview.example.com"
    return (
        _TEMPLATE.replace("__HERO_MEDIA__", media_tag("hero", _HERO_MOCK))
        .replace("__GALLERY_MEDIA__", media_tag("gallery", _GALLERY_MOCK))
        .replace("__PWA_HEAD__", head_tags())
        # json.dumps then strip the quotes: this lands in HTML text nodes, and a
        # quoted JSON string cannot contain a character that closes the tag.
        .replace("__BASE__", json.dumps(base)[1:-1])
    )


_TEMPLATE = (
    "<!doctype html><html lang=\"en\"><head>"
    + head("Preview — open what the agent built", _CSS, "__PWA_HEAD__")
    + """</head><body>

<nav><div class="wrap">
  <span class="mark">"""
    + mark_svg()
    + """Preview</span>
  <span class="host mono">__BASE__</span>
  <a class="btn go sm" href="/__auth/login">Sign in</a>
</div></nav>

<section class="hero"><div class="wrap">
  <span class="eyebrow">Sandboxed live previews</span>
  <h1>Open what the agent <em>built</em>.</h1>
  <p class="lead">Your coding agent writes and serves web apps inside sealed cloud
     sandboxes that nothing else on the internet can reach. This is the door into
     them: one sign-in, every site it has ever served, each on its own real
     hostname.</p>
  <div class="cta">
    <a class="btn go" href="/__auth/login">Sign in with Google</a>
    <a class="btn" href="#access">See who can do what</a>
  </div>
  <p class="invite">Invite only — <b>an admin adds your address before you can sign in.</b></p>

  <div class="stage">
    <div class="win">
      <div class="bar">
        <span class="dots"><i></i><i></i><i></i></span>
        <span class="url"><span class="sbx">sbx-9f2a1c7d</span><span
          class="port">-3000</span>.__BASE__<span class="caret"></span></span>
      </div>
      <div class="screen">__HERO_MEDIA__</div>
    </div>
    <p class="caption"><b>A suspended sandbox wakes on the first request.</b> Memory
       and running processes come back with it, so the server the agent started is
       already listening — there is no cold boot to sit through.</p>
  </div>
</div></section>

<section><div class="wrap">
  <div class="head">
    <span class="eyebrow">The three states</span>
    <h2>A preview is running, frozen, or finished.</h2>
    <p>That is the whole model, and it is the only thing you have to keep in your
       head. Colour means the same thing on every screen here.</p>
  </div>
  <div class="states">
    <div class="card st a">
      <span class="pill live">live</span>
      <h3>Serving right now</h3>
      <p>The sandbox is awake and answering. Open the URL and you get the app as it
         stands this second, WebSockets and hot reload included.</p>
      <div class="cost"><span>costs compute while awake</span></div>
    </div>
    <div class="card st b">
      <span class="pill frozen">suspended</span>
      <h3>Frozen after ten idle minutes</h3>
      <p>Nobody used it, so the proxy paused it. Disk, memory and processes are held
         exactly as they were. Your next request thaws it.</p>
      <div class="cost"><span>costs nothing while frozen</span></div>
    </div>
    <div class="card st">
      <span class="pill gone">ended</span>
      <h3>Gone with the chat session</h3>
      <p>Sandboxes live as long as the conversation that created them. Close the
         session and the site stops resolving — the screenshot is what's left.</p>
      <div class="cost"><span>tile stays in your gallery</span></div>
    </div>
  </div>
</div></section>

<section><div class="wrap">
  <div class="head">
    <span class="eyebrow">What signing in gets you</span>
    <h2>Four things you can't get from the link alone.</h2>
  </div>
  <div class="gets">

    <div class="card get">
      <span class="pill">gallery</span>
      <h3>Every site, across every session</h3>
      <p>Sandbox IDs only ever appear once, in a chat transcript. The gallery
         remembers each one the proxy has served, so a site you built last week is
         still one click away.</p>
      <div class="vis">__GALLERY_MEDIA__</div>
    </div>

    <div class="card get">
      <span class="pill">routing</span>
      <h3>A real hostname, not a tunnel path</h3>
      <p>Each app is served at the root of its own subdomain, so absolute asset
         paths, service workers and OAuth redirects behave exactly as they will in
         production.</p>
      <div class="vis"><div class="anat">
        <div class="row">
          <span class="seg s-id"><span class="t">sbx-9f2a1c7d</span>
            <span class="rule"></span><span class="lab">sandbox</span></span>
          <span class="seg s-pt"><span class="t">-3000</span>
            <span class="rule"></span><span class="lab">port</span></span>
          <span class="seg s-bd"><span class="dim">.</span><span class="t">__BASE__</span>
            <span class="rule"></span><span class="lab">your domain</span></span>
        </div>
      </div></div>
    </div>

    <div class="card get">
      <span class="pill">access</span>
      <h3>The link is useless without you</h3>
      <p>Sandboxes are created refusing public traffic, so their own URLs answer 403
         to the whole internet. Only this proxy holds the token, and it checks your
         session on every single request.</p>
      <div class="vis"><div class="gate">
        <div class="no">anyone → sandbox directly <span class="code">403</span></div>
        <div class="no">signed out → this proxy <span class="code">302 sign in</span></div>
        <div class="yes">you, signed in → your app <span class="code">200</span></div>
      </div></div>
    </div>

    <div class="card get">
      <span class="pill">offline</span>
      <h3>Installs like an app, costs nothing to browse</h3>
      <p>Add it to your phone's home screen and the grid paints instantly. Tiles are
         screenshots, never live frames — browsing your gallery never wakes a
         sandbox or spends a cent.</p>
      <div class="vis"><div class="tiles">
        <span></span><span></span><span></span><span></span><span></span><span></span>
      </div></div>
    </div>

  </div>
</div></section>

<section id="access"><div class="wrap">
  <div class="head">
    <span class="eyebrow">Access model</span>
    <h2>Signing in shows you sites. Running the agent is granted separately.</h2>
    <p>The agent spends real model tokens and real compute on every message, so
       viewing and driving are two different grants. Nobody can hand out admin.</p>
  </div>
  <div class="roles"><table>
    <thead><tr>
      <th>You are</th><th>Open previews</th><th>Run the agent</th><th>Manage people</th>
    </tr></thead>
    <tbody>
      <tr><td class="who">preview</td><td class="tick">Yes</td>
          <td class="no">No</td><td class="no">No</td></tr>
      <tr><td class="who">agent</td><td class="tick">Yes</td>
          <td class="tick">Yes</td><td class="no">No</td></tr>
      <tr><td class="who">admin</td><td class="tick">Yes</td>
          <td class="tick">Yes</td><td class="tick">Yes</td></tr>
      <tr><td class="who">not invited</td><td class="no">No — ask an admin to add you</td>
          <td class="no">No</td><td class="no">No</td></tr>
    </tbody>
  </table></div>
</div></section>

<section class="close"><div class="wrap">
  <h2>Ready when you are.</h2>
  <p class="lead" style="margin:16px auto 0">Sign in with the Google account whose
     address your admin added.</p>
  <div class="cta"><a class="btn go" href="/__auth/login">Sign in with Google</a></div>
</div></section>

<footer><div class="wrap">
  <span>Preview proxy for the sandboxed coding agent.</span>
  <span class="mono">__BASE__</span>
</div></footer>

</body></html>
"""
)
