#!/usr/bin/env python3
"""Проверка относительных ссылок в markdown-документации.

Документация живёт в README.md, *.md в корне и docs/*.md. Ссылки вида
`[INSTALL.md](INSTALL.md)` молча устаревают при переезде файла — а «инструкция,
которая ведёт в никуда» в проекте с деньгами хуже, чем отсутствие инструкции.
Скрипт проверяет, что все относительные цели существуют (и что у них есть
якоря-заголовки, если они заявлены).

Запуск: python tools/check_docs_links.py      (0 — всё ок, 1 — есть битые ссылки)
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LINK_RE = re.compile(r"!?\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
CODE_FENCE = re.compile(r"^```")


def md_files() -> list[Path]:
    files = [ROOT / "README.md"]
    files += sorted(p for p in ROOT.glob("*.md"))
    files += sorted((ROOT / "legacy").glob("*.md"))
    files += sorted((ROOT / "docs").glob("*.md"))
    return [p for p in dict.fromkeys(files) if p.exists()]


def anchors_of(path: Path) -> set[str]:
    """GitHub-стиль якорей: lowercase, пробелы → '-', не-буквы выкидываются."""
    result: set[str] = set()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return result
    for line in text.splitlines():
        match = re.match(r"^#{1,6}\s+(.*)$", line.strip())
        if not match:
            continue
        title = match.group(1).strip()
        title = re.sub(r"`|\*|_|#|\[|\]|\(|\)|✕|✅|🌟|🚀|💰|📁|📊|⚙️|🧪|🔒|📄|✨|🤖|🛡️|🗂|📱|💬|📦|📝|📈|⚡|📲|🧯|🪟|🏗|📚|🤝|📓", "", title)
        slug = re.sub(r"[^\w\- ]", "", title, flags=re.UNICODE).strip().lower().replace(" ", "-")
        if slug:
            result.add(slug)
    return result


def main() -> int:
    broken: list[str] = []
    checked = 0
    for file in md_files():
        in_fence = False
        for lineno, line in enumerate(file.read_text(encoding="utf-8").splitlines(), 1):
            if CODE_FENCE.match(line.strip()):
                in_fence = not in_fence
                continue
            if in_fence:
                continue
            for target in LINK_RE.findall(line):
                if target.startswith(("#", "mailto:")):
                    continue
                if re.match(r"^[a-z]+://", target):
                    continue  # внешние ссылки проверяются людьми/ссылкой в docs
                checked += 1
                raw_path, _, anchor = target.partition("#")
                if raw_path.startswith(("http://", "https://")):
                    continue
                candidate = (file.parent / raw_path).resolve() if raw_path else file
                if not candidate.exists():
                    broken.append(f"{file.relative_to(ROOT)}:{lineno} → {target} (файл не найден)")
                    continue
                if anchor and candidate.suffix == ".md":
                    slug = anchor.strip().lower().replace("%D0%B0", "а")  # наивная страховка
                    if slug and slug not in anchors_of(candidate):
                        broken.append(
                            f"{file.relative_to(ROOT)}:{lineno} → {target} (нет заголовка «{anchor}»)"
                        )

    if broken:
        print(f"⚠️  битых ссылок: {len(broken)}")
        for item in broken:
            print(f"   - {item}")
        print("\nПоправьте документацию — инструкция без работающей ссылки хуже, чем её отсутствие.")
        return 1
    print(f"✅ Ссылки в документации в порядке (проверено относительных ссылок: {checked})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
