"""Administrative subsidy rule configuration matrix and granular CRUD endpoints."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Response, status
from sqlmodel import Session, col, select

from kidcarematch_backend.auth.security import get_current_admin
from kidcarematch_backend.db.db import SubsidyRule, User, get_session
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
    current_admin: Annotated[User, Depends(get_current_admin)],
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
    summary="Bulk Update or Replace Subsidy Rule Matrix",
)
def bulk_update_subsidy_rules(
    payload: Annotated[list[SubsidyRuleCreate] | BulkSubsidyRulesRequest, Body(...)],
    current_admin: Annotated[User, Depends(get_current_admin)],
    session: Annotated[Session, Depends(get_session)],
    replace: bool = Query(
        default=True,
        description="Whether to replace unmentioned active rules (True) or perform in-place upsert (False)",
    ),
) -> list[SubsidyRule]:
    """Accept a bulk payload of subsidy rule definitions to update or replace the active configuration matrix in-place within a single database transaction."""
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
        if item.rule_id is not None and item.rule_id in existing_rules:
            # Update existing rule in-place
            rule = existing_rules[item.rule_id]
            rule.subsidy_name = item.subsidy_name
            rule.tier_name = item.tier_name
            rule.care_type = item.care_type
            rule.metric = item.metric
            rule.applicant_status = item.applicant_status
            rule.citizenship = item.citizenship
            rule.income_min = item.income_min
            rule.income_max = item.income_max
            rule.subsidy_amount = item.subsidy_amount
            rule.effective_date = item.effective_date
            payload_rule_ids.add(item.rule_id)
        else:
            # Create new rule
            new_id = item.rule_id or str(uuid.uuid4())
            new_rule = SubsidyRule(
                rule_id=new_id,
                subsidy_name=item.subsidy_name,
                tier_name=item.tier_name,
                care_type=item.care_type,
                metric=item.metric,
                applicant_status=item.applicant_status,
                citizenship=item.citizenship,
                income_min=item.income_min,
                income_max=item.income_max,
                subsidy_amount=item.subsidy_amount,
                effective_date=item.effective_date,
            )
            session.add(new_rule)
            payload_rule_ids.add(new_id)

    # If replace is True, remove existing rules not mentioned in payload
    if replace:
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
    current_admin: Annotated[User, Depends(get_current_admin)],
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
        subsidy_name=payload.subsidy_name,
        tier_name=payload.tier_name,
        care_type=payload.care_type,
        metric=payload.metric,
        applicant_status=payload.applicant_status,
        citizenship=payload.citizenship,
        income_min=payload.income_min,
        income_max=payload.income_max,
        subsidy_amount=payload.subsidy_amount,
        effective_date=payload.effective_date,
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
    current_admin: Annotated[User, Depends(get_current_admin)],
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
    current_admin: Annotated[User, Depends(get_current_admin)],
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
