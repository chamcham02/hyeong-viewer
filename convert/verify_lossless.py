#!/usr/bin/env python3
"""verify_lossless — 변환 결과(tree.json 또는 tree를 인라인한 HTML)가 원문 hwpx와
글자·서식·순서·표 모양까지 완전히 같은지 검사한다. 변환기(hwpx2tree)와 코드를 공유하지 않는
독립 추출기를 쓴다. 하나라도 다르면 종료 코드 1.

    python3 -I verify_lossless.py SOURCE.hwpx tree.json|built.html
"""
import json
import re
import sys
import zipfile
import xml.etree.ElementTree as ET

P = '{http://www.hancom.co.kr/hwpml/2011/paragraph}'
INL = {'lineBreak': '\n', 'tab': '\t', 'fwSpace': '　', 'nbSpace': ' ', 'hyphen': '-'}


def char_formats(header):
    """charPr id → 서식 문자열 (정규식으로 직접 읽는다)."""
    bfs = {}
    for m in re.finditer(r'<hh:borderFill id="(\d+)"(.*?)</hh:borderFill>', header, re.S):
        sides = re.findall(r'<hh:(?:left|right|top|bottom)Border type="([^"]+)"', m.group(2))
        face = re.search(r'faceColor="([^"]+)"', m.group(2))
        face = face.group(1).upper() if face and face.group(1).lower() != 'none' else None
        bfs[m.group(1)] = (len(sides) == 4 and 'NONE' not in sides, face)
    out = {}
    for m in re.finditer(r'<hh:charPr id="(\d+)"([^>]*)>(.*?)</hh:charPr>', header, re.S):
        attrs, body, f = m.group(2), m.group(3), {}
        if '<hh:bold' in body: f['b'] = '1'
        if '<hh:italic' in body: f['i'] = '1'
        if re.search(r'<hh:underline type="(?!NONE)', body): f['u'] = '1'
        if re.search(r'<hh:strikeout shape="(?!NONE)', body): f['k'] = '1'
        c = re.search(r'textColor="([^"]+)"', attrs).group(1).upper()
        if c != '#000000': f['c'] = c
        h = re.search(r'shadeColor="([^"]+)"', attrs).group(1).upper()
        if h not in ('NONE', '#FFFFFF'): f['h'] = h
        boxed, face = bfs.get(re.search(r'borderFillIDRef="(\d+)"', attrs).group(1), (False, None))
        if boxed: f['x'] = '1'
        if face and face != '#FFFFFF': f['g'] = face
        out[m.group(1)] = f
    return out


def canon(f):
    return ','.join('%s=%s' % (k, f[k]) for k in sorted(f))


def reference(path):
    z = zipfile.ZipFile(path)
    cf = char_formats(z.read('Contents/header.xml').decode('utf-8'))
    seq, tables, nfn = [], {}, [0]

    def para(p, pid):
        k, tno, mark = 0, 0, [None]
        buf, fmt, fns = [], [], []

        def emit():
            nonlocal k, buf, fmt, fns
            if buf or fns:
                seq.append((pid if k == 0 else '%s~%d' % (pid, k), ''.join(buf), fmt))
                for num, fp in fns:
                    for j, q in enumerate(fp):
                        para(q, '%s/fn%d/p%d' % (pid, num, j))
            buf, fmt, fns, k = [], [], [], k + 1

        def put(text, cp):
            if text:
                f = dict(cf.get(cp, {}))
                if mark[0]: f['m'] = mark[0]
                buf.append(text)
                fmt.extend([canon(f)] * len(text))

        for run in p.findall(P + 'run'):
            cp = run.get('charPrIDRef')
            for el in run:
                if el.tag == P + 't':
                    put(el.text, cp)
                    for sub in el:
                        n = sub.tag[len(P):]
                        if n in INL: put(INL[n], cp)
                        elif n == 'markpenBegin': mark[0] = (sub.get('color') or '#FFFF00').upper()
                        elif n == 'markpenEnd': mark[0] = None
                        put(sub.tail, cp)
                elif el.tag == P + 'tbl':
                    emit()
                    tid = '%s/t%d' % (pid, tno)
                    tno += 1
                    shape = []
                    for tr in el.findall(P + 'tr'):
                        for tc in tr.findall(P + 'tc'):
                            a, s = tc.find(P + 'cellAddr'), tc.find(P + 'cellSpan')
                            r, c = int(a.get('rowAddr')), int(a.get('colAddr'))
                            shape.append((r, c, int(s.get('rowSpan')), int(s.get('colSpan'))))
                            for sl in tc.findall(P + 'subList'):
                                for j, q in enumerate(sl.findall(P + 'p')):
                                    para(q, '%s/r%dc%d/p%d' % (tid, r, c, j))
                    tables[tid] = (int(el.get('rowCnt')), int(el.get('colCnt')), tuple(shape))
                elif el.tag == P + 'ctrl':
                    for fnode in el:
                        if fnode.tag in (P + 'footNote', P + 'endNote'):
                            nfn[0] += 1
                            fns.append((int(fnode.get('number')),
                                        [q for sl in fnode.findall(P + 'subList') for q in sl.findall(P + 'p')]))
        emit()

    for name in sorted(n for n in z.namelist() if re.match(r'Contents/section\d+\.xml$', n)):
        sec = int(re.search(r'(\d+)\.xml$', name).group(1))
        root = ET.fromstring(z.read(name))
        for i, p in enumerate(c for c in root if c.tag == P + 'p'):
            para(p, 's%d/p%d' % (sec, i))
    return seq, tables, nfn[0]


def from_tree(data):
    fmts = [canon({k: str(v) for k, v in f.items()}) for f in data['fmts']]
    fns = data['fns']
    seq, tables, derived = [], {}, []

    def para(p):
        text = ''.join(s[0] for s in p['s'])
        fl = []
        for s in p['s']:
            fl.extend([fmts[s[1]]] * len(s[0]))
        seq.append((p['pid'], text, fl))
        for s in p['s']:
            if len(s) == 3:
                for q in fns[str(s[2])]:
                    raw(q)
        return text

    def cells(tid, rows, cols, cl):
        tables[tid] = (rows, cols, tuple((c['r'], c['c'], c['rs'], c['cs']) for c in cl))
        for c in cl:
            for item in c['ps']:
                raw(item)

    def raw(item):
        if 'tbl' in item:
            t = item['tbl']
            cells(t['pid'], t['rows'], t['cols'], t['cells'])
        else:
            text = para(item)
            art = item.get('art')
            if art:
                for a, b in [art['name'], art['ref']] + art.get('els', []):
                    derived.append((item['pid'], 0 <= a < b <= len(text)))

    def node(nd):
        if 'bxt' in nd:
            b = nd['bxt']
            tables[b['pid']] = (b['rows'], b['cols'], tuple(tuple(x) for x in b['cells']))
        if 's' in nd and 'pid' in nd:
            text = para(nd)
            if nd['k'] == 'crime' and not nd.get('syn'):
                derived.append((nd['pid'], nd['name'] in text))
            if 'term' in nd:
                derived.append((nd['pid'], nd['term'] in text))
            ox = nd.get('ox')
            if ox:
                ok = all(0 <= v < len(text) and text[v] in 'OX' for v in ox['v'])
                if 'q' in ox:
                    ok = ok and 0 <= ox['q'][0] < ox['q'][1] <= ox['a'][0] <= len(text)
                derived.append((nd['pid'], ok))
        if nd.get('k') in ('stat', 'tbl', 'case'):
            cells(nd['pid'], nd['rows'], nd['cols'], nd['cells'])
        for ch in nd.get('c', []):
            node(ch)

    for item in data['meta'].get('cover', []):
        raw(item)
    node(data['root'])
    return seq, tables, len(fns), derived


def load(path):
    if path.endswith('.json'):
        return json.load(open(path, encoding='utf-8'))
    html = open(path, encoding='utf-8').read()
    m = re.search(r'<script id="tree-data" type="application/json">(.*?)</script>', html, re.S)
    if not m:
        sys.exit('HTML에 tree-data 스크립트가 없습니다')
    return json.loads(m.group(1).replace('<\\/', '</'))


def main():
    src, out = sys.argv[1], sys.argv[2]
    rs, rt, rfn = reference(src)
    ts, tt, tfn, derived = from_tree(load(out))
    errors = []
    rp, tp = [x[0] for x in rs], [x[0] for x in ts]
    if rp != tp:
        i = next((i for i, (a, b) in enumerate(zip(rp, tp)) if a != b), min(len(rp), len(tp)))
        errors.append('(a) 문단 순서 불일치 @%d: 원문 %s / 트리 %s (원문 %d, 트리 %d개)'
                      % (i, rp[i:i + 3], tp[i:i + 3], len(rp), len(tp)))
    if len(set(tp)) != len(tp):
        errors.append('(a) 중복 pid')
    rmap = {x[0]: x for x in rs}
    for pid, text, fl in ts:
        r = rmap.get(pid)
        if r is None:
            continue
        if r[1] != text:
            errors.append('(b) 텍스트 불일치 %s: %r ≠ %r' % (pid, r[1][:60], text[:60]))
        elif r[2] != fl:
            j = next(i for i, (a, b) in enumerate(zip(r[2], fl)) if a != b) if len(r[2]) == len(fl) else -1
            errors.append('(c) 서식 불일치 %s @%d' % (pid, j))
    if rt != tt:
        miss = sorted(set(rt) ^ set(tt))
        bad = sorted(k for k in set(rt) & set(tt) if rt[k] != tt[k])
        errors.append('(d) 표 불일치: 누락/초과 %s, 모양 다름 %s' % (miss[:5], bad[:5]))
    if rfn != tfn:
        errors.append('(e) 각주 수 불일치: 원문 %d / 트리 %d' % (rfn, tfn))
    badd = [pid for pid, ok in derived if not ok]
    if badd:
        errors.append('(f) 파생 데이터 범위 오류 %d건: %s' % (len(badd), badd[:5]))
    chars = sum(len(x[1]) for x in rs)
    print('원문 조각 %d개, %d자, 표 %d개, 각주 %d개, 파생 검사 %d건'
          % (len(rs), chars, len(rt), rfn, len(derived)))
    if errors:
        for e in errors[:30]:
            print('FAIL', e)
        print('총 %d건 실패' % len(errors))
        sys.exit(1)
    print('OK — 순서·텍스트·서식·표·각주·파생 데이터 모두 일치 (무손실)')


if __name__ == '__main__':
    main()
