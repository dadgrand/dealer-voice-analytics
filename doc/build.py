# -*- coding: utf-8 -*-
"""Сборка документа: главы + инлайн SVG + оглавление -> out/document.html"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOC, DIAG, OUT = ROOT / "doc", ROOT / "diagrams", ROOT / "out"

CHAPTERS = [
    "ch00_title.html", "ch01_task.html", "ch02_decisions.html", "ch03_hardware.html",
    "ch04_audio.html", "ch05_cloud.html", "ch06_pipeline.html", "ch07_crm.html",
    "ch08_costs.html", "ch09_realtime.html", "ch10_security.html", "ch11_rollout.html",
    "ch12_roi.html", "ch13_appendix.html",
]

TOC = [
    ("#summary", "Резюме решения", False),
    ("#ch1", "1. Постановка задачи, модель нагрузки, допущения", False),
    ("#ch2", "2. Ключевые архитектурные решения", False),
    ("#ch3", "3. Хардвер: бейдж, док-станция, площадочный шлюз", False),
    ("#ch4", "4. Аудиотракт и разделение голосов", False),
    ("#ch5", "5. Облачная архитектура и стек", False),
    ("#ch6", "6. Пайплайн: путь данных от микрофона до CRM", False),
    ("#ch7", "7. CRM-модуль", False),
    ("#ch8", "8. Расчёты", False),
    ("#ch9", "9. Этап 2: подсказки в реальном времени", False),
    ("#ch10", "10. Надёжность, безопасность, право", False),
    ("#ch11", "11. Внедрение и человеческий фактор", False),
    ("#ch12", "12. Почему это будет полезно: модель ценности", False),
    ("#appA", "Приложение А. BOM бейджа: партномера", True),
    ("#appB", "Приложение Б. Цены и бенчмарки: источники", True),
    ("#appC", "Приложение В. Схема LLM-разбора", True),
]


def inline_svg(html: str) -> str:
    def repl(m):
        name = m.group(1)
        svg = (DIAG / f"{name}.svg").read_text(encoding="utf-8")
        return svg.replace('<?xml version="1.0"?>', "")
    return re.sub(r"<!--SVG:([\w]+)-->", repl, html)


def toc_html() -> str:
    items = []
    for href, title, is_app in TOC:
        cls = ' class="app"' if is_app else ""
        items.append(f'<li{cls}><a href="{href}"><span>{title}</span><span class="toc-line"></span></a></li>')
    return '<section class="toc"><h2>Содержание</h2><ol>' + "\n".join(items) + "</ol></section>"


def build() -> Path:
    css = (DOC / "style.css").read_text(encoding="utf-8")
    body = []
    for i, ch in enumerate(CHAPTERS):
        html = (DOC / ch).read_text(encoding="utf-8")
        body.append(inline_svg(html))
        if i == 0:  # оглавление после титула+резюме
            body.append(toc_html())
    paged = (DOC / "paged.polyfill.js").read_text(encoding="utf-8")
    hook = ("window.PagedConfig = { auto: true, after: () => { "
            "window.__pagedDone = true; console.log('PAGED_DONE'); } };")
    doc = f"""<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8">
<title>Система аналитики живых разговоров в дилерском центре</title>
<style>{css}</style></head>
<body>
{''.join(body)}
<script>{hook}</script>
<script>{paged}</script>
</body></html>"""
    OUT.mkdir(exist_ok=True)
    out = OUT / "document.html"
    out.write_text(doc, encoding="utf-8")
    print(f"OK: {out} ({out.stat().st_size/1024:.0f} KB)")
    return out


if __name__ == "__main__":
    build()
