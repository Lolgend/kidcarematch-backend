"""Administrative subsidy rule configuration matrix and granular CRUD endpoints."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Body, Depends, HTTPException, Response, status
from sqlmodel import Session, col, select

from kidcarematch_backend.auth.security import get_current_admin
from kidcarematch_backend.db.db import SubsidyRule, get_session
from kidcarematch_backend.schemas.admin import (
    BulkSubsidyRulesRequest,
    SubsidyRuleCreate,
    SubsidyRuleResponse,
    SubsidyRuleUpdate,
)

router = APIRouter(dependencies=[Depends(get_current_admin)])


@router.get(
    "",
    response_model=list[SubsidyRuleResponse],
    status_code=status.HTTP_200_OK,
    summary="List Subsidy Rules",
)
def list_subsidy_rules(
    session: Annotated[Session, Depends(get_session)],
) -> list[SubsidyRule]:
    """Retrieve full active configuration matrix of childcare subsidy rules."""
    rules = session.exec(
        select(SubsidyRule).order_by(
            col(SubsidyRule.care_type),
            col(SubsidyRule.subsidy_name),
            col(SubsidyRule.rule_id),
        )
    ).all()
    return list(rules)


@router.put(
    "",
    response_model=list[SubsidyRuleResponse],
    status_code=status.HTTP_200_OK,
    summary="Bulk Replace Subsidy Rule Matrix",
)
def bulk_update_subsidy_rules(
    payload: Annotated[list[SubsidyRuleCreate] | BulkSubsidyRulesRequest, Body(...)],
    session: Annotated[Session, Depends(get_session)],
) -> list[SubsidyRule]:
    """Accept a bulk payload of subsidy rule definitions to replace the active configuration matrix within a single database transaction."""
    rule_items: list[SubsidyRuleCreate] = (
        payload.rules if isinstance(payload, BulkSubsidyRulesRequest) else payload
    )

    # Check for duplicate explicit rule_ids within incoming bulk payload
    seen_ids: set[str] = set()
    for item in rule_items:
        if item.rule_id is not None:
            if item.rule_id in seen_ids:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Duplicate rule_id '{item.rule_id}' in bulk payload.",
                )
            seen_ids.add(item.rule_id)

    existing_rules = {
        r.rule_id: r
        for r in session.exec(select(SubsidyRule)).all()
        if r.rule_id is not None
    }
    payload_rule_ids: set[str] = set()

    for item in rule_items:
        data = item.model_dump(exclude={"rule_id"})
        if item.rule_id is not None and item.rule_id in existing_rules:
            # Update existing rule in-place
            rule = existing_rules[item.rule_id]
            for key, val in data.items():
                setattr(rule, key, val)
            payload_rule_ids.add(item.rule_id)
        else:
            # Create new rule
            new_id = item.rule_id or str(uuid.uuid4())
            new_rule = SubsidyRule(rule_id=new_id, **data)
            session.add(new_rule)
            payload_rule_ids.add(new_id)

    # Unconditionally replace configuration matrix: remove existing rules not mentioned in payload
    for r_id, r_obj in existing_rules.items():
        if r_id not in payload_rule_ids:
            session.delete(r_obj)

    session.commit()

    updated_rules = session.exec(
        select(SubsidyRule).order_by(
            col(SubsidyRule.care_type),
            col(SubsidyRule.subsidy_name),
            col(SubsidyRule.rule_id),
        )
    ).all()
    return list(updated_rules)


@router.post(
    "",
    response_model=SubsidyRuleResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create Subsidy Rule",
)
def create_subsidy_rule(
    payload: SubsidyRuleCreate,
    session: Annotated[Session, Depends(get_session)],
) -> SubsidyRule:
    """Create an individual subsidy rule with schema validation against canonical enums."""
    if payload.rule_id is not None:
        existing = session.get(SubsidyRule, payload.rule_id)
        if existing:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Subsidy rule with rule_id '{payload.rule_id}' already exists.",
            )

    rule = SubsidyRule(
        rule_id=payload.rule_id or str(uuid.uuid4()),
        **payload.model_dump(exclude={"rule_id"}),
    )
    session.add(rule)
    session.commit()
    session.refresh(rule)
    return rule


@router.put(
    "/{rule_id}",
    response_model=SubsidyRuleResponse,
    status_code=status.HTTP_200_OK,
    summary="Update Subsidy Rule",
)
def update_subsidy_rule(
    rule_id: str,
    payload: SubsidyRuleUpdate,
    session: Annotated[Session, Depends(get_session)],
) -> SubsidyRule:
    """Update an existing subsidy rule in-place by ID."""
    rule = session.get(SubsidyRule, rule_id)
    if not rule:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Subsidy rule '{rule_id}' not found.",
        )

    update_data = payload.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(rule, field, value)

    session.add(rule)
    session.commit()
    session.refresh(rule)
    return rule


@router.delete(
    "/{rule_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete Subsidy Rule",
)
def delete_subsidy_rule(
    rule_id: str,
    session: Annotated[Session, Depends(get_session)],
) -> Response:
    """Permanently remove a subsidy rule by ID."""
    rule = session.get(SubsidyRule, rule_id)
    if not rule:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Subsidy rule '{rule_id}' not found.",
        )

    session.delete(rule)
    session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
