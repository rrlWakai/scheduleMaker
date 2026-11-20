from __future__ import annotations

import re
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)


CONTRACT_VERSION = "1.0"

Identifier = Annotated[str, StringConstraints(min_length=1, pattern=r"^\S+$")]
UuidString = Annotated[
    str,
    StringConstraints(
        pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
    ),
]
Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
ClockTime = Annotated[str, StringConstraints(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")]
DecimalString = Annotated[
    str,
    StringConstraints(pattern=r"^(?:0|[1-9]\d*)(?:\.\d+)?$"),
]
PositiveInt = Annotated[int, Field(gt=0)]
NonNegativeInt = Annotated[int, Field(ge=0)]
RandomSeed = Annotated[int, Field(ge=0, le=2_147_483_647)]
WorkerCount = Annotated[int, Field(gt=0, le=2_147_483_647)]
Weekday = Annotated[int, Field(ge=1, le=5)]


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


ResultStatus = Literal["FEASIBLE", "INFEASIBLE", "UNKNOWN", "TIME_LIMIT"]


def result_status_for(solver_status: str, time_limit_reached: bool) -> ResultStatus:
    if solver_status in {"OPTIMAL", "FEASIBLE"}:
        return "FEASIBLE"
    if solver_status == "INFEASIBLE":
        return "INFEASIBLE"
    return "TIME_LIMIT" if time_limit_reached else "UNKNOWN"


def _validate_utc_timestamp(value: str) -> str:
    if not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z",
        value,
    ):
        raise ValueError("must be an RFC 3339 UTC timestamp ending in Z")
    try:
        datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ValueError("must be a valid RFC 3339 timestamp") from exc
    return value


def _validate_date(value: str) -> str:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("must be an ISO YYYY-MM-DD date")
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("must be a valid calendar date") from exc
    return value


def _validate_clock(value: str) -> str:
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
        raise ValueError("must be a strict HH:mm time")
    time.fromisoformat(value)
    return value


def _validate_decimal_string(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("must be a decimal string") from exc
    if not parsed.is_finite() or parsed < 0:
        raise ValueError("must be a finite nonnegative decimal")
    return value


class GenerationMetadata(ContractModel):
    generation_id: UuidString
    requested_at: str
    academic_term_id: Identifier
    snapshot_version: PositiveInt
    snapshot_sha256: Sha256
    correlation_id: UuidString | None = None

    _timestamp = field_validator("requested_at")(_validate_utc_timestamp)


class AcademicTerm(ContractModel):
    id: Identifier
    academic_year: Annotated[str, StringConstraints(pattern=r"^\d{4}-\d{4}$")]
    term: Literal["first", "second", "summer"]
    name: Annotated[str, StringConstraints(min_length=1)]
    starts_on: str
    ends_on: str
    status: Literal["active", "planned"]
    applicable_days: list[Weekday]

    _dates = field_validator("starts_on", "ends_on")(_validate_date)

    @field_validator("applicable_days")
    @classmethod
    def unique_days(cls, value: list[int]) -> list[int]:
        if not value or value != sorted(set(value)):
            raise ValueError("must be nonempty, unique, and sorted")
        return value

    @model_validator(mode="after")
    def valid_range_and_year(self) -> AcademicTerm:
        start = date.fromisoformat(self.starts_on)
        end = date.fromisoformat(self.ends_on)
        if end < start:
            raise ValueError("ends_on must not precede starts_on")
        first_year, second_year = map(int, self.academic_year.split("-"))
        if second_year != first_year + 1:
            raise ValueError("academic_year must contain consecutive years")
        return self


class TimeGridDay(ContractModel):
    day_of_week: Weekday
    operating_start: ClockTime
    operating_end: ClockTime
    valid_start_times: list[ClockTime]

    @field_validator("valid_start_times")
    @classmethod
    def unique_starts(cls, value: list[str]) -> list[str]:
        if not value or value != sorted(set(value)):
            raise ValueError("must be nonempty, unique, and sorted")
        return value

    @model_validator(mode="after")
    def valid_hours(self) -> TimeGridDay:
        opening = time.fromisoformat(self.operating_start)
        closing = time.fromisoformat(self.operating_end)
        if opening >= closing:
            raise ValueError("operating_start must precede operating_end")
        if any(not opening <= time.fromisoformat(item) < closing for item in self.valid_start_times):
            raise ValueError("valid starts must fall inside operating hours")
        return self


class TimeGrid(ContractModel):
    configuration_id: Identifier
    version: PositiveInt
    academic_term_id: Identifier
    approval_status: Literal["approved"]
    approved_at: str
    timezone: Annotated[str, StringConstraints(min_length=1)]
    days: list[TimeGridDay]

    _timestamp = field_validator("approved_at")(_validate_utc_timestamp)

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("must be a recognized IANA timezone") from exc
        return value


class AvailabilityWindow(ContractModel):
    day_of_week: Weekday
    start_time: ClockTime
    end_time: ClockTime
    availability_type: Literal["available", "unavailable", "preferred"]

    @model_validator(mode="after")
    def positive_duration(self) -> AvailabilityWindow:
        if time.fromisoformat(self.start_time) >= time.fromisoformat(self.end_time):
            raise ValueError("end_time must be later than start_time")
        return self


class Program(ContractModel):
    id: Identifier
    code: Identifier
    name: Annotated[str, StringConstraints(min_length=1)]
    status: Literal["active"]


class YearLevel(ContractModel):
    id: Identifier
    level: PositiveInt
    name: Annotated[str, StringConstraints(min_length=1)]


class Section(ContractModel):
    id: Identifier
    program_id: Identifier
    year_level_id: Identifier
    academic_term_id: Identifier
    code: Identifier
    name: Annotated[str, StringConstraints(min_length=1)]
    capacity: NonNegativeInt | None
    status: Literal["active"]
    availability_windows: list[AvailabilityWindow]


class Subject(ContractModel):
    id: Identifier
    code: Identifier
    title: Annotated[str, StringConstraints(min_length=1)]
    status: Literal["active"]
    units: DecimalString | None
    lecture_hours: DecimalString | None
    lab_hours: DecimalString | None

    _decimals = field_validator("units", "lecture_hours", "lab_hours")(
        _validate_decimal_string
    )


class MeetingRequirement(ContractModel):
    id: Identifier
    meeting_type: Literal["lecture", "laboratory", "seminar", "other"]
    frequency_per_week: PositiveInt
    duration_minutes: PositiveInt
    modality: Literal["face_to_face", "online", "hybrid"]
    required_room_type: Literal["lecture", "laboratory", "seminar", "office", "other"] | None
    required_equipment: list[Identifier] = Field(default_factory=list)
    mandatory: bool
    allowed_days: list[Weekday] | None
    valid_start_times: list[ClockTime] | None

    @field_validator("required_equipment")
    @classmethod
    def unique_required_equipment(cls, value: list[str]) -> list[str]:
        if value != sorted(set(value)):
            raise ValueError("must be unique and sorted")
        return value

    @field_validator("allowed_days")
    @classmethod
    def unique_allowed_days(cls, value: list[int] | None) -> list[int] | None:
        if value is not None and (not value or value != sorted(set(value))):
            raise ValueError("must be nonempty, unique, and sorted when specified")
        return value

    @field_validator("valid_start_times")
    @classmethod
    def unique_valid_starts(cls, value: list[str] | None) -> list[str] | None:
        if value is not None and (not value or value != sorted(set(value))):
            raise ValueError("must be nonempty, unique, and sorted when specified")
        return value

    @model_validator(mode="after")
    def modality_room_rule(self) -> MeetingRequirement:
        if self.modality == "online" and self.required_room_type is not None:
            raise ValueError("online requirements must not require a physical room type")
        if self.modality == "online" and self.required_equipment:
            raise ValueError("online requirements must not require physical room equipment")
        return self


class Offering(ContractModel):
    id: Identifier
    academic_term_id: Identifier
    section_id: Identifier
    subject_id: Identifier
    expected_enrollment: NonNegativeInt | None
    status: Literal["open"]
    meeting_plan_id: Identifier
    meeting_plan_version: PositiveInt
    meeting_plan_approval_status: Literal["approved"]
    meeting_plan_approved_at: str
    meeting_requirements: list[MeetingRequirement]

    _timestamp = field_validator("meeting_plan_approved_at")(_validate_utc_timestamp)


class Professor(ContractModel):
    id: Identifier
    employment_type: Literal["full_time", "part_time", "visiting"]
    profile_status: Literal["active"]
    account_status: Literal["active"]
    availability_windows: list[AvailabilityWindow]
    part_time_weekly_target_minutes: NonNegativeInt | None


class ProfessorAssignment(ContractModel):
    id: Identifier
    professor_id: Identifier
    subject_offering_id: Identifier
    assignment_role: Literal["lead", "co_instructor", "lab_instructor", "substitute"]
    status: Literal["active"]
    eligible_for_scheduling: Literal[True]
    assigned_units: DecimalString | None

    _decimal = field_validator("assigned_units")(_validate_decimal_string)


class WorkdayException(ContractModel):
    professor_id: Identifier
    authorized_additional_days: list[Weekday]

    @field_validator("authorized_additional_days")
    @classmethod
    def unique_days(cls, value: list[int]) -> list[int]:
        if value != sorted(set(value)):
            raise ValueError("must be unique and sorted")
        return value


class FullTimeWorkdayPolicy(ContractModel):
    policy_id: Identifier
    policy_version: PositiveInt
    academic_term_id: Identifier
    standard_days: list[Weekday]
    exceptions: list[WorkdayException]

    @field_validator("standard_days")
    @classmethod
    def unique_days(cls, value: list[int]) -> list[int]:
        if not value or value != sorted(set(value)):
            raise ValueError("must be nonempty, unique, and sorted")
        return value


class Room(ContractModel):
    id: Identifier
    capacity: PositiveInt
    room_type: Literal["lecture", "laboratory", "seminar", "office", "other"]
    status: Literal["available"]
    equipment: list[Identifier] = Field(default_factory=list)

    @field_validator("equipment")
    @classmethod
    def unique_equipment(cls, value: list[str]) -> list[str]:
        if value != sorted(set(value)):
            raise ValueError("must be unique and sorted")
        return value


class CombinedSourceRequirement(ContractModel):
    offering_id: Identifier
    meeting_requirement_id: Identifier


class SharedMeetingRequirement(ContractModel):
    id: Identifier
    source_requirements: list[CombinedSourceRequirement]


class CombinedClass(ContractModel):
    id: Identifier
    offering_ids: list[Identifier]
    shared_meeting_requirements: list[SharedMeetingRequirement]


class GenerationScope(ContractModel):
    academic_term_id: Identifier
    included_offering_ids: list[Identifier]
    reoptimized_published_schedule_ids: list[Identifier]
    preserved_draft_schedule_ids: list[Identifier]
    replacement_draft_schedule_ids: list[Identifier]
    fixed_schedule_ids: list[Identifier]


class RoomAllocation(ContractModel):
    allocation_id: Identifier
    room_id: Identifier


class PublishedScheduleBaseline(ContractModel):
    schedule_id: Identifier
    subject_offering_id: Identifier
    meeting_instance_id: Identifier | None = None
    day_of_week: Weekday
    start_time: ClockTime
    end_time: ClockTime
    section_ids: list[Identifier]
    professor_ids: list[Identifier]
    room_allocations: list[RoomAllocation]

    @model_validator(mode="after")
    def valid_interval(self) -> PublishedScheduleBaseline:
        if time.fromisoformat(self.start_time) >= time.fromisoformat(self.end_time):
            raise ValueError("end_time must be later than start_time")
        if not self.section_ids:
            raise ValueError("section_ids must not be empty")
        return self


class ExistingCommitment(ContractModel):
    schedule_id: Identifier
    subject_offering_id: Identifier
    schedule_status: Literal["published", "draft"]
    day_of_week: Weekday
    start_time: ClockTime
    end_time: ClockTime
    section_ids: list[Identifier]
    professor_ids: list[Identifier]
    room_allocations: list[RoomAllocation]

    @model_validator(mode="after")
    def valid_interval(self) -> ExistingCommitment:
        if time.fromisoformat(self.start_time) >= time.fromisoformat(self.end_time):
            raise ValueError("end_time must be later than start_time")
        if not self.section_ids:
            raise ValueError("section_ids must not be empty")
        return self


class ObjectiveWeights(ContractModel):
    part_time_target_deviation_per_minute: NonNegativeInt
    full_time_load_spread_per_minute: NonNegativeInt
    professor_gap_per_minute: NonNegativeInt
    section_gap_per_minute: NonNegativeInt
    room_capacity_waste_per_seat: NonNegativeInt
    section_daily_load_spread_per_minute: NonNegativeInt
    preferred_window_miss_per_meeting: NonNegativeInt
    optional_meeting_omission: NonNegativeInt
    published_placement_change_per_meeting: PositiveInt


class SolverConfig(ContractModel):
    time_limit_ms: PositiveInt
    worker_count: WorkerCount
    random_seed: RandomSeed
    objective_weights: ObjectiveWeights

    @field_validator("time_limit_ms")
    @classmethod
    def bounded_time_limit(cls, value: int) -> int:
        if value > 300_000:
            raise ValueError("must not exceed 300000 milliseconds")
        return value


class SolveRequest(ContractModel):
    contract_version: Literal["1.0"]
    metadata: GenerationMetadata
    academic_term: AcademicTerm
    time_grid: TimeGrid
    generation_scope: GenerationScope
    published_timetable_baseline: list[PublishedScheduleBaseline]
    programs: list[Program]
    year_levels: list[YearLevel]
    sections: list[Section]
    subjects: list[Subject]
    offerings: list[Offering]
    professors: list[Professor]
    professor_assignments: list[ProfessorAssignment]
    full_time_workday_policy: FullTimeWorkdayPolicy | None
    rooms: list[Room]
    combined_classes: list[CombinedClass]
    existing_commitments: list[ExistingCommitment]
    solver_config: SolverConfig


class ProfessorAssignmentResult(ContractModel):
    subject_offering_id: Identifier
    faculty_assignment_id: Identifier
    assignment_role: Literal["lead", "co_instructor", "lab_instructor", "substitute"]


class MeetingAssignment(ContractModel):
    meeting_instance_id: Identifier
    source_requirement_id: Identifier
    occurrence_number: PositiveInt
    subject_offering_ids: list[Identifier]
    section_ids: list[Identifier]
    professor_id: Identifier
    professor_assignments: list[ProfessorAssignmentResult]
    is_primary: Literal[True]
    room_id: Identifier | None
    meeting_type: Literal["lecture", "laboratory", "seminar", "other"]
    modality: Literal["face_to_face", "online", "hybrid"]
    day_of_week: Weekday
    start_time: ClockTime
    end_time: ClockTime
    duration_minutes: PositiveInt

    @model_validator(mode="after")
    def valid_interval(self) -> MeetingAssignment:
        if time.fromisoformat(self.start_time) >= time.fromisoformat(self.end_time):
            raise ValueError("end_time must be later than start_time")
        if self.modality == "online" and self.room_id is not None:
            raise ValueError("online assignments must not have a physical room")
        if self.modality != "online" and self.room_id is None:
            raise ValueError("in-person assignments require a physical room")
        if not self.subject_offering_ids or not self.section_ids:
            raise ValueError("assignment offering and section lists must not be empty")
        if len(set(self.subject_offering_ids)) != len(self.subject_offering_ids):
            raise ValueError("subject_offering_ids must be unique")
        if len(set(self.section_ids)) != len(self.section_ids):
            raise ValueError("section_ids must be unique")
        faculty_ids = [item.faculty_assignment_id for item in self.professor_assignments]
        if len(set(faculty_ids)) != len(faculty_ids):
            raise ValueError("professor_assignments must be unique")
        return self


class ValidationSummary(ContractModel):
    independent_validation_status: Literal["passed", "failed", "not_run"]
    scheduled_meeting_count: NonNegativeInt
    required_mandatory_meeting_count: NonNegativeInt
    desired_meeting_count: NonNegativeInt
    required_offering_count: NonNegativeInt
    represented_offering_count: NonNegativeInt
    complete_candidate: bool
    validation_errors: list[Annotated[str, StringConstraints(min_length=1)]]

    @model_validator(mode="after")
    def coherent_counts(self) -> ValidationSummary:
        if self.represented_offering_count > self.required_offering_count:
            raise ValueError("represented offering count exceeds required offering count")
        if self.required_mandatory_meeting_count > self.desired_meeting_count:
            raise ValueError("mandatory meeting count exceeds desired meeting count")
        if self.independent_validation_status == "passed" and self.validation_errors:
            raise ValueError("passed validation must have no validation errors")
        if self.complete_candidate and (
            self.independent_validation_status != "passed"
            or self.represented_offering_count != self.required_offering_count
            or self.scheduled_meeting_count < self.required_mandatory_meeting_count
        ):
            raise ValueError("complete candidate counts or validation status are inconsistent")
        return self


class Diagnostic(ContractModel):
    code: Identifier
    message: Annotated[str, StringConstraints(min_length=1)]
    evidence: Literal["solver_proven", "input_check", "heuristic"]
    meeting_requirement_ids: list[Identifier]
    related_entity_ids: list[Identifier]


class WorkloadMetric(ContractModel):
    professor_id: Identifier
    employment_type: Literal["full_time", "part_time", "visiting"]
    assigned_teaching_minutes: NonNegativeInt
    weekly_target_minutes: NonNegativeInt | None
    signed_target_deviation_minutes: int | None
    absolute_target_deviation_minutes: NonNegativeInt | None

    @model_validator(mode="after")
    def workload_values_match(self) -> WorkloadMetric:
        if self.employment_type == "part_time":
            if self.weekly_target_minutes is None:
                raise ValueError("part-time workload metrics require a weekly target")
            deviation = self.assigned_teaching_minutes - self.weekly_target_minutes
            if self.signed_target_deviation_minutes != deviation:
                raise ValueError("signed workload deviation does not match actual and target")
            if self.absolute_target_deviation_minutes != abs(deviation):
                raise ValueError("absolute workload deviation does not match")
        elif any(
            value is not None
            for value in (
                self.weekly_target_minutes,
                self.signed_target_deviation_minutes,
                self.absolute_target_deviation_minutes,
            )
        ):
            raise ValueError("non-part-time workload metrics must not include a target")
        return self


class FullTimeDistribution(ContractModel):
    minimum_assigned_minutes: NonNegativeInt
    maximum_assigned_minutes: NonNegativeInt
    spread_minutes: NonNegativeInt

    @model_validator(mode="after")
    def coherent_spread(self) -> FullTimeDistribution:
        if self.maximum_assigned_minutes < self.minimum_assigned_minutes:
            raise ValueError("maximum workload must not be below minimum workload")
        if self.spread_minutes != self.maximum_assigned_minutes - self.minimum_assigned_minutes:
            raise ValueError("spread does not match minimum and maximum workload")
        return self


class PlacementMetrics(ContractModel):
    comparable_meeting_count: NonNegativeInt
    retained_placement_count: NonNegativeInt
    changed_placement_count: NonNegativeInt
    unmatched_baseline_schedule_count: NonNegativeInt

    @model_validator(mode="after")
    def coherent_counts(self) -> PlacementMetrics:
        if self.retained_placement_count + self.changed_placement_count != self.comparable_meeting_count:
            raise ValueError("retained and changed counts must equal comparable count")
        return self


class Warning(ContractModel):
    code: Identifier
    message: Annotated[str, StringConstraints(min_length=1)]
    related_entity_ids: list[Identifier] | None = None


class SolveResponse(ContractModel):
    contract_version: Literal["1.0"]
    generation_id: UuidString
    academic_term_id: Identifier
    snapshot_sha256: Sha256
    correlation_id: UuidString | None
    solver_status: Literal["OPTIMAL", "FEASIBLE", "INFEASIBLE", "UNKNOWN"]
    result_status: ResultStatus
    started_at: str
    completed_at: str
    solver_execution_ms: NonNegativeInt
    objective_value: float | None
    best_bound: float | None
    optimality_proven: bool
    time_limit_reached: bool
    assignments: list[MeetingAssignment]
    validation_summary: ValidationSummary
    unscheduled_mandatory_meeting_ids: list[Identifier] | None
    diagnostics: list[Diagnostic]
    workload_metrics: list[WorkloadMetric]
    full_time_distribution: FullTimeDistribution | None
    placement_metrics: PlacementMetrics
    warnings: list[Warning]

    _timestamps = field_validator("started_at", "completed_at")(_validate_utc_timestamp)

    @model_validator(mode="after")
    def coherent_result(self) -> SolveResponse:
        started = datetime.fromisoformat(self.started_at[:-1] + "+00:00")
        completed = datetime.fromisoformat(self.completed_at[:-1] + "+00:00")
        if completed < started:
            raise ValueError("completed_at must not precede started_at")
        if self.result_status != result_status_for(self.solver_status, self.time_limit_reached):
            raise ValueError("result_status does not match solver status and time-limit state")
        if self.solver_status == "OPTIMAL" and not self.optimality_proven:
            raise ValueError("OPTIMAL status requires optimality_proven")
        if self.solver_status != "OPTIMAL" and self.optimality_proven:
            raise ValueError("only OPTIMAL status can prove optimality")
        if self.solver_status == "OPTIMAL" and self.time_limit_reached:
            raise ValueError("OPTIMAL status cannot report that the time limit ended search")
        if len(self.assignments) != self.validation_summary.scheduled_meeting_count:
            raise ValueError("scheduled_meeting_count must match assignments")
        instance_ids = [item.meeting_instance_id for item in self.assignments]
        if len(instance_ids) != len(set(instance_ids)):
            raise ValueError("meeting_instance_id values must be unique")
        workload_professor_ids = [item.professor_id for item in self.workload_metrics]
        if len(workload_professor_ids) != len(set(workload_professor_ids)):
            raise ValueError("workload metrics must be unique per professor")
        if self.result_status == "FEASIBLE":
            if not self.validation_summary.complete_candidate:
                raise ValueError("feasible solver results must be complete candidates")
            if self.validation_summary.independent_validation_status != "passed":
                raise ValueError("feasible solver results must pass independent validation")
        elif self.assignments or self.validation_summary.complete_candidate:
            raise ValueError("non-feasible solver results cannot contain a candidate")
        if self.result_status in {"INFEASIBLE", "UNKNOWN", "TIME_LIMIT"} and (
            self.validation_summary.independent_validation_status != "not_run"
        ):
            raise ValueError("results without a candidate must use not_run validation")
        return self


def decimal_value(value: str | None) -> Decimal | None:
    return Decimal(value) if value is not None else None


def date_value(value: str) -> date:
    return date.fromisoformat(value)


def clock_value(value: str) -> time:
    return time.fromisoformat(value)
