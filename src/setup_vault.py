"""
Setup на Obsidian vault за DZI Generator.

Какво прави:
  1. Създава 7-те type folders (Daily, MOCs, Topics, Lessons, Resources, Generated, _Attachments)
  2. Подпапки в Generated/ (Изпити, Работни листове, Домашни)
  3. Подпапки в Resources/ (Книги, Статии, Видеа)
  4. Home.md с навигационни линкове
  5. 13 MOC файла (5 за класове + 6 тематични + ДЗИ + НВО 10 + Pedagogy)
  6. 2 templates: topic-template.md, daily-template.md в _Templates/
  7. Default README.md в _Attachments/ (за git tracking)

Употреба:
    python3 setup_vault.py [--vault PATH]

По подразбиране vault-ът е ~/dzi-generator/vault.
Скриптът е idempotent — пуска се много пъти безопасно (не презаписва съществуващи файлове).
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from datetime import datetime


# ============================================================
# Folder structure
# ============================================================

FOLDERS = [
    "Daily",
    "MOCs",
    "Topics",
    "Lessons",
    "Resources",
    "Resources/Books",
    "Resources/Articles",
    "Resources/Videos",
    "Generated",
    "Generated/Exams",
    "Generated/Worksheets",
    "Generated/Homework",
    "_Attachments",
    "_Templates",
]


# ============================================================
# File templates
# ============================================================

HOME_MD = """---
title: Начало
type: home
---

# Начало

Това е началната точка на vault-а за DZI Generator. Оттук намираш всичко.

## По класове

- [[klas-8-moc|8 клас]]
- [[klas-9-moc|9 клас]]
- [[klas-10-moc|10 клас]]
- [[klas-11-moc|11 клас]]
- [[klas-12-moc|12 клас]]

## По тематични области

- [[spreadsheets-moc|Електронни таблици]]
- [[databases-moc|Бази данни]]
- [[web-moc|Уеб технологии]]
- [[graphics-moc|Графика и обработка на изображения]]
- [[video-audio-moc|Видео и аудио]]
- [[info-systems-moc|Информационни системи]]

## Изпити

- [[dzi-moc|ДЗИ — Държавен зрелостен изпит]]
- [[nvo-10-moc|НВО — 10 клас]]

## Работни области

- [[pedagogy-moc|Педагогика и методика]]
- [[Daily/]] — ежедневни бележки
- [[Topics/]] — атомарни концепти
- [[Lessons/]] — урочни планове
- [[Generated/]] — генерирани материали от системата

## Бързи връзки

- Днешна бележка: `Cmd+P` → "Daily notes: Open today's note"
- Нова тема: copy `_Templates/topic-template.md` → Topics/
- Търсене: `Cmd+Shift+F`

---

*Vault setup: {date}*
"""


# ============================================================
# MOC templates
# ============================================================

MOC_KLAS = """---
title: {title}
type: moc
class: {class_num}
tags: [moc, klas{class_num}]
---

# {title}

MOC за {title} — обзор на всички теми, които се учат, и линкове към атомарните бележки в Topics/.

## Учебни области

> Попълни тук кои тематични области се учат в този клас, със срок и брой часове. Например:
>
> - **1 срок:** [[spreadsheets-moc|Електронни таблици]] — 18 часа
> - **2 срок:** [[web-moc|Уеб]] — 14 часа

## Teми (атомарни)

> Линкни тук всички бележки от Topics/, които се учат в този клас.
> Може да ползваш Dataview след като инсталираш плъгина:
>
> ```dataview
> LIST FROM "Topics" WHERE contains(class, {class_num})
> ```

## Учебници и ресурси

> Препоръчителни и допълнителни ресурси за този клас.

## Изпити, които покрива

> НВО, ДЗИ, годишни тестове...

---

*Връзки:* [[Home|← Начало]]
"""


MOC_TEMA = """---
title: {title}
type: moc
topic_area: {topic_id}
tags: [moc, {topic_id}]
---

# {title}

MOC за тематична област "{title}". Прогресията на темата през класовете и атомарните концепти.

## Прогресия по класове

> Попълни как се развива темата от 8 към 12 клас. Например за Електронни таблици:
>
> - **8 клас:** Базови формули, формат на клетки, прости функции
> - **9 клас:** Условни функции (SUMIF, COUNTIF), VLOOKUP
> - **10 клас:** Pivot tables, обобщаващи таблици, графики
> - **11-12 клас:** Сложни анализи, връзка с бази данни, макроси

## Атомарни концепти (Topics/)

> Линкни всичко от Topics/ което попада в тази област.

## Връзки с други области

> Например ЕТ ↔ Бази данни (импорт/експорт), Web ↔ Графика (responsive images)

## Изпитна релевантност

- ДЗИ — да/не, кои задачи
- НВО 10 — да/не

---

*Връзки:* [[Home|← Начало]]
"""


MOC_DZI = """---
title: ДЗИ — Държавен зрелостен изпит
type: moc
exam_type: DZI
tags: [moc, dzi, exam]
---

# ДЗИ — Държавен зрелостен изпит по Информационни технологии

Всичко за матурата — формат, теми, стратегия, линкове към архивирани матури.

## Формат

- **Част 1:** 90 минути — 15 multiple choice + 10 fill-in (~65 точки)
- **Част 2:** 150 минути — 3 практически задачи (Excel, графика, HTML/CSS) (~35 точки)
- **Общо:** 100 точки

## Тематични области в изпита

- [[spreadsheets-moc|Електронни таблици]] — често 5-7 въпроса в Част 1, винаги 1 практическа в Част 2
- [[databases-moc|Бази данни]] — 3-5 въпроса
- [[web-moc|Уеб]] — 4-6 въпроса, винаги 1 практическа
- [[graphics-moc|Графика]] — 2-4 въпроса, винаги 1 практическа
- [[video-audio-moc|Видео и аудио]] — 2-3 въпроса
- [[info-systems-moc|Информационни системи]] — 2-3 въпроса

## Архив на матури

> Линкове към PDF-те и парсваните DB записи.

## Тестови варианти, които съм създал

> Когато системата генерира пробен ДЗИ, той се записва в Generated/Exams/ и се линква оттук.

---

*Връзки:* [[Home|← Начало]] | [[nvo-10-moc|НВО 10]]
"""


MOC_NVO_10 = """---
title: НВО — 10 клас
type: moc
exam_type: NVO_10
tags: [moc, nvo, klas10, exam]
---

# НВО — 10 клас по Информационни технологии

Информация за националното външно оценяване в 10 клас.

## Формат

> Попълни тук точния формат — брой задачи, време, точки.

## Сравнение с ДЗИ

> Какви теми се припокриват, в какво се различава трудността.

---

*Връзки:* [[Home|← Начало]] | [[dzi-moc|ДЗИ]]
"""


MOC_PEDAGOGY = """---
title: Педагогика и методика
type: moc
tags: [moc, pedagogy]
---

# Педагогика и методика

Бележки и материали свързани с преподаването — стратегии, наблюдения от часовете, методи, връзка с магистратурата (МП ПОМФИ).

## Активни тематични области

- Конструктивистка педагогика
- Bloom's Taxonomy в ИТ контекст
- Оценяване и обратна връзка
- Диференцирано обучение

## Магистратура (МП ПОМФИ — ТУ София)

> Линкове към coursework и курсови работи.

## Наблюдения от часовете

> Когато пишеш `Daily/` бележка, която съдържа педагогическо наблюдение,
> добави tag `#pedagogy` и тя ще се появи тук в graph view-а.

---

*Връзки:* [[Home|← Начало]]
"""


# ============================================================
# Topic and daily templates
# ============================================================

TOPIC_TEMPLATE = """---
title: 
aliases: []
type: topic
parent_topic: 
class: []
concepts: []
prerequisites: []
bloom: 
exam_relevance: []
tags: []
---

# 

## Кратко описание

> Една-две изречения за концепцията, разбираеми от ученик.

## Кога се учи

> 8 клас, 9 клас 2 срок, и т.н.

## Ключови концепти

- 

## Примери

```

```

## Често срещани грешки на учениците

- 

## Свързани въпроси (АРХИВ)

> Когато системата свърже въпроси с тази бележка, тук ще се появят links.

## Връзки с други теми

- 
"""


DAILY_TEMPLATE = """---
date: {date}
type: daily
tags: []
---

# {date}

## Часове днес

- 

## Наблюдения

- 

## TODO

- [ ] 
"""


# ============================================================
# MOC definitions (filename, template, params)
# ============================================================

CLASS_MOCS = [
    ("klas-8-moc.md", MOC_KLAS, {"title": "8 клас", "class_num": 8}),
    ("klas-9-moc.md", MOC_KLAS, {"title": "9 клас", "class_num": 9}),
    ("klas-10-moc.md", MOC_KLAS, {"title": "10 клас", "class_num": 10}),
    ("klas-11-moc.md", MOC_KLAS, {"title": "11 клас", "class_num": 11}),
    ("klas-12-moc.md", MOC_KLAS, {"title": "12 клас", "class_num": 12}),
]

TEMA_MOCS = [
    ("spreadsheets-moc.md", MOC_TEMA, {"title": "Електронни таблици", "topic_id": "spreadsheets"}),
    ("databases-moc.md", MOC_TEMA, {"title": "Бази данни", "topic_id": "databases"}),
    ("web-moc.md", MOC_TEMA, {"title": "Уеб технологии", "topic_id": "web"}),
    ("graphics-moc.md", MOC_TEMA, {"title": "Графика и обработка на изображения", "topic_id": "graphics"}),
    ("video-audio-moc.md", MOC_TEMA, {"title": "Видео и аудио", "topic_id": "video_audio"}),
    ("info-systems-moc.md", MOC_TEMA, {"title": "Информационни системи", "topic_id": "info_systems"}),
]

EXAM_MOCS = [
    ("dzi-moc.md", MOC_DZI, {}),
    ("nvo-10-moc.md", MOC_NVO_10, {}),
]

OTHER_MOCS = [
    ("pedagogy-moc.md", MOC_PEDAGOGY, {}),
]


# ============================================================
# Main
# ============================================================

def write_if_missing(path: Path, content: str) -> bool:
    """Записва файл само ако още не съществува. Връща True ако създаден."""
    if path.exists():
        return False
    path.write_text(content, encoding="utf-8")
    return True


def setup(vault: Path) -> None:
    if not vault.exists():
        print(f"❌ Vault folder не съществува: {vault}")
        print(f"   Създай го първо: mkdir -p {vault}")
        return
    
    print(f"📂 Vault: {vault}")
    
    # 1. Create folders
    print(f"\n📁 Създавам папки...")
    for folder in FOLDERS:
        (vault / folder).mkdir(parents=True, exist_ok=True)
        print(f"   ✓ {folder}/")
    
    # 2. Home
    print(f"\n📄 Home.md...")
    home = vault / "Home.md"
    if write_if_missing(home, HOME_MD.format(date=datetime.now().strftime("%Y-%m-%d"))):
        print(f"   ✓ Home.md създаден")
    else:
        print(f"   ⏭️  Home.md вече съществува, пропускам")
    
    # 3. MOCs
    print(f"\n📄 MOCs...")
    all_mocs = CLASS_MOCS + TEMA_MOCS + EXAM_MOCS + OTHER_MOCS
    for fname, template, params in all_mocs:
        path = vault / "MOCs" / fname
        content = template.format(**params) if params else template
        if write_if_missing(path, content):
            print(f"   ✓ MOCs/{fname}")
        else:
            print(f"   ⏭️  MOCs/{fname}")
    
    # 4. Templates
    print(f"\n📄 Templates...")
    topic_tpl = vault / "_Templates" / "topic-template.md"
    if write_if_missing(topic_tpl, TOPIC_TEMPLATE):
        print(f"   ✓ _Templates/topic-template.md")
    else:
        print(f"   ⏭️  _Templates/topic-template.md")
    
    daily_tpl = vault / "_Templates" / "daily-template.md"
    if write_if_missing(daily_tpl, DAILY_TEMPLATE.format(date="{{date:YYYY-MM-DD}}")):
        print(f"   ✓ _Templates/daily-template.md")
    else:
        print(f"   ⏭️  _Templates/daily-template.md")
    
    # 5. README in _Attachments (for git tracking)
    readme = vault / "_Attachments" / "README.md"
    if write_if_missing(readme, "# _Attachments\n\nPDFs, изображения и други файлове, прикачени към бележките.\n"):
        print(f"   ✓ _Attachments/README.md")
    
    print(f"\n✅ Готово!")
    print(f"\nСледващи стъпки в Obsidian:")
    print(f"   1. Cmd+R за refresh на vault-а")
    print(f"   2. Отвори Home.md от sidebar-а")
    print(f"   3. Settings → Core plugins → Enable Daily Notes, Templates")
    print(f"   4. Settings → Templates → Template folder location: _Templates")
    print(f"   5. Settings → Daily notes → Date format: YYYY-MM-DD, Folder: Daily")
    print(f"   6. (Optional) Settings → Community plugins → Browse → Dataview")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--vault", type=Path,
                   default=Path.home() / "dzi-generator" / "vault")
    args = p.parse_args()
    setup(args.vault)


if __name__ == "__main__":
    main()
