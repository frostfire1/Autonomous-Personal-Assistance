"""Tests for multi-agent internal consultation: specialists report to Master, Master talks to Telegram."""
import asyncio
from datetime import UTC, datetime
from zoneinfo import ZoneInfo
import pytest

from apa.chat import actions, agent, assistant
from apa.chat.agent import GROUNDED, ROLE_HINT, PERSONA_EXTRA
from apa.pipeline import chat


def test_grounded_roles_and_hints_cover_all_specialists():
    specialists = ["A-02", "A-03", "A-04", "A-05", "A-07", "A-09", "A-12"]
    for s in specialists:
        assert s in GROUNDED, f"Specialist {s} must be in GROUNDED"
        assert s in ROLE_HINT, f"Specialist {s} must have a ROLE_HINT"
    assert "A-04" in PERSONA_EXTRA
    assert "A-05" in PERSONA_EXTRA
    assert "A-09" in PERSONA_EXTRA


def test_consult_agent_tool_in_converse(monkeypatch):
    """Assistant invokes consult_agent to ask Biohacker A-03 internally, then Master answers."""
    shown = []
    consulted = []

    class FakeLlm:
        async def complete(self, route, system, user, **kw):
            shown.append((system, user, kw))
            idx = len(shown) - 1
            if idx == 0:
                # First step: Master consults specialist A-03 internally
                return type("R", (), {
                    "content": '{"tool": "consult_agent", "args": {"agent": "A-03", "question": "Joe sering begadang jam 12 malam"}}',
                    "prompt_tokens": 15,
                    "completion_tokens": 10
                })()
            else:
                # Second step: Master synthesizes response for Telegram
                return type("R", (), {
                    "content": '{"say": "Menurut Biohacker A-03, tidur larut malam menurunkan deep sleep kamu. Coba matikan layar 1 jam sebelum tidur."}',
                    "prompt_tokens": 20,
                    "completion_tokens": 15
                })()

    async def fake_ask_agent(llm, agent_id, q_text, facts, history, custom):
        consulted.append((agent_id, q_text))
        return "Saran Biohacker: Circadian rhythm terganggu jika tidur di atas jam 23:00.", 50

    async def fake_context(sm, tz, now, history_block, reply_to):
        return "CONTEXT DATA"

    monkeypatch.setattr(assistant, "_context", fake_context)
    monkeypatch.setattr(agent, "ask_agent", fake_ask_agent)

    tz = ZoneInfo("Asia/Jakarta")
    now = datetime.now(UTC)

    reply, tokens = asyncio.run(assistant.converse(
        sm=None,
        llm=FakeLlm(),
        redis=None,
        text="kenapa aku ngerasa lemas ya?",
        history_block="user: halo",
        tz=tz,
        now=now,
    ))

    # Specialist was consulted internally
    assert len(consulted) == 1
    assert consulted[0][0] == "A-03"
    assert "Joe sering begadang" in consulted[0][1]
    assert "Menurut Biohacker A-03" in reply

    # Verify the internal transcript delivered opinion to MASTER, NOT to Telegram
    assert "OPINION FROM Health & Biohacker (A-03) TO MASTER (INTERNAL):" in shown[1][1]
    assert "Circadian rhythm terganggu" in shown[1][1]


def test_pipeline_execute_ask_specialists_report_to_master_not_telegram(monkeypatch):
    """When intent is ask, specialists consult internally with Master. Only Master speaks to Telegram."""
    from types import SimpleNamespace

    async def fake_fin(sm, tz):
        return "FIN_DATA"

    async def fake_health(sm, tz):
        return "HEALTH_DATA"

    consulted = []
    async def fake_ask(llm, agent_id, text, facts, history, custom):
        consulted.append((agent_id, text, facts))
        if agent_id == "A-03":
            return "Perbaiki jam tidurmu ke jam 22:00.", 10
        if agent_id == "A-04":
            return "Spek ThinkPad ini sudah cukup bagus untuk coding.", 10
        return "Analisis keuangan aman.", 10

    master_saw = []
    async def fake_converse(sm, llm, redis, prompt, history, tz, now, reply_to=None):
        master_saw.append(prompt)
        return "Jawaban Master yang mensintesis masukan para spesialis.", 30

    monkeypatch.setattr(chat, "finance_snapshot", fake_fin)
    monkeypatch.setattr(chat, "health_snapshot", fake_health)
    monkeypatch.setattr(chat, "ask_agent", fake_ask)
    monkeypatch.setattr(chat, "converse", fake_converse)

    # 1. Single specialist agent consulted internally
    parsed_single = SimpleNamespace(intent="ask", agents=["A-03"], items=None)
    reply, role = asyncio.run(chat._execute(
        sm=None, llm=None, redis=None,
        p=parsed_single,
        text="kenapa sering ngantuk?",
        history=[], draft=None, tz=ZoneInfo("Asia/Jakarta"),
        sent_at=datetime.now(UTC),
        custom=None, reply_to=None,
    ))
    # Role sent to Telegram is MASTER (A-00), NOT A-03!
    assert role == "A-00"
    assert reply == "Jawaban Master yang mensintesis masukan para spesialis."
    assert consulted[0][0] == "A-03"
    assert "INPUT DARI Health & Biohacker (A-03) UNTUK MASTER" in master_saw[0]
    assert "Perbaiki jam tidurmu" in master_saw[0]

    # 2. Multi-specialist discussion: both report to Master internally
    consulted.clear()
    master_saw.clear()
    parsed_multi = SimpleNamespace(intent="ask", agents=["A-03", "A-04"], items=None)
    reply, role = asyncio.run(chat._execute(
        sm=None, llm=None, redis=None,
        p=parsed_multi,
        text="mau beli laptop baru tapi lagi capek kerja",
        history=[], draft=None, tz=ZoneInfo("Asia/Jakarta"),
        sent_at=datetime.now(UTC),
        custom=None, reply_to=None,
    ))
    # Only Master talks to Telegram!
    assert role == "A-00"
    assert reply == "Jawaban Master yang mensintesis masukan para spesialis."
    assert "INPUT DARI Health & Biohacker (A-03) UNTUK MASTER" in master_saw[0]
    assert "INPUT DARI Tech & Lab Architect (A-04) UNTUK MASTER" in master_saw[0]
