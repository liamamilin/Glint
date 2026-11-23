"""Skill registry and base interface for Glint micro-skills."""

from __future__ import annotations

from abc import ABC, abstractmethod

from models import RouteRequest, RouteResponse


class BaseSkill(ABC):
    name: str

    @abstractmethod
    async def execute(self, request: RouteRequest) -> RouteResponse:
        """Run the micro-skill for the incoming route request."""
