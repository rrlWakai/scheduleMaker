from __future__ import annotations

import logging
import json
import os
import secrets
import uuid
from contextlib import asynccontextmanager
from typing import Literal

import ortools
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from ortools.sat.python import cp_model
from pydantic import BaseModel, ValidationError
from starlette.concurrency import run_in_threadpool

from ccsync_scheduler.contract import SolveRequest
from ccsync_scheduler.contract_validation import (
    ContractValidationError,
    pydantic_issues_to_dicts,
    validate_request,
    validation_issues_to_dicts,
)
from ccsync_scheduler.contract_solver import (
    ContractSolverError,
    solve_contract_request,
)

logger = logging.getLogger("ccsync_scheduler")
MAX_CONTRACT_BODY_BYTES = 10 * 1024 * 1024


class LiveResponse(BaseModel):
    status: Literal["alive"] = "alive"


class ReadyResponse(BaseModel):
    status: Literal["ready"] = "ready"
    checks: dict[str, str]


def _check_solver_runtime() -> None:
    model = cp_model.CpModel()
    variable = model.NewBoolVar("startup_check")
    model.Add(variable == 1)
    solver = cp_model.CpSolver()
    solver.parameters.num_search_workers = 1
    status = solver.Solve(model)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE) or solver.Value(variable) != 1:
        raise RuntimeError("OR-Tools CP-SAT startup check did not produce a valid solution.")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting CCSync local scheduling engine.")
    _check_solver_runtime()
    app.state.solver_ready = True
    logger.info("OR-Tools CP-SAT %s is ready.", ortools.__version__)
    try:
        yield
    finally:
        app.state.solver_ready = False
        logger.info("Stopped CCSync local scheduling engine.")


app = FastAPI(
    title="CCSync Local Scheduling Engine",
    version="0.1.0",
    lifespan=lifespan,
)


def _error_response(
    *,
    status_code: int,
    code: str,
    message: str,
    details: list[dict[str, str]],
    correlation_id: str | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "code": code,
                "message": message,
                "details": details,
                "correlation_id": correlation_id,
                "retryable": False,
            }
        },
    )


def _authorize_request(request: Request) -> JSONResponse | None:
    configured_key = os.environ.get("SCHEDULER_API_KEY", "").strip()
    if not configured_key:
        return _error_response(
            status_code=503,
            code="scheduler_auth_not_configured",
            message="The scheduling engine API key is not configured.",
            details=[{"path": "$", "code": "api_key_not_configured"}],
        )

    provided_key = request.headers.get("x-ccsync-key", "")
    if not provided_key or not secrets.compare_digest(
        provided_key.encode("utf-8"),
        configured_key.encode("utf-8"),
    ):
        return _error_response(
            status_code=401,
            code="invalid_api_key",
            message="A valid scheduler API key is required.",
            details=[{"path": "$", "code": "invalid_api_key"}],
        )

    return None


@app.get("/health/live", response_model=LiveResponse)
async def health_live() -> LiveResponse:
    return LiveResponse()


@app.get("/health/ready", response_model=ReadyResponse)
async def health_ready(request: Request) -> ReadyResponse | JSONResponse:
    auth_error = _authorize_request(request)
    if auth_error is not None:
        return auth_error

    if not getattr(request.app.state, "solver_ready", False):
        raise HTTPException(status_code=503, detail="Scheduling engine is not ready.")
    return ReadyResponse(
        checks={
            "application": "ready",
            "ortools": ortools.__version__,
        }
    )


def _duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


@app.post("/api/v1/validate")
async def validate_contract_request(request: Request) -> JSONResponse:
    auth_error = _authorize_request(request)
    if auth_error is not None:
        return auth_error

    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type != "application/json":
        return _error_response(
            status_code=415,
            code="unsupported_media_type",
            message="Content-Type must be application/json.",
            details=[{"path": "$", "code": "content_type_must_be_json"}],
        )

    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > MAX_CONTRACT_BODY_BYTES:
                return _error_response(
                    status_code=413,
                    code="request_too_large",
                    message="The request exceeds the configured JSON body limit.",
                    details=[{"path": "$", "code": "body_too_large"}],
                )
        except ValueError:
            return _error_response(
                status_code=400,
                code="invalid_content_length",
                message="The request has an invalid Content-Length header.",
                details=[{"path": "$", "code": "invalid_content_length"}],
            )

    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_CONTRACT_BODY_BYTES:
            return _error_response(
                status_code=413,
                code="request_too_large",
                message="The request exceeds the configured JSON body limit.",
                details=[{"path": "$", "code": "body_too_large"}],
            )
        body.extend(chunk)
    try:
        raw = json.loads(body, object_pairs_hook=_duplicate_json_keys)
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return _error_response(
            status_code=400,
            code="invalid_json",
            message="The request body is not valid JSON.",
            details=[{"path": "$", "code": "malformed_json"}],
        )

    correlation_id: str | None = None
    if isinstance(raw, dict):
        metadata = raw.get("metadata")
        if isinstance(metadata, dict):
            candidate = metadata.get("correlation_id")
            if isinstance(candidate, str):
                try:
                    parsed_id = uuid.UUID(candidate)
                except ValueError:
                    parsed_id = None
                if parsed_id is not None and str(parsed_id) == candidate:
                    correlation_id = candidate
    try:
        parsed = validate_request(raw)
    except ContractValidationError as exc:
        return _error_response(
            status_code=422,
            code="invalid_contract",
            message="The request does not satisfy contract version 1.0.",
            details=validation_issues_to_dicts(list(exc.issues)),
            correlation_id=correlation_id,
        )
    except ValidationError as exc:
        return _error_response(
            status_code=422,
            code="invalid_contract",
            message="The request does not satisfy contract version 1.0.",
            details=pydantic_issues_to_dicts(exc),
            correlation_id=correlation_id,
        )
    return JSONResponse(
        content={
            "valid": True,
            "generation_id": parsed.metadata.generation_id,
            "snapshot_sha256": parsed.metadata.snapshot_sha256,
            "correlation_id": parsed.metadata.correlation_id,
            "required_offering_count": len(parsed.offerings),
        }
    )


@app.post("/api/v1/solve")
async def solve_contract_endpoint(request: Request) -> JSONResponse:
    auth_error = _authorize_request(request)
    if auth_error is not None:
        return auth_error

    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type != "application/json":
        return _error_response(
            status_code=415,
            code="unsupported_media_type",
            message="Content-Type must be application/json.",
            details=[{"path": "$", "code": "content_type_must_be_json"}],
        )

    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            declared_length = int(content_length)
            if declared_length < 0:
                raise ValueError
            if declared_length > MAX_CONTRACT_BODY_BYTES:
                return _error_response(
                    status_code=413,
                    code="request_too_large",
                    message="The request exceeds the configured JSON body limit.",
                    details=[{"path": "$", "code": "body_too_large"}],
                )
        except ValueError:
            return _error_response(
                status_code=400,
                code="invalid_content_length",
                message="The request has an invalid Content-Length header.",
                details=[{"path": "$", "code": "invalid_content_length"}],
            )

    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_CONTRACT_BODY_BYTES:
            return _error_response(
                status_code=413,
                code="request_too_large",
                message="The request exceeds the configured JSON body limit.",
                details=[{"path": "$", "code": "body_too_large"}],
            )
        body.extend(chunk)
    try:
        raw = json.loads(body, object_pairs_hook=_duplicate_json_keys)
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError, RecursionError):
        return _error_response(
            status_code=400,
            code="invalid_json",
            message="The request body is not valid JSON.",
            details=[{"path": "$", "code": "malformed_json"}],
        )

    correlation_id: str | None = None
    if isinstance(raw, dict):
        metadata = raw.get("metadata")
        if isinstance(metadata, dict):
            candidate = metadata.get("correlation_id")
            if isinstance(candidate, str):
                try:
                    parsed_id = uuid.UUID(candidate)
                except ValueError:
                    parsed_id = None
                if parsed_id is not None and str(parsed_id) == candidate:
                    correlation_id = candidate
    try:
        parsed: SolveRequest = validate_request(raw)
    except ContractValidationError as exc:
        return _error_response(
            status_code=422,
            code="invalid_contract",
            message="The request does not satisfy contract version 1.0.",
            details=validation_issues_to_dicts(list(exc.issues)),
            correlation_id=correlation_id,
        )
    except ValidationError as exc:
        return _error_response(
            status_code=422,
            code="invalid_contract",
            message="The request does not satisfy contract version 1.0.",
            details=pydantic_issues_to_dicts(exc),
            correlation_id=correlation_id,
        )

    try:
        response = await run_in_threadpool(solve_contract_request, parsed)
    except ContractSolverError as exc:
        logger.exception("Contract solve failed (%s).", exc.code)
        return _error_response(
            status_code=500,
            code=exc.code,
            message="The scheduling solver could not produce a validated result.",
            details=[{"path": "$", "code": exc.code}],
            correlation_id=correlation_id,
        )
    return JSONResponse(content=response.model_dump(mode="json"))
