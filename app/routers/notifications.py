"""Notificările hostului."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.container import Container
from app.core.contracts.models import Notification


def build_router(c: Container) -> APIRouter:
    router = APIRouter(tags=["notifications"])

    @router.get("/notifications", response_model=list[Notification])
    async def list_notifications(unread_only: bool = False, limit: int = 200) -> list[Notification]:
        return c.notifications.list(unread_only=unread_only, limit=limit)

    @router.post("/notifications/read-all")
    async def mark_all_read() -> dict[str, int]:
        return {"marked": c.notifications.mark_all_read()}

    @router.post("/notifications/{notification_id}/read", response_model=Notification)
    async def mark_read(notification_id: str) -> Notification:
        notification = c.notifications.mark_read(notification_id)
        if notification is None:
            raise HTTPException(404, f"unknown notification {notification_id}")
        return notification

    return router
