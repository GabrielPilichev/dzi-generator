---
title: Stub topics audit
type: audit
tags: [audit, stub-topics]
---

# Stub topics audit

Topics with zero approved questions: **7**.

An approved question satisfies `is_ai_generated = 0 OR quality_score >= 1.0`.
Topics here render in the UI but contribute nothing to quiz pools.

## AI и програмиране

| Class | Slug | Title | AI-pending | Sections used |
|-------|------|-------|-----------:|---------------|
| 9 | `machine-learning` | Машинно самообучение | 0 | 9кл/grade9-programming-and-ai, 10кл/grade10-creating-content |
| 10 | `dataset` | Набор от данни (dataset) | 0 | 10кл/grade10-creating-content |

## Бази данни

| Class | Slug | Title | AI-pending | Sections used |
|-------|------|-------|-----------:|---------------|
| 11 | `access-data-entry-objects` | Обекти за въвеждане на данни в БД | 0 | 11кл/grade11-m1-databases-and-information-systems |

## Графика и обработка на изображения

| Class | Slug | Title | AI-pending | Sections used |
|-------|------|-------|-----------:|---------------|
| 11 | `magic-wand` | Магическа пръчка | 0 | 11кл/grade11-m2-raster-images |

## Компютърни мрежи и услуги

| Class | Slug | Title | AI-pending | Sections used |
|-------|------|-------|-----------:|---------------|
| 9 | `local-network` | Локална мрежа | 0 | 9кл/grade9-computer-networks-and-services |

## Уеб технологии

| Class | Slug | Title | AI-pending | Sections used |
|-------|------|-------|-----------:|---------------|
| 12 | `css-selectors` | CSS селектори | 0 | 12кл/grade12-m3-build-test-publish-website |

## Хардуер

| Class | Slug | Title | AI-pending | Sections used |
|-------|------|-------|-----------:|---------------|
| 8 | `device-manager` | Диспечер за управление на хардуера | 1 | 8кл/grade8-computer-systems |

## Notes

- A non-zero `AI-pending` count means there are AI-generated questions waiting for review (`is_ai_generated = 1` and `quality_score < 1.0`). Run `src/review_export.py` to surface them, then `src/review_import.py` after manual approval.
- Topics with `AI-pending = 0` need either (a) human-authored questions, (b) an AI generation pass via `src/generate_questions.py`, or (c) explicit documentation that the topic is intentionally questionless.
