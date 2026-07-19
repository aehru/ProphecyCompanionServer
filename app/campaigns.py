import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.codes import gen_unique_code, hash_token, verify_token
from app.config import settings
from app.db import get_session
from app.logging_setup import event
from app.models import Campaign
from app.schemas import CreateCampaignIn, CreateCampaignOut, DeleteCampaignIn

router = APIRouter()
log = logging.getLogger(__name__)

SessionDep = Annotated[AsyncSession, Depends(get_session)]


@router.post("/campaigns", status_code=201, response_model=CreateCampaignOut)
async def create_campaign(
    body: CreateCampaignIn, session: SessionDep, request: Request
) -> CreateCampaignOut:
    # Keyed on the socket peer IP — behind a reverse proxy that's the proxy
    # until forwarded headers are handled (TODO.md).
    ip = request.client.host if request.client else "unknown"
    if not request.app.state.create_limiter.allow(ip):
        log.warning(event("create_rate_limited", ip=ip, limit=settings.create_limit_per_hour))
        raise HTTPException(status_code=429, detail="Too many campaigns created; retry later.")
    code = await gen_unique_code(session, settings.code_length)
    campaign = Campaign(code=code, name=body.name, gm_token_hash=hash_token(body.gm_token))
    session.add(campaign)
    await session.commit()
    await session.refresh(campaign)
    # `code` is the join capability — only the internal id goes to the log.
    log.info(event("campaign_created", campaign_id=campaign.id, ip=ip))
    return CreateCampaignOut(campaign_id=campaign.id, code=code)


@router.delete("/campaigns/{code}", status_code=204)
async def delete_campaign(
    code: str, body: DeleteCampaignIn, session: SessionDep, request: Request
) -> Response:
    ip = request.client.host if request.client else "unknown"
    campaign = await session.scalar(select(Campaign).where(Campaign.code == code))
    if campaign is None:
        log.warning(event("delete_unknown_campaign", ip=ip))
        raise HTTPException(status_code=404, detail="Unknown campaign code.")
    if not verify_token(body.gm_token, campaign.gm_token_hash):
        log.warning(event("bad_gm_token", campaign_id=campaign.id, ip=ip, via="http_delete"))
        raise HTTPException(status_code=403, detail="Bad GM token.")
    campaign_id = campaign.id
    # FK cascade removes the campaign's projections.
    await session.delete(campaign)
    await session.commit()
    log.info(event("campaign_deleted", campaign_id=campaign_id, ip=ip))
    return Response(status_code=204)
