from secrets import compare_digest
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.codes import gen_unique_code, hash_token
from app.config import settings
from app.db import get_session
from app.models import Campaign
from app.schemas import CreateCampaignIn, CreateCampaignOut, DeleteCampaignIn

router = APIRouter()

SessionDep = Annotated[AsyncSession, Depends(get_session)]


@router.post("/campaigns", status_code=201, response_model=CreateCampaignOut)
async def create_campaign(body: CreateCampaignIn, session: SessionDep) -> CreateCampaignOut:
    code = await gen_unique_code(session, settings.code_length)
    campaign = Campaign(code=code, name=body.name, gm_token_hash=hash_token(body.gm_token))
    session.add(campaign)
    await session.commit()
    await session.refresh(campaign)
    return CreateCampaignOut(campaign_id=campaign.id, code=code)


@router.delete("/campaigns/{code}", status_code=204)
async def delete_campaign(code: str, body: DeleteCampaignIn, session: SessionDep) -> Response:
    campaign = await session.scalar(select(Campaign).where(Campaign.code == code))
    if campaign is None:
        raise HTTPException(status_code=404, detail="Unknown campaign code.")
    if not compare_digest(hash_token(body.gm_token), campaign.gm_token_hash):
        raise HTTPException(status_code=403, detail="Bad GM token.")
    # FK cascade removes the campaign's projections.
    await session.delete(campaign)
    await session.commit()
    return Response(status_code=204)
