// e2e — 빌드된 단일 HTML을 실제 브라우저로 열어 확인한다.
//  1) 콘솔 오류 없음  2) 모든 죄의 마인드맵(전부 펼침)에 그 죄의 원문 문단이 정확히 한 번씩, 글자 그대로 있음
//  3) 원문 보기의 문단 순서가 데이터 순서와 같음  4) 가로 맵에서 카드끼리 겹치지 않음
//  5) 휴대폰 폭에서 가로 스크롤 없음  6) 가리기·카드 채점·진도 저장·검색 이동이 동작함
// 사용법: node tests/e2e.mjs built.html   (PLAYWRIGHT 경로는 PW_MODULE, 브라우저는 PW_CHROMIUM으로 바꿀 수 있음)
import path from 'node:path';
const pwPath = process.env.PW_MODULE || '/opt/node22/lib/node_modules/playwright/index.mjs';
const { chromium } = await import(pwPath);
const file = path.resolve(process.argv[2] || 'dist/demo.html');
const browser = await chromium.launch({ executablePath: process.env.PW_CHROMIUM || '/opt/pw-browsers/chromium' });
const fails = [];
const fail = m => { fails.push(m); console.log('FAIL', m); };

async function newPage(viewport, scheme = 'light') {
  const ctx = await browser.newContext({ viewport, colorScheme: scheme });
  const page = await ctx.newPage();
  page.on('pageerror', e => fail('pageerror: ' + e.message));
  page.on('console', m => { if (m.type() === 'error' && !/Failed to load resource/.test(m.text())) fail('console: ' + m.text()); });
  await page.route('**/*', r => r.request().url().startsWith('file:') ? r.continue() : r.abort());
  await page.goto('file://' + file);
  await page.waitForFunction(() => window.__hv && window.__hv.crimes() > 0);
  return { ctx, page };
}

// 페이지 안에서 쓰는 도구
const HELPERS = () => {
  window.__t = {
    textOf(el) { const c = el.cloneNode(true); c.querySelectorAll('.deco').forEach(x => x.remove()); return c.textContent; },
    expected(ci, withFn) {
      const D = __hv.data(), crime = __hv.idx().crimes[ci].node, out = [];
      const T = p => (p.s || []).map(s => s[0]).join('');
      const para = p => { out.push([p.pid, T(p)]); if (withFn) for (const s of p.s || []) if (s.length === 3) for (const q of D.fns[String(s[2])]) raw(q); };
      const raw = it => { if (it.tbl) it.tbl.cells.forEach(c => c.ps.forEach(raw)); else para(it); };
      (function walk(n) {
        if (n.s && n.pid) para(n);
        if (n.cells) n.cells.forEach(c => c.ps.forEach(raw));
        (n.c || []).forEach(walk);
      })(crime);
      return out;
    },
  };
};

// ── 데스크톱: 맵 · 원문 · 겹침 ──
{
  const { ctx, page } = await newPage({ width: 1440, height: 900 });
  await page.evaluate(HELPERS);
  const n = await page.evaluate(() => __hv.crimes());
  let checked = 0, paras = 0;
  for (let ci = 0; ci < n; ci++) {
    const r = await page.evaluate(ci => {
      __hv.view('map'); __hv.mode('study'); __hv.open(ci); __hv.level(3);
      const exp = __t.expected(ci, false).filter(([, t]) => t.trim());
      const got = new Map();
      const dup = [];
      document.querySelectorAll('#mmCanvas .tx[data-pid]').forEach(el => { const k = el.dataset.pid; if (got.has(k)) dup.push(k); got.set(k, __t.textOf(el)); });
      const missing = [], wrong = [];
      for (const [pid, t] of exp) { if (!got.has(pid)) missing.push(pid); else if (got.get(pid) !== t) wrong.push(pid); }
      // 겹침
      const boxes = [...document.querySelectorAll('#mmCanvas .card')].map(c => c.getBoundingClientRect()).filter(b => b.width);
      let overlaps = 0;
      for (let i = 0; i < boxes.length; i++) for (let j = i + 1; j < boxes.length; j++) {
        const a = boxes[i], b = boxes[j];
        if (a.left < b.right - 1 && b.left < a.right - 1 && a.top < b.bottom - 1 && b.top < a.bottom - 1) overlaps++;
      }
      // 원문 보기 순서
      __hv.view('read');
      const order = [...document.querySelectorAll('#reader .tx[data-pid]')].filter(el => !el.closest('.fnlist')).map(el => el.dataset.pid);
      const expOrder = __t.expected(ci, false).map(([p]) => p);
      const readText = [...document.querySelectorAll('#reader .tx[data-pid]')].filter(el => !el.closest('.fnlist')).map(el => __t.textOf(el));
      const expText = __t.expected(ci, false).map(([, t]) => t);
      return { name: __hv.idx().crimes[ci].node.name, n: exp.length, missing, wrong, dup, overlaps,
        orderOK: JSON.stringify(order) === JSON.stringify(expOrder), textOK: JSON.stringify(readText) === JSON.stringify(expText) };
    }, ci);
    paras += r.n; checked++;
    if (r.missing.length) fail(`[${r.name}] 맵에 없는 문단 ${r.missing.length}개: ${r.missing.slice(0, 3)}`);
    if (r.wrong.length) fail(`[${r.name}] 맵 글자 불일치 ${r.wrong.length}개: ${r.wrong.slice(0, 3)}`);
    if (r.dup.length) fail(`[${r.name}] 맵에 중복 문단: ${r.dup.slice(0, 3)}`);
    if (r.overlaps) fail(`[${r.name}] 겹치는 카드 ${r.overlaps}쌍`);
    if (!r.orderOK) fail(`[${r.name}] 원문 보기 문단 순서 불일치`);
    if (!r.textOK) fail(`[${r.name}] 원문 보기 글자 불일치`);
  }
  console.log(`맵·원문 확인: 죄 ${checked}개, 문단 ${paras}개`);

  // 가리기 → 카드 눌러 공개
  const hide = await page.evaluate(() => {
    __hv.view('map'); __hv.open(Math.min(1, __hv.crimes() - 1)); __hv.level(2); __hv.mode('hide');
    const c = document.querySelector('#mmCanvas .card.concealed');
    if (!c) return 'no concealed card';
    const id = c.dataset.id; c.click();
    const after = document.querySelector(`#mmCanvas .card[data-id="${id}"]`);
    return after && !after.classList.contains('concealed') ? 'ok' : 'still concealed';
  });
  if (hide !== 'ok') fail('가리기 공개: ' + hide);

  // 검색 → 이동
  const search = await page.evaluate(async () => {
    __hv.mode('study');
    const q = document.querySelector('#q');
    const D = __hv.data();
    const target = __hv.idx().crimes[Math.min(3, __hv.crimes() - 1)].node.c.find(x => x.k === 'el');
    q.value = target.s.map(s => s[0]).join('').trim().slice(0, 6);
    q.dispatchEvent(new Event('input'));
    await new Promise(r => setTimeout(r, 250));
    const hit = document.querySelector('#results .r');
    if (!hit) return 'no results';
    hit.click();
    await new Promise(r => setTimeout(r, 300));
    return document.querySelector('#mmCanvas .card.flash') ? 'ok' : 'no flash';
  });
  if (search !== 'ok') fail('검색 이동: ' + search);

  // 암기 카드 채점 → 저장 → 새로고침 후 유지
  const before = await page.evaluate(() => {
    __hv.view('quiz');
    const sel = document.querySelector('#qDeck'); sel.value = 'all'; sel.dispatchEvent(new Event('change', { bubbles: true }));
    const card = document.querySelector('.qcard[data-card]'); if (!card) return null;
    const id = card.dataset.card;
    const rev = document.querySelector('#qReveal'); if (rev) rev.click();
    let ox = document.querySelector('[data-ox]'); while (ox) { ox.click(); ox = document.querySelector('[data-ox]'); }
    const g = document.querySelector('[data-qg="2"]'); if (!g) return 'no grade';
    g.click();
    return id;
  });
  if (!before || before === 'no grade') fail('카드 채점 실패: ' + before);
  else {
    await page.reload();
    await page.waitForFunction(() => window.__hv && window.__hv.crimes() > 0);
    const kept = await page.evaluate(id => { try { return Object.keys(JSON.parse(localStorage.getItem(Object.keys(localStorage).find(k => k.endsWith(':progress')))).cards).includes(id); } catch (e) { return false; } }, before);
    if (!kept) fail('진도 저장이 새로고침 후 사라짐');
  }
  await ctx.close();
}

// ── 휴대폰: 가로 스크롤 · 다크 ──
{
  const { ctx, page } = await newPage({ width: 390, height: 844 }, 'dark');
  const n = await page.evaluate(() => __hv.crimes());
  for (const v of ['map', 'overview', 'quiz', 'read']) {
    for (const ci of [0, Math.floor(n / 2), n - 1]) {
      const over = await page.evaluate(([v, ci]) => { __hv.open(ci); __hv.view(v); if (v === 'map') __hv.level(3); return document.documentElement.scrollWidth - innerWidth; }, [v, ci]);
      if (over > 0) fail(`휴대폰 ${v} #${ci}: 가로로 ${over}px 넘침`);
    }
  }
  const bg = await page.evaluate(() => getComputedStyle(document.body).backgroundColor);
  if (bg === 'rgb(255, 255, 255)') fail('다크 모드 배경이 흰색');
  await ctx.close();
}

await browser.close();
if (fails.length) { console.log(`실패 ${fails.length}건`); process.exit(1); }
console.log('e2e OK');
