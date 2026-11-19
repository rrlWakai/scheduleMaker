from __future__ import annotations

import json

from ccsync_scheduler.models import (
    AcademicProfessor,
    AcademicRoom,
    AcademicSchedulingProblem,
    AcademicSection,
    AcademicSubject,
    ApprovedProfessorAssignment,
    CombinedClass,
    EmploymentType,
    Program,
    Professor,
    RequiredSession,
    Room,
    SchedulingProblem,
    Section,
    Subject,
    SubjectOffering,
    TimeSlot,
    YearLevel,
)
from ccsync_scheduler.solver import solve


def make_feasible_problem() -> SchedulingProblem:
    slots = frozenset(
        TimeSlot(day, period)
        for day in range(2)
        for period in range(4)
    )
    professors = {
        "prof-ada": Professor("prof-ada", slots),
        "prof-grace": Professor(
            "prof-grace",
            frozenset(slot for slot in slots if slot.period >= 1),
        ),
    }
    sections = {
        "section-a": Section("section-a", slots),
        "section-b": Section("section-b", slots),
    }
    subjects = {
        "subject-algebra": Subject("subject-algebra", duration_periods=1),
        "subject-programming": Subject("subject-programming", duration_periods=2),
        "subject-networks": Subject("subject-networks", duration_periods=1),
    }
    rooms = {
        "room-101": Room("room-101"),
        "room-lab": Room("room-lab"),
        "room-201": Room("room-201"),
    }
    sessions = (
        RequiredSession(
            "session-algebra",
            "prof-ada",
            "section-a",
            "subject-algebra",
            ("room-101", "room-201"),
        ),
        RequiredSession(
            "session-programming",
            "prof-ada",
            "section-b",
            "subject-programming",
            ("room-lab",),
        ),
        RequiredSession(
            "session-networks",
            "prof-grace",
            "section-a",
            "subject-networks",
            ("room-101", "room-201"),
        ),
        RequiredSession(
            "session-writing",
            "prof-grace",
            "section-b",
            "subject-algebra",
            ("room-101", "room-201"),
        ),
    )

    return SchedulingProblem(
        days=2,
        periods_per_day=4,
        available_slots=slots,
        professors=professors,
        sections=sections,
        subjects=subjects,
        rooms=rooms,
        required_sessions=sessions,
    )


def make_infeasible_problem() -> SchedulingProblem:
    only_slot = frozenset({TimeSlot(0, 0)})
    slots = frozenset(TimeSlot(0, period) for period in range(2))
    return SchedulingProblem(
        days=1,
        periods_per_day=2,
        available_slots=slots,
        professors={"prof-ada": Professor("prof-ada", only_slot)},
        sections={
            "section-a": Section("section-a", slots),
            "section-b": Section("section-b", slots),
        },
        subjects={
            "subject-algebra": Subject("subject-algebra", duration_periods=1),
            "subject-networks": Subject("subject-networks", duration_periods=1),
        },
        rooms={
            "room-101": Room("room-101"),
            "room-201": Room("room-201"),
        },
        required_sessions=(
            RequiredSession(
                "session-a",
                "prof-ada",
                "section-a",
                "subject-algebra",
                ("room-101", "room-201"),
            ),
            RequiredSession(
                "session-b",
                "prof-ada",
                "section-b",
                "subject-networks",
                ("room-101", "room-201"),
            ),
        ),
    )


def make_global_problem() -> AcademicSchedulingProblem:
    day_names = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday")
    slots = frozenset(
        TimeSlot(day, period)
        for day in range(len(day_names))
        for period in range(6)
    )
    professor_availability = frozenset(slot for slot in slots if slot.day < 3)
    part_time_availability = frozenset(
        slot for slot in slots if slot.day in {1, 3} and 1 <= slot.period < 5
    )
    sections = {
        "ccs-1a": AcademicSection("ccs-1a", "ccs", "year-1", slots),
        "ccs-2a": AcademicSection("ccs-2a", "ccs", "year-2", slots),
        "cs-1a": AcademicSection("cs-1a", "cs", "year-1", slots),
        "is-2a": AcademicSection("is-2a", "is", "year-2", slots),
    }
    professors = {
        "prof-ada": AcademicProfessor(
            id="prof-ada",
            employment_type=EmploymentType.FULL_TIME,
            available_slots=professor_availability,
        ),
        "prof-ben": AcademicProfessor(
            id="prof-ben",
            employment_type=EmploymentType.FULL_TIME,
            available_slots=slots,
            authorized_workday_exception_days=frozenset({3}),
        ),
        "prof-cy": AcademicProfessor(
            id="prof-cy",
            employment_type=EmploymentType.PART_TIME,
            available_slots=part_time_availability,
            weekly_target_periods=4,
        ),
    }
    subjects = {
        "math": AcademicSubject("math", duration_periods=1),
        "programming": AcademicSubject("programming", duration_periods=2),
        "networks": AcademicSubject("networks", duration_periods=2),
        "accounting": AcademicSubject("accounting", duration_periods=1),
        "ethics": AcademicSubject("ethics", duration_periods=1),
    }
    offerings = {
        "offer-ccs-1a-math": SubjectOffering(
            "offer-ccs-1a-math", "ccs-1a", "math", 2, 32, "general"
        ),
        "offer-ccs-2a-math": SubjectOffering(
            "offer-ccs-2a-math", "ccs-2a", "math", 2, 30, "general"
        ),
        "offer-ccs-1a-programming": SubjectOffering(
            "offer-ccs-1a-programming", "ccs-1a", "programming", 1, 32, "lab"
        ),
        "offer-cs-1a-networks": SubjectOffering(
            "offer-cs-1a-networks", "cs-1a", "networks", 1, 24, "lab"
        ),
        "offer-is-2a-accounting": SubjectOffering(
            "offer-is-2a-accounting", "is-2a", "accounting", 1, 38, "general"
        ),
        "offer-ccs-1a-ethics": SubjectOffering(
            "offer-ccs-1a-ethics", "ccs-1a", "ethics", 1, 32, "lecture_hall"
        ),
        "offer-cs-1a-ethics": SubjectOffering(
            "offer-cs-1a-ethics", "cs-1a", "ethics", 1, 28, "lecture_hall"
        ),
    }
    professor_assignments = (
        ApprovedProfessorAssignment("offer-ccs-1a-math", "prof-ada", True),
        ApprovedProfessorAssignment("offer-ccs-2a-math", "prof-ada", True),
        ApprovedProfessorAssignment("offer-ccs-2a-math", "prof-ben", True),
        ApprovedProfessorAssignment("offer-ccs-1a-programming", "prof-ada", True),
        ApprovedProfessorAssignment("offer-ccs-1a-programming", "prof-cy", True),
        ApprovedProfessorAssignment("offer-cs-1a-networks", "prof-ben", True),
        ApprovedProfessorAssignment("offer-is-2a-accounting", "prof-cy", True),
        ApprovedProfessorAssignment("offer-ccs-1a-ethics", "prof-ada", True),
        ApprovedProfessorAssignment("offer-ccs-1a-ethics", "prof-ben", True),
        ApprovedProfessorAssignment("offer-cs-1a-ethics", "prof-ada", True),
        ApprovedProfessorAssignment("offer-cs-1a-ethics", "prof-ben", True),
    )

    return AcademicSchedulingProblem(
        day_names=day_names,
        periods_per_day=6,
        available_slots=slots,
        programs={
            "ccs": Program("ccs"),
            "cs": Program("cs"),
            "is": Program("is"),
        },
        year_levels={
            "year-1": YearLevel("year-1", 1),
            "year-2": YearLevel("year-2", 2),
        },
        sections=sections,
        professors=professors,
        subjects=subjects,
        offerings=offerings,
        professor_assignments=professor_assignments,
        rooms={
            "room-general-35": AcademicRoom("room-general-35", 35, "general"),
            "room-general-45": AcademicRoom("room-general-45", 45, "general"),
            "room-lab-32": AcademicRoom("room-lab-32", 32, "lab"),
            "room-lab-40": AcademicRoom("room-lab-40", 40, "lab"),
            "room-lecture-80": AcademicRoom("room-lecture-80", 80, "lecture_hall"),
        },
        combined_classes=(
            CombinedClass(
                "combined-ethics-2026",
                ("offer-ccs-1a-ethics", "offer-cs-1a-ethics"),
            ),
        ),
    )


def main() -> None:
    result = solve(make_feasible_problem())
    output = {
        "status": result.status.value,
        "assignments": [
            {
                "session_id": assignment.session_id,
                "day": assignment.start.day,
                "start_period": assignment.start.period,
                "room_id": assignment.room_id,
            }
            for assignment in result.assignments
        ],
    }
    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
