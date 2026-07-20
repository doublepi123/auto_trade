"""LLM token usage summary API."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.database import get_db
from app.main import app
from app.models import Base, LLMInteraction


class TestLLMUsageSummary:
    @classmethod
    def setup_class(cls) -> None:
        cls.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(bind=cls.engine)

        def override_get_db():
            with Session(bind=cls.engine) as db:
                yield db

        app.dependency_overrides[get_db] = override_get_db
        cls.client = TestClient(app)

    @classmethod
    def teardown_class(cls) -> None:
        app.dependency_overrides.pop(get_db, None)
        cls.engine.dispose()

    def setup_method(self) -> None:
        with Session(bind=self.engine) as db:
            db.query(LLMInteraction).delete()
            db.commit()

    def test_seeded_rows_are_aggregated_by_day_and_type(self) -> None:
        today = datetime.now(timezone.utc) - timedelta(minutes=1)
        yesterday = today - timedelta(days=1)
        with Session(bind=self.engine) as db:
            db.add_all(
                [
                    LLMInteraction(
                        interaction_type="analyze",
                        success=True,
                        prompt_tokens=100,
                        completion_tokens=40,
                        total_tokens=140,
                        created_at=yesterday,
                    ),
                    LLMInteraction(
                        interaction_type="preview",
                        success=False,
                        prompt_tokens=60,
                        completion_tokens=None,
                        total_tokens=60,
                        created_at=today,
                    ),
                    LLMInteraction(
                        interaction_type="analyze",
                        success=True,
                        prompt_tokens=None,
                        completion_tokens=20,
                        total_tokens=20,
                        created_at=today,
                    ),
                    LLMInteraction(
                        interaction_type="analyze",
                        success=True,
                        prompt_tokens=999,
                        completion_tokens=999,
                        total_tokens=1998,
                        created_at=today - timedelta(days=40),
                    ),
                ]
            )
            db.commit()

        response = self.client.get("/api/llm-usage/summary?days=30")

        assert response.status_code == 200, response.text
        assert response.json() == {
            "days": 30,
            "total_interactions": 3,
            "successful_interactions": 2,
            "total_prompt_tokens": 160,
            "total_completion_tokens": 60,
            "total_tokens": 220,
            "by_day": [
                {
                    "date": yesterday.date().isoformat(),
                    "interactions": 1,
                    "prompt_tokens": 100,
                    "completion_tokens": 40,
                    "total_tokens": 140,
                },
                {
                    "date": today.date().isoformat(),
                    "interactions": 2,
                    "prompt_tokens": 60,
                    "completion_tokens": 20,
                    "total_tokens": 80,
                },
            ],
            "by_type": [
                {
                    "interaction_type": "analyze",
                    "interactions": 2,
                    "total_tokens": 160,
                },
                {
                    "interaction_type": "preview",
                    "interactions": 1,
                    "total_tokens": 60,
                },
            ],
        }

    def test_empty_database_returns_zero_totals(self) -> None:
        response = self.client.get("/api/llm-usage/summary?days=30")

        assert response.status_code == 200
        assert response.json() == {
            "days": 30,
            "total_interactions": 0,
            "successful_interactions": 0,
            "total_prompt_tokens": 0,
            "total_completion_tokens": 0,
            "total_tokens": 0,
            "by_day": [],
            "by_type": [],
        }

    def test_days_must_be_between_one_and_365(self) -> None:
        assert self.client.get("/api/llm-usage/summary?days=0").status_code == 422
        assert self.client.get("/api/llm-usage/summary?days=366").status_code == 422
