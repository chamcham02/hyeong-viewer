#!/usr/bin/env python3
"""leakcheck — 공개 레포에 원문 내용이 섞여 들어가지 않았는지 검사한다.

원문(.hwpx)의 모든 문단에서 공백을 뺀 20자 조각을 만들고, 레포의 텍스트 파일(.git 제외)에
같은 조각이 하나라도 있으면 실패(종료 코드 1)한다. 공개 레포에 push하기 전에 실행한다.

    python3 -I tools/leakcheck.py 원문.hwpx [레포 경로=.] [--window 20]
"""
import argparse
import os
import re
import sys
import zipfile
import xml.etree.ElementTree as ET

P = '{http://www.hancom.co.kr/hwpml/2011/paragraph}'
SKIP_DIRS = {'.git', 'node_modules', '__pycache__'}
TEXT_EXT = {'.py', '.js', '.mjs', '.css', '.html', '.htm', '.json', '.md', '.txt', '.sh', '.yml', '.yaml', '.toml', '.cfg', ''}


def source_windows(path, w):
    z = zipfile.ZipFile(path)
    wins = set()
    for name in z.namelist():
        if not re.match(r'Contents/section\d+\.xml$', name):
            continue
        root = ET.fromstring(z.read(name))
        for p in root.iter(P + 'p'):
            t = ''.join((x.text or '') + ''.join(c.tail or '' for c in x) for x in p.iter(P + 't'))
            t = re.sub(r'\s+', '', t)
            for i in range(0, max(0, len(t) - w + 1)):
                wins.add(t[i:i + w])
    return wins


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('src')
    ap.add_argument('repo', nargs='?', default='.')
    ap.add_argument('--window', type=int, default=20)
    a = ap.parse_args()
    wins = source_windows(a.src, a.window)
    hits = []
    files = 0
    for d, dirs, names in os.walk(a.repo):
        dirs[:] = [x for x in dirs if x not in SKIP_DIRS]
        for n in names:
            fp = os.path.join(d, n)
            if os.path.splitext(n)[1].lower() not in TEXT_EXT or os.path.getsize(fp) > 50_000_000:
                continue
            try:
                text = open(fp, encoding='utf-8').read()
            except (UnicodeDecodeError, OSError):
                continue
            files += 1
            t = re.sub(r'\s+', '', text)
            for i in range(0, max(0, len(t) - a.window + 1)):
                if t[i:i + a.window] in wins:
                    hits.append((fp, t[i:i + a.window]))
                    break
    print('원문 조각 %d개, 검사한 파일 %d개' % (len(wins), files))
    if hits:
        for fp, s in hits:
            print('LEAK', fp, '…%s…' % s)
        sys.exit(1)
    print('OK — 원문 내용이 없습니다')


if __name__ == '__main__':
    main()
