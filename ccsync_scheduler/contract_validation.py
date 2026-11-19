from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, time, timedelta
from typing import Any

from pydantic import ValidationError

from ccsync_scheduler.contract import (
    CONTRACT_VERSION,
    MeetingAssignment,
    PlacementMetrics,
    PublishedScheduleBaseline,
    SolveRequest,
    SolveResponse,
    clock_value,
    date_value,
)


@dataclass(frozen=True)
class ContractIssue:
    path: str
    code: str


class ContractValidationError(ValueError):
    def __init__(self, issues: list[ContractIssue]) -> None:
        self.issues = tuple(issues)
        super().__init__("The request does not satisfy the scheduling contract.")


def _duplicate_issues(values: list[str], path: str) -> list[ContractIssue]:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for value in values:
        if value in seen:
            duplicates.add(value)
        seen.add(value)
    if not duplicates:
        return []
    return [ContractIssue(path, "duplicate_id")]


def _overlapping_windows(windows: list[Any], path: str) -> list[ContractIssue]:
    by_day: dict[int, list[tuple[int, int]]] = defaultdict(list)
    for window in windows:
        by_day[window.day_of_week].append(
            (
                clock_value(window.start_time).hour * 60
                + clock_value(window.start_time).minute,
                clock_value(window.end_time).hour * 60
                + clock_value(window.end_time).minute,
            )
        )
    for intervals in by_day.values():
        intervals.sort()
        if any(current_start < previous_end for (_, previous_end), (current_start, _) in zip(intervals, intervals[1:])):
            return [ContractIssue(path, "overlapping_windows")]
    return []


def _array_id_issues(model: SolveRequest) -> list[ContractIssue]:
    collections = (
        ("programs", [item.id for item in model.programs]),
        ("year_levels", [item.id for item in model.year_levels]),
        ("sections", [item.id for item in model.sections]),
        ("subjects", [item.id for item in model.subjects]),
        ("offerings", [item.id for item in model.offerings]),
        ("professors", [item.id for item in model.professors]),
        ("professor_assignments", [item.id for item in model.professor_assignments]),
        ("rooms", [item.id for item in model.rooms]),
        ("combined_classes", [item.id for item in model.combined_classes]),
        (
            "published_timetable_baseline",
            [item.schedule_id for item in model.published_timetable_baseline],
        ),
        (
            "existing_commitments",
            [item.schedule_id for item in model.existing_commitments],
        ),
    )
    issues: list[ContractIssue] = []
    for path, values in collections:
        issues.extend(_duplicate_issues(values, path))
    for baseline in model.published_timetable_baseline:
        issues.extend(
            _duplicate_issues(
                baseline.section_ids,
                f"published_timetable_baseline[{baseline.schedule_id}].section_ids",
            )
        )
        issues.extend(
            _duplicate_issues(
                baseline.professor_ids,
                f"published_timetable_baseline[{baseline.schedule_id}].professor_ids",
            )
        )
        issues.extend(
            _duplicate_issues(
                [item.allocation_id for item in baseline.room_allocations],
                f"published_timetable_baseline[{baseline.schedule_id}].room_allocations",
            )
        )
    for commitment in model.existing_commitments:
        issues.extend(
            _duplicate_issues(
                commitment.section_ids,
                f"existing_commitments[{commitment.schedule_id}].section_ids",
            )
        )
        issues.extend(
            _duplicate_issues(
                commitment.professor_ids,
                f"existing_commitments[{commitment.schedule_id}].professor_ids",
            )
        )
        issues.extend(
            _duplicate_issues(
                [item.allocation_id for item in commitment.room_allocations],
                f"existing_commitments[{commitment.schedule_id}].room_allocations",
            )
        )

    requirements = [
        requirement.id
        for offering in model.offerings
        for requirement in offering.meeting_requirements
    ]
    shared = [
        requirement.id
        for group in model.combined_classes
        for requirement in group.shared_meeting_requirements
    ]
    issues.extend(_duplicate_issues(requirements, "offerings.meeting_requirements"))
    issues.extend(_duplicate_issues(shared, "combined_classes.shared_meeting_requirements"))
    if set(requirements).intersection(shared):
        issues.append(ContractIssue("combined_classes.shared_meeting_requirements", "requirement_id_namespace_collision"))
    if any(":" in value for value in requirements + shared):
        issues.append(ContractIssue("meeting_requirements", "id_must_not_contain_occurrence_separator"))
    return issues


@dataclass(frozen=True)
class MeetingSource:
    instance_id: str
    requirement_id: str
    occurrence: int
    offering_ids: tuple[str, ...]
    section_ids: tuple[str, ...]
    professor_id: str
    faculty_assignments: tuple[tuple[str, str, str], ...]
    meeting_type: str
    modality: str
    duration_minutes: int
    required_room_type: str | None
    mandatory: bool
    allowed_days: tuple[int, ...]
    valid_start_times: tuple[str, ...]


def _time_grid_days(model: SolveRequest) -> dict[int, Any]:
    return {item.day_of_week: item for item in model.time_grid.days}


def canonical_snapshot_json(request_data: dict[str, Any]) -> str:
    payload = {
        key: value
        for key, value in request_data.items()
        if key not in {"contract_version", "metadata"}
    }
    return json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def compute_snapshot_sha256(request_data: dict[str, Any]) -> str:
    canonical = canonical_snapshot_json(request_data)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _validate_request_semantics(model: SolveRequest) -> list[ContractIssue]:
    issues = _array_id_issues(model)
    term_id = model.academic_term.id
    term_days = set(model.academic_term.applicable_days)
    grid_days = _time_grid_days(model)
    starts = date_value(model.academic_term.starts_on)
    ends = date_value(model.academic_term.ends_on)
    term_weekdays: set[int] = set()
    current = starts
    while current <= ends:
        term_weekdays.add(current.isoweekday())
        current += timedelta(days=1)

    if model.metadata.academic_term_id != term_id:
        issues.append(ContractIssue("metadata.academic_term_id", "academic_term_mismatch"))
    if model.time_grid.academic_term_id != term_id:
        issues.append(ContractIssue("time_grid.academic_term_id", "academic_term_mismatch"))
    if model.generation_scope.academic_term_id != term_id:
        issues.append(ContractIssue("generation_scope.academic_term_id", "academic_term_mismatch"))
    if model.academic_term.applicable_days != sorted(grid_days):
        issues.append(ContractIssue("time_grid.days", "must_match_applicable_days"))
    if len(model.time_grid.days) != len(grid_days):
        issues.append(ContractIssue("time_grid.days", "duplicate_day"))
    if not term_days.issubset(term_weekdays):
        issues.append(ContractIssue("academic_term.applicable_days", "weekday_not_in_term"))
    if not model.offerings:
        issues.append(ContractIssue("offerings", "required_offering_set_empty"))

    programs = {item.id for item in model.programs}
    years = {item.id for item in model.year_levels}
    sections = {item.id: item for item in model.sections}
    subjects = {item.id for item in model.subjects}
    professors = {item.id: item for item in model.professors}
    offerings = {item.id: item for item in model.offerings}
    rooms = {item.id: item for item in model.rooms}

    if set(model.generation_scope.included_offering_ids) != set(offerings):
        issues.append(
            ContractIssue("generation_scope.included_offering_ids", "must_equal_offerings")
        )
    if len(model.generation_scope.included_offering_ids) != len(
        set(model.generation_scope.included_offering_ids)
    ):
        issues.append(ContractIssue("generation_scope.included_offering_ids", "duplicate_id"))

    scope = model.generation_scope
    scope_lists = {
        "reoptimized_published_schedule_ids": scope.reoptimized_published_schedule_ids,
        "preserved_draft_schedule_ids": scope.preserved_draft_schedule_ids,
        "replacement_draft_schedule_ids": scope.replacement_draft_schedule_ids,
        "fixed_schedule_ids": scope.fixed_schedule_ids,
    }
    for name, values in scope_lists.items():
        issues.extend(_duplicate_issues(values, f"generation_scope.{name}"))
    if set(scope.preserved_draft_schedule_ids).intersection(scope.replacement_draft_schedule_ids):
        issues.append(ContractIssue("generation_scope", "draft_scope_overlap"))
    if set(scope.reoptimized_published_schedule_ids).intersection(scope.replacement_draft_schedule_ids):
        issues.append(ContractIssue("generation_scope", "published_schedule_cannot_be_draft_replacement"))
    if set(scope.fixed_schedule_ids).intersection(scope.replacement_draft_schedule_ids):
        issues.append(ContractIssue("generation_scope", "fixed_schedule_cannot_be_replaced"))
    if set(scope.fixed_schedule_ids).intersection(scope.reoptimized_published_schedule_ids):
        issues.append(ContractIssue("generation_scope", "fixed_schedule_cannot_be_reoptimized"))

    for section in model.sections:
        if section.program_id not in programs:
            issues.append(ContractIssue(f"sections[{section.id}].program_id", "unknown_reference"))
        if section.year_level_id not in years:
            issues.append(ContractIssue(f"sections[{section.id}].year_level_id", "unknown_reference"))
        if section.academic_term_id != term_id:
            issues.append(ContractIssue(f"sections[{section.id}].academic_term_id", "academic_term_mismatch"))
        issues.extend(
            _overlapping_windows(
                section.availability_windows,
                f"sections[{section.id}].availability_windows",
            )
        )
        if any(window.day_of_week not in term_days for window in section.availability_windows):
            issues.append(ContractIssue(f"sections[{section.id}].availability_windows", "day_outside_term"))

    for professor in model.professors:
        target = professor.part_time_weekly_target_minutes
        if professor.employment_type == "part_time" and target is None:
            issues.append(
                ContractIssue(
                    f"professors[{professor.id}].part_time_weekly_target_minutes",
                    "required_for_part_time",
                )
            )
        if professor.employment_type != "part_time" and target is not None:
            issues.append(
                ContractIssue(
                    f"professors[{professor.id}].part_time_weekly_target_minutes",
                    "not_allowed_for_employment_type",
                )
            )
        issues.extend(
            _overlapping_windows(
                professor.availability_windows,
                f"professors[{professor.id}].availability_windows",
            )
        )
        if any(window.day_of_week not in term_days for window in professor.availability_windows):
            issues.append(ContractIssue(f"professors[{professor.id}].availability_windows", "day_outside_term"))

    for offering in model.offerings:
        if offering.academic_term_id != term_id:
            issues.append(ContractIssue(f"offerings[{offering.id}].academic_term_id", "academic_term_mismatch"))
        section = sections.get(offering.section_id)
        if section is None:
            issues.append(ContractIssue(f"offerings[{offering.id}].section_id", "unknown_reference"))
        elif section.academic_term_id != offering.academic_term_id:
            issues.append(ContractIssue(f"offerings[{offering.id}].section_id", "academic_term_mismatch"))
        if offering.subject_id not in subjects:
            issues.append(ContractIssue(f"offerings[{offering.id}].subject_id", "unknown_reference"))
        if not offering.meeting_requirements:
            issues.append(
                ContractIssue(f"offerings[{offering.id}].meeting_requirements", "required")
            )
        for requirement in offering.meeting_requirements:
            path = f"offerings[{offering.id}].meeting_requirements[{requirement.id}]"
            allowed = set(requirement.allowed_days or term_days)
            if not allowed.issubset(term_days):
                issues.append(ContractIssue(f"{path}.allowed_days", "day_outside_term"))
            if requirement.valid_start_times is not None:
                applicable_days = allowed if requirement.allowed_days is not None else term_days
                for day in applicable_days:
                    day_grid = grid_days.get(day)
                    if day_grid is None or not set(requirement.valid_start_times).issubset(
                        day_grid.valid_start_times
                    ):
                        issues.append(ContractIssue(f"{path}.valid_start_times", "not_subset_of_time_grid"))
                        break
            elif requirement.allowed_days is not None and any(day not in grid_days for day in allowed):
                issues.append(ContractIssue(f"{path}.allowed_days", "day_missing_from_time_grid"))

    assignments_by_offering: dict[str, list[Any]] = defaultdict(list)
    for assignment in model.professor_assignments:
        if assignment.subject_offering_id not in offerings:
            issues.append(ContractIssue(f"professor_assignments[{assignment.id}].subject_offering_id", "unknown_reference"))
        if assignment.professor_id not in professors:
            issues.append(ContractIssue(f"professor_assignments[{assignment.id}].professor_id", "unknown_reference"))
        assignments_by_offering[assignment.subject_offering_id].append(assignment)
    for offering_id in offerings:
        count = len(assignments_by_offering[offering_id])
        if count == 0:
            issues.append(ContractIssue(f"professor_assignments[{offering_id}]", "missing_professor_assignment"))
        elif count > 1:
            ids = {item.professor_id for item in assignments_by_offering[offering_id]}
            code = "multiple_distinct_professors" if len(ids) > 1 else "ambiguous_assignment_rows"
            issues.append(ContractIssue(f"professor_assignments[{offering_id}]", code))

    if any(item.employment_type == "full_time" for item in model.professors):
        policy = model.full_time_workday_policy
        if policy is None:
            issues.append(ContractIssue("full_time_workday_policy", "required_for_full_time_professors"))
        else:
            if policy.academic_term_id != term_id:
                issues.append(ContractIssue("full_time_workday_policy.academic_term_id", "academic_term_mismatch"))
            if not set(policy.standard_days).issubset(term_days):
                issues.append(ContractIssue("full_time_workday_policy.standard_days", "day_outside_term"))
            exception_ids: list[str] = []
            for exception in policy.exceptions:
                exception_ids.append(exception.professor_id)
                professor = professors.get(exception.professor_id)
                if professor is None:
                    issues.append(ContractIssue("full_time_workday_policy.exceptions", "unknown_professor"))
                elif professor.employment_type != "full_time":
                    issues.append(ContractIssue("full_time_workday_policy.exceptions", "professor_not_full_time"))
                if not set(exception.authorized_additional_days).issubset(term_days):
                    issues.append(ContractIssue("full_time_workday_policy.exceptions", "day_outside_term"))
                if set(exception.authorized_additional_days).intersection(policy.standard_days):
                    issues.append(ContractIssue("full_time_workday_policy.exceptions", "exception_not_additional"))
            issues.extend(_duplicate_issues(exception_ids, "full_time_workday_policy.exceptions"))

    if model.full_time_workday_policy is not None and not any(
        item.employment_type == "full_time" for item in model.professors
    ):
        issues.append(ContractIssue("full_time_workday_policy", "not_applicable"))

    plan_ids = [item.meeting_plan_id for item in model.offerings]
    issues.extend(_duplicate_issues(plan_ids, "offerings.meeting_plan_id"))

    sources: dict[str, MeetingSource] = {}
    _collect_meeting_sources(model, sources, issues)

    baseline_ids = {item.schedule_id for item in model.published_timetable_baseline}
    fixed_published_ids = {
        item.schedule_id
        for item in model.existing_commitments
        if item.schedule_status == "published"
    }
    expected_baseline_ids = set(scope.reoptimized_published_schedule_ids) | fixed_published_ids
    if baseline_ids != expected_baseline_ids:
        issues.append(
            ContractIssue(
                "published_timetable_baseline",
                "must_match_reoptimized_and_fixed_published_schedules",
            )
        )
    fixed_ids = {item.schedule_id for item in model.existing_commitments}
    if fixed_ids != set(scope.fixed_schedule_ids):
        issues.append(
            ContractIssue("existing_commitments", "must_match_fixed_schedule_ids")
        )

    for baseline in model.published_timetable_baseline:
        path = f"published_timetable_baseline[{baseline.schedule_id}]"
        if baseline.subject_offering_id not in offerings:
            issues.append(ContractIssue(f"{path}.subject_offering_id", "unknown_or_unrequired_offering"))
            continue
        offering = offerings[baseline.subject_offering_id]
        if offering.section_id not in baseline.section_ids:
            issues.append(ContractIssue(f"{path}.section_ids", "offering_section_missing"))
        if not set(baseline.section_ids).issubset(sections):
            issues.append(ContractIssue(f"{path}.section_ids", "unknown_section"))
        if len(set(baseline.professor_ids)) > 1:
            issues.append(ContractIssue(f"{path}.professor_ids", "multiple_distinct_professors"))
        if baseline.professor_ids and any(pid not in professors for pid in baseline.professor_ids):
            issues.append(ContractIssue(f"{path}.professor_ids", "unknown_professor"))
        if baseline.meeting_instance_id is not None:
            source = sources.get(baseline.meeting_instance_id)
            if source is None:
                issues.append(ContractIssue(f"{path}.meeting_instance_id", "unknown_meeting_instance"))
            else:
                if baseline.subject_offering_id not in source.offering_ids:
                    issues.append(ContractIssue(f"{path}.meeting_instance_id", "offering_mismatch"))
                if set(baseline.section_ids) - set(source.section_ids):
                    issues.append(ContractIssue(f"{path}.section_ids", "meeting_section_mismatch"))
                if baseline.professor_ids and baseline.professor_ids != [source.professor_id]:
                    issues.append(ContractIssue(f"{path}.professor_ids", "professor_assignment_mismatch"))
        source = sources.get(baseline.meeting_instance_id or "")
        if source is not None:
            if source.modality == "online" and baseline.room_allocations:
                issues.append(ContractIssue(f"{path}.room_allocations", "online_schedule_has_room"))
            if source.modality != "online" and len(baseline.room_allocations) != 1:
                issues.append(ContractIssue(f"{path}.room_allocations", "physical_schedule_requires_one_active_room"))

    for commitment in model.existing_commitments:
        path = f"existing_commitments[{commitment.schedule_id}]"
        commitment_offering = offerings.get(commitment.subject_offering_id)
        if commitment_offering is None:
            issues.append(ContractIssue(f"{path}.subject_offering_id", "unknown_offering"))
        elif commitment_offering.section_id not in commitment.section_ids:
            issues.append(ContractIssue(f"{path}.section_ids", "offering_section_missing"))
        if any(section_id not in sections for section_id in commitment.section_ids):
            issues.append(ContractIssue(f"{path}.section_ids", "unknown_section"))
        if any(professor_id not in professors for professor_id in commitment.professor_ids):
            issues.append(ContractIssue(f"{path}.professor_ids", "unknown_professor"))
        if commitment.schedule_status == "published" and commitment.schedule_id not in baseline_ids:
            issues.append(ContractIssue(path, "published_commitment_missing_from_baseline"))
    baseline_by_schedule = {
        item.schedule_id: item for item in model.published_timetable_baseline
    }
    for commitment in model.existing_commitments:
        if commitment.schedule_status != "published":
            continue
        baseline = baseline_by_schedule.get(commitment.schedule_id)
        if baseline is None:
            continue
        baseline_allocations = sorted(
            (item.allocation_id, item.room_id) for item in baseline.room_allocations
        )
        commitment_allocations = sorted(
            (item.allocation_id, item.room_id) for item in commitment.room_allocations
        )
        if (
            baseline.subject_offering_id != commitment.subject_offering_id
            or baseline.day_of_week != commitment.day_of_week
            or baseline.start_time != commitment.start_time
            or baseline.end_time != commitment.end_time
            or set(baseline.section_ids) != set(commitment.section_ids)
            or set(baseline.professor_ids) != set(commitment.professor_ids)
            or baseline_allocations != commitment_allocations
        ):
            issues.append(
                ContractIssue(
                    f"existing_commitments[{commitment.schedule_id}]",
                    "published_baseline_commitment_mismatch",
                )
            )

    # Stable baseline rows that refer to the same meeting must agree on placement.
    baseline_by_instance: dict[str, list[PublishedScheduleBaseline]] = defaultdict(list)
    for item in model.published_timetable_baseline:
        if item.meeting_instance_id is not None:
            baseline_by_instance[item.meeting_instance_id].append(item)
    for meeting_id, items in baseline_by_instance.items():
        if len(
            {
                (
                    item.day_of_week,
                    item.start_time,
                    tuple(sorted(allocation.room_id for allocation in item.room_allocations)),
                )
                for item in items
            }
        ) > 1:
            issues.append(
                ContractIssue(
                    f"published_timetable_baseline[{meeting_id}]",
                    "duplicate_meeting_baseline_disagrees",
                )
            )
    return issues


def _collect_meeting_sources(
    model: SolveRequest,
    sources: dict[str, MeetingSource],
    issues: list[ContractIssue],
) -> None:
    offers = {item.id: item for item in model.offerings}
    assignments_by_offering: dict[str, list[Any]] = defaultdict(list)
    for assignment in model.professor_assignments:
        assignments_by_offering[assignment.subject_offering_id].append(assignment)
    consumed: set[tuple[str, str]] = set()
    grouped_offerings: set[str] = set()
    for group in model.combined_classes:
        path = f"combined_classes[{group.id}]"
        if len(group.offering_ids) < 2:
            issues.append(ContractIssue(path, "combined_class_requires_two_offerings"))
            continue
        if len(group.offering_ids) != len(set(group.offering_ids)):
            issues.append(ContractIssue(path, "duplicate_offering_id"))
            continue
        if any(offering_id not in offers for offering_id in group.offering_ids):
            issues.append(ContractIssue(path, "unknown_offering_reference"))
            continue
        if not group.shared_meeting_requirements:
            issues.append(ContractIssue(path, "missing_shared_meeting_requirements"))
        overlap = grouped_offerings.intersection(group.offering_ids)
        if overlap:
            issues.append(ContractIssue(path, "offering_in_multiple_combined_classes"))
            continue
        grouped_offerings.update(group.offering_ids)

        group_offerings = [offers[item] for item in group.offering_ids]
        if len({item.section_id for item in group_offerings}) != len(group_offerings):
            issues.append(ContractIssue(path, "combined_sections_must_be_distinct"))
        if len({item.subject_id for item in group_offerings}) != 1:
            issues.append(ContractIssue(path, "combined_subjects_must_match"))
        if any(item.academic_term_id != model.academic_term.id for item in group_offerings):
            issues.append(ContractIssue(path, "academic_term_mismatch"))
        assignments = [
            assignments_by_offering.get(offering_id, [])
            for offering_id in group.offering_ids
        ]
        if any(len(rows) != 1 for rows in assignments):
            issues.append(ContractIssue(path, "missing_or_ambiguous_professor_assignment"))
            continue
        professor_ids = {rows[0].professor_id for rows in assignments}
        if len(professor_ids) != 1:
            issues.append(ContractIssue(path, "combined_professors_disagree"))
            continue
        professor_id = next(iter(professor_ids))
        for shared in group.shared_meeting_requirements:
            shared_path = f"{path}.shared_meeting_requirements[{shared.id}]"
            source_pairs = [
                (item.offering_id, item.meeting_requirement_id)
                for item in shared.source_requirements
            ]
            source_offerings = [item[0] for item in source_pairs]
            if (
                len(source_offerings) != len(set(source_offerings))
                or set(source_offerings) != set(group.offering_ids)
            ):
                issues.append(ContractIssue(shared_path, "incomplete_or_duplicate_source_requirements"))
                continue
            requirements: list[tuple[str, Any]] = []
            source_invalid = False
            for offering_id, requirement_id in source_pairs:
                matching = [
                    item
                    for item in offers[offering_id].meeting_requirements
                    if item.id == requirement_id
                ]
                if len(matching) != 1:
                    issues.append(ContractIssue(shared_path, "unknown_meeting_requirement"))
                    source_invalid = True
                    continue
                key = (offering_id, requirement_id)
                if key in consumed:
                    issues.append(ContractIssue(shared_path, "meeting_requirement_reused"))
                    source_invalid = True
                    continue
                consumed.add((offering_id, requirement_id))
                requirements.append((offering_id, matching[0]))
            if source_invalid or len(requirements) != len(group.offering_ids):
                continue
            first = requirements[0][1]
            if any(
                (
                    item.meeting_type,
                    item.frequency_per_week,
                    item.duration_minutes,
                    item.modality,
                    item.required_room_type,
                    item.mandatory,
                    item.allowed_days,
                    item.valid_start_times,
                )
                != (
                    first.meeting_type,
                    first.frequency_per_week,
                    first.duration_minutes,
                    first.modality,
                    first.required_room_type,
                    first.mandatory,
                    first.allowed_days,
                    first.valid_start_times,
                )
                for _, item in requirements[1:]
            ):
                issues.append(ContractIssue(shared_path, "incompatible_source_requirements"))
                continue
            if first.modality != "online" and any(
                item.expected_enrollment is None for item in group_offerings
            ):
                issues.append(ContractIssue(path, "missing_expected_enrollment"))
                continue
            faculty = tuple(
                sorted(
                    (
                        row[0].subject_offering_id,
                        row[0].id,
                        row[0].assignment_role,
                    )
                    for row in assignments
                )
            )
            for occurrence in range(1, first.frequency_per_week + 1):
                instance_id = f"{shared.id}:{occurrence}"
                if instance_id in sources:
                    issues.append(ContractIssue("combined_classes", "duplicate_meeting_instance_id"))
                    continue
                sources[instance_id] = MeetingSource(
                    instance_id=instance_id,
                    requirement_id=shared.id,
                    occurrence=occurrence,
                    offering_ids=tuple(sorted(group.offering_ids)),
                    section_ids=tuple(
                        sorted(item.section_id for item in group_offerings)
                    ),
                    professor_id=professor_id,
                    faculty_assignments=faculty,
                    meeting_type=first.meeting_type,
                    modality=first.modality,
                    duration_minutes=first.duration_minutes,
                    required_room_type=first.required_room_type,
                    mandatory=first.mandatory,
                    allowed_days=tuple(
                        first.allowed_days or model.academic_term.applicable_days
                    ),
                    valid_start_times=tuple(first.valid_start_times or ()),
                )

    for offering in model.offerings:
        assignment_rows = assignments_by_offering.get(offering.id, [])
        if len(assignment_rows) != 1:
            continue
        assignment = assignment_rows[0]
        for requirement in offering.meeting_requirements:
            if (offering.id, requirement.id) in consumed:
                continue
            if offering.expected_enrollment is None and requirement.modality != "online":
                issues.append(
                    ContractIssue(
                        f"offerings[{offering.id}].expected_enrollment",
                        "required_for_physical_meeting",
                    )
                )
            for occurrence in range(1, requirement.frequency_per_week + 1):
                instance_id = f"{requirement.id}:{occurrence}"
                if instance_id in sources:
                    issues.append(ContractIssue("offerings.meeting_requirements", "duplicate_meeting_instance_id"))
                    continue
                sources[instance_id] = MeetingSource(
                    instance_id=instance_id,
                    requirement_id=requirement.id,
                    occurrence=occurrence,
                    offering_ids=(offering.id,),
                    section_ids=(offering.section_id,),
                    professor_id=assignment.professor_id,
                    faculty_assignments=(
                        (
                            assignment.subject_offering_id,
                            assignment.id,
                            assignment.assignment_role,
                        ),
                    ),
                    meeting_type=requirement.meeting_type,
                    modality=requirement.modality,
                    duration_minutes=requirement.duration_minutes,
                    required_room_type=requirement.required_room_type,
                    mandatory=requirement.mandatory,
                    allowed_days=tuple(
                        requirement.allowed_days or model.academic_term.applicable_days
                    ),
                    valid_start_times=tuple(requirement.valid_start_times or ()),
                )


def meeting_sources_for_request(model: SolveRequest) -> dict[str, MeetingSource]:
    sources: dict[str, MeetingSource] = {}
    issues: list[ContractIssue] = []
    _collect_meeting_sources(model, sources, issues)
    if issues:
        raise ContractValidationError(issues)
    return sources


def validate_request(raw: Any) -> SolveRequest:
    if not isinstance(raw, dict):
        raise ContractValidationError([ContractIssue("$", "object_required")])
    if "contract_version" not in raw:
        raise ContractValidationError(
            [ContractIssue("contract_version", "missing_contract_version")]
        )
    if raw["contract_version"] != CONTRACT_VERSION:
        raise ContractValidationError(
            [ContractIssue("contract_version", "unsupported_contract_version")]
        )
    model = SolveRequest.model_validate(raw)
    issues = _validate_request_semantics(model)
    try:
        expected_hash = compute_snapshot_sha256(raw)
    except UnicodeEncodeError:
        issues.append(ContractIssue("$", "invalid_unicode_for_canonical_json"))
    else:
        if model.metadata.snapshot_sha256 != expected_hash:
            issues.append(ContractIssue("metadata.snapshot_sha256", "snapshot_hash_mismatch"))
    if issues:
        raise ContractValidationError(issues)
    return model


def _minutes(value: str) -> int:
    parsed = clock_value(value)
    return parsed.hour * 60 + parsed.minute


def _fits_window(start: int, end: int, windows: list[Any], day: int) -> bool:
    return any(
        window.day_of_week == day
        and _minutes(window.start_time) <= start
        and end <= _minutes(window.end_time)
        for window in windows
    )


def _availability_error(
    start: int,
    end: int,
    day: int,
    windows: list[Any],
) -> bool:
    for window in windows:
        if window.day_of_week != day:
            continue
        window_start = _minutes(window.start_time)
        window_end = _minutes(window.end_time)
        if window.availability_type == "unavailable" and start < window_end and window_start < end:
            return True
    positive = [
        window
        for window in windows
        if window.day_of_week == day
        and window.availability_type in {"available", "preferred"}
    ]
    return bool(positive) and not _fits_window(start, end, positive, day)


def placement_metrics_for_assignments(
    request: SolveRequest,
    assignments: list[MeetingAssignment] | tuple[MeetingAssignment, ...],
    sources: dict[str, MeetingSource],
) -> PlacementMetrics:
    assignments_by_instance = {
        item.meeting_instance_id: item for item in assignments
    }
    baseline_by_instance: dict[str, list[PublishedScheduleBaseline]] = defaultdict(list)
    unmatched_schedule_count = 0
    for baseline in request.published_timetable_baseline:
        if baseline.meeting_instance_id is None:
            unmatched_schedule_count += 1
            continue
        source = sources.get(baseline.meeting_instance_id)
        candidate = assignments_by_instance.get(baseline.meeting_instance_id)
        if source is None or len(baseline.room_allocations) > 1:
            unmatched_schedule_count += 1
            continue
        if (source.modality == "online" and baseline.room_allocations) or (
            source.modality != "online" and len(baseline.room_allocations) != 1
        ):
            unmatched_schedule_count += 1
            continue
        baseline_by_instance[baseline.meeting_instance_id].append(baseline)
        if candidate is None:
            continue
    comparable = 0
    retained = 0
    changed = 0
    for meeting_id, rows in baseline_by_instance.items():
        first = rows[0]
        prior_room = first.room_allocations[0].room_id if first.room_allocations else None
        candidate = assignments_by_instance.get(meeting_id)
        comparable += 1
        if (
            candidate is not None
            and candidate.day_of_week == first.day_of_week
            and candidate.start_time == first.start_time
            and candidate.room_id == prior_room
        ):
            retained += 1
        else:
            changed += 1
    return PlacementMetrics(
        comparable_meeting_count=comparable,
        retained_placement_count=retained,
        changed_placement_count=changed,
        unmatched_baseline_schedule_count=unmatched_schedule_count,
    )


def validate_response_against_request(
    request: SolveRequest,
    response: SolveResponse,
) -> list[ContractIssue]:
    issues: list[ContractIssue] = []
    if response.contract_version != request.contract_version:
        issues.append(ContractIssue("contract_version", "request_response_mismatch"))
    if response.generation_id != request.metadata.generation_id:
        issues.append(ContractIssue("generation_id", "request_response_mismatch"))
    if response.academic_term_id != request.academic_term.id:
        issues.append(ContractIssue("academic_term_id", "request_response_mismatch"))
    if response.snapshot_sha256 != request.metadata.snapshot_sha256:
        issues.append(ContractIssue("snapshot_sha256", "request_response_mismatch"))
    if response.correlation_id != request.metadata.correlation_id:
        issues.append(ContractIssue("correlation_id", "request_response_mismatch"))

    sources: dict[str, MeetingSource] = {}
    _collect_meeting_sources(request, sources, issues)
    assignment_map = {item.meeting_instance_id: item for item in response.assignments}
    required_ids = {key for key, value in sources.items() if value.mandatory}
    desired_ids = set(sources)
    returned_ids = set(assignment_map)
    if not required_ids.issubset(returned_ids):
        issues.append(ContractIssue("assignments", "missing_mandatory_meeting"))
    if not returned_ids.issubset(desired_ids):
        issues.append(ContractIssue("assignments", "unknown_meeting_instance"))
    if response.validation_summary.required_mandatory_meeting_count != len(required_ids):
        issues.append(ContractIssue("validation_summary.required_mandatory_meeting_count", "count_mismatch"))
    if response.validation_summary.desired_meeting_count != len(desired_ids):
        issues.append(ContractIssue("validation_summary.desired_meeting_count", "count_mismatch"))
    if response.validation_summary.required_offering_count != len(request.offerings):
        issues.append(ContractIssue("validation_summary.required_offering_count", "count_mismatch"))

    represented = {
        offering_id
        for assignment in response.assignments
        for offering_id in assignment.subject_offering_ids
    }
    if response.validation_summary.represented_offering_count != len(represented):
        issues.append(ContractIssue("validation_summary.represented_offering_count", "count_mismatch"))
    if represented != set(request.generation_scope.included_offering_ids):
        issues.append(ContractIssue("assignments", "required_offering_not_represented"))

    sections = {item.id: item for item in request.sections}
    offerings = {item.id: item for item in request.offerings}
    professors = {item.id: item for item in request.professors}
    rooms = {item.id: item for item in request.rooms}
    grids = _time_grid_days(request)
    policy = request.full_time_workday_policy

    for instance_id, assignment in assignment_map.items():
        source = sources.get(instance_id)
        if source is None:
            continue
        path = f"assignments[{instance_id}]"
        if assignment.source_requirement_id != source.requirement_id:
            issues.append(ContractIssue(f"{path}.source_requirement_id", "meeting_reference_mismatch"))
        if assignment.occurrence_number != source.occurrence:
            issues.append(ContractIssue(f"{path}.occurrence_number", "meeting_reference_mismatch"))
        if tuple(sorted(assignment.subject_offering_ids)) != source.offering_ids:
            issues.append(ContractIssue(f"{path}.subject_offering_ids", "meeting_reference_mismatch"))
        if tuple(sorted(assignment.section_ids)) != source.section_ids:
            issues.append(ContractIssue(f"{path}.section_ids", "meeting_reference_mismatch"))
        if assignment.professor_id != source.professor_id:
            issues.append(ContractIssue(f"{path}.professor_id", "professor_assignment_mismatch"))
        expected_faculty = set(source.faculty_assignments)
        actual_faculty = {
            (item.subject_offering_id, item.faculty_assignment_id, item.assignment_role)
            for item in assignment.professor_assignments
        }
        if actual_faculty != expected_faculty:
            issues.append(ContractIssue(f"{path}.professor_assignments", "assignment_reference_mismatch"))
        if assignment.meeting_type != source.meeting_type or assignment.modality != source.modality:
            issues.append(ContractIssue(path, "meeting_requirement_mismatch"))
        if assignment.duration_minutes != source.duration_minutes:
            issues.append(ContractIssue(f"{path}.duration_minutes", "duration_mismatch"))
        start = _minutes(assignment.start_time)
        end = _minutes(assignment.end_time)
        if end - start != source.duration_minutes:
            issues.append(ContractIssue(path, "interval_duration_mismatch"))
        grid = grids.get(assignment.day_of_week)
        if grid is None or assignment.day_of_week not in source.allowed_days:
            issues.append(ContractIssue(f"{path}.day_of_week", "day_not_allowed"))
        elif (
            assignment.start_time
            not in (source.valid_start_times or tuple(grid.valid_start_times))
            or start < _minutes(grid.operating_start)
            or end > _minutes(grid.operating_end)
        ):
            issues.append(ContractIssue(path, "time_grid_violation"))
        professor = professors.get(assignment.professor_id)
        if professor is None:
            issues.append(ContractIssue(f"{path}.professor_id", "unknown_professor"))
        else:
            if _availability_error(start, end, assignment.day_of_week, professor.availability_windows):
                issues.append(ContractIssue(path, "professor_availability_violation"))
            if professor.employment_type == "full_time" and policy is not None:
                exception = next(
                    (
                        item
                        for item in policy.exceptions
                        if item.professor_id == professor.id
                    ),
                    None,
                )
                allowed_days = set(policy.standard_days)
                if exception is not None:
                    allowed_days.update(exception.authorized_additional_days)
                if assignment.day_of_week not in allowed_days:
                    issues.append(ContractIssue(f"{path}.day_of_week", "full_time_workday_violation"))
        for section_id in source.section_ids:
            section = sections.get(section_id)
            if section is not None and section.availability_windows:
                if not _fits_window(
                    start,
                    end,
                    section.availability_windows,
                    assignment.day_of_week,
                ):
                    issues.append(ContractIssue(path, "section_availability_violation"))
        if assignment.modality == "online":
            if assignment.room_id is not None:
                issues.append(ContractIssue(f"{path}.room_id", "online_room_not_allowed"))
        else:
            room = rooms.get(assignment.room_id or "")
            if room is None:
                issues.append(ContractIssue(f"{path}.room_id", "unknown_or_unavailable_room"))
            else:
                enrollment = sum(
                    offerings[item].expected_enrollment or 0
                    for item in source.offering_ids
                )
                if room.capacity < enrollment:
                    issues.append(ContractIssue(f"{path}.room_id", "room_capacity_too_small"))
                if source.required_room_type is not None and room.room_type != source.required_room_type:
                    issues.append(ContractIssue(f"{path}.room_id", "room_type_mismatch"))

    expected_workload: dict[str, int] = {professor_id: 0 for professor_id in professors}
    for assignment in response.assignments:
        expected_workload[assignment.professor_id] = (
            expected_workload.get(assignment.professor_id, 0)
            + assignment.duration_minutes
        )
    workload_rows = {item.professor_id: item for item in response.workload_metrics}
    if set(workload_rows) != set(professors):
        issues.append(ContractIssue("workload_metrics", "must_cover_each_professor_once"))
    for professor_id, professor in professors.items():
        metric = workload_rows.get(professor_id)
        if metric is None:
            continue
        if metric.employment_type != professor.employment_type:
            issues.append(
                ContractIssue(f"workload_metrics[{professor_id}]", "employment_type_mismatch")
            )
        if metric.assigned_teaching_minutes != expected_workload[professor_id]:
            issues.append(
                ContractIssue(f"workload_metrics[{professor_id}]", "assigned_minutes_mismatch")
            )
        if metric.weekly_target_minutes != professor.part_time_weekly_target_minutes:
            issues.append(
                ContractIssue(f"workload_metrics[{professor_id}]", "weekly_target_mismatch")
            )
    full_time_metrics = [
        expected_workload[professor_id]
        for professor_id, professor in professors.items()
        if professor.employment_type == "full_time"
    ]
    distribution = response.full_time_distribution
    if not full_time_metrics:
        if distribution is not None:
            issues.append(ContractIssue("full_time_distribution", "not_applicable"))
    elif distribution is None:
        issues.append(
            ContractIssue("full_time_distribution", "required_for_full_time_professors")
        )
    elif (
        distribution.minimum_assigned_minutes != min(full_time_metrics)
        or distribution.maximum_assigned_minutes != max(full_time_metrics)
        or distribution.spread_minutes != max(full_time_metrics) - min(full_time_metrics)
    ):
        issues.append(
            ContractIssue("full_time_distribution", "workload_distribution_mismatch")
        )

    returned = list(response.assignments)
    for index, first in enumerate(returned):
        first_start = _minutes(first.start_time)
        first_end = _minutes(first.end_time)
        for second in returned[index + 1 :]:
            if first.day_of_week != second.day_of_week:
                continue
            second_start = _minutes(second.start_time)
            second_end = _minutes(second.end_time)
            if first_start < second_end and second_start < first_end:
                if first.professor_id == second.professor_id:
                    issues.append(ContractIssue("assignments", "professor_overlap"))
                if set(first.section_ids).intersection(second.section_ids):
                    issues.append(ContractIssue("assignments", "section_overlap"))
                if first.room_id is not None and first.room_id == second.room_id:
                    issues.append(ContractIssue("assignments", "room_overlap"))

    for assignment in response.assignments:
        source = sources.get(assignment.meeting_instance_id)
        if source is None:
            continue
        start = _minutes(assignment.start_time)
        end = _minutes(assignment.end_time)
        for commitment in request.existing_commitments:
            if commitment.day_of_week != assignment.day_of_week:
                continue
            commitment_start = _minutes(commitment.start_time)
            commitment_end = _minutes(commitment.end_time)
            if not (start < commitment_end and commitment_start < end):
                continue
            if assignment.professor_id in commitment.professor_ids:
                issues.append(ContractIssue("assignments", "protected_professor_overlap"))
            if set(assignment.section_ids).intersection(commitment.section_ids):
                issues.append(ContractIssue("assignments", "protected_section_overlap"))
            if assignment.room_id is not None and assignment.room_id in {
                allocation.room_id for allocation in commitment.room_allocations
            }:
                issues.append(ContractIssue("assignments", "protected_room_overlap"))

    expected_metrics = placement_metrics_for_assignments(
        request,
        response.assignments,
        sources,
    )
    if response.placement_metrics != expected_metrics:
        issues.append(ContractIssue("placement_metrics", "placement_metrics_mismatch"))
    return issues


def validation_issues_to_dicts(issues: list[ContractIssue]) -> list[dict[str, str]]:
    return [{"path": issue.path, "code": issue.code} for issue in issues]


def pydantic_issues_to_dicts(error: ValidationError) -> list[dict[str, str]]:
    return [
        {
            "path": ".".join(str(part) for part in item["loc"]) or "$",
            "code": str(item["type"]),
        }
        for item in error.errors(include_input=False, include_context=False, include_url=False)
    ]
