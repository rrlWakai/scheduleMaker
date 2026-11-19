from __future__ import annotations

import asyncio
import copy
import json

import httpx
import pytest
from pydantic import ValidationError

from ccsync_scheduler.contract import SolveResponse
from ccsync_scheduler.contract_validation import (
    ContractValidationError,
    canonical_snapshot_json,
    compute_snapshot_sha256,
    validate_request,
    validate_response_against_request,
)
from ccsync_scheduler.main import app


def make_request() -> dict:
    request = {
        "contract_version": "1.0",
        "metadata": {
            "generation_id": "a11ce000-1111-4111-8111-111111111111",
            "requested_at": "2026-01-15T10:00:00Z",
            "academic_term_id": "42",
            "snapshot_version": 1,
            "snapshot_sha256": "0" * 64,
            "correlation_id": "b22ce000-2222-4222-8222-222222222222",
        },
        "academic_term": {
            "id": "42",
            "academic_year": "2025-2026",
            "term": "second",
            "name": "Second Term",
            "starts_on": "2026-02-02",
            "ends_on": "2026-05-30",
            "status": "planned",
            "applicable_days": [1, 2, 3],
        },
        "time_grid": {
            "configuration_id": "grid-term-42",
            "version": 1,
            "academic_term_id": "42",
            "approval_status": "approved",
            "approved_at": "2026-01-10T09:00:00Z",
            "timezone": "Asia/Manila",
            "days": [
                {
                    "day_of_week": day,
                    "operating_start": "08:00",
                    "operating_end": "17:00",
                    "valid_start_times": ["08:00", "09:00", "10:00"],
                }
                for day in [1, 2, 3]
            ],
        },
        "generation_scope": {
            "academic_term_id": "42",
            "included_offering_ids": ["7001"],
            "reoptimized_published_schedule_ids": [],
            "preserved_draft_schedule_ids": [],
            "replacement_draft_schedule_ids": [],
            "fixed_schedule_ids": [],
        },
        "published_timetable_baseline": [],
        "programs": [
            {"id": "3", "code": "BSIT", "name": "Information Technology", "status": "active"}
        ],
        "year_levels": [{"id": "2", "level": 2, "name": "Second Year"}],
        "sections": [
            {
                "id": "201",
                "program_id": "3",
                "year_level_id": "2",
                "academic_term_id": "42",
                "code": "BSIT-2A",
                "name": "BSIT Second Year A",
                "capacity": 40,
                "status": "active",
                "availability_windows": [],
            }
        ],
        "subjects": [
            {
                "id": "88",
                "code": "IT201",
                "title": "Example Subject",
                "status": "active",
                "units": "3.0",
                "lecture_hours": "3.0",
                "lab_hours": "0.0",
            }
        ],
        "offerings": [
            {
                "id": "7001",
                "academic_term_id": "42",
                "section_id": "201",
                "subject_id": "88",
                "expected_enrollment": 30,
                "status": "open",
                "meeting_plan_id": "plan-7001",
                "meeting_plan_version": 1,
                "meeting_plan_approval_status": "approved",
                "meeting_plan_approved_at": "2026-01-12T10:00:00Z",
                "meeting_requirements": [
                    {
                        "id": "req-7001-lecture",
                        "meeting_type": "lecture",
                        "frequency_per_week": 1,
                        "duration_minutes": 90,
                        "modality": "face_to_face",
                        "required_room_type": None,
                        "mandatory": True,
                        "allowed_days": None,
                        "valid_start_times": None,
                    }
                ],
            }
        ],
        "professors": [
            {
                "id": "501",
                "employment_type": "full_time",
                "profile_status": "active",
                "account_status": "active",
                "availability_windows": [
                    {
                        "day_of_week": 1,
                        "start_time": "08:00",
                        "end_time": "12:00",
                        "availability_type": "available",
                    }
                ],
                "part_time_weekly_target_minutes": None,
            }
        ],
        "professor_assignments": [
            {
                "id": "9001",
                "professor_id": "501",
                "subject_offering_id": "7001",
                "assignment_role": "lead",
                "status": "active",
                "eligible_for_scheduling": True,
                "assigned_units": "3.0",
            }
        ],
        "full_time_workday_policy": {
            "policy_id": "policy-2026-1",
            "policy_version": 1,
            "academic_term_id": "42",
            "standard_days": [1, 2, 3],
            "exceptions": [],
        },
        "rooms": [
            {"id": "30", "capacity": 40, "room_type": "lecture", "status": "available"},
            {"id": "31", "capacity": 40, "room_type": "lecture", "status": "available"},
        ],
        "combined_classes": [],
        "existing_commitments": [],
        "solver_config": {
            "time_limit_ms": 30000,
            "worker_count": 1,
            "random_seed": 1,
            "objective_weights": {
                "part_time_target_deviation_per_minute": 10,
                "full_time_load_spread_per_minute": 2,
                "professor_gap_per_minute": 3,
                "section_gap_per_minute": 3,
                "room_capacity_waste_per_seat": 1,
                "section_daily_load_spread_per_minute": 2,
                "preferred_window_miss_per_meeting": 5,
                "optional_meeting_omission": 100,
                "published_placement_change_per_meeting": 1,
            },
        },
    }
    return refresh_hash(request)


def refresh_hash(request: dict) -> dict:
    request["metadata"]["snapshot_sha256"] = compute_snapshot_sha256(request)
    return request


def baseline_schedule(*, day: int = 1, start: str = "09:00") -> dict:
    return {
        "schedule_id": "schedule-8001",
        "subject_offering_id": "7001",
        "meeting_instance_id": "req-7001-lecture:1",
        "day_of_week": day,
        "start_time": start,
        "end_time": "10:30",
        "section_ids": ["201"],
        "professor_ids": ["501"],
        "room_allocations": [{"allocation_id": "allocation-1", "room_id": "30"}],
    }


def make_response(
    *,
    start: str = "09:00",
    room_id: str = "30",
    placement_metrics: dict | None = None,
) -> dict:
    return {
        "contract_version": "1.0",
        "generation_id": "a11ce000-1111-4111-8111-111111111111",
        "academic_term_id": "42",
        "snapshot_sha256": "0" * 64,
        "correlation_id": "b22ce000-2222-4222-8222-222222222222",
        "solver_status": "OPTIMAL",
        "result_status": "FEASIBLE",
        "started_at": "2026-01-15T10:00:01Z",
        "completed_at": "2026-01-15T10:00:02Z",
        "solver_execution_ms": 950,
        "objective_value": 0,
        "best_bound": 0,
        "optimality_proven": True,
        "time_limit_reached": False,
        "assignments": [
            {
                "meeting_instance_id": "req-7001-lecture:1",
                "source_requirement_id": "req-7001-lecture",
                "occurrence_number": 1,
                "subject_offering_ids": ["7001"],
                "section_ids": ["201"],
                "professor_id": "501",
                "professor_assignments": [
                    {
                        "subject_offering_id": "7001",
                        "faculty_assignment_id": "9001",
                        "assignment_role": "lead",
                    }
                ],
                "is_primary": True,
                "room_id": room_id,
                "meeting_type": "lecture",
                "modality": "face_to_face",
                "day_of_week": 1,
                "start_time": start,
                "end_time": f"{int(start[:2]) + (int(start[3:]) + 90) // 60:02d}:{(int(start[3:]) + 90) % 60:02d}",
                "duration_minutes": 90,
            }
        ],
        "validation_summary": {
            "independent_validation_status": "passed",
            "scheduled_meeting_count": 1,
            "required_mandatory_meeting_count": 1,
            "desired_meeting_count": 1,
            "required_offering_count": 1,
            "represented_offering_count": 1,
            "complete_candidate": True,
            "validation_errors": [],
        },
        "unscheduled_mandatory_meeting_ids": None,
        "diagnostics": [],
        "workload_metrics": [
            {
                "professor_id": "501",
                "employment_type": "full_time",
                "assigned_teaching_minutes": 90,
                "weekly_target_minutes": None,
                "signed_target_deviation_minutes": None,
                "absolute_target_deviation_minutes": None,
            }
        ],
        "full_time_distribution": {
            "minimum_assigned_minutes": 90,
            "maximum_assigned_minutes": 90,
            "spread_minutes": 0,
        },
        "placement_metrics": placement_metrics
        or {
            "comparable_meeting_count": 0,
            "retained_placement_count": 0,
            "changed_placement_count": 0,
            "unmatched_baseline_schedule_count": 0,
        },
        "warnings": [],
    }


def error_codes(request: dict) -> set[str]:
    try:
        validate_request(request)
    except ContractValidationError as exc:
        return {issue.code for issue in exc.issues}
    except ValidationError as exc:
        return {str(issue["type"]) for issue in exc.errors()}
    raise AssertionError("request unexpectedly validated")


def test_valid_contract_request() -> None:
    model = validate_request(make_request())

    assert model.metadata.academic_term_id == model.academic_term.id
    assert len(model.offerings) == 1


def test_missing_required_fields_and_unknown_fields_are_rejected() -> None:
    missing = make_request()
    del missing["solver_config"]
    with pytest.raises(ValidationError):
        validate_request(missing)

    unknown = make_request()
    unknown["unexpected"] = True
    with pytest.raises(ValidationError):
        validate_request(unknown)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("academic_term", "term"), "fall"),
        (("offerings", 0, "meeting_requirements", 0, "duration_minutes"), True),
        (("academic_term", "starts_on"), "2026-02-30"),
        (("time_grid", "days", 0, "operating_start"), "8:00"),
        (("offerings", 0, "meeting_requirements", 0, "duration_minutes"), 0),
    ],
)
def test_invalid_enum_types_dates_times_and_durations_are_rejected(
    path: tuple[str | int, ...],
    value: object,
) -> None:
    request = make_request()
    cursor = request
    for key in path[:-1]:
        cursor = cursor[key]
    cursor[path[-1]] = value
    refresh_hash(request)

    with pytest.raises(ValidationError):
        validate_request(request)


def test_duplicate_ids_and_broken_references_are_rejected() -> None:
    duplicate = make_request()
    duplicate["programs"].append(copy.deepcopy(duplicate["programs"][0]))
    refresh_hash(duplicate)
    assert "duplicate_id" in error_codes(duplicate)

    broken = make_request()
    broken["offerings"][0]["section_id"] = "missing-section"
    refresh_hash(broken)
    assert "unknown_reference" in error_codes(broken)


def test_missing_or_multiple_distinct_professors_are_rejected() -> None:
    missing = make_request()
    missing["professor_assignments"] = []
    refresh_hash(missing)
    assert "missing_professor_assignment" in error_codes(missing)

    ambiguous = make_request()
    ambiguous["professors"].append(
        {
            **ambiguous["professors"][0],
            "id": "502",
            "availability_windows": [],
        }
    )
    ambiguous["professor_assignments"].append(
        {
            **ambiguous["professor_assignments"][0],
            "id": "9002",
            "professor_id": "502",
        }
    )
    refresh_hash(ambiguous)
    assert "multiple_distinct_professors" in error_codes(ambiguous)


def test_scope_and_published_baseline_must_be_consistent() -> None:
    invalid_scope = make_request()
    invalid_scope["generation_scope"]["included_offering_ids"] = []
    refresh_hash(invalid_scope)
    assert "must_equal_offerings" in error_codes(invalid_scope)

    invalid_baseline = make_request()
    invalid_baseline["published_timetable_baseline"] = [baseline_schedule()]
    refresh_hash(invalid_baseline)
    assert "must_match_reoptimized_and_fixed_published_schedules" in error_codes(
        invalid_baseline
    )


def test_valid_baseline_is_context_and_metrics_compare_placement() -> None:
    request_data = make_request()
    request_data["generation_scope"]["reoptimized_published_schedule_ids"] = [
        "schedule-8001"
    ]
    request_data["published_timetable_baseline"] = [baseline_schedule()]
    refresh_hash(request_data)
    request = validate_request(request_data)

    response_data = make_response(
        start="10:00",
        room_id="31",
        placement_metrics={
            "comparable_meeting_count": 1,
            "retained_placement_count": 0,
            "changed_placement_count": 1,
            "unmatched_baseline_schedule_count": 0,
        },
    )
    response_data["snapshot_sha256"] = request.metadata.snapshot_sha256
    response = SolveResponse.model_validate(response_data)

    assert validate_response_against_request(request, response) == []

    retained_data = make_response(
        placement_metrics={
            "comparable_meeting_count": 1,
            "retained_placement_count": 1,
            "changed_placement_count": 0,
            "unmatched_baseline_schedule_count": 0,
        }
    )
    retained_data["snapshot_sha256"] = request.metadata.snapshot_sha256
    retained_response = SolveResponse.model_validate(retained_data)
    assert validate_response_against_request(request, retained_response) == []


def test_unlinked_baseline_is_reported_as_unmatched() -> None:
    request_data = make_request()
    request_data["generation_scope"]["reoptimized_published_schedule_ids"] = [
        "schedule-8001"
    ]
    baseline = baseline_schedule()
    baseline["meeting_instance_id"] = None
    request_data["published_timetable_baseline"] = [baseline]
    refresh_hash(request_data)
    request = validate_request(request_data)

    response_data = make_response(
        placement_metrics={
            "comparable_meeting_count": 0,
            "retained_placement_count": 0,
            "changed_placement_count": 0,
            "unmatched_baseline_schedule_count": 1,
        }
    )
    response_data["snapshot_sha256"] = request.metadata.snapshot_sha256
    response = SolveResponse.model_validate(response_data)

    assert validate_response_against_request(request, response) == []


def test_invalid_availability_workload_room_and_solver_settings_are_rejected() -> None:
    overlapping = make_request()
    overlapping["professors"][0]["availability_windows"].append(
        {
            "day_of_week": 1,
            "start_time": "11:00",
            "end_time": "13:00",
            "availability_type": "preferred",
        }
    )
    refresh_hash(overlapping)
    assert "overlapping_windows" in error_codes(overlapping)

    workload = make_request()
    workload["professors"][0]["employment_type"] = "part_time"
    refresh_hash(workload)
    assert "required_for_part_time" in error_codes(workload)

    room = make_request()
    room["rooms"][0]["capacity"] = 0
    refresh_hash(room)
    with pytest.raises(ValidationError):
        validate_request(room)

    solver_config = make_request()
    solver_config["solver_config"]["time_limit_ms"] = 300001
    refresh_hash(solver_config)
    with pytest.raises(ValidationError):
        validate_request(solver_config)

    invalid_worker_count = make_request()
    invalid_worker_count["solver_config"]["worker_count"] = 2_147_483_648
    refresh_hash(invalid_worker_count)
    with pytest.raises(ValidationError):
        validate_request(invalid_worker_count)

    invalid_seed = make_request()
    invalid_seed["solver_config"]["random_seed"] = 2_147_483_648
    refresh_hash(invalid_seed)
    with pytest.raises(ValidationError):
        validate_request(invalid_seed)


def test_response_rejects_incomplete_candidate_and_professor_changes() -> None:
    request = validate_request(make_request())
    incomplete = make_response()
    incomplete["snapshot_sha256"] = request.metadata.snapshot_sha256
    incomplete["validation_summary"]["complete_candidate"] = False
    with pytest.raises(ValidationError):
        SolveResponse.model_validate(incomplete)

    changed_professor = make_response()
    changed_professor["snapshot_sha256"] = request.metadata.snapshot_sha256
    changed_professor["assignments"][0]["professor_id"] = "999"
    response = SolveResponse.model_validate(changed_professor)
    assert "professor_assignment_mismatch" in {
        issue.code for issue in validate_response_against_request(request, response)
    }


def test_unsupported_and_missing_contract_versions_are_rejected_explicitly() -> None:
    unsupported = make_request()
    unsupported["contract_version"] = "1.1"
    refresh_hash(unsupported)
    with pytest.raises(ContractValidationError) as exc:
        validate_request(unsupported)
    assert exc.value.issues[0].code == "unsupported_contract_version"

    missing = make_request()
    del missing["contract_version"]
    refresh_hash(missing)
    with pytest.raises(ContractValidationError) as exc:
        validate_request(missing)
    assert exc.value.issues[0].code == "missing_contract_version"


@pytest.mark.parametrize(
    "path",
    [
        ("time_grid", "days", 0, "weekday"),
        ("professors", 0, "availability_windows", 0, "day_of_week"),
        ("offerings", 0, "meeting_requirements", 0, "allowed_days"),
    ],
)
def test_weekend_days_are_rejected(path: tuple[str | int, ...]) -> None:
    request = make_request()
    cursor = request
    for key in path[:-1]:
        cursor = cursor[key]
    if path[-1] == "allowed_days":
        cursor[path[-1]] = [6]
    else:
        cursor[path[-1]] = 6
    refresh_hash(request)

    with pytest.raises(ValidationError):
        validate_request(request)


def test_snapshot_hash_uses_exact_canonical_json() -> None:
    payload = {
        "contract_version": "1.0",
        "metadata": {"snapshot_sha256": "ignored"},
        "academic_term": {"id": "42"},
        "solver_config": {"time_limit_ms": 30000},
    }
    canonical = '{"academic_term":{"id":"42"},"solver_config":{"time_limit_ms":30000}}'

    assert canonical_snapshot_json(payload) == canonical
    assert compute_snapshot_sha256(payload) == (
        "8d87342fdc0f670215822c26e0d50d8bd3c9f227724b77a90ad464982a520d65"
    )


def test_response_result_status_and_term_must_match_request() -> None:
    request = validate_request(make_request())

    mismatched_status = make_response()
    mismatched_status["snapshot_sha256"] = request.metadata.snapshot_sha256
    mismatched_status["result_status"] = "TIME_LIMIT"
    mismatched_status["time_limit_reached"] = True
    with pytest.raises(ValidationError):
        SolveResponse.model_validate(mismatched_status)

    mismatched_term = make_response()
    mismatched_term["snapshot_sha256"] = request.metadata.snapshot_sha256
    mismatched_term["academic_term_id"] = "999"
    response = SolveResponse.model_validate(mismatched_term)
    assert "request_response_mismatch" in {
        issue.code for issue in validate_response_against_request(request, response)
    }


def test_validation_only_endpoint_and_safe_error_envelope() -> None:
    async def send(payload: bytes, content_type: str = "application/json") -> httpx.Response:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://local.test",
            ) as client:
                return await client.post(
                    "/api/v1/validate",
                    content=payload,
                    headers={
                        "content-type": content_type,
                        "X-CCSync-Key": "test-scheduler-key",
                    },
                )

    valid = asyncio.run(send(json.dumps(make_request()).encode()))
    assert valid.status_code == 200
    assert valid.json()["valid"] is True

    invalid = make_request()
    invalid["unexpected"] = "must not be echoed"
    response = asyncio.run(send(json.dumps(invalid).encode()))
    assert response.status_code == 422
    assert "must not be echoed" not in response.text

    unsupported_version = make_request()
    unsupported_version["contract_version"] = "1.1"
    refresh_hash(unsupported_version)
    unsupported = asyncio.run(send(json.dumps(unsupported_version).encode()))
    assert unsupported.status_code == 422
    assert unsupported.json()["error"]["details"][0]["code"] == (
        "unsupported_contract_version"
    )

    malformed = asyncio.run(send(b"{"))
    assert malformed.status_code == 400
    assert "traceback" not in malformed.text.lower()

    oversized = asyncio.run(send(b" " * (10 * 1024 * 1024 + 1)))
    assert oversized.status_code == 413
    assert oversized.json()["error"]["code"] == "request_too_large"
