# AI Directory Submission Patterns

Most AI directories follow a small number of patterns. Knowing them lets the
skill move quickly and skip dead ends.

## Where the "Submit" link lives

In order of likelihood:

1. **Header navigation** — link or button labeled `Submit`, `Submit Tool`,
   `Submit AI`, `Add Tool`, `List Your Tool`, `+ Add`, or a plus icon.
2. **Footer** — under a "Resources", "For Creators", or "Tools" column.
3. **Direct URL path** — try these before scraping the homepage:
   `/submit`, `/submit-tool`, `/submit-your-ai-tool`, `/submit-ai-tool`,
   `/add`, `/add-tool`, `/list-your-tool`, `/new`, `/post`.

`scripts/find_submit_link.sh <homepage>` automates this: it probes the common
paths via `curl` first (fast), then falls back to scanning the homepage with
`agent-browser` for the labels above.

## Typical form fields

Almost every form asks for some subset of:

| Field | Common labels |
|---|---|
| Name | `Tool name`, `Product name`, `Title`, `Name` |
| URL  | `Website`, `URL`, `Link`, `Tool URL` |
| Tagline / short description | `Tagline`, `Short description`, `One-liner`, `Pitch` (often 60–160 chars) |
| Long description | `Description`, `About`, `Details` (often 200–1000 chars) |
| Category | `Category`, `Tag`, `Type` (select or multi-select) |
| Pricing | `Pricing`, `Pricing model` — usually one of: Free, Freemium, Paid, Free trial, Contact |
| Logo | `Logo`, `Icon`, `Image` — file upload or URL |
| Screenshot | `Screenshot`, `Cover image` |
| Email | `Email`, `Your email`, `Contact email` |
| Social | `Twitter`, `X`, `LinkedIn`, `GitHub` |
| Captcha | reCAPTCHA, hCaptcha, Cloudflare Turnstile |

Map the user's `submission-info.json` to these fields by best-match. Re-read
the snapshot after toggling `Pricing` / `Category` controls because the DOM
often grows.

## Embedded forms (Tally, Typeform, Google Forms)

Many directories embed third-party form services in an `<iframe>`. The default
`agent-browser snapshot` does **not** traverse iframes, so the snapshot will
look empty (just nav links) even though a form is visible to the user.

Detect this case after the initial snapshot:

```bash
agent-browser --session submitdir eval \
  'Array.from(document.querySelectorAll("iframe")).map(f => f.src).filter(Boolean)'
```

If the result contains a URL on one of these hosts, the form is embedded:

- `tally.so/embed/...`
- `*.typeform.com/...`
- `docs.google.com/forms/...`
- `airtable.com/embed/...`
- `*.fillout.com/...`
- `*.paperform.co/...`

Open the iframe `src` directly:

```bash
agent-browser --session submitdir open "$IFRAME_SRC"
agent-browser --session submitdir wait --load networkidle
agent-browser --session submitdir snapshot -i
```

The bare form will then be in the top-level document and the snapshot will
show all fields normally. Verified end-to-end with aitoolslist.io → Tally:
seven fields (Your Name, Your Email, Tool Name, Tool's Link, Tool category,
Short Description, Additional Notes) appeared cleanly after switching to the
embed URL.

## What to skip (no point trying)

Mark these as `skipped` with a reason rather than wasting a browser session:

- **Pay-to-list** — anything that requires payment up-front. Common signals:
  Stripe checkout on the submit page, "$X to list", "Lifetime listing $X",
  "Pro submission". Free-tier form usually exists too, but check carefully.
- **GitHub-PR-only** — `awesome-*` lists on `github.com`. The fetcher already
  drops `github.com/...` URLs, but watch for in-page "Submit a PR" links.
- **Auth-walled with OAuth** — "Sign in with X" required before form. If the
  user hasn't provided a session/state file for that provider, skip.
- **Email-only** — "Email us your tool at ...". Note the email in the report;
  don't try to automate sending.
- **Dead / parked / 404** — `find_submit_link.sh` exits non-zero. Skip.
- **Captcha required** — agent-browser can't solve image/recaptcha v2
  challenges. Note and skip (or mark for manual follow-up).

## Confirmation signals

After clicking submit, look for any of:

- URL change to `/thank-you`, `/success`, `/submitted`, `/pending`, `/confirm`.
- Visible text containing `thank`, `received`, `submitted`, `review`,
  `approved`, `pending`.
- A toast / alert role element.
- The form fields disappearing or being replaced.

Use `agent-browser diff snapshot` after submit to see what actually changed.
If none of the above appear within ~10s of network idle, treat as `unknown`
and capture a screenshot for manual review.

## Lessons from real runs (read these)

### Refs invalidate when the DOM changes

After clicking a combobox option, expanding a section, or any interaction that
adds/removes elements, the ref numbers shift. A subsequent `fill @e60 "..."`
may now match 7 elements (the wrapped rich-text editor's inner nodes) and
silently refuse to commit. **Always re-snapshot after a click that changes the
DOM.** The pattern:

```bash
ab click @e8                  # open dropdown
ab snapshot -i                # refs may have shifted, get fresh ones
ab click @e15                 # pick option using NEW ref
ab snapshot -i                # again, because picking an option changed the DOM
ab fill @e60 "$DESCRIPTION"
```

### Rich-text editors (ProseMirror / TipTap / TinyMCE)

If `fill @ref "..."` errors with "matched N elements" on a description field
that looks like a textarea, it's almost certainly a wrapped rich-text editor.
The visible ref points to a wrapper whose internal contenteditable + toolbar
nodes also match the textbox role. Two reliable workarounds:

```bash
# Option 1 — click into the editor first, then type
ab click @eEDITOR
ab keyboard inserttext "$LONG_DESCRIPTION"

# Option 2 — directly set the contenteditable via eval
ab eval "document.querySelector('[contenteditable=true]').innerText = $(jq -n --arg v \"$LONG_DESCRIPTION\" '$v')"
```

### "List Your X" / "Register" links are submit-adjacent

`find_submit_link.sh` looks for "Submit", "Add Tool", "List Your Tool", etc.
Some directories use longer phrasings like **"List Your AI Agent"**, **"List
your business"**, or simply **"Register"** that take you to a sign-up form
before any submission. Treat these as login-required and ask the user to
sign up if they want to be listed there.

### Account-signup-before-submit

Some directories (e.g. AI Agent Store at aiagentstore.ai/register) require an
account before you can submit. The page shows email/password fields and a
"Sign up with Google" button — NOT a tool-listing form. This is a login wall
even if the URL is `/register` rather than `/login`.

### Off-topic directories

Some directories specialize. AI Agents Live only accepts AI agents / platforms,
not image-generation tools. The page will say something like "We only accept
X listings." Honour these — submitting off-topic gets the listing rejected
*and* may flag the account.

### Logo upload is sometimes required

Forms that include a `<input type=file>` for a logo often reject the
submission without it. Submission-info.json should include `logo_path` (a
real local file). If absent, classify these as `failed: logo required` so
they show up in the report for the user to handle manually.

### Slow pages can wedge the run

Without an explicit timeout, an `agent-browser fill @e1 "..."` waiting for a
slow-rendering field can hang for the default 25s × every miss attempt.
Always set `AGENT_BROWSER_DEFAULT_TIMEOUT=12000` (12s) when running unattended.
`run_batch.sh` does this by default.

## Pacing and politeness

- Use one named browser session per run (`--session submitdir`) so cookies and
  bot-detection signals stay consistent.
- Sleep 2–5 seconds between directories to avoid looking like a flood. The
  default `wait --load networkidle` after each submit usually provides this.
- Stop early if 3 directories in a row return Cloudflare / captcha walls —
  the IP is likely being rate-limited.

## Reporting

For each directory record:

- `name`, `url` (homepage), `submit_url` (resolved or null)
- `status`: `submitted` | `skipped` | `failed` | `unknown`
- `reason`: short string (e.g. `"paid-only"`, `"captcha"`, `"login-required"`,
  `"no submit form found"`, `"timeout"`)
- `screenshot`: path to a screenshot of the result (always capture on
  `submitted` and `unknown`)
- `final_url`: where the browser ended up after submit
