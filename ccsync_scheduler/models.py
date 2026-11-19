from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class SolverStatus(str, Enum):
    OPTIMAL = "OPTIMAL"
    FEASIBLE = "FEASIBLE"
    INFEASIBLE = "INFEASIBLE"
    UNKNOWN = "UNKNOWN"
    MODEL_INVALID = "MODEL_INVALID"


@dataclass(frozen=True, order=True)
class TimeSlot:
    day: int
    period: int


@dataclass(frozen=True)
class Professor:
    id: str
    available_slots: frozenset[TimeSlot]


@dataclass(frozen=True)
class Section:
    id: str
    available_slots: frozenset[TimeSlot]


@dataclass(frozen=True)
class Subject:
    id: str
    duration_periods: int


@dataclass(frozen=True)
class Room:
    id: str


@dataclass(frozen=True)
class RequiredSession:
    id: str
    professor_id: str
    section_id: str
    subject_id: str
    eligible_room_ids: tuple[str, ...]


@dataclass(frozen=True)
class SchedulingProblem:
    days: int
    periods_per_day: int
    available_slots: frozenset[TimeSlot]
    professors: dict[str, Professor]
    sections: dict[str, Section]
    subjects: dict[str, Subject]
    rooms: dict[str, Room]
    required_sessions: tuple[RequiredSession, ...]


@dataclass(frozen=True)
class ScheduledSession:
    session_id: str
    start: TimeSlot
    room_id: str


@dataclass(frozen=True)
class SolveResult:
    status: SolverStatus
    assignments: tuple[ScheduledSession, ...] = ()
    details: str | None = None


class EmploymentType(str, Enum):
    FULL_TIME = "full_time"
    PART_TIME = "part_time"
    VISITING = "visiting"


@dataclass(frozen=True)
class Program:
    id: str


@dataclass(frozen=True)
class YearLevel:
    id: str
    level: int


@dataclass(frozen=True)
class AcademicProfessor:
    id: str
    employment_type: EmploymentType
    available_slots: frozenset[TimeSlot]
    weekly_target_periods: int | None = None
    authorized_workday_exception_days: frozenset[int] = frozenset()


@dataclass(frozen=True)
class AcademicSection:
    id: str
    program_id: str
    year_level_id: str
    available_slots: frozenset[TimeSlot]


@dataclass(frozen=True)
class AcademicSubject:
    id: str
    duration_periods: int


@dataclass(frozen=True)
class SubjectOffering:
    id: str
    section_id: str
    subject_id: str
    meetings_per_week: int
    expected_enrollment: int
    required_room_type: str


@dataclass(frozen=True)
class ApprovedProfessorAssignment:
    offering_id: str
    professor_id: str
    approved: bool


@dataclass(frozen=True)
class AcademicRoom:
    id: str
    capacity: int
    room_type: str


@dataclass(frozen=True)
class CombinedClass:
    id: str
    offering_ids: tuple[str, ...]


@dataclass(frozen=True)
class AcademicSchedulingProblem:
    day_names: tuple[str, ...]
    periods_per_day: int
    available_slots: frozenset[TimeSlot]
    programs: dict[str, Program]
    year_levels: dict[str, YearLevel]
    sections: dict[str, AcademicSection]
    professors: dict[str, AcademicProfessor]
    subjects: dict[str, AcademicSubject]
    offerings: dict[str, SubjectOffering]
    professor_assignments: tuple[ApprovedProfessorAssignment, ...]
    rooms: dict[str, AcademicRoom]
    combined_classes: tuple[CombinedClass, ...] = ()


@dataclass(frozen=True)
class RequiredClassMeeting:
    id: str
    offering_ids: tuple[str, ...]
    section_ids: tuple[str, ...]
    subject_id: str
    duration_periods: int
    expected_enrollment: int
    required_room_type: str
    eligible_professor_ids: tuple[str, ...]


@dataclass(frozen=True)
class AcademicAssignment:
    meeting_id: str
    start: TimeSlot
    professor_id: str
    room_id: str


@dataclass(frozen=True)
class OptimizationWeights:
    part_time_target_deviation: int = 10
    full_time_load_spread: int = 2
    daily_gaps: int = 3
    room_capacity_waste: int = 1
    section_daily_spread: int = 2


@dataclass(frozen=True)
class OptimizationMetrics:
    part_time_target_deviation_periods: int
    full_time_workload_spread_periods: int
    professor_gap_periods: int
    section_gap_periods: int
    room_capacity_waste_seats: int
    section_daily_load_spread_periods: int


@dataclass(frozen=True)
class AcademicSolveResult:
    status: SolverStatus
    assignments: tuple[AcademicAssignment, ...] = ()
    metrics: OptimizationMetrics | None = None
    solver_wall_time_seconds: float = 0.0
    details: str | None = None
