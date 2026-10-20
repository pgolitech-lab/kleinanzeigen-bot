#!/usr/bin/env bash
# Cache-bust Mini App: пересобирает import map в web-app/index.html.
#
# Каждый JS-модуль получает ?v=<хэш содержимого> — меняется только у реально
# изменённых файлов, остальное Telegram/браузер берёт из кэша. В самих .js
# импорты пишутся БЕЗ ?v (import "../api.js") — версию подставляет import map.
# Точка входа (app.js) и app.css версионируются прямо в index.html.
#
# Запуск после любой правки web-app/ перед коммитом:
#   scripts/bump_cache.sh          # переписать index.html
#   scripts/bump_cache.sh --check  # только проверить (exit 1, если устарел)
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
exec python3 - "$ROOT/web-app" "${1:-}" <<'PY'
import hashlib
import json
import re
import sys
from pathlib import Path

webapp = Path(sys.argv[1])
check = sys.argv[2] == "--check"
index = webapp / "index.html"


def ver(p: Path) -> str:
    return hashlib.sha1(p.read_bytes()).hexdigest()[:10]


js_files = sorted((webapp / "js").rglob("*.js"))
if not js_files:
    sys.exit(f"нет JS-файлов в {webapp / 'js'}")

# Версионированные импорты внутри модулей ломают схему (обходят import map)
stale = [str(p.relative_to(webapp)) for p in js_files
         if re.search(r"""from\s+["'][^"']+\?v=|import\(\s*["'][^"']+\?v=""", p.read_text())]
if stale:
    sys.exit("импорты с ?v= внутри модулей (уберите, версию даёт import map): " + ", ".join(stale))

imports = {}
for p in js_files:
    rel = "./" + p.relative_to(webapp).as_posix()
    imports[rel] = f"{rel}?v={ver(p)}"
block = json.dumps({"imports": imports}, indent=2, ensure_ascii=False)
block = "\n".join("  " + line for line in block.splitlines())

html = index.read_text()
if '<script type="importmap">' not in html:
    sys.exit('в index.html нет <script type="importmap">')
new = re.sub(r'(<script type="importmap">\n).*?(\n\s*</script>)',
             lambda m: m.group(1) + block + m.group(2), html, count=1, flags=re.S)
new, n_entry = re.subn(r'src="js/app\.js(\?v=[^"]*)?"',
                       f'src="js/app.js?v={ver(webapp / "js" / "app.js")}"', new)
new, n_css = re.subn(r'href="css/app\.css(\?v=[^"]*)?"',
                     f'href="css/app.css?v={ver(webapp / "css" / "app.css")}"', new)
if n_entry != 1 or n_css != 1:
    sys.exit(f"index.html: ожидалась ровно одна ссылка на js/app.js и css/app.css (нашли {n_entry}/{n_css})")

if new == html:
    print("index.html актуален")
    sys.exit(0)
if check:
    sys.exit("index.html устарел — запустите scripts/bump_cache.sh")
index.write_text(new)
print(f"index.html обновлён: {len(imports)} модулей в import map")
PY
