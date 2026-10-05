"""Canonical registry of LearnPilot subjects.

One list of static product metadata: identity, route slug, Bulgarian copy,
display order, declared grades, the educational paths of each grade and feature
flags. Availability of content is never stored here; it is derived from the
curriculum records (see ``src.subject_browsing.subject_content_grades``).

Lives in ``src/`` so importer tooling can use it without importing ``web.app``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Iterable, Mapping

IT = "it"
PHYSICS = "physics"

PLATFORM_GRADES = (8, 9, 10, 11, 12)

# Educational paths of a grade in the official МОН curriculum. A grade may have
# both; a platform grade without any is one where the subject is not studied.
GENERAL = "general"    # общообразователна подготовка
PROFILED = "profiled"  # профилирана подготовка (XI-XII клас)
GRADE_PATHS = (GENERAL, PROFILED)


def _empty_groups() -> Mapping[int, tuple[dict, ...]]:
    return MappingProxyType({})


def _empty_paths() -> Mapping[int, tuple[str, ...]]:
    return MappingProxyType({})


def _empty_supported_paths() -> Mapping[int, str]:
    return MappingProxyType({})


def grade_paths(general: Iterable[int] = (),
                profiled: Iterable[int] = ()) -> Mapping[int, tuple[str, ...]]:
    """Read-only grade -> educational paths map, in ascending grade order.

    A grade named in both arguments has both paths, general first.
    """
    paths: dict[int, tuple[str, ...]] = {}
    for path, grades in ((GENERAL, general), (PROFILED, profiled)):
        for grade in grades:
            paths[grade] = (*paths.get(grade, ()), path)
    return MappingProxyType(dict(sorted(paths.items())))


@dataclass(frozen=True)
class SubjectDefinition:
    id: str
    route_slug: str
    title: str
    description: str
    search_terms: str
    # Grades LearnPilot supports: they have routes and accept imported content.
    grades: tuple[int, ...] = PLATFORM_GRADES
    # Official educational paths of each grade the subject is studied in. This
    # is not content availability: a general grade may still have no content,
    # and a profiled path may be supported (IT) or outside LearnPilot's scope.
    grade_paths: Mapping[int, tuple[str, ...]] = field(default_factory=_empty_paths)
    # Which path LearnPilot supports, only for supported grades with several.
    supported_paths: Mapping[int, str] = field(default_factory=_empty_supported_paths)
    # Grades whose general education exists only under some framework plans.
    framework_plan_dependent_grades: tuple[int, ...] = ()
    legacy_aliases: tuple[str, ...] = ()
    active_description: str | None = None
    dzi: bool = False
    # Navigation-only placeholders for grades that have no database content yet.
    placeholder_topic_groups: Mapping[int, tuple[dict, ...]] = field(default_factory=_empty_groups)

    def paths_for(self, grade: object) -> tuple[str, ...]:
        """Every educational path of a grade; empty when the subject is not studied in it."""
        return tuple(self.grade_paths.get(grade, ())) if type(grade) is int else ()

    def supported_path(self, grade: object) -> str | None:
        """The path LearnPilot supports in a grade; None for an unsupported grade."""
        if type(grade) is not int or grade not in self.grades:
            return None
        paths = self.paths_for(grade)
        if len(paths) == 1:
            return paths[0]
        return self.supported_paths.get(grade)

    @property
    def profiled_grades_outside_scope(self) -> tuple[int, ...]:
        """Grades with a profiled path that LearnPilot does not support."""
        return tuple(
            grade for grade, paths in self.grade_paths.items()
            if PROFILED in paths and self.supported_path(grade) != PROFILED
        )


_PHYSICS_TOPIC_GROUPS_BY_GRADE = MappingProxyType({
    8: (
        {"slug": "mechanics", "title": "Механика", "status": "in_preparation"},
        {"slug": "thermal-phenomena", "title": "Топлинни явления", "status": "in_preparation"},
        {"slug": "pressure", "title": "Налягане", "status": "in_preparation"},
        {"slug": "electricity", "title": "Електричество", "status": "in_preparation"},
    ),
    9: (
        {"slug": "mechanics", "title": "Механика", "status": "in_preparation"},
        {"slug": "oscillations-and-waves", "title": "Трептения и вълни", "status": "in_preparation"},
        {"slug": "sound", "title": "Звук", "status": "in_preparation"},
        {"slug": "light", "title": "Светлина", "status": "in_preparation"},
    ),
    10: (
        {"slug": "mechanics", "title": "Механика", "status": "in_preparation"},
        {"slug": "thermal-processes", "title": "Топлинни процеси", "status": "in_preparation"},
        {"slug": "electric-phenomena", "title": "Електрични явления", "status": "in_preparation"},
        {"slug": "magnetic-phenomena", "title": "Магнитни явления", "status": "in_preparation"},
    ),
})


# Display order is the order of this tuple.
SUBJECTS: tuple[SubjectDefinition, ...] = (
    SubjectDefinition(
        id=IT,
        route_slug="information-technologies",
        title="Информационни технологии",
        description="Текущото съдържание с класове, теми, въпроси и тренировъчни тестове.",
        search_terms="ИТ informatsionni tehnologii теми въпроси тестове",
        # "informatika_it" is legacy IT/DZI data, never the Informatics subject.
        legacy_aliases=("IT", "informatika_it", "Информационни технологии"),
        active_description="Текущото съдържание с класове, теми, въпроси и тренировъчни тестове.",
        # Profiled 11-12 is in scope: the existing IT/ДЗИ content lives there.
        grade_paths=grade_paths(general=(8, 9, 10), profiled=(11, 12)),
        dzi=True,
    ),
    SubjectDefinition(
        id=PHYSICS,
        route_slug="physics",
        title="Физика",
        description="Раздел по класове за бъдещи теми, упражнения и тестове по физика.",
        search_terms="physics fizika природни науки",
        grades=(8, 9, 10),
        grade_paths=grade_paths(general=(8, 9, 10), profiled=(11, 12)),
        framework_plan_dependent_grades=(8,),
        placeholder_topic_groups=_PHYSICS_TOPIC_GROUPS_BY_GRADE,
    ),
    SubjectDefinition(
        id="informatics",
        route_slug="informatics",
        title="Информатика",
        description="Бъдещ раздел за алгоритми, програмиране и задачи по информатика.",
        search_terms="informatika алгоритми програмиране",
        # General-education Informatics exists for VIII клас only, and only under
        # some framework plans; IX-X have no programme.
        grades=(8,),
        grade_paths=grade_paths(general=(8,), profiled=(11, 12)),
        framework_plan_dependent_grades=(8,),
    ),
    SubjectDefinition(
        id="biology",
        route_slug="biology",
        title="Биология",
        description="Бъдещ раздел по класове за теми и упражнения по биология.",
        search_terms="biologiya природни науки",
        grades=(8, 9, 10),
        grade_paths=grade_paths(general=(8, 9, 10), profiled=(11, 12)),
        framework_plan_dependent_grades=(8,),
    ),
    SubjectDefinition(
        id="mathematics",
        route_slug="mathematics",
        title="Математика",
        description="Бъдещ раздел по класове за задачи, теми и тестове по математика.",
        search_terms="matematika алгебра геометрия",
        # XI-XII have both paths; LearnPilot supports the general one.
        grade_paths=grade_paths(general=PLATFORM_GRADES, profiled=(11, 12)),
        supported_paths=MappingProxyType({11: GENERAL, 12: GENERAL}),
    ),
    SubjectDefinition(
        id="chemistry",
        route_slug="chemistry",
        title="Химия",
        description="Бъдещ раздел по класове за теми и упражнения по химия.",
        search_terms="himiya природни науки",
        grades=(8, 9, 10),
        grade_paths=grade_paths(general=(8, 9, 10), profiled=(11, 12)),
        framework_plan_dependent_grades=(8,),
    ),
)


def validate_registry(subjects: Iterable[SubjectDefinition]) -> None:
    """Reject ambiguous identity or malformed grade declarations."""
    ids: set[str] = set()
    owners: dict[str, str] = {}
    for subject in subjects:
        if subject.id in ids:
            raise ValueError(f"Duplicate subject id: {subject.id!r}")
        ids.add(subject.id)
        if len(set(subject.legacy_aliases)) != len(subject.legacy_aliases):
            raise ValueError(f"Duplicate legacy alias within {subject.id!r}")
        for identifier in {subject.id, subject.route_slug, *subject.legacy_aliases}:
            if owners.setdefault(identifier, subject.id) != subject.id:
                raise ValueError(
                    f"Identifier {identifier!r} is claimed by both "
                    f"{owners[identifier]!r} and {subject.id!r}"
                )
        grades = subject.grades
        if any(type(grade) is not int or grade not in PLATFORM_GRADES for grade in grades):
            raise ValueError(f"{subject.id!r} declares a grade outside {PLATFORM_GRADES}")
        if list(grades) != sorted(set(grades)):
            raise ValueError(f"{subject.id!r} grades must be unique and ascending")
        for grade, paths in subject.grade_paths.items():
            if type(grade) is not int or grade not in PLATFORM_GRADES:
                raise ValueError(f"{subject.id!r} has paths for a grade outside {PLATFORM_GRADES}")
            if (not isinstance(paths, tuple) or not paths or len(set(paths)) != len(paths)
                    or any(path not in GRADE_PATHS for path in paths)):
                raise ValueError(f"{subject.id!r} grade {grade} has invalid paths {paths!r}")
        for grade in grades:
            paths = subject.grade_paths.get(grade)
            if not paths:
                raise ValueError(
                    f"{subject.id!r} supports grade {grade} without an educational path"
                )
            if len(paths) > 1 and subject.supported_paths.get(grade) not in paths:
                raise ValueError(
                    f"{subject.id!r} grade {grade} has several paths; declare the supported one"
                )
        for grade, path in subject.supported_paths.items():
            if grade not in grades or path not in subject.grade_paths.get(grade, ()):
                raise ValueError(
                    f"{subject.id!r} supported path {path!r} does not fit grade {grade}"
                )
        for grade in subject.framework_plan_dependent_grades:
            if GENERAL not in subject.grade_paths.get(grade, ()):
                raise ValueError(
                    f"{subject.id!r} grade {grade} is framework-plan dependent without a general path"
                )
        for grade in subject.placeholder_topic_groups:
            if grade not in grades:
                raise ValueError(
                    f"{subject.id!r} has placeholder groups for undeclared grade {grade}"
                )


validate_registry(SUBJECTS)

# Every lookup goes through these module-level maps so tests can patch them.
_SUBJECTS_BY_ID: dict[str, SubjectDefinition] = {subject.id: subject for subject in SUBJECTS}
_IDENTIFIERS: dict[str, str] = {
    identifier: subject.id
    for subject in SUBJECTS
    for identifier in (subject.id, subject.route_slug, *subject.legacy_aliases)
}
_ROUTE_SLUGS: dict[str, str] = {subject.route_slug: subject.id for subject in SUBJECTS}


def all_subjects() -> tuple[SubjectDefinition, ...]:
    """Every registered subject in display order."""
    return tuple(_SUBJECTS_BY_ID.values())


def get_subject(subject_id: object) -> SubjectDefinition | None:
    """The definition for an exact canonical id; anything else is None."""
    if not isinstance(subject_id, str):
        return None
    return _SUBJECTS_BY_ID.get(subject_id)


def subject_for_route_slug(slug: object) -> SubjectDefinition | None:
    """URL lookup: route slugs only, never ids, aliases or stored legacy values."""
    if not isinstance(slug, str):
        return None
    subject_id = _ROUTE_SLUGS.get(slug)
    return _SUBJECTS_BY_ID.get(subject_id) if subject_id is not None else None


def resolve_subject_identifier(value: object) -> str | None:
    """Canonical id for an id, route slug or legacy alias. Unknown stays None."""
    if not isinstance(value, str):
        return None
    subject_id = _IDENTIFIERS.get(value.strip())
    if subject_id is None or subject_id not in _SUBJECTS_BY_ID:
        return None
    return subject_id


def subject_supports_grade(subject_id: object, grade: object) -> bool:
    subject = get_subject(subject_id)
    return subject is not None and type(grade) is int and grade in subject.grades


def subject_grade_paths(subject_id: object, grade: object) -> tuple[str, ...]:
    """Educational paths of a grade; empty for an unknown subject or a grade it is not studied in."""
    subject = get_subject(subject_id)
    return subject.paths_for(grade) if subject is not None else ()
