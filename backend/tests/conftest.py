"""API 集成测试：用内存 SQLite 覆盖 PostgreSQL engine，不依赖外部服务。"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app import main
from app.db import Base, CarrierRow, MaskRow, ProtectedReceiverRow, Scenario
from app.seed import DEMO_CARRIERS, DEMO_POLICY
from app.services.masks import MASKS


@pytest.fixture()
def client(monkeypatch):
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        for m in MASKS.values():
            s.add(MaskRow(name=m.name, points=[list(p) for p in m.points],
                          span_mhz=m.span_mhz, description=m.description))
        sc = Scenario(name="教学演示场景", description="t", band_low_mhz=80,
                      band_high_mhz=220, guard_required_mhz=1.0,
                      leakage_limit_dbm=-45.0, reuse_policy=DEMO_POLICY,
                      carriers=[CarrierRow(**kw) for kw in DEMO_CARRIERS])
        s.add(ProtectedReceiverRow(
            receiver_key="rx-demo-guard", version=1, name="演示保护窗",
            window_low_mhz=185.0, window_high_mhz=190.0, power_limit_dbm=-55.0,
            polarizations=["H", "V"], description="", is_current=True,
            is_deleted=False))
        s.add(sc)
        s.commit()
    monkeypatch.setattr(main, "engine", engine)
    with TestClient(main.app) as c:
        yield c
