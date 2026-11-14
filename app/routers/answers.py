"""Inboxul întrebărilor la care hostul răspunde direct prin PMS."""
from typing import Literal
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, ConfigDict
from app.core.host_answers import AnswerConflict
from app.core.channel_gateway import ChannelError


class ReplyIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    text: str = Field(min_length=1, max_length=4000)


class DeliveryCheck(BaseModel):
    delivered: bool = Field(strict=True)


def build_router(c):
    router = APIRouter(tags=["answers"])

    @router.get("/answers")
    async def list_answers(status: Literal["pending", "all"] = "pending"):
        return c.host_answers.list(status)

    @router.post("/answers/{answer_id}/reply")
    async def reply(answer_id: str, body: ReplyIn):
        try:
            return await c.host_answers.send(answer_id, body.text)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        except AnswerConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except ChannelError as exc:
            raise HTTPException(502, "Delivery could not be confirmed. Check the PMS conversation before retrying.") from exc

    @router.post("/answers/{answer_id}/delivery-check")
    async def delivery_check(answer_id: str, body: DeliveryCheck):
        try:
            return await c.host_answers.reconcile(answer_id, body.delivered)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        except AnswerConflict as exc:
            raise HTTPException(409, str(exc)) from exc

    return router
