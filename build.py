#!/usr/bin/env python3
"""build — viewer(HTML/CSS/JS)와 tree.json을 합쳐 오프라인 단일 HTML 파일을 만든다.

    python3 -I build.py --data tree.json --out 결과.html [--title "형법각칙 마인드맵"]
"""
import argparse
import html
import json
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))


def build(data_path, out_path, title=None):
    viewer = os.path.join(HERE, 'viewer')
    page = open(os.path.join(viewer, 'index.html'), encoding='utf-8').read()
    css = open(os.path.join(viewer, 'style.css'), encoding='utf-8').read()
    js = open(os.path.join(viewer, 'app.js'), encoding='utf-8').read()
    data = json.load(open(data_path, encoding='utf-8'))
    blob = json.dumps(data, ensure_ascii=False, separators=(',', ':')).replace('</', '<\\/')
    if '</script' in js.lower():
        raise SystemExit('app.js에 </script 문자열이 있어 인라인할 수 없습니다')
    name = title or ((data.get('meta') or {}).get('name') or '정리자료') + ' 마인드맵'
    page = page.replace('<link rel="stylesheet" href="style.css">', '<style>\n' + css + '\n</style>')
    page = page.replace('<script src="app.js"></script>', '<script>\n' + js + '\n</script>')
    page = page.replace('<script id="tree-data" type="application/json"></script>',
                        '<script id="tree-data" type="application/json">' + blob + '</script>')
    page = re.sub(r'<title>.*?</title>', '<title>' + html.escape(name) + '</title>', page, count=1)
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(page)
    return os.path.getsize(out_path)


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--data', required=True)
    ap.add_argument('--out', required=True)
    ap.add_argument('--title')
    a = ap.parse_args()
    size = build(a.data, a.out, a.title)
    print('%s (%.1f KB)' % (a.out, size / 1024))
