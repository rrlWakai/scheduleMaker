from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from collections.abc import Callable

import httpx
import pytest
from ortools.sat.python import cp_model

import ccsync_scheduler.contract_solver as contract_solver
from ccsync_scheduler.contract import SolveResponse
from ccsync_scheduler.contract import SolveRequest
from ccsync_scheduler.contract_validation import (
    ContractIssue,
    validate_request,
)
from ccsync_scheduler.contract_solver import (
    ContractSolverError,
    solve_contract_request,
)
from ccsync_scheduler.main import app
from test_contract import make_request, refresh_hash


def _validated_request(data: dict) -> SolveRequest:
    refresh_hash(data)
    return validate_request(data)


def _published_baseline_request(
    *,
    day: int = 1,
    start: str = "09:00",
    room_id: str = "30",
) -> dict:
    data = make_request()
    data["professors"][0]["availability_windows"] = []
    data["generation_scope"]["reoptimized_published_schedule_ids"] = [
        "schedule-8001"
    ]
    data["published_timetable_baseline"] = [
        {
            "schedule_id": "schedule-8001",
            "subject_offering_id": "7001",
            "meeting_instance_id": "req-7001-lecture:1",
            "day_of_week": day,
            "start_time": start,
            "end_time": "10:30" if start == "09:00" else "09:30",
            "section_ids": ["201"],
            "professor_ids": ["501"],
            "room_allocations": [
                {"allocation_id": "allocation-8001", "room_id": room_id}
            ],
        }
    ]
    return refresh_hash(data)


def test_real_minute_duration_and_fixed_professor_assignment() -> None:
    response = solve_contract_request(validate_request(make_request()))

    assert response.solver_status in {"OPTIMAL", "FEASIBLE"}
    assert response.validation_summary.complete_candidate
    assert len(response.assignments) == 1
    assignment = response.assignments[0]
    assert assignment.professor_id == "501"
    assert assignment.professor_assignments[0].faculty_assignment_id == "9001"
    assert assignment.duration_minutes == 90
    start = int(assignment.start_time[:2]) * 60 + int(assignment.start_time[3:])
    end = int(assignment.end_time[:2]) * 60 + int(assignment.end_time[3:])
    assert end - start == 90


def test_variable_duration_occurrences_do_not_collide() -> None:
    data = make_request()
    data["offerings"][0]["meeting_requirements"][0]["frequency_per_week"] = 2
    response = solve_contract_request(_validated_request(data))

    assert len(response.assignments) == 2
    intervals = sorted(
        (
            item.day_of_week,
            int(item.start_time[:2]) * 60 + int(item.start_time[3:]),
            int(item.end_time[:2]) * 60 + int(item.end_time[3:]),
        )
        for item in response.assignments
    )
    for (first_day, _, first_end), (second_day, second_start, _) in zip(
        intervals, intervals[1:]
    ):
        assert first_day != second_day or first_end <= second_start


def test_professor_availability_is_enforced() -> None:
    data = make_request()
    data["professors"][0]["availability_windows"].append(
        {
            "day_of_week": 2,
            "start_time": "09:00",
            "end_time": "10:30",
            "availability_type": "unavailable",
        }
    )
    response = solve_contract_request(_validated_request(data))

    assert response.solver_status in {"OPTIMAL", "FEASIBLE"}
    assert response.assignments[0].day_of_week != 2


def test_full_time_exception_adds_authorized_thursday() -> None:
    data = make_request()
    data["academic_term"]["applicable_days"] = [1, 2, 3, 4]
    data["time_grid"]["days"].append(
        {
            "day_of_week": 4,
            "operating_start": "08:00",
            "operating_end": "17:00",
            "valid_start_times": ["08:00", "09:00", "10:00"],
        }
    )
    data["offerings"][0]["meeting_requirements"][0]["allowed_days"] = [4]

    no_exception = dict(data)
    no_exception["full_time_workday_policy"] = {
        **data["full_time_workday_policy"],
        "standard_days": [1, 2, 3],
        "exceptions": [],
    }
    result_without_exception = solve_contract_request(_validated_request(no_exception))
    with_exception = dict(data)
    with_exception["full_time_workday_policy"] = {
        **data["full_time_workday_policy"],
        "standard_days": [1, 2, 3],
        "exceptions": [
            {"professor_id": "501", "authorized_additional_days": [4]}
        ],
    }
    result_with_exception = solve_contract_request(_validated_request(with_exception))

    assert result_without_exception.solver_status == "INFEASIBLE"
    assert result_with_exception.solver_status in {"OPTIMAL", "FEASIBLE"}
    assert result_with_exception.assignments[0].day_of_week == 4


def test_part_time_target_is_a_soft_objective_in_real_minutes() -> None:
    data = make_request()
    data["professors"][0]["employment_type"] = "part_time"
    data["professors"][0]["part_time_weekly_target_minutes"] = 120
    data["full_time_workday_policy"] = None
    response = solve_contract_request(_validated_request(data))

    metric = next(item for item in response.workload_metrics if item.professor_id == "501")
    assert metric.assigned_teaching_minutes == 90
    assert metric.weekly_target_minutes == 120
    assert metric.signed_target_deviation_minutes == -30
    assert metric.absolute_target_deviation_minutes == 30


def test_preferred_windows_are_a_soft_objective_after_hard_availability() -> None:
    data = make_request()
    data["professors"][0]["availability_windows"] = [
        {
            "day_of_week": 1,
            "start_time": "08:00",
            "end_time": "10:00",
            "availability_type": "available",
        },
        {
            "day_of_week": 1,
            "start_time": "10:00",
            "end_time": "12:00",
            "availability_type": "preferred",
        },
    ]
    weights = data["solver_config"]["objective_weights"]
    for name in weights:
        weights[name] = 0
    weights["preferred_window_miss_per_meeting"] = 5
    weights["published_placement_change_per_meeting"] = 1
    response = solve_contract_request(_validated_request(data))

    assert response.assignments[0].start_time == "10:00"
    assert not any(item.code == "PREFERRED_WINDOW_MISSED" for item in response.warnings)


@pytest.mark.parametrize(
    ("change", "diagnostic_code"),
    [
        (
            lambda data: [
                room.update(capacity=20) for room in data["rooms"]
            ],
            "NO_VALID_PLACEMENT",
        ),
        (
            lambda data: data["offerings"][0]["meeting_requirements"][0].__setitem__(
                "required_room_type", "laboratory"
            ),
            "NO_VALID_PLACEMENT",
        ),
    ],
)
def test_room_capacity_and_required_type_are_hard_constraints(
    change: Callable[[dict], object],
    diagnostic_code: str,
) -> None:
    data = make_request()
    change(data)
    response = solve_contract_request(_validated_request(data))

    assert response.solver_status == "INFEASIBLE"
    assert any(item.code == diagnostic_code for item in response.diagnostics)


def test_published_baseline_is_retained_when_valid_and_metrics_are_reported() -> None:
    data = _published_baseline_request()
    response = solve_contract_request(validate_request(data))

    assert response.assignments[0].day_of_week == 1
    assert response.assignments[0].start_time == "09:00"
    assert response.assignments[0].room_id == "30"
    assert response.placement_metrics.comparable_meeting_count == 1
    assert response.placement_metrics.retained_placement_count == 1
    assert response.placement_metrics.changed_placement_count == 0


def test_minimal_change_weight_is_configurable_but_only_soft() -> None:
    def solve_with_weight(weight: int):
        data = _published_baseline_request()
        data["rooms"][1]["capacity"] = 30
        weights = data["solver_config"]["objective_weights"]
        for name in weights:
            weights[name] = 0
        weights["room_capacity_waste_per_seat"] = 2
        weights["published_placement_change_per_meeting"] = weight
        return solve_contract_request(_validated_request(data))

    low_weight = solve_with_weight(1)
    high_weight = solve_with_weight(100)

    assert low_weight.assignments[0].room_id == "31"
    assert low_weight.placement_metrics.changed_placement_count == 1
    assert high_weight.assignments[0].room_id == "30"
    assert high_weight.placement_metrics.retained_placement_count == 1


def test_hard_availability_overrides_published_baseline_preference() -> None:
    data = _published_baseline_request(day=2)
    data["professors"][0]["availability_windows"] = [
        {
            "day_of_week": 1,
            "start_time": "08:00",
            "end_time": "12:00",
            "availability_type": "available",
        },
        {
            "day_of_week": 2,
            "start_time": "09:00",
            "end_time": "10:30",
            "availability_type": "unavailable",
        },
    ]
    response = solve_contract_request(_validated_request(data))

    assert response.assignments[0].day_of_week == 1
    assert response.placement_metrics.changed_placement_count == 1


def test_explicit_combined_class_covers_both_offerings_once() -> None:
    data = make_request()
    second_section = dict(data["sections"][0])
    second_section.update(id="202", code="BSIT-2B", name="BSIT Second Year B")
    data["sections"].append(second_section)

    second_offering = dict(data["offerings"][0])
    second_offering.update(
        id="7002",
        section_id="202",
        meeting_plan_id="plan-7002",
        meeting_requirements=[
            {
                **data["offerings"][0]["meeting_requirements"][0],
                "id": "req-7002-lecture",
            }
        ],
    )
    data["offerings"].append(second_offering)
    data["generation_scope"]["included_offering_ids"] = ["7001", "7002"]
    data["professor_assignments"].append(
        {
            **data["professor_assignments"][0],
            "id": "9002",
            "subject_offering_id": "7002",
        }
    )
    data["combined_classes"] = [
        {
            "id": "combined-1",
            "offering_ids": ["7001", "7002"],
            "shared_meeting_requirements": [
                {
                    "id": "shared-lecture",
                    "source_requirements": [
                        {
                            "offering_id": "7001",
                            "meeting_requirement_id": "req-7001-lecture",
                        },
                        {
                            "offering_id": "7002",
                            "meeting_requirement_id": "req-7002-lecture",
                        },
                    ],
                }
            ],
        }
    ]
    for room in data["rooms"]:
        room["capacity"] = 80
    response = solve_contract_request(_validated_request(data))

    assert len(response.assignments) == 1
    assert response.assignments[0].subject_offering_ids == ["7001", "7002"]
    assert response.validation_summary.represented_offering_count == 2
    assert len(response.assignments[0].professor_assignments) == 2


def test_infeasible_and_unknown_results_are_not_marked_as_candidates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    infeasible_data = make_request()
    for room in infeasible_data["rooms"]:
        room["capacity"] = 20
    infeasible = solve_contract_request(_validated_request(infeasible_data))
    assert infeasible.solver_status == "INFEASIBLE"
    assert infeasible.result_status == "INFEASIBLE"
    assert not infeasible.validation_summary.complete_candidate
    assert infeasible.time_limit_reached is False

    def unknown_solver(wall_time: float) -> type:
        class UnknownSolver:
            def __init__(self) -> None:
                self.parameters = SimpleNamespace()

            def Solve(self, model: cp_model.CpModel) -> int:
                return cp_model.UNKNOWN

            def WallTime(self) -> float:
                return wall_time

        return UnknownSolver

    monkeypatch.setattr(contract_solver.cp_model, "CpSolver", unknown_solver(0.001))
    unknown = solve_contract_request(validate_request(make_request()))
    assert unknown.solver_status == "UNKNOWN"
    assert unknown.result_status == "UNKNOWN"
    assert unknown.time_limit_reached is False
    assert unknown.diagnostics[-1].code == "UNKNOWN_NO_CANDIDATE"
    assert unknown.optimality_proven is False
    assert unknown.assignments == []

    monkeypatch.setattr(contract_solver.cp_model, "CpSolver", unknown_solver(30.001))
    timed_out = solve_contract_request(validate_request(make_request()))
    assert timed_out.solver_status == "UNKNOWN"
    assert timed_out.result_status == "TIME_LIMIT"
    assert timed_out.time_limit_reached is True
    assert timed_out.diagnostics[-1].code == "TIME_LIMIT_NO_CANDIDATE"

    def feasible_solver(wall_time: float) -> type:
        class FeasibleSolver:
            def __init__(self) -> None:
                self.parameters = SimpleNamespace()

            def Solve(self, model: cp_model.CpModel) -> int:
                return cp_model.FEASIBLE

            def BooleanValue(self, variable: cp_model.IntVar) -> bool:
                return variable.Index() == 0

            def ObjectiveValue(self) -> float:
                return 0.0

            def BestObjectiveBound(self) -> float:
                return 0.0

            def WallTime(self) -> float:
                return wall_time

        return FeasibleSolver

    monkeypatch.setattr(contract_solver.cp_model, "CpSolver", feasible_solver(0.001))
    feasible = solve_contract_request(validate_request(make_request()))
    assert feasible.solver_status == "FEASIBLE"
    assert feasible.result_status == "FEASIBLE"
    assert feasible.time_limit_reached is False
    assert feasible.optimality_proven is False
    assert feasible.validation_summary.complete_candidate

    monkeypatch.setattr(contract_solver.cp_model, "CpSolver", feasible_solver(30.001))
    feasible_at_limit = solve_contract_request(validate_request(make_request()))
    assert feasible_at_limit.result_status == "FEASIBLE"
    assert feasible_at_limit.time_limit_reached is True


def test_fixed_commitments_are_hard_constraints() -> None:
    data = make_request()
    commitment_rows = []
    for day in [1, 2, 3]:
        schedule_id = f"protected-{day}"
        commitment_rows.append(
            {
                "schedule_id": schedule_id,
                "subject_offering_id": "7001",
                "schedule_status": "draft",
                "day_of_week": day,
                "start_time": "08:00",
                "end_time": "17:00",
                "section_ids": ["201"],
                "professor_ids": ["501"],
                "room_allocations": [
                    {"allocation_id": f"allocation-{day}", "room_id": "30"}
                ],
            }
        )
    data["existing_commitments"] = commitment_rows
    data["generation_scope"]["fixed_schedule_ids"] = [
        item["schedule_id"] for item in commitment_rows
    ]
    response = solve_contract_request(_validated_request(data))

    assert response.solver_status == "INFEASIBLE"
    assert not response.validation_summary.complete_candidate


def test_independent_validation_failure_prevents_feasible_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        contract_solver,
        "validate_response_against_request",
        lambda request, response: [ContractIssue("assignments", "test_failure")],
    )

    with pytest.raises(ContractSolverError, match="Independent candidate validation failed"):
        solve_contract_request(validate_request(make_request()))


def test_solve_endpoint_validates_and_returns_contract_response() -> None:
    async def send(payload: bytes) -> httpx.Response:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://local.test",
            ) as client:
                return await client.post(
                    "/api/v1/solve",
                    content=payload,
                    headers={
                        "content-type": "application/json",
                        "X-CCSync-Key": "test-scheduler-key",
                    },
                )

    response = asyncio.run(send(json.dumps(make_request()).encode()))
    assert response.status_code == 200
    parsed = SolveResponse.model_validate(response.json())
    assert parsed.solver_status in {"OPTIMAL", "FEASIBLE"}
    assert parsed.validation_summary.complete_candidate

    invalid = make_request()
    invalid["unexpected"] = True
    rejected = asyncio.run(send(json.dumps(invalid).encode()))
    assert rejected.status_code == 422
