"""
The design system every page here is built from: tokens, type, and the handful of
components (buttons, fields, pills) that appear on more than one page.

It lives apart from pages.py so that a colour or a control can be changed in ONE
place and land on the sign-in screen, the landing page, the gallery and the admin
panel at once — they are one product and used to drift apart.

Two ideas run through it, and the rest is consequence:

  * **State is the palette.** A preview site is either running, suspended, or
    gone, and that is the only status a visitor ever needs. So the accents are
    not brand decoration — `--live` (warm amber) means a sandbox is up and
    billing, `--frozen` (cold blue) means it is paused with its memory intact,
    `--gone` (grey) means the session ended. Warm-for-running / cold-for-frozen
    is the inversion worth remembering: the thing that costs money glows.
  * **The hostname is the interface.** Routing here is `<sandboxId>-<port>` in
    the Host header, so a hostname is the one identifier a user actually handles.
    Mono type is therefore a first-class voice — eyebrows, hostnames, state
    labels — not just a wrapper for code samples.

Everything is `.replace()`-substituted into page templates, never f-string
formatted: these strings are almost entirely CSS braces.
"""

# Webfonts. A CDN failure degrades to the fallback stacks below and nothing
# breaks — which is why the gallery, an installable PWA, can afford them.
FONTS_HEAD = (
    '<link rel="preconnect" href="https://fonts.googleapis.com">'
    '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
    '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?'
    "family=Familjen+Grotesk:wght@500;600;700&"
    "family=Public+Sans:wght@400;500;600&"
    "family=IBM+Plex+Mono:wght@400;500;600&display=swap"
    '">'
)

# --------------------------------------------------------------------------
# Tokens + the shared component layer. Included by every page in this package.
# --------------------------------------------------------------------------
BASE_CSS = """
:root{
  /* ground */
  --ground:#080A0E; --panel:#0F1218; --raise:#151A23; --sunk:#0B0E13;
  --line:#1E2531; --line2:#2C3548;
  /* type */
  --text:#E9EDF4; --muted:#98A2B4; --faint:#5E6879;
  /* state — see the module docstring; these mean something */
  --live:#FFC24B; --live-deep:#8A5E0B; --live-dim:#3A2A08;
  --frozen:#7FD8F5; --frozen-deep:#215C74; --frozen-dim:#0C242E;
  --gone:#6E7787;
  --bad:#FF7A6B;
  /* shape */
  --r-sm:8px; --r:12px; --r-lg:18px;
  --display:"Familjen Grotesk",ui-sans-serif,system-ui,-apple-system,sans-serif;
  --body:"Public Sans",ui-sans-serif,system-ui,-apple-system,sans-serif;
  --mono:"IBM Plex Mono",ui-monospace,SFMono-Regular,Menlo,monospace;
}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{
  margin:0;background:var(--ground);color:var(--text);
  font:400 16px/1.6 var(--body);
  -webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility;
  -webkit-tap-highlight-color:transparent;overscroll-behavior-y:contain;
}
h1,h2,h3{font-family:var(--display);font-weight:600;letter-spacing:-.02em;margin:0;line-height:1.1}
p{margin:0}
a{color:inherit}
img{max-width:100%}
::selection{background:var(--live);color:#141007}

/* Mono voice: eyebrows, hostnames, state labels. */
.mono{font-family:var(--mono);font-variant-ligatures:none}
.eyebrow{
  font-family:var(--mono);font-size:11.5px;font-weight:500;letter-spacing:.14em;
  text-transform:uppercase;color:var(--faint);
}

/* --- state pills --------------------------------------------------------- */
.pill{
  display:inline-flex;align-items:center;gap:6px;flex:0 0 auto;
  font-family:var(--mono);font-size:11px;font-weight:500;letter-spacing:.1em;
  text-transform:uppercase;padding:3px 9px;border-radius:999px;
  border:1px solid var(--line2);color:var(--muted);background:var(--sunk);
  white-space:nowrap;
}
.pill::before{content:"";width:6px;height:6px;border-radius:50%;background:currentColor;flex:0 0 auto}
.pill.live{color:var(--live);border-color:var(--live-deep);background:var(--live-dim)}
.pill.live::before{box-shadow:0 0 0 3px rgba(255,194,75,.16)}
.pill.frozen{color:var(--frozen);border-color:var(--frozen-deep);background:var(--frozen-dim)}
.pill.gone{color:var(--gone)}

/* --- buttons ------------------------------------------------------------- */
.btn{
  font:500 14px/1 var(--body);
  display:inline-flex;align-items:center;justify-content:center;gap:8px;
  min-height:42px;padding:0 16px;border-radius:var(--r-sm);
  border:1px solid var(--line2);background:var(--raise);color:var(--text);
  cursor:pointer;text-decoration:none;touch-action:manipulation;user-select:none;
  white-space:nowrap;
  transition:background .15s,border-color .15s,color .15s,transform .08s;
}
.btn:active{transform:translateY(1px)}
.btn:disabled{opacity:.45;cursor:default;transform:none}
.btn.go{
  background:var(--live);border-color:var(--live);color:#171204;font-weight:600;
  box-shadow:0 1px 0 rgba(255,255,255,.22) inset;
}
.btn.ghost{background:transparent;border-color:transparent;color:var(--muted)}
.btn.sm{min-height:34px;padding:0 11px;font-size:13px}
/* Destructive controls sit quiet until you reach for them — a grid of red trash
   icons reads as an error state, which is exactly what it is not. */
.btn.danger{color:var(--faint)}
@media (hover:hover){
  .btn:hover{border-color:#3B465C;background:#1A2029}
  .btn.go:hover{background:#FFCE6C;border-color:#FFCE6C}
  .btn.ghost:hover{background:var(--raise);border-color:var(--line)}
  .btn.danger:hover{border-color:var(--bad);background:rgba(255,122,107,.08)}
}
@media (max-width:560px){ .btn{min-height:44px} }

/* --- fields -------------------------------------------------------------- */
.field{
  font:400 14px/1 var(--body);min-height:42px;padding:0 12px;
  border-radius:var(--r-sm);border:1px solid var(--line);
  background:var(--sunk);color:var(--text);
}
.field::placeholder{color:var(--faint)}
select.field{
  appearance:none;padding-right:30px;cursor:pointer;
  background-image:linear-gradient(45deg,transparent 50%,var(--faint) 50%),
                   linear-gradient(135deg,var(--faint) 50%,transparent 50%);
  background-position:calc(100% - 15px) 19px,calc(100% - 10px) 19px;
  background-size:5px 5px,5px 5px;background-repeat:no-repeat;
}

/* Focus is never removed, only restyled — this is the whole keyboard story. */
:focus-visible{outline:2px solid var(--live);outline-offset:2px;border-radius:4px}

/* --- surfaces ------------------------------------------------------------ */
.card{background:var(--panel);border:1px solid var(--line);border-radius:var(--r)}
.hair{height:1px;background:var(--line);border:0;margin:0}

/* Honour the OS switch everywhere: motion here is polish, never information. */
@media (prefers-reduced-motion:reduce){
  *,*::before,*::after{
    animation-duration:.001ms !important;animation-iteration-count:1 !important;
    transition-duration:.001ms !important;scroll-behavior:auto !important;
  }
}
"""


def head(title: str, extra_css: str = "", pwa: str = "") -> str:
    """The `<head>` every page in this package shares.

    `pwa` is pwa.head_tags() where a page should be installable, and empty where
    it should not — the sign-in screen and landing page pass it, the admin panel
    does not.
    """
    return (
        '<meta charset="utf-8">'
        f"<title>{title}</title>"
        '<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">'
        f"{pwa}{FONTS_HEAD}"
        f"<style>{BASE_CSS}{extra_css}</style>"
    )
