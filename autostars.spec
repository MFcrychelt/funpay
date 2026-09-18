# -*- mode: python ; coding: utf-8 -*-
"""Сборка exe автовыдачи (CLI) для Windows: `pyinstaller autostars.spec`.

Результат: dist/AutoStarsBot.exe — цикл автовыдачи и весь CLI
(--check, --stats, --report, --balance, --pause/--resume, --retry-order).

GUI (flet) собирается отдельно через `flet pack` (см. build.bat и
docs/WINDOWS_BUILD.md): flet требует Flutter-рантайм, и `flet pack` умеет
приложить его правильно, а «голый» PyInstaller — нет.

Конфигурация (.env), база (autostars.db) и лог (autostars.log) лежат РЯДОМ с
exe-файлом и внутрь сборки не попадают: пути разрешаются относительно
каталога исполняемого файла (autostars/paths.py).
"""

from pathlib import Path

ROOT = Path(SPECPATH).resolve()

datas = [
    (str(ROOT / ".env.example"), "."),
]
if (ROOT / "config.example.json").exists():
    datas.append((str(ROOT / "config.example.json"), "."))

icon = str(ROOT / "ico.ico") if (ROOT / "ico.ico").exists() else None

a_bot = Analysis(
    [str(ROOT / "autostars_bot.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=[
        "aiosqlite",
        "bs4",
        "httpx",
        "httpx._transports.default",
        "autostars",
        "autostars.main",
        "autostars.services.bot_control",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "numpy", "scipy", "pandas", "flet"],
    noarchive=False,
)

pyz_bot = PYZ(a_bot.pure)

EXE(
    pyz_bot,
    a_bot.scripts,
    a_bot.binaries,
    a_bot.datas,
    [],
    name="AutoStarsBot",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    icon=icon,
    onefile=True,
)
