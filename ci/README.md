# CI — GitHub Actions

`github-ci.yml` — полный конвейер проекта (ruff, `compileall`, pytest на 3.10/3.11/3.12,
смоук CLI в изолированной папке, GUI-смоук, legacy-тесты, проверка ссылок в
документации, `docker build`).

Файл лежит здесь, а не в `.github/workflows/`, по технической причине: GitHub не
позволяет токену без права `workflows` создавать файлы в `.github/workflows/`.
Содержимое при этом рабочее — его нужно один раз скопировать:

```bash
mkdir -p .github/workflows
cp ci/github-ci.yml .github/workflows/ci.yml
git add .github/workflows/ci.yml && git commit -m "ci: включить GitHub Actions" && git push
```

После этого Actions стартует на каждом push и pull request (проверить:
`gh run list` / вкладка Actions в репозитории).

Что делают джобы:

| Джоба | Проверки | Блокирует merge? |
|---|---|---|
| `core` | `pip install -e ".[dev]"` → ruff, `compileall`, `pytest -q` (247 тестов), смоук CLI (`--calc`, `--config-show`, `--stats --json`, guard `--test-order`), `check_env.py`, `tools/check_docs_links.py` — на Python 3.10/3.11/3.12 | да |
| `gui` | установка `.[gui,dev]`, `tools/gui_smoke.py` (окно не открывается), `pytest tests/test_gui_bridge.py tests/test_gui_settings.py` | да |
| `legacy` | `pytest legacy/tests` c `FunPayAPI --no-deps` | нет (`continue-on-error`) |
| `docker` | `docker build` + `--help` внутри образа | да |

Сеть в CI есть, но ключей нет: тесты и смоуки намеренно не делают реальных
запросов к FunPay/GAMEAU (см. [docs/TESTING.md](../docs/TESTING.md)).
