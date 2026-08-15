#!/usr/bin/env python3
"""Render docs/SRS.md into a standalone HTML page for publishing.

docs/SRS.md stays the single source of truth. Re-run this after editing it,
then republish the artifact from the same file path to keep the URL stable.

    python3 scripts/build_srs_page.py

No third-party dependencies: the Markdown subset used by the SRS (headings,
paragraphs, lists, tables, fenced code, rules, and inline emphasis/code/links)
is converted directly.
"""

from __future__ import annotations

import html
import re
import sys
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "docs" / "SRS.md"
OUTPUT = ROOT / "docs" / "srs.html"


# --------------------------------------------------------------------------
# Slugs — match GitHub's algorithm so the in-document anchor links resolve.
# --------------------------------------------------------------------------

def slugify(text: str) -> str:
    text = unicodedata.normalize("NFKD", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = text.lower()
    text = re.sub(r"[^\w\s-]", "", text)
    # One hyphen per whitespace character, not per run — GitHub does not collapse,
    # so "M0 — Decide" (space, stripped dash, space) anchors as "m0--decide".
    return re.sub(r"\s", "-", text.strip())


# --------------------------------------------------------------------------
# Inline formatting
# --------------------------------------------------------------------------

CODE_TOKEN = "\x00CODE{}\x00"


def inline(text: str) -> str:
    """Escape, then apply code spans, links, bold, and italics."""
    spans: list[str] = []

    def stash(match: re.Match[str]) -> str:
        spans.append(html.escape(match.group(1)))
        return CODE_TOKEN.format(len(spans) - 1)

    text = re.sub(r"`([^`]+)`", stash, text)
    text = html.escape(text)

    text = re.sub(
        r"\[([^\]]+)\]\(([^)\s]+)\)",
        lambda m: f'<a href="{m.group(2)}">{m.group(1)}</a>',
        text,
    )
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"(?<![\*\w])\*(?!\s)([^*]+?)(?<!\s)\*(?![\*\w])", r"<em>\1</em>", text)

    for index, span in enumerate(spans):
        text = text.replace(CODE_TOKEN.format(index), f"<code>{span}</code>")
    return text


# --------------------------------------------------------------------------
# Block parsing
# --------------------------------------------------------------------------

TABLE_DIVIDER = re.compile(r"^\|[\s:|-]+\|$")


def split_row(line: str) -> list[str]:
    cells = line.strip().split("|")
    if cells and not cells[0].strip():
        cells = cells[1:]
    if cells and not cells[-1].strip():
        cells = cells[:-1]
    return [cell.strip() for cell in cells]


def convert(markdown: str) -> tuple[str, list[tuple[int, str, str]]]:
    lines = markdown.split("\n")
    out: list[str] = []
    headings: list[tuple[int, str, str]] = []
    i = 0
    n = len(lines)

    while i < n:
        line = lines[i]
        stripped = line.strip()

        if not stripped:
            i += 1
            continue

        # Fenced code
        if stripped.startswith("```"):
            language = stripped[3:].strip()
            i += 1
            body: list[str] = []
            while i < n and not lines[i].strip().startswith("```"):
                body.append(lines[i])
                i += 1
            i += 1
            klass = f' class="lang-{html.escape(language)}"' if language else ""
            out.append(
                f'<div class="codewrap"><pre><code{klass}>'
                + html.escape("\n".join(body))
                + "</code></pre></div>"
            )
            continue

        # Horizontal rule
        if re.fullmatch(r"-{3,}", stripped):
            out.append('<hr />')
            i += 1
            continue

        # Heading
        heading = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if heading:
            level = len(heading.group(1))
            raw = heading.group(2).strip()
            slug = slugify(raw)
            if level <= 3:
                headings.append((level, slug, re.sub(r"[*`]", "", raw)))
            out.append(f'<h{level} id="{slug}">{inline(raw)}</h{level}>')
            i += 1
            continue

        # Table
        if stripped.startswith("|") and i + 1 < n and TABLE_DIVIDER.match(lines[i + 1].strip()):
            header = split_row(stripped)
            i += 2
            rows: list[list[str]] = []
            while i < n and lines[i].strip().startswith("|"):
                rows.append(split_row(lines[i].strip()))
                i += 1
            head = "".join(f"<th>{inline(cell)}</th>" for cell in header)
            body_html: list[str] = []
            for row in rows:
                row = row + [""] * (len(header) - len(row))
                body_html.append(
                    "<tr>" + "".join(f"<td>{inline(cell)}</td>" for cell in row) + "</tr>"
                )
            out.append(
                '<div class="tablewrap"><table><thead><tr>'
                + head
                + "</tr></thead><tbody>"
                + "".join(body_html)
                + "</tbody></table></div>"
            )
            continue

        # Blockquote — used for callouts
        if stripped.startswith(">"):
            quoted: list[str] = []
            while i < n and lines[i].strip().startswith(">"):
                quoted.append(re.sub(r"^\s*>\s?", "", lines[i]))
                i += 1
            inner, _ = convert("\n".join(quoted))
            out.append(f"<blockquote>{inner}</blockquote>")
            continue

        # Lists
        list_match = re.match(r"^(\s*)([-*]|\d+\.)\s+(.*)$", line)
        if list_match:
            ordered = bool(re.match(r"^\d+\.$", list_match.group(2)))
            items: list[str] = []
            while i < n:
                current = re.match(r"^(\s*)([-*]|\d+\.)\s+(.*)$", lines[i])
                if not current:
                    if lines[i].strip() and lines[i].startswith("  "):
                        items[-1] += " " + inline(lines[i].strip())
                        i += 1
                        continue
                    break
                if bool(re.match(r"^\d+\.$", current.group(2))) != ordered:
                    break
                items.append(inline(current.group(3)))
                i += 1
            tag = "ol" if ordered else "ul"
            out.append(f"<{tag}>" + "".join(f"<li>{item}</li>" for item in items) + f"</{tag}>")
            continue

        # Paragraph
        buffer = [stripped]
        i += 1
        while i < n and lines[i].strip() and not re.match(
            r"^\s*(#{1,6}\s|[-*]\s|\d+\.\s|\||>|```|-{3,}$)", lines[i]
        ):
            buffer.append(lines[i].strip())
            i += 1
        out.append(f"<p>{inline(' '.join(buffer))}</p>")

    return "\n".join(out), headings


# --------------------------------------------------------------------------
# Semantic decoration — turn spec vocabulary into visual state
# --------------------------------------------------------------------------

PRIORITY = {"M": "must", "S": "should", "C": "could", "W": "wont"}


def decorate(markup: str) -> str:
    markup = re.sub(
        r"<td>([MSCW])</td>",
        lambda m: f'<td><span class="pri pri-{PRIORITY[m.group(1)]}">{m.group(1)}</span></td>',
        markup,
    )
    markup = re.sub(
        r"<td>(v[123])</td>",
        r'<td><span class="ms">\1</span></td>',
        markup,
    )
    markup = re.sub(
        r"<td>([A-Z]{1,4}(?:-[A-Z]{2,4})?-\d+)</td>",
        r'<td><span class="rid">\1</span></td>',
        markup,
    )
    return markup


def strip_inline_toc(markdown: str) -> str:
    """Drop the in-body contents list; the page has a persistent rail instead."""
    return re.sub(
        r"^## Table of contents\n.*?(?=^---$)",
        "",
        markdown,
        flags=re.S | re.M,
    )


# --------------------------------------------------------------------------
# Page shell
# --------------------------------------------------------------------------

CSS = """
:root {
  color-scheme: light dark;

  --paper:       #F2F5F4;
  --surface:     #FFFFFF;
  --surface-2:   #EAEFED;
  --ink:         #101917;
  --ink-2:       #46544F;
  --ink-3:       #75847E;
  --rule:        #D9E2DD;
  --rule-strong: #BCC9C3;

  --accent:      #0A6B57;
  --accent-ink:  #075042;
  --accent-soft: #E0EEE9;

  --amber:       #8F5605;
  --amber-soft:  #F8EEDC;
  --red:         #A02A21;
  --red-soft:    #F8E6E3;

  --sans: ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
  --mono: ui-monospace, SFMono-Regular, "SF Mono", "JetBrains Mono", Menlo, Consolas, monospace;

  --measure: 74ch;
  --rail: 16.5rem;
}

@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --paper:       #0D1413;
    --surface:     #141F1C;
    --surface-2:   #1A2724;
    --ink:         #E6EDE9;
    --ink-2:       #A5B4AD;
    --ink-3:       #7A8983;
    --rule:        #23312C;
    --rule-strong: #33443E;

    --accent:      #4FD0AE;
    --accent-ink:  #7BE0C4;
    --accent-soft: #142A24;

    --amber:       #E3AC63;
    --amber-soft:  #2A2013;
    --red:         #F0897C;
    --red-soft:    #2E1815;
  }
}

:root[data-theme="dark"] {
  --paper:       #0D1413;
  --surface:     #141F1C;
  --surface-2:   #1A2724;
  --ink:         #E6EDE9;
  --ink-2:       #A5B4AD;
  --ink-3:       #7A8983;
  --rule:        #23312C;
  --rule-strong: #33443E;

  --accent:      #4FD0AE;
  --accent-ink:  #7BE0C4;
  --accent-soft: #142A24;

  --amber:       #E3AC63;
  --amber-soft:  #2A2013;
  --red:         #F0897C;
  --red-soft:    #2E1815;
}

* { box-sizing: border-box; }

body {
  margin: 0;
  background: var(--paper);
  color: var(--ink);
  font-family: var(--sans);
  font-size: 16px;
  line-height: 1.62;
  -webkit-font-smoothing: antialiased;
}

/* ---------- masthead ---------- */

.masthead {
  border-bottom: 1px solid var(--rule);
  background: var(--surface);
}

.masthead-inner {
  max-width: 78rem;
  margin: 0 auto;
  padding: 1.6rem 1.75rem 1.5rem;
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 0.6rem 1.5rem;
}

.wordmark {
  font-family: var(--mono);
  font-size: 1.02rem;
  letter-spacing: -0.01em;
  color: var(--ink);
  font-weight: 600;
}

.wordmark span { color: var(--accent); }

.masthead-title {
  font-size: 0.95rem;
  color: var(--ink-2);
  margin-right: auto;
}

.meta {
  display: flex;
  gap: 1.1rem;
  font-family: var(--mono);
  font-size: 0.74rem;
  letter-spacing: 0.04em;
  text-transform: uppercase;
  color: var(--ink-3);
}

.meta b {
  color: var(--ink-2);
  font-weight: 600;
}

/* ---------- shell ---------- */

.shell {
  max-width: 78rem;
  margin: 0 auto;
  padding: 0 1.75rem 6rem;
  display: grid;
  grid-template-columns: var(--rail) minmax(0, 1fr);
  gap: 3.25rem;
  align-items: start;
}

/* ---------- section rail ---------- */

.rail {
  position: sticky;
  top: 1.75rem;
  max-height: calc(100vh - 3.5rem);
  overflow-y: auto;
  padding: 2.25rem 0 2rem;
  font-size: 0.855rem;
}

.rail-label {
  font-family: var(--mono);
  font-size: 0.68rem;
  letter-spacing: 0.12em;
  text-transform: uppercase;
  color: var(--ink-3);
  padding-bottom: 0.7rem;
  border-bottom: 1px solid var(--rule);
  margin-bottom: 0.8rem;
}

.rail a {
  display: block;
  padding: 0.26rem 0 0.26rem 0.85rem;
  border-left: 2px solid transparent;
  color: var(--ink-2);
  text-decoration: none;
  line-height: 1.4;
}

.rail a:hover {
  color: var(--accent-ink);
  border-left-color: var(--rule-strong);
}

.rail a.lvl3 {
  padding-left: 1.7rem;
  font-size: 0.79rem;
  color: var(--ink-3);
}

.rail a.active {
  color: var(--accent-ink);
  border-left-color: var(--accent);
  font-weight: 600;
}

/* ---------- content ---------- */

.doc {
  padding: 2.5rem 0 0;
  min-width: 0;
}

.doc > * { max-width: var(--measure); }
.doc > .tablewrap,
.doc > .codewrap { max-width: none; }

h1, h2, h3, h4 {
  text-wrap: balance;
  line-height: 1.22;
  margin: 0;
}

h1 {
  font-size: clamp(1.95rem, 1.4rem + 1.7vw, 2.65rem);
  letter-spacing: -0.026em;
  font-weight: 680;
  margin-bottom: 0.9rem;
}

h2 {
  font-size: 1.42rem;
  letter-spacing: -0.017em;
  font-weight: 650;
  margin-top: 3.9rem;
  padding-top: 1.4rem;
  border-top: 1px solid var(--rule);
  position: relative;
}

h2::before {
  content: "";
  position: absolute;
  top: -1px;
  left: 0;
  width: 2.6rem;
  height: 2px;
  background: var(--accent);
}

h3 {
  font-size: 1.06rem;
  letter-spacing: -0.008em;
  font-weight: 650;
  margin-top: 2.5rem;
  color: var(--ink);
}

h4 {
  font-size: 0.93rem;
  font-weight: 650;
  margin-top: 1.9rem;
  color: var(--ink-2);
}

p { margin: 1rem 0; }

.doc > h1 + p {
  font-size: 1.03rem;
  color: var(--ink-2);
}

a {
  color: var(--accent-ink);
  text-decoration-color: color-mix(in srgb, var(--accent) 45%, transparent);
  text-underline-offset: 0.18em;
}

a:hover { text-decoration-color: var(--accent); }

:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 3px;
  border-radius: 2px;
}

strong { font-weight: 650; color: var(--ink); }

ul, ol {
  margin: 1rem 0;
  padding-left: 1.35rem;
  display: flex;
  flex-direction: column;
  gap: 0.42rem;
}

li { padding-left: 0.15rem; }
li::marker { color: var(--ink-3); }

hr {
  border: 0;
  border-top: 1px solid var(--rule);
  margin: 2.6rem 0;
  max-width: var(--measure);
}

h2 + hr, h1 + hr { display: none; }

blockquote {
  margin: 1.6rem 0;
  padding: 0.95rem 1.2rem;
  border-left: 2px solid var(--accent);
  background: var(--accent-soft);
  border-radius: 0 5px 5px 0;
  color: var(--ink-2);
  font-size: 0.93rem;
}

blockquote > :first-child { margin-top: 0; }
blockquote > :last-child { margin-bottom: 0; }
blockquote strong { color: var(--ink); }

/* ---------- code ---------- */

code {
  font-family: var(--mono);
  font-size: 0.845em;
  background: var(--surface-2);
  border-radius: 3px;
  padding: 0.12em 0.36em;
  color: var(--ink);
}

.codewrap {
  margin: 1.5rem 0;
  overflow-x: auto;
  background: var(--surface);
  border: 1px solid var(--rule);
  border-radius: 6px;
}

pre {
  margin: 0;
  padding: 1.1rem 1.25rem;
  min-width: min-content;
}

pre code {
  background: none;
  padding: 0;
  font-size: 0.795rem;
  line-height: 1.62;
  color: var(--ink-2);
  white-space: pre;
}

/* ---------- tables ---------- */

.tablewrap {
  margin: 1.6rem 0;
  overflow-x: auto;
  border: 1px solid var(--rule);
  border-radius: 6px;
  background: var(--surface);
}

table {
  border-collapse: collapse;
  width: 100%;
  font-size: 0.855rem;
  font-variant-numeric: tabular-nums;
}

th {
  text-align: left;
  font-family: var(--mono);
  font-size: 0.68rem;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  font-weight: 600;
  color: var(--ink-3);
  padding: 0.72rem 0.95rem;
  border-bottom: 1px solid var(--rule-strong);
  white-space: nowrap;
  background: var(--surface-2);
}

td {
  padding: 0.68rem 0.95rem;
  border-bottom: 1px solid var(--rule);
  vertical-align: top;
  line-height: 1.5;
  color: var(--ink-2);
}

tbody tr:last-child td { border-bottom: 0; }
td strong { color: var(--ink); }

/* ---------- spec vocabulary ---------- */

.rid, .ms, .pri {
  font-family: var(--mono);
  font-size: 0.735rem;
  white-space: nowrap;
}

.rid {
  color: var(--ink);
  font-weight: 600;
  letter-spacing: -0.01em;
}

.ms {
  display: inline-block;
  padding: 0.1rem 0.4rem;
  border-radius: 3px;
  border: 1px solid var(--rule-strong);
  color: var(--ink-2);
}

.pri {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 1.4rem;
  height: 1.4rem;
  border-radius: 3px;
  font-weight: 700;
}

.pri-must   { background: var(--accent-soft); color: var(--accent-ink); }
.pri-should { background: var(--surface-2);   color: var(--ink-2); }
.pri-could  { background: var(--amber-soft);  color: var(--amber); }
.pri-wont   { background: var(--red-soft);    color: var(--red); }

/* ---------- responsive ---------- */

@media (max-width: 62rem) {
  .shell {
    grid-template-columns: minmax(0, 1fr);
    gap: 0;
    padding: 0 1.25rem 4rem;
  }
  .rail {
    position: static;
    max-height: none;
    overflow: visible;
    padding: 1.75rem 0 0;
    border-bottom: 1px solid var(--rule);
  }
  .rail-nav {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(11rem, 1fr));
    gap: 0 1rem;
    padding-bottom: 1.25rem;
  }
  .rail a.lvl3 { display: none; }
  .doc { padding-top: 2rem; }
  .masthead-inner { padding: 1.25rem; }
}

@media (prefers-reduced-motion: reduce) {
  * { scroll-behavior: auto !important; }
}

html { scroll-behavior: smooth; }
h1, h2, h3 { scroll-margin-top: 1.5rem; }
"""

JS = """
const links = Array.from(document.querySelectorAll('.rail a'));
const byId = new Map(links.map(a => [a.getAttribute('href').slice(1), a]));
const targets = links
  .map(a => document.getElementById(a.getAttribute('href').slice(1)))
  .filter(Boolean);

let current = null;
function setActive(id) {
  if (id === current) return;
  current = id;
  links.forEach(a => a.classList.remove('active'));
  const link = byId.get(id);
  if (link) link.classList.add('active');
}

const observer = new IntersectionObserver((entries) => {
  const visible = entries
    .filter(e => e.isIntersecting)
    .sort((a, b) => a.boundingClientRect.top - b.boundingClientRect.top);
  if (visible.length) {
    setActive(visible[0].target.id);
    return;
  }
  const above = targets.filter(t => t.getBoundingClientRect().top < 120);
  if (above.length) setActive(above[above.length - 1].id);
}, { rootMargin: '-10% 0px -70% 0px', threshold: 0 });

targets.forEach(t => observer.observe(t));
"""


def build() -> None:
    if not SOURCE.exists():
        sys.exit(f"Source not found: {SOURCE}")

    markdown = strip_inline_toc(SOURCE.read_text(encoding="utf-8"))

    def front_matter(field: str, default: str = "") -> str:
        match = re.search(rf"^\*\*{field}:\*\*\s*(.+)$", markdown, re.M)
        return html.escape(match.group(1).strip()) if match else default

    version = front_matter("Version", "—")
    date = front_matter("Date")
    status = front_matter("Status")

    body, headings = convert(markdown)
    body = decorate(body)

    nav = "".join(
        f'<a class="lvl{level}" href="#{slug}">{html.escape(title)}</a>'
        for level, slug, title in headings
        if level in (2, 3)
    )

    page = f"""<title>droid-assistant SRS</title>
<meta name="viewport" content="width=device-width, initial-scale=1" />
<style>{CSS}</style>

<header class="masthead">
  <div class="masthead-inner">
    <div class="wordmark">droid<span>-</span>assistant</div>
    <div class="masthead-title">Software Requirements Specification</div>
    <div class="meta">
      <span><b>v{version}</b></span>
      <span>{date}</span>
      <span><b>{status}</b></span>
    </div>
  </div>
</header>

<div class="shell">
  <nav class="rail" aria-label="Sections">
    <div class="rail-label">Contents</div>
    <div class="rail-nav">{nav}</div>
  </nav>
  <main class="doc">
{body}
  </main>
</div>

<script>{JS}</script>
"""

    OUTPUT.write_text(page, encoding="utf-8")
    print(f"Wrote {OUTPUT.relative_to(ROOT)}  ({len(page):,} bytes, {len(headings)} headings)")


if __name__ == "__main__":
    build()
