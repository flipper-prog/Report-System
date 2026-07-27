const fs = require('fs');
const path = require('path');
const {
  Document, Packer, Paragraph, TextRun, HeadingLevel, AlignmentType,
  Table, TableRow, TableCell, WidthType, BorderStyle, ShadingType,
  PageNumber, Header, Footer, LevelFormat, convertInchesToTwip,
  TableOfContents, PageOrientation, VerticalAlign,
} = require('docx');

const SRC = process.argv[2];
const OUT = process.argv[3];

const FONT = '맑은 고딕';
// 다이어그램용: 한글 Windows에 기본 탑재된 고정폭 글꼴.
// 罫線 문자(─│┌┘▼)와 한글·ASCII를 동일 폭으로 렌더링해 정렬이 유지됨.
const FONT_MONO = { ascii: '굴림체', eastAsia: '굴림체', hAnsi: '굴림체', cs: '굴림체' };

// A4 = 11906 x 16838 DXA. Margins 2cm(1134) L/R, 2.2cm top / 2cm bottom
const MARGIN_LR = 1134;
const CONTENT_W = 11906 - MARGIN_LR * 2; // 9638

const C = {
  ink: '1A1A1A',
  sub: '5A5A5A',
  light: '8A8A8A',
  rule: 'BFBFBF',
  headBg: 'E8EAED',
  zebra: 'F7F8F9',
  boxBg: 'F4F5F7',
  accent: '1F3864',
};

// ---------- inline parsing ----------
function inlineRuns(text, base = {}) {
  const runs = [];
  // split on **bold** and *italic*
  const re = /(\*\*[^*]+\*\*|\*[^*]+\*|`[^`]+`)/g;
  let last = 0, m;
  const push = (t, opts) => {
    if (!t) return;
    runs.push(new TextRun({ text: t, font: FONT, size: 20, color: C.ink, ...base, ...opts }));
  };
  while ((m = re.exec(text)) !== null) {
    push(text.slice(last, m.index));
    const tok = m[0];
    if (tok.startsWith('**')) push(tok.slice(2, -2), { bold: true });
    else if (tok.startsWith('`')) push(tok.slice(1, -1), { font: FONT_MONO, size: 18 });
    else push(tok.slice(1, -1), { italics: true, color: C.sub });
    last = m.index + tok.length;
  }
  push(text.slice(last));
  if (runs.length === 0) push(' ');
  return runs;
}

const stripMd = (s) => s.replace(/\*\*/g, '').replace(/(^|\s)\*([^*]+)\*/g, '$1$2').replace(/`/g, '');

// ---------- width calc ----------
function textWidth(s) {
  let w = 0;
  for (const ch of stripMd(s)) w += /[ᄀ-퟿＀-￯]/.test(ch) ? 2 : 1;
  return w;
}

function columnWidths(rows) {
  const n = rows[0].length;
  const maxw = new Array(n).fill(1);
  for (const r of rows) {
    for (let i = 0; i < n; i++) {
      // cap influence of very long cells
      maxw[i] = Math.max(maxw[i], Math.min(textWidth(r[i] || ''), 90));
    }
  }
  const total = maxw.reduce((a, b) => a + b, 0);
  const minW = n <= 2 ? 1800 : n <= 4 ? 1150 : 900;
  let widths = maxw.map((w) => Math.max(minW, Math.round((w / total) * CONTENT_W)));
  // normalize to exactly CONTENT_W
  let sum = widths.reduce((a, b) => a + b, 0);
  let diff = CONTENT_W - sum;
  // distribute diff proportionally to the widest columns
  const order = widths.map((w, i) => [w, i]).sort((a, b) => b[0] - a[0]).map((x) => x[1]);
  let k = 0;
  while (diff !== 0 && k < 10000) {
    const i = order[k % n];
    const step = diff > 0 ? Math.min(diff, 20) : Math.max(diff, -20);
    if (widths[i] + step >= minW) { widths[i] += step; diff -= step; }
    k++;
  }
  return widths;
}

// ---------- builders ----------
const noBorder = { style: BorderStyle.NONE, size: 0, color: 'FFFFFF' };
const thin = (color = C.rule) => ({ style: BorderStyle.SINGLE, size: 4, color });

function makeTable(rows) {
  const widths = columnWidths(rows);
  const trs = rows.map((cells, ri) => {
    const isHead = ri === 0;
    return new TableRow({
      tableHeader: isHead,
      cantSplit: true,
      children: cells.map((cell, ci) => new TableCell({
        width: { size: widths[ci], type: WidthType.DXA },
        margins: { top: 70, bottom: 70, left: 110, right: 110 },
        verticalAlign: VerticalAlign.CENTER,
        shading: isHead
          ? { type: ShadingType.CLEAR, fill: C.headBg, color: 'auto' }
          : (ri % 2 === 0 ? { type: ShadingType.CLEAR, fill: C.zebra, color: 'auto' } : undefined),
        borders: {
          top: isHead ? thin(C.light) : thin(),
          bottom: isHead ? thin(C.light) : thin(),
          left: thin(), right: thin(),
        },
        children: [new Paragraph({
          spacing: { before: 0, after: 0, line: 260 },
          alignment: AlignmentType.LEFT,
          children: inlineRuns(cell, isHead ? { bold: true, size: 18 } : { size: 18 }),
        })],
      })),
    });
  });
  return new Table({
    rows: trs,
    columnWidths: widths,
    width: { size: CONTENT_W, type: WidthType.DXA },
    layout: 'fixed',
  });
}

function makeCodeBox(lines) {
  const paras = lines.map((l) => new Paragraph({
    spacing: { before: 0, after: 0, line: 240 },
    alignment: AlignmentType.LEFT,
    children: [new TextRun({ text: l.length ? l : ' ', font: FONT_MONO, size: 16, color: C.ink })],
  }));
  return new Table({
    columnWidths: [CONTENT_W],
    width: { size: CONTENT_W, type: WidthType.DXA },
    layout: 'fixed',
    rows: [new TableRow({
      children: [new TableCell({
        width: { size: CONTENT_W, type: WidthType.DXA },
        margins: { top: 160, bottom: 160, left: 200, right: 160 },
        shading: { type: ShadingType.CLEAR, fill: C.boxBg, color: 'auto' },
        borders: { top: thin(), bottom: thin(), left: thin(), right: thin() },
        children: paras,
      })],
    })],
  });
}

function makeQuote(text) {
  return new Table({
    columnWidths: [CONTENT_W],
    width: { size: CONTENT_W, type: WidthType.DXA },
    layout: 'fixed',
    rows: [new TableRow({
      children: [new TableCell({
        width: { size: CONTENT_W, type: WidthType.DXA },
        margins: { top: 150, bottom: 150, left: 220, right: 180 },
        shading: { type: ShadingType.CLEAR, fill: 'F0F3F8', color: 'auto' },
        borders: {
          top: noBorder, bottom: noBorder, right: noBorder,
          left: { style: BorderStyle.SINGLE, size: 18, color: C.accent },
        },
        children: [new Paragraph({
          spacing: { before: 0, after: 0, line: 300 },
          children: inlineRuns(text, { size: 19 }),
        })],
      })],
    })],
  });
}

// ---------- parse markdown ----------
const raw = fs.readFileSync(SRC, 'utf8').split(/\r?\n/);
const body = [];
let i = 0;

// skip cover-related front lines (handled separately)
// find index of "## 문서 관리 정보"
while (i < raw.length && !/^## 문서 관리 정보/.test(raw[i])) i++;

const isTableRow = (l) => /^\s*\|.*\|\s*$/.test(l);
const isSep = (l) => /^\s*\|[\s:|-]+\|\s*$/.test(l);
const splitRow = (l) => l.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map((s) => s.trim());

function para(text, opts = {}) {
  const { size = 20, bold = false, italics = false, color = C.ink,
    before = 60, after = 130, line = 310, align = AlignmentType.JUSTIFIED,
    indent, keepNext = false } = opts;
  return new Paragraph({
    spacing: { before, after, line },
    alignment: align,
    keepNext,
    indent,
    children: inlineRuns(text, { size, bold, italics, color }),
  });
}

while (i < raw.length) {
  const line = raw[i];

  // horizontal rule / blank
  if (/^\s*---\s*$/.test(line) || /^\s*$/.test(line)) { i++; continue; }

  // headings
  let m;
  if ((m = line.match(/^#\s+(.*)$/))) {
    const t = stripMd(m[1]);
    const isChapter = /^제\d+장/.test(t);
    body.push(new Paragraph({
      heading: HeadingLevel.HEADING_1,
      pageBreakBefore: isChapter,
      spacing: { before: isChapter ? 0 : 320, after: 240 },
      border: { bottom: { style: BorderStyle.SINGLE, size: 10, color: C.accent, space: 8 } },
      children: [new TextRun({ text: t, font: FONT, size: 30, bold: true, color: C.accent })],
    }));
    i++; continue;
  }
  if ((m = line.match(/^##\s+(.*)$/))) {
    body.push(new Paragraph({
      heading: HeadingLevel.HEADING_2,
      spacing: { before: 300, after: 140 },
      keepNext: true,
      children: [new TextRun({ text: stripMd(m[1]), font: FONT, size: 23, bold: true, color: C.ink })],
    }));
    i++; continue;
  }
  if ((m = line.match(/^###\s+(.*)$/))) {
    body.push(new Paragraph({
      heading: HeadingLevel.HEADING_3,
      spacing: { before: 240, after: 110 },
      keepNext: true,
      children: [new TextRun({ text: stripMd(m[1]), font: FONT, size: 20, bold: true, color: C.sub })],
    }));
    i++; continue;
  }

  // code block
  if (/^\s*```/.test(line)) {
    i++;
    const lines = [];
    while (i < raw.length && !/^\s*```/.test(raw[i])) { lines.push(raw[i]); i++; }
    i++;
    body.push(makeCodeBox(lines));
    body.push(new Paragraph({ spacing: { before: 0, after: 160 }, children: [] }));
    continue;
  }

  // table
  if (isTableRow(line) && i + 1 < raw.length && isSep(raw[i + 1])) {
    const rows = [splitRow(line)];
    i += 2;
    while (i < raw.length && isTableRow(raw[i])) { rows.push(splitRow(raw[i])); i++; }
    const n = rows[0].length;
    const norm = rows.map((r) => {
      const c = r.slice(0, n);
      while (c.length < n) c.push('');
      return c;
    });
    body.push(makeTable(norm));
    body.push(new Paragraph({ spacing: { before: 0, after: 200 }, children: [] }));
    continue;
  }

  // blockquote
  if (/^\s*>\s?/.test(line)) {
    const buf = [];
    while (i < raw.length && /^\s*>\s?/.test(raw[i])) { buf.push(raw[i].replace(/^\s*>\s?/, '')); i++; }
    body.push(makeQuote(buf.filter((s) => s.trim()).join(' ')));
    body.push(new Paragraph({ spacing: { before: 0, after: 180 }, children: [] }));
    continue;
  }

  // bullet list
  if (/^\s*[-*]\s+/.test(line)) {
    while (i < raw.length && /^\s*[-*]\s+/.test(raw[i])) {
      const t = raw[i].replace(/^\s*[-*]\s+/, '');
      body.push(new Paragraph({
        numbering: { reference: 'bullets', level: 0 },
        spacing: { before: 30, after: 30, line: 290 },
        children: inlineRuns(t),
      }));
      i++;
    }
    body.push(new Paragraph({ spacing: { before: 0, after: 100 }, children: [] }));
    continue;
  }

  // ordered list
  if (/^\s*\d+\.\s+/.test(line)) {
    while (i < raw.length && /^\s*\d+\.\s+/.test(raw[i])) {
      const t = raw[i].replace(/^\s*\d+\.\s+/, '');
      body.push(new Paragraph({
        numbering: { reference: 'ordered', level: 0 },
        spacing: { before: 30, after: 30, line: 290 },
        children: inlineRuns(t),
      }));
      i++;
    }
    body.push(new Paragraph({ spacing: { before: 0, after: 100 }, children: [] }));
    continue;
  }

  // caption: **[표 x] ...**  /  **[그림 x] ...**
  if (/^\*\*\[(표|그림|부록)/.test(line)) {
    body.push(new Paragraph({
      spacing: { before: 220, after: 90 },
      keepNext: true,
      children: [new TextRun({ text: stripMd(line), font: FONT, size: 18, bold: true, color: C.accent })],
    }));
    i++; continue;
  }

  // italic-only note line
  if (/^\*[^*].*\*$/.test(line.trim())) {
    body.push(para(line.trim(), { size: 17, italics: true, color: C.sub, before: 20, after: 160, align: AlignmentType.LEFT }));
    i++; continue;
  }

  // normal paragraph
  body.push(para(line));
  i++;
}

// ---------- cover ----------
const gap = (n = 1) => Array.from({ length: n }, () => new Paragraph({ spacing: { before: 0, after: 0, line: 300 }, children: [] }));

const cover = [
  ...gap(4),
  new Paragraph({
    alignment: AlignmentType.LEFT, spacing: { after: 120 },
    children: [new TextRun({ text: '사업 제안서', font: FONT, size: 20, color: C.sub, characterSpacing: 60 })],
  }),
  new Paragraph({
    alignment: AlignmentType.LEFT, spacing: { after: 40 },
    border: { bottom: { style: BorderStyle.SINGLE, size: 14, color: C.accent, space: 14 } },
    children: [new TextRun({ text: ' ', font: FONT, size: 6 })],
  }),
  ...gap(1),
  new Paragraph({
    alignment: AlignmentType.LEFT, spacing: { before: 200, after: 80, line: 420 },
    children: [new TextRun({ text: '분양하마 AI 분양 수요 전환 시스템', font: FONT, size: 46, bold: true, color: C.accent })],
  }),
  new Paragraph({
    alignment: AlignmentType.LEFT, spacing: { after: 300, line: 420 },
    children: [new TextRun({ text: '구축·운영 제안서', font: FONT, size: 46, bold: true, color: C.accent })],
  }),
  new Paragraph({
    alignment: AlignmentType.LEFT, spacing: { after: 60, line: 340 },
    children: [new TextRun({ text: '현장분석 · 판매논리 · 콘텐츠 · 광고 · 고객관리 · 계약 데이터를', font: FONT, size: 22, color: C.sub })],
  }),
  new Paragraph({
    alignment: AlignmentType.LEFT, spacing: { after: 500, line: 340 },
    children: [new TextRun({ text: '하나의 운영체계로', font: FONT, size: 22, color: C.sub })],
  }),
  ...gap(6),
  new Table({
    columnWidths: [2200, 7438],
    width: { size: CONTENT_W, type: WidthType.DXA },
    layout: 'fixed',
    rows: [
      ['제 출 처', '시행사 · 분양대행사 경영진, 개발사업 총괄, 현장 본부장 · 분양 총괄'],
      ['제 출 사', '분양하마 (테이블플립 자회사)'],
      ['문서 등급', '표준 제안서 (Rev. 4.0)'],
      ['작성 기준일', '2026년 7월 26일'],
    ].map(([k, v]) => new TableRow({
      children: [
        new TableCell({
          width: { size: 2200, type: WidthType.DXA },
          margins: { top: 90, bottom: 90, left: 0, right: 120 },
          borders: { top: noBorder, bottom: noBorder, left: noBorder, right: noBorder },
          children: [new Paragraph({ spacing: { before: 0, after: 0 }, children: [new TextRun({ text: k, font: FONT, size: 18, bold: true, color: C.sub })] })],
        }),
        new TableCell({
          width: { size: 7438, type: WidthType.DXA },
          margins: { top: 90, bottom: 90, left: 0, right: 0 },
          borders: { top: noBorder, bottom: noBorder, left: noBorder, right: noBorder },
          children: [new Paragraph({ spacing: { before: 0, after: 0 }, children: [new TextRun({ text: v, font: FONT, size: 18, color: C.ink })] })],
        }),
      ],
    })),
  }),
];

// ---------- document ----------
const doc = new Document({
  creator: '분양하마',
  title: '분양하마 AI 분양 수요 전환 시스템 구축·운영 제안서',
  description: '시행사·분양대행사 대상 사업 제안서',
  styles: {
    default: {
      document: { run: { font: FONT, size: 20, color: C.ink }, paragraph: { spacing: { line: 310 } } },
    },
    paragraphStyles: [{
      id: 'Normal', name: 'Normal', quickFormat: true,
      run: { font: FONT, size: 20, color: C.ink },
      paragraph: { spacing: { line: 310 } },
    }],
  },
  numbering: {
    config: [
      {
        reference: 'bullets',
        levels: [{
          level: 0, format: LevelFormat.BULLET, text: '•', alignment: AlignmentType.LEFT,
          style: { paragraph: { indent: { left: 340, hanging: 200 } }, run: { font: FONT, size: 20 } },
        }],
      },
      {
        reference: 'ordered',
        levels: [{
          level: 0, format: LevelFormat.DECIMAL, text: '%1.', alignment: AlignmentType.LEFT,
          style: { paragraph: { indent: { left: 360, hanging: 220 } }, run: { font: FONT, size: 20 } },
        }],
      },
    ],
  },
  sections: [
    {
      properties: {
        page: {
          size: { width: 11906, height: 16838, orientation: PageOrientation.PORTRAIT },
          margin: { top: 1400, right: MARGIN_LR, bottom: 1300, left: MARGIN_LR },
        },
      },
      children: cover,
    },
    {
      properties: {
        page: {
          size: { width: 11906, height: 16838, orientation: PageOrientation.PORTRAIT },
          margin: { top: 1400, right: MARGIN_LR, bottom: 1300, left: MARGIN_LR },
        },
      },
      headers: {
        default: new Header({
          children: [new Paragraph({
            alignment: AlignmentType.RIGHT,
            spacing: { after: 60 },
            border: { bottom: { style: BorderStyle.SINGLE, size: 4, color: C.rule, space: 6 } },
            children: [new TextRun({ text: '분양하마 AI 분양 수요 전환 시스템 제안서', font: FONT, size: 15, color: C.light })],
          })],
        }),
      },
      footers: {
        default: new Footer({
          children: [new Paragraph({
            alignment: AlignmentType.CENTER,
            spacing: { before: 60 },
            children: [new TextRun({ children: [PageNumber.CURRENT], font: FONT, size: 16, color: C.sub })],
          })],
        }),
      },
      children: body,
    },
  ],
});

Packer.toBuffer(doc).then((buf) => {
  fs.writeFileSync(OUT, buf);
  console.log('written:', OUT, (buf.length / 1024).toFixed(0) + 'KB');
});
