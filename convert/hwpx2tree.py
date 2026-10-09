#!/usr/bin/env python3
"""hwpx2tree — 정리자료(.hwpx)를 마인드맵용 트리 JSON으로 변환한다.

표준 라이브러리만 사용한다. 문단 스타일 '이름'(개요/챕터 제목/대표법리/일반법리/
세부법리/참조/중간분류 …)과 들여쓰기로 계층을 정하고, 원문 문단의 순서와 글자·서식을
그대로 보존한다(트리를 깊이우선으로 순회하면 원문 순서가 된다).

사용법:
    python3 -I hwpx2tree.py SOURCE.hwpx -o tree.json [--log build.log] [--dump blocks.txt]
"""
import argparse
import hashlib
import json
import re
import sys
import zipfile
import xml.etree.ElementTree as ET

HP = '{http://www.hancom.co.kr/hwpml/2011/paragraph}'
HH = '{http://www.hancom.co.kr/hwpml/2011/head}'
HC = '{http://www.hancom.co.kr/hwpml/2011/core}'

# Wingdings 등 사설 영역(PUA) 글머리표를 보이는 문자로 바꾼다 (표시용, 원문 텍스트 아님).
PUA_BULLET = {'': '■', '': '☑', '': '✓', '': '▪', '': '➢', '': '•'}

INLINE_CHARS = {'lineBreak': '\n', 'tab': '\t', 'fwSpace': '　', 'nbSpace': ' ', 'hyphen': '-'}

# 스타일 이름 → 문단 등급(rank). 숫자가 클수록 깊다.
STYLE_RANK = {'일반법리': 2, '동그라미': 2, '중간분류': 2.5, '세부법리': 3, '예시': 3, '참조': 4}
LEVEL_NAME = {2: 'g', 2.5: 'm', 3: 'd', 4: 'n', 4.5: 'x'}  # 일반/중간/세부/참조/ex

STAT_RE = re.compile(
    r'^\s*(?P<name>[^()\n]{0,60}?(?:\([^()]*\)[^()\n]{0,60}?)*죄)\s*'
    r'\((?P<ref>(?:[가-힣]+\s*)?제\s*\d+\s*조[^()]*)\)\s*(?P<josa>은|는)?')
CIRCLED = re.compile(r'[①-⑳]')
STAT_TAIL = re.compile(r'\s*(?:한\s*)?(?:때|경우)\s*(?:에\s*)?성립한다|\s*성립한다')
PART_RE = re.compile(r'^\s*제\s*\d+\s*편')
CHAP_RE = re.compile(r'^\s*제\s*\d+\s*장')
SEP_RE = re.compile(r'^[\s.·…‥]{10,}$')
VERDICT_RE = re.compile(r'(?<![A-Za-z])[OX](?![A-Za-z])')
DEF_RE = re.compile(r'^\s*[‘\']([^’\']{1,40})[’\']\s*(?:이|가)?란')


# ───────────────────────── header.xml ─────────────────────────
class Header:
    def __init__(self, xml):
        root = ET.fromstring(xml)
        self.styles = {s.get('id'): s.get('name') for s in root.iter(HH + 'style')}
        self.borderfills = {}
        for bf in root.iter(HH + 'borderFill'):
            sides = [bf.find(HH + n) for n in ('leftBorder', 'rightBorder', 'topBorder', 'bottomBorder')]
            boxed = all(s is not None and s.get('type', 'NONE') != 'NONE' for s in sides)
            wb = bf.find('.//' + HC + 'winBrush')
            face = wb.get('faceColor') if wb is not None else None
            if face is None or face.lower() == 'none':
                face = None
            self.borderfills[bf.get('id')] = (boxed, face)
        self.charprs = {}
        for cp in root.iter(HH + 'charPr'):
            f = {}
            if cp.find(HH + 'bold') is not None:
                f['b'] = 1
            if cp.find(HH + 'italic') is not None:
                f['i'] = 1
            ul = cp.find(HH + 'underline')
            if ul is not None and ul.get('type', 'NONE') != 'NONE':
                f['u'] = 1
            so = cp.find(HH + 'strikeout')
            if so is not None and so.get('shape', 'NONE') != 'NONE':
                f['k'] = 1
            tc = (cp.get('textColor') or '#000000').upper()
            if tc != '#000000':
                f['c'] = tc
            sh = (cp.get('shadeColor') or 'none').upper()
            if sh not in ('NONE', '#FFFFFF'):
                f['h'] = sh
            boxed, face = self.borderfills.get(cp.get('borderFillIDRef'), (False, None))
            if boxed:
                f['x'] = 1
            if face and face.upper() != '#FFFFFF':
                f['g'] = face.upper()
            self.charprs[cp.get('id')] = f
        self.paraprs = {}
        for pp in root.iter(HH + 'paraPr'):
            head = pp.find(HH + 'heading')
            left = pp.find('.//' + HC + 'left')
            intent = pp.find('.//' + HC + 'intent')
            self.paraprs[pp.get('id')] = {
                'left': int(left.get('value')) if left is not None else 0,
                'intent': int(intent.get('value')) if intent is not None else 0,
                'head': (head.get('type'), head.get('idRef'), int(head.get('level', '0')))
                if head is not None else ('NONE', '0', 0),
            }
        self.bullets = {}
        for b in root.iter(HH + 'bullet'):
            ch = b.get('char') or ''
            self.bullets[b.get('id')] = ''.join(PUA_BULLET.get(c, c) for c in ch)
        self.numberings = {}
        for n in root.iter(HH + 'numbering'):
            self.numberings[n.get('id')] = {int(h.get('level')): (h.text or '') for h in n.iter(HH + 'paraHead')}


# ───────────────────────── 추출 (section → 블록) ─────────────────────────
class Extractor:
    """section XML의 문단을 '조각(piece)' 단위로 꺼낸다.

    한 문단은 표가 끼어 있는 위치에서 조각으로 나뉜다. 조각 k(0부터)의 pid는 k==0이면 문단 pid,
    아니면 'pid~k'. 텍스트·각주 앵커가 없는 조각은 버린다. 표는 {'t':'table'} 항목이 된다.
    """

    def __init__(self, hdr, log):
        self.h = hdr
        self.log = log
        self.fmts = [{}]
        self.fmt_index = {'': 0}
        self.fns = {}
        self.unknown_inline = set()

    def fmt_id(self, f):
        key = ','.join('%s=%s' % (k, f[k]) for k in sorted(f))
        if key not in self.fmt_index:
            self.fmt_index[key] = len(self.fmts)
            self.fmts.append(dict(f))
        return self.fmt_index[key]

    def paragraph(self, p, pid):
        """문단 하나 → 항목 목록: piece dict 또는 table dict (문서 순서)."""
        style = self.h.styles.get(p.get('styleIDRef'), p.get('styleIDRef'))
        pp = self.h.paraprs.get(p.get('paraPrIDRef'), {'left': 0, 'intent': 0, 'head': ('NONE', '0', 0)})
        base = {'st': style, 'pp': p.get('paraPrIDRef'), 'left': pp['left'], 'head': pp['head']}
        out = []
        piece_no = 0
        cur = []          # [[text, fmt] | ['', fmt, fnNo]]
        state = {'mark': None}
        tbl_no = 0

        def flush():
            nonlocal cur, piece_no
            if cur:
                piece = dict(base)
                piece['t'] = 'para'
                piece['pid'] = pid if piece_no == 0 else '%s~%d' % (pid, piece_no)
                piece['s'] = merge_segs(cur)
                out.append(piece)
            cur = []
            piece_no += 1

        def add(text, cp):
            if not text:
                return
            f = dict(self.h.charprs.get(cp, {}))
            if state['mark']:
                f['m'] = state['mark']
            cur.append([text, self.fmt_id(f)])

        for run in p.findall(HP + 'run'):
            cp = run.get('charPrIDRef')
            for ch in run:
                tag = ch.tag
                if tag == HP + 't':
                    add(ch.text, cp)
                    for sub in ch:
                        name = sub.tag.split('}')[1]
                        if name in INLINE_CHARS:
                            add(INLINE_CHARS[name], cp)
                        elif name == 'markpenBegin':
                            state['mark'] = (sub.get('color') or '#FFFF00').upper()
                        elif name == 'markpenEnd':
                            state['mark'] = None
                        else:
                            self.unknown_inline.add(name)
                        add(sub.tail, cp)
                elif tag == HP + 'tbl':
                    flush()
                    tb = self.table(ch, '%s/t%d' % (pid, tbl_no))
                    tb['holder'] = {'st': style, 'left': pp['left'], 'head': pp['head']}
                    out.append(tb)
                    tbl_no += 1
                elif tag == HP + 'ctrl':
                    for c in ch:
                        if c.tag == HP + 'footNote' or c.tag == HP + 'endNote':
                            num = int(c.get('number'))
                            paras = []
                            for sl in c.findall(HP + 'subList'):
                                for j, q in enumerate(sl.findall(HP + 'p')):
                                    paras.extend(self.paragraph(q, '%s/fn%d/p%d' % (pid, num, j)))
                            self.fns[num] = paras
                            cur.append(['', 0, num])
        flush()
        return out

    def table(self, tbl, tid):
        cells = []
        for tr in tbl.findall(HP + 'tr'):
            for tc in tr.findall(HP + 'tc'):
                a = tc.find(HP + 'cellAddr')
                sp = tc.find(HP + 'cellSpan')
                r, c = int(a.get('rowAddr')), int(a.get('colAddr'))
                items = []
                for sl in tc.findall(HP + 'subList'):
                    for j, q in enumerate(sl.findall(HP + 'p')):
                        items.extend(self.paragraph(q, '%s/r%dc%d/p%d' % (tid, r, c, j)))
                cells.append({'r': r, 'c': c, 'rs': int(sp.get('rowSpan')), 'cs': int(sp.get('colSpan')),
                              'bf': tc.get('borderFillIDRef'), 'ps': items})
        return {'t': 'table', 'pid': tid, 'rows': int(tbl.get('rowCnt')), 'cols': int(tbl.get('colCnt')),
                'cells': cells}

    def section(self, xml, sec):
        root = ET.fromstring(xml)
        items = []
        for i, p in enumerate(c for c in root if c.tag == HP + 'p'):
            items.extend(self.paragraph(p, 's%d/p%d' % (sec, i)))
        return items


def merge_segs(segs):
    out = []
    for s in segs:
        if out and len(s) == 2 and len(out[-1]) == 2 and out[-1][1] == s[1]:
            out[-1][0] += s[0]
        else:
            out.append(list(s))
    return out


def ptext(piece):
    return ''.join(s[0] for s in piece['s'])


# ───────────────────────── 트리 구성 ─────────────────────────
class Builder:
    def __init__(self, hdr, fmts, log):
        self.h = hdr
        self.fmts = fmts
        self.log = log
        self.n = 0
        self.root = self.node('book')
        self.stack = [(-10, self.root)]
        self.numctr = {}
        self.stats = {'implicit_crimes': [], 'inferred_headings': [], 'continuations': 0}

    def node(self, k, **kw):
        self.n += 1
        d = {'id': 'n%d' % self.n, 'k': k}
        d.update(kw)
        d.setdefault('c', [])
        return d

    def attach(self, nd, rank, push=True, stack=None):
        st = self.stack if stack is None else stack
        while st[-1][0] >= rank:
            st.pop()
        st[-1][1]['c'].append(nd)
        if push:
            st.append((rank, nd))
        return nd

    def open_kind(self, kind, stack=None):
        for _, nd in reversed(self.stack if stack is None else stack):
            if nd['k'] == kind:
                return nd
        return None

    # 문단 조각 → 노드 공통 필드
    def para_fields(self, pc):
        f = {'pid': pc['pid'], 's': pc['s'], 'st': pc['st']}
        htype, hid, hlv = pc['head']
        if htype == 'BULLET':
            f['b'] = self.h.bullets.get(hid, '')
        elif htype == 'NUMBER':
            key = (hid, hlv)
            self.numctr[key] = self.numctr.get(key, 0) + 1
            fmt = self.h.numberings.get(hid, {}).get(hlv + 1, '^%d.' % (hlv + 1))
            f['num'] = re.sub(r'\^\d', str(self.numctr[key]), fmt)
        if htype != 'NUMBER':
            self.numctr.clear()
        if pc['left']:
            f['ind'] = pc['left']
        return f

    def rule_rank(self, pc):
        st, left = pc['st'], pc['left']
        if st in STYLE_RANK:
            r = STYLE_RANK[st]
            if st == '참조' and left >= 1100:
                r = 4.5
            return r
        if left < 50:
            return 2
        if left < 250:
            return 2.5
        if left < 750:
            return 3
        if left < 1100:
            return 4
        return 4.5

    # ── 메인 루프 ──
    def build(self, items):
        units = list(items)
        for i, it in enumerate(units):
            nxt = next((u for u in units[i + 1:] if u['t'] == 'table' or ptext(u).strip()), None)
            if it['t'] == 'table':
                self.table_unit(it)
            else:
                self.para_unit(it, nxt)
        return self.root

    def para_unit(self, pc, nxt, stack=None, in_box=False):
        text = ptext(pc)
        st = pc['st']
        bare = text.strip()
        has_fn = any(len(s) == 3 for s in pc['s'])
        fields = self.para_fields(pc)
        if not bare and not has_fn:
            self.attach(self.node('sp', **fields), 99, push=False, stack=stack)
            return
        if SEP_RE.match(text):
            self.attach(self.node('sep', **fields), 99, push=False, stack=stack)
            return
        if st in ('개요 1', '개요 2', '개요 1 사본1', '개요 2 사본1', '장 제목', '문서 제목') and not in_box:
            if PART_RE.match(text):
                self.attach(self.node('part', title=bare, **fields), -2)
                return
            if CHAP_RE.match(text):
                nd = self.node('chap', title=bare, **fields)
                if all(self.fmts[s[1]].get('k') for s in pc['s'] if s[0].strip()):
                    nd['del'] = 1
                self.attach(nd, -1)
                return
            if st in ('장 제목', '문서 제목'):
                self.attach(self.node('title', title=bare, **fields), -3, push=False)
                return
        if st == '챕터 제목' and bare.startswith('[') and not in_box:
            m = re.match(r'^\[(.+?)\]\s*(.*)$', bare, re.S)
            name, sub = (m.group(1), m.group(2)) if m else (bare.strip('[]'), '')
            self.ensure_chapter()
            nd = self.node('crime', name=name.strip(), **fields)
            if sub.strip():
                nd['sub'] = sub.strip()
            if name.strip() in ('자주 반복되는 개념들',) or name.strip().endswith('총론'):
                nd['ov'] = 1
            self.attach(nd, 0)
            return
        heading = st == '대표법리' or st == '챕터 제목'
        if not heading and st in ('바탕글', '본문', 'Normal') and self.looks_like_heading(pc, text, nxt):
            heading = True
            self.stats['inferred_headings'].append((pc['pid'], bare[:40]))
        if heading:
            if in_box:
                self.attach(self.node('el', box=1, **fields), 1.6, stack=stack)
            else:
                self.ensure_crime_context()
                self.attach(self.node('el', **fields), 1)
            return
        rank = self.rule_rank(pc)
        fields['lv'] = LEVEL_NAME.get(rank, 'n')
        nd = self.node('p', **fields)
        st_ = self.stack if stack is None else stack
        # 글머리표 없는 문단: 같은 들여쓰기의 직전 글머리표 문단에 딸린 연속 문단으로 본다.
        if pc['head'][0] == 'NONE' and rank >= 2:
            top_rank, top = st_[-1]
            if (top['k'] == 'p' and top.get('b') and top.get('ind', 0) == pc['left']
                    and abs(top_rank - rank) < 0.01):
                nd['cont'] = 1
                self.stats['continuations'] += 1
                self.attach(nd, top_rank + 0.25, stack=stack)
                return
        self.attach(nd, rank, stack=stack)

    def looks_like_heading(self, pc, text, nxt):
        bare = text.strip()
        if pc['left'] >= 50 or pc['head'][0] != 'NONE' or len(bare) > 40:
            return False
        if re.search(r'(다|함|음|임)\.?\s*$', bare) or ':' in bare:
            return False
        if nxt is None or nxt['t'] != 'para':
            return False
        return nxt['st'] in ('일반법리', '세부법리', '참조')

    def ensure_chapter(self):
        if not self.open_kind('chap') and not self.open_kind('part'):
            nd = self.node('chap', title='공통 개념', syn=1)
            self.attach(nd, -1)

    def ensure_crime_context(self, names=None):
        if self.open_kind('crime'):
            return
        self.ensure_chapter()
        chap = self.open_kind('chap')
        if names and len(names) <= 2:
            name = '·'.join(names)
        else:
            name = re.sub(r'^\s*제\s*\d+\s*장\s*', '', chap['title']) if chap and chap.get('title') else '(무제)'
        nd = self.node('crime', name=name, syn=1)
        self.stats['implicit_crimes'].append(name)
        self.attach(nd, 0)

    # ── 표 ──
    def table_unit(self, tb, stack=None, in_box=False):
        cells_paras = [[u for u in c['ps'] if u['t'] == 'para'] for c in tb['cells']]
        first = next((u for ps in cells_paras for u in ps if ptext(u).strip()), None)
        hrank = self.holder_rank(tb)
        if tb['cols'] > 1:
            nd = self.node('tbl', pid=tb['pid'], rows=tb['rows'], cols=tb['cols'], cells=self.raw_cells(tb))
            self.attach(nd, max(hrank, 2), push=False, stack=stack)
            return
        arts = []
        for ps in cells_paras:
            for u in ps:
                m = STAT_RE.match(ptext(u))
                if m and len(m.group('name')) <= 90:
                    arts.append(m.group('name').strip())
        if arts and not in_box:
            self.ensure_crime_context(arts)
            nd = self.node('stat', pid=tb['pid'], rows=tb['rows'], cols=tb['cols'], cells=self.raw_cells(tb, statute=True))
            self.attach(nd, 1)
            return
        if first is not None and first['st'] == '대표법리':
            self.sub_box(tb, stack)
            return
        nd = self.node('case', pid=tb['pid'], rows=tb['rows'], cols=tb['cols'], cells=self.raw_cells(tb))
        self.attach(nd, max(hrank, 2), push=False, stack=stack)

    def holder_rank(self, tb):
        h = tb.get('holder')
        if not h:
            return 2
        if h['st'] in STYLE_RANK:
            return STYLE_RANK[h['st']]
        return 2 if h['left'] < 50 else self.rule_rank(h)

    def sub_box(self, tb, stack):
        outer = self.stack if stack is None else stack
        # 박스는 현재 죄의 마지막 구성요건 제목(없으면 죄) 아래로 들어간다.
        idx = None
        for i in range(len(outer) - 1, -1, -1):
            if outer[i][1]['k'] == 'el' and not outer[i][1].get('box'):
                idx = i
                break
            if outer[i][1]['k'] == 'crime':
                idx = i
                break
        if idx is None:
            self.ensure_crime_context()
            idx = len(self.stack) - 1
            outer = self.stack
        bstack = list(outer[:idx + 1])
        boxid = 'b%d' % self.n
        units = []
        for c in tb['cells']:
            for u in c['ps']:
                units.append(u)
        first_el = None
        for i, u in enumerate(units):
            if u['t'] == 'table':
                self.table_unit(u, stack=bstack, in_box=True)
                continue
            nxt = next((v for v in units[i + 1:] if v['t'] == 'table' or ptext(v).strip()), None)
            self.para_unit(u, nxt, stack=bstack, in_box=True)
            top = bstack[-1][1]
            if top['k'] == 'el' and top.get('box') == 1:
                top['box'] = boxid
                if first_el is None:
                    first_el = top
        # 박스(표) 모양은 첫 박스요건 노드에 남긴다(검증·렌더용).
        if first_el is not None:
            first_el['bxt'] = {'pid': tb['pid'], 'rows': tb['rows'], 'cols': tb['cols'],
                               'cells': [[c['r'], c['c'], c['rs'], c['cs']] for c in tb['cells']]}
        # 박스가 끝나면 바깥 스택에서 박스 부모보다 깊은 단계는 닫는다(문서 순서 보존).
        del outer[idx + 1:]

    def raw_cells(self, tb, statute=False):
        out = []
        for c in tb['cells']:
            ps = []
            for u in c['ps']:
                if u['t'] == 'table':
                    ps.append({'tbl': {'pid': u['pid'], 'rows': u['rows'], 'cols': u['cols'],
                                       'cells': self.raw_cells(u)}})
                    continue
                f = self.para_fields(u)
                if statute:
                    art = parse_statute(ptext(u))
                    if art:
                        f['art'] = art
                ps.append(f)
            out.append({'r': c['r'], 'c': c['c'], 'rs': c['rs'], 'cs': c['cs'], 'bf': c['bf'], 'ps': ps})
        return out


def parse_statute(text):
    m = STAT_RE.match(text)
    if not m or len(m.group('name')) > 90:
        return None
    name = m.group('name').strip()
    ns = text.index(name)
    art = {'name': [ns, ns + len(name)], 'ref': [m.start('ref'), m.end('ref')]}
    pos = [mm.start() for mm in CIRCLED.finditer(text, m.end())]
    els = []
    for i, p in enumerate(pos):
        end = pos[i + 1] if i + 1 < len(pos) else len(text)
        seg = text[p + 1:end]
        if i + 1 == len(pos):
            t = STAT_TAIL.search(seg)
            if t:
                end = p + 1 + t.start()
        s, e = p + 1, end
        while s < e and text[s].isspace():
            s += 1
        while e > s and text[e - 1] in ' ,，　':
            e -= 1
        els.append([s, e])
    if els:
        art['els'] = els
    return art


# ───────────────────────── 보강(enrich) ─────────────────────────
def node_text(nd):
    return ''.join(s[0] for s in nd.get('s', []))


def enrich(root, fmts, log):
    stats = {'ox': 0, 'ox_prop': 0, 'def': 0, 'hong': 0, 'yak': 0, 'star': 0, 'links': 0, 'mn': 0}
    seen_ox = set()

    def walk(nd, crime, heading, parent):
        if nd['k'] == 'crime':
            crime = nd
        if nd['k'] == 'el':
            heading = nd
        if 's' in nd and nd['k'] in ('p', 'el'):
            tag_node(nd, fmts, stats)
        if nd['k'] == 'p' and crime is not None:
            ox = extract_ox(node_text(nd))
            if ox:
                key = hashlib.sha1(node_text(nd).encode()).hexdigest()
                if key not in seen_ox:
                    seen_ox.add(key)
                    nd['ox'] = ox
                    stats['ox' if 'q' in ox else 'ox_prop'] += 1
        if nd['k'] == 'p':
            mn = mnemonic(nd, fmts)
            if mn:
                nd['mn'] = mn
                stats['mn'] += 1
        for ch in nd['c']:
            walk(ch, crime, heading, nd)

    walk(root, None, None, None)
    link_statutes(root, stats)
    return stats


def tag_node(nd, fmts, stats):
    text = node_text(nd)
    bare = text.strip()
    tags = []
    nonsp = [(ch, s[1]) for s in nd['s'] for ch in s[0] if not ch.isspace()]
    blue = sum(1 for _, f in nonsp if fmts[f].get('c') in ('#0000FF', '#0432FF'))
    if nonsp and (blue / len(nonsp) >= 0.6 or bare.startswith('(홍') or bare.startswith('홍)')
                  or bare.startswith('채기표')):
        tags.append('hong')
        stats['hong'] += 1
    if '약술형' in bare:
        tags.append('yak')
        stats['yak'] += 1
    m = DEF_RE.match(bare)
    if m and nd['k'] == 'p':
        tags.append('def')
        nd['term'] = m.group(1)
        stats['def'] += 1
    if '★' in bare:
        tags.append('star')
        stats['star'] += 1
    if re.search(r'判例|판례|대법원|전원합의체|전합', bare):
        tags.append('pan')
    if re.search(r'통설|通說|학설|다수설|소수설|견해', bare):
        tags.append('hak')
    if re.search(r'최근|판례변경|변경된', bare):
        tags.append('new')
    if tags:
        nd['tags'] = tags


def quote_mask(text):
    mask = [False] * len(text)
    for o, c in (('‘', '’'), ('“', '”')):
        i = 0
        while True:
            a = text.find(o, i)
            if a < 0:
                break
            b = text.find(c, a + 1)
            if b < 0:
                break
            for k in range(a, b + 1):
                mask[k] = True
            i = b + 1
    return mask


def depth_map(text):
    d, out = 0, []
    for ch in text:
        if ch in '([':
            d += 1
            out.append(d)
            continue
        out.append(d)
        if ch in ')]' and d > 0:
            d -= 1
    return out


SEP_TOKENS = [':', ' – ', ' - ', '→', '=>', '⇒']


def extract_ox(text):
    mask = quote_mask(text)
    depth = depth_map(text)
    verdicts = [m.start() for m in VERDICT_RE.finditer(text) if not mask[m.start()]]
    if not verdicts:
        return None
    first0 = next((v for v in verdicts if depth[v] == 0), None)
    if first0 is None:
        return None
    best = None
    for tok in SEP_TOKENS:
        i = 0
        while True:
            j = text.find(tok, i)
            if j < 0 or j >= first0:
                break
            if depth[j] == 0 and not mask[j]:
                if best is None or j > best[0]:
                    best = (j, j + len(tok))
            i = j + 1
    if best is not None:
        q = re.sub(r'\s', '', text[:best[0]])
        if len(q) >= 3:
            a0 = best[1]
            vs = [v for v in verdicts if v >= a0]
            return {'q': [0, best[0]], 'a': [a0, len(text)], 'v': vs}
    # 명제형: 판정어만 가린다
    return {'v': [v for v in verdicts]}


def mnemonic(nd, fmts):
    """글자 테두리(두문자) 낱글자를 모은다. 연속으로 테두리 친 2글자 이상은 장식으로 보고 제외."""
    chars = []
    for s in nd['s']:
        if fmts[s[1]].get('x') and len(s) == 2:
            t = s[0].strip()
            if 1 <= len(t) <= 2:
                chars.append(t)
    return ''.join(chars) if len(chars) >= 2 else None


def norm(s):
    s = re.sub(r'\(=?[^()]*\)', '', s)
    s = re.split(r'\s[–-]\s', s)[0]
    return re.sub(r'[\s·,./()\[\]‘’“”\'"~→⇒=]+', '', s)


def link_statutes(root, stats):
    def walk(nd, crime):
        if nd['k'] == 'crime':
            crime = nd
            crime['_arts'] = []
            for ch in nd['c']:
                if ch['k'] == 'stat':
                    for ci, cell in enumerate(ch['cells']):
                        for pi, p in enumerate(cell['ps']):
                            art = p.get('art')
                            if art and 'els' in art:
                                t = ''.join(s[0] for s in p['s'])
                                crime['_arts'].append((t[art['name'][0]:art['name'][1]],
                                                       [norm(t[a:b]) for a, b in art['els']]))
        if nd['k'] == 'el' and crime is not None and crime.get('_arts'):
            head = ''.join(s[0] for s in nd['s'])
            cands = [norm(x) for x in re.split(r'/', head)]
            cands = [c for c in cands if len(c) >= 2]
            links = []
            for ai, (aname, els) in enumerate(crime['_arts']):
                for ei, el in enumerate(els):
                    if any(c and (c in el or (len(el) >= 3 and el in c)) for c in cands):
                        links.append([ai, ei])
            if links:
                nd['ln'] = links
                stats['links'] += 1
            else:
                nd['issue'] = 1
        for ch in nd['c']:
            walk(ch, crime)
        if nd['k'] == 'crime':
            nd['arts'] = [a for a, _ in nd.pop('_arts')]

    walk(root, None)


# ───────────────────────── 메인 ─────────────────────────

def extract_all(path, log):
    z = zipfile.ZipFile(path)
    hdr = Header(z.read('Contents/header.xml'))
    ex = Extractor(hdr, log)
    names = sorted(n for n in z.namelist() if re.match(r'Contents/section\d+\.xml$', n))
    secs = {}
    for n in names:
        k = int(re.search(r'(\d+)', n.split('/')[-1]).group(1))
        secs[k] = section_with_holders(ex, z.read(n), k)
    return hdr, ex, secs


def section_with_holders(ex, xml, sec):
    root = ET.fromstring(xml)
    items = []
    for i, p in enumerate(c for c in root if c.tag == HP + 'p'):
        items.extend(ex.paragraph(p, 's%d/p%d' % (sec, i)))
    return items



def raw_item(u):
    if u['t'] == 'table':
        return {'tbl': {'pid': u['pid'], 'rows': u['rows'], 'cols': u['cols'],
                        'cells': [{'r': c['r'], 'c': c['c'], 'rs': c['rs'], 'cs': c['cs'], 'bf': c['bf'],
                                   'ps': [raw_item(x) for x in c['ps']]} for c in u['cells']]}}
    d = {'pid': u['pid'], 's': u['s'], 'st': u['st']}
    if u['left']:
        d['ind'] = u['left']
    return d


def strip_private(nd):
    for k in [k for k in nd if k.startswith('_')]:
        del nd[k]
    for ch in nd.get('c', []):
        strip_private(ch)


def prune(nd):
    if not nd.get('c'):
        nd.pop('c', None)
    else:
        for ch in nd['c']:
            prune(ch)


def cover_meta(secs, fns):
    cover = []
    for it in secs.get(0, []):
        cover.append(raw_item(it))
    texts = []

    def collect(items):
        for c in items:
            if 'tbl' in c:
                for cell in c['tbl']['cells']:
                    collect(cell['ps'])
            else:
                t = ''.join(s[0] for s in c['s']).strip()
                if t:
                    texts.append(t)

    collect(cover)
    meta = {
        'title': texts[0] if texts else '',
        'subtitle': texts[1] if len(texts) > 1 else '',
        'notice': next((t for t in texts if '유출' in t or '배포' in t), ''),
        'cover': cover,
    }
    return meta


def log_tree(nd, out, depth=0):
    k = nd['k']
    t = node_text(nd).strip().replace('\n', '⏎')
    label = {'crime': '[죄] ' + nd.get('name', ''), 'chap': '[장] ' + nd.get('title', ''),
             'part': '[편] ' + nd.get('title', ''), 'stat': '[조문박스]', 'tbl': '[비교표 %sx%s]' % (nd.get('rows'), nd.get('cols')),
             'case': '[사례박스]', 'el': ('[박스요건] ' if nd.get('box') else '[구성요건] ') + t[:60]}.get(k)
    if label is None:
        if k in ('sp', 'sep'):
            label = None
        else:
            tags = ','.join(nd.get('tags', []))
            label = '%s%s %s%s' % (nd.get('lv', k), '+' if nd.get('cont') else '', t[:70], (' {%s}' % tags) if tags else '')
            if nd.get('ox'):
                label += ' ⟨OX⟩'
    if label is not None:
        extra = ''
        if k == 'el' and nd.get('ln'):
            extra = ' ↔' + str(nd['ln'])
        out.append('  ' * depth + label + extra + '   <' + str(nd.get('pid', '')) + '>')
    for ch in nd.get('c', []):
        log_tree(ch, out, depth + 1)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('src')
    ap.add_argument('-o', '--out', required=True)
    ap.add_argument('--log')
    ap.add_argument('--dump', help='원문 블록 스트림 덤프(검토용)')
    ap.add_argument('--name', default=None, help='자료 이름(기본: 원문 첫 제목)')
    a = ap.parse_args(argv)
    logs = []
    log = logs.append
    hdr, ex, secs = extract_all(a.src, log)
    body_secs = [k for k in sorted(secs) if k != 0] or [0]
    items = []
    for k in body_secs:
        items.extend(secs[k])
    b = Builder(hdr, ex.fmts, log)
    root = b.build(items)
    stats = enrich(root, ex.fmts, log)
    strip_private(root)
    meta = cover_meta(secs if 0 in secs and len(secs) > 1 else {}, ex.fns)
    if a.name:
        meta['name'] = a.name
    title_node = next((c for c in root['c'] if c['k'] == 'title'), None)
    meta['name'] = meta.get('name') or (title_node['title'] if title_node else meta.get('title', ''))
    fns = {str(k): [raw_item(u) for u in v] for k, v in sorted(ex.fns.items())}
    data = {'v': 1, 'meta': meta, 'fmts': ex.fmts, 'fns': fns,
            'root': root}
    prune(root)
    with open(a.out, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, separators=(',', ':'))
    if a.dump:
        with open(a.dump, 'w', encoding='utf-8') as f:
            dump_items(items, f)
    crimes = []

    def collect(nd):
        if nd['k'] == 'crime':
            crimes.append(nd)
        for ch in nd.get('c', []):
            collect(ch)

    collect(root)
    summary = ['crimes=%d' % len(crimes), 'footnotes=%d' % len(ex.fns), 'fmts=%d' % len(ex.fmts)]
    summary += ['%s=%s' % kv for kv in stats.items()]
    summary += ['continuations=%d' % b.stats['continuations']]
    lines = ['# hwpx2tree build log', ' '.join(summary),
             '## implicit crimes', *b.stats['implicit_crimes'],
             '## inferred headings', *['%s %s' % x for x in b.stats['inferred_headings']],
             '## unknown inline tags', ', '.join(sorted(ex.unknown_inline)) or '(none)',
             '## tree']
    log_tree(root, lines)
    lines.append('## O/X cards')
    ox_lines = []

    def oxwalk(nd, crime):
        if nd['k'] == 'crime':
            crime = nd.get('name')
        if nd.get('ox'):
            t = node_text(nd)
            ox = nd['ox']
            if 'q' in ox:
                ox_lines.append('[%s] Q: %s || A: %s' % (crime, t[ox['q'][0]:ox['q'][1]].strip(), t[ox['a'][0]:].strip()))
            else:
                ox_lines.append('[%s] P: %s' % (crime, t.strip()))
        for ch in nd.get('c', []):
            oxwalk(ch, crime)

    oxwalk(root, None)
    lines += ox_lines
    if a.log:
        with open(a.log, 'w', encoding='utf-8') as f:
            f.write('\n'.join(lines) + '\n')
    print(' '.join(summary), file=sys.stderr)


def dump_items(items, f, depth=0):
    for it in items:
        if it['t'] == 'table':
            h = it.get('holder') or {}
            f.write('%s<TABLE %s %dx%d holder=%s/%s>\n' % ('  ' * depth, it['pid'], it['rows'], it['cols'],
                                                         h.get('st'), h.get('left')))
            for c in it['cells']:
                f.write('%s <CELL r%dc%d %dx%d>\n' % ('  ' * depth, c['r'], c['c'], c['rs'], c['cs']))
                dump_items(c['ps'], f, depth + 1)
        else:
            t = ptext(it).replace('\n', '⏎')
            if not t.strip():
                continue
            htype, hid, _ = it['head']
            f.write('%s[%s|L%d|%s] %s   <%s>\n' % ('  ' * depth, it['st'], it['left'],
                                                 htype[0] if htype != 'NONE' else '-', t, it['pid']))


if __name__ == '__main__':
    main()
