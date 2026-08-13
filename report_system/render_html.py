"""리포트 마크다운 → 단일 HTML 파일 (배포용).

의존성 없이 report.generate_markdown 이 생성하는 문법 부분집합만 처리한다:
제목(#/##/###), 표, 목록, 인용, 굵게, 취소선, 코드, 수평선.
라이트·다크 모두에서 읽히도록 CSS 변수로 팔레트를 정의한다.
"""
from __future__ import annotations

import html
import re

_CSS = """
:root{
  --bg:#ffffff; --fg:#1a1a1a; --muted:#5a5a5a; --line:#d8dce2;
  --accent:#1f3864; --thead:#eef1f5; --zebra:#fafbfc; --code:#f4f5f7;
  --warn:#8a1f1f; --ok:#1f5c2e;
}
@media (prefers-color-scheme: dark){
  :root:not([data-theme="light"]){
    --bg:#14171c; --fg:#e8eaed; --muted:#a3aab5; --line:#2d323b;
    --accent:#8fb2e8; --thead:#1e232b; --zebra:#191d23; --code:#1b1f26;
    --warn:#f08a8a; --ok:#7fd39a;
  }
}
:root[data-theme="dark"]{
  --bg:#14171c; --fg:#e8eaed; --muted:#a3aab5; --line:#2d323b;
  --accent:#8fb2e8; --thead:#1e232b; --zebra:#191d23; --code:#1b1f26;
  --warn:#f08a8a; --ok:#7fd39a;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
 font-family:-apple-system,BlinkMacSystemFont,"Malgun Gothic","맑은 고딕",
 "Apple SD Gothic Neo",sans-serif;line-height:1.65;font-size:15px}
.wrap{max-width:1000px;margin:0 auto;padding:40px 24px 80px}
h1{font-size:1.9rem;color:var(--accent);border-bottom:2px solid var(--accent);
 padding-bottom:.4em;margin:0 0 1em}
h2{font-size:1.3rem;margin:2.2em 0 .7em;padding-bottom:.3em;
 border-bottom:1px solid var(--line)}
h3{font-size:1.08rem;margin:1.6em 0 .5em;color:var(--muted)}
p{margin:.7em 0}
blockquote{margin:1.2em 0;padding:.9em 1.1em;background:var(--zebra);
 border-left:4px solid var(--accent);color:var(--muted)}
.tablewrap{overflow-x:auto;margin:1em 0}
table{border-collapse:collapse;width:100%;font-size:.9rem}
th,td{border:1px solid var(--line);padding:7px 10px;text-align:left;
 vertical-align:top}
th{background:var(--thead);font-weight:600;white-space:nowrap}
tbody tr:nth-child(even){background:var(--zebra)}
code{background:var(--code);padding:1px 5px;border-radius:3px;
 font-family:ui-monospace,Menlo,Consolas,monospace;font-size:.87em}
pre{background:var(--code);padding:14px;border-radius:6px;overflow-x:auto;
 border:1px solid var(--line)}
ul{margin:.6em 0;padding-left:1.4em}
li{margin:.25em 0}
del{color:var(--warn)}
em{color:var(--muted)}
hr{border:0;border-top:1px solid var(--line);margin:2.5em 0}
.meta{color:var(--muted);font-size:.85rem;margin-top:3em;
 border-top:1px solid var(--line);padding-top:1em}

/* 목차 — 11개 절짜리 문서는 목차 없이 읽히지 않는다 */
nav.toc{background:var(--zebra);border:1px solid var(--line);border-radius:6px;
 padding:14px 18px;margin:1.5em 0 2.5em}
nav.toc strong{display:block;font-size:.85rem;color:var(--muted);
 letter-spacing:.04em;margin-bottom:.5em}
nav.toc ol{margin:0;padding-left:1.3em;columns:2;column-gap:28px}
nav.toc li{margin:.2em 0;break-inside:avoid}
nav.toc a{color:var(--fg);text-decoration:none}
nav.toc a:hover{color:var(--accent);text-decoration:underline}
h2,h3{scroll-margin-top:12px}

/* 판정·등급 배지 — 표 셀 전체가 해당 단어일 때만 적용한다 */
.badge{display:inline-block;padding:1px 8px;border-radius:10px;
 font-size:.82em;font-weight:600;white-space:nowrap}
.b-pos{background:rgba(31,92,46,.13);color:var(--ok)}
.b-neg{background:rgba(138,31,31,.13);color:var(--warn)}
.b-neu{background:var(--code);color:var(--muted)}
.b-fact{background:rgba(31,56,100,.12);color:var(--accent)}
.b-fore{background:rgba(138,31,31,.10);color:var(--warn)}
.b-lim{background:var(--code);color:var(--muted)}

@media print{
  @page{margin:14mm}
  body{background:#fff;color:#000;font-size:10.5pt}
  .wrap{max-width:none;padding:0}
  nav.toc{display:none}
  h1{color:#000;border-color:#000}
  h2{break-after:avoid;page-break-after:avoid}
  table{font-size:9pt}
  .tablewrap,table,tr,blockquote{break-inside:avoid;page-break-inside:avoid}
  th{background:#eee !important}
  tbody tr:nth-child(even){background:#f7f7f7 !important}
  a{color:#000;text-decoration:none}
}
"""

#: 셀 전체가 이 단어일 때만 배지로 바꾼다. 부분 일치는 문장 속 단어를
#: 잘못 강조하므로 허용하지 않는다.
_BADGES = {
    "긍정": "b-pos", "부정": "b-neg", "중립": "b-neu",
    "확보 가능": "b-pos", "미확보": "b-neg",
    "조건부": "b-neu", "대체 가능": "b-neu",
    "FACT": "b-fact", "CALCULATION": "b-fact", "INFERENCE": "b-neu",
    "FORECAST": "b-fore", "LIMITATION": "b-lim",
    "사용 가능": "b-pos", "사용 금지": "b-neg", "조건부 사용": "b-neu",
    "방향 유지": "b-pos", "뒤집힘": "b-neg", "강도 하락": "b-neu",
    "A": "b-pos", "D": "b-neg",
}


def _inline(s: str) -> str:
    s = html.escape(s)
    s = re.sub(r"~~(.+?)~~", r"<del>\1</del>", s)
    s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"`([^`]+?)`", r"<code>\1</code>", s)
    s = re.sub(r"(?<![\w*])\*([^*]+?)\*(?![\w*])", r"<em>\1</em>", s)
    return s


def _is_row(l: str) -> bool:
    return l.strip().startswith("|") and l.strip().endswith("|")


def _is_sep(l: str) -> bool:
    return bool(re.fullmatch(r"\s*\|[\s:|-]+\|\s*", l))


def _cells(l: str) -> list[str]:
    return [c.strip() for c in l.strip().strip("|").split("|")]


def _cell_html(text: str) -> str:
    cls = _BADGES.get(text.strip())
    if cls:
        return f"<span class='badge {cls}'>{html.escape(text.strip())}</span>"
    return _inline(text)


def _slug(text: str, used: set[str]) -> str:
    """제목 → 앵커 id. 한글이 그대로 들어가도 되지만 충돌만 피한다."""
    base = re.sub(r"[^0-9A-Za-z가-힣]+", "-", text).strip("-").lower() or "sec"
    slug, n = base, 2
    while slug in used:
        slug, n = f"{base}-{n}", n + 1
    used.add(slug)
    return slug


def markdown_to_html(md: str, title: str = "현장 진단리포트") -> str:
    lines = md.split("\n")
    out: list[str] = []
    toc: list[tuple[str, str]] = []      # (앵커, 제목) — h2 만 목차에 올린다
    used: set[str] = set()
    i = 0
    while i < len(lines):
        l = lines[i]

        if not l.strip():
            i += 1
            continue

        if l.startswith("```"):
            i += 1
            buf = []
            while i < len(lines) and not lines[i].startswith("```"):
                buf.append(html.escape(lines[i]))
                i += 1
            i += 1
            out.append("<pre>" + "\n".join(buf) + "</pre>")
            continue

        if _is_row(l) and i + 1 < len(lines) and _is_sep(lines[i + 1]):
            head = _cells(l)
            i += 2
            body = []
            while i < len(lines) and _is_row(lines[i]):
                body.append(_cells(lines[i]))
                i += 1
            t = ["<div class='tablewrap'><table><thead><tr>"]
            t += [f"<th>{_inline(c)}</th>" for c in head]
            t.append("</tr></thead><tbody>")
            for r in body:
                r = (r + [""] * len(head))[:len(head)]
                t.append("<tr>" + "".join(f"<td>{_cell_html(c)}</td>" for c in r)
                         + "</tr>")
            t.append("</tbody></table></div>")
            out.append("".join(t))
            continue

        if l.startswith("### "):
            sid = _slug(l[4:], used)
            out.append(f"<h3 id='{sid}'>{_inline(l[4:])}</h3>"); i += 1; continue
        if l.startswith("## "):
            sid = _slug(l[3:], used)
            toc.append((sid, l[3:]))
            out.append(f"<h2 id='{sid}'>{_inline(l[3:])}</h2>"); i += 1; continue
        if l.startswith("# "):
            out.append(f"<h1>{_inline(l[2:])}</h1>")
            out.append("@@TOC@@")          # 제목 직후에 목차를 끼운다
            i += 1; continue

        if re.fullmatch(r"\s*---+\s*", l):
            out.append("<hr>"); i += 1; continue

        if l.lstrip().startswith("> "):
            buf = []
            while i < len(lines) and lines[i].lstrip().startswith(">"):
                buf.append(lines[i].lstrip()[1:].strip())
                i += 1
            out.append(f"<blockquote>{_inline(' '.join(x for x in buf if x))}</blockquote>")
            continue

        if re.match(r"\s*[-*]\s+", l):
            items = []
            while i < len(lines) and re.match(r"\s*[-*]\s+", lines[i]):
                items.append(_inline(re.sub(r"^\s*[-*]\s+", "", lines[i])))
                i += 1
            out.append("<ul>" + "".join(f"<li>{x}</li>" for x in items) + "</ul>")
            continue

        out.append(f"<p>{_inline(l)}</p>")
        i += 1

    toc_html = ""
    if len(toc) >= 3:
        toc_html = ("<nav class='toc'><strong>목차</strong><ol>"
                    + "".join(f"<li><a href='#{sid}'>{_inline(t)}</a></li>"
                              for sid, t in toc)
                    + "</ol></nav>")
    body_html = "".join(out)
    if "@@TOC@@" in body_html:
        body_html = body_html.replace("@@TOC@@", toc_html, 1)
    else:
        body_html = toc_html + body_html

    return (
        "<!doctype html><html lang='ko'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{html.escape(title)}</title><style>{_CSS}</style></head>"
        f"<body><div class='wrap'>{body_html}"
        "<p class='meta'>report-system 자동 생성 · 본 문서의 전망은 조건부이며 "
        "가격·수익·계약을 보장하지 않습니다.</p></div></body></html>")
