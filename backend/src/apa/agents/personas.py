"""Agent registry: id, role, execution type and the LiteLLM route each one uses (blueprint 2, 4.8)."""

from dataclasses import dataclass

ROUTINE = "router-routine-classifier"
VISION = "router-vision-ocr"
ARBITER = "router-arbiter-persona"
MENTAL = "router-mental-health"
MACRO = "router-macro-reasoning"
RESEARCH = "router-deep-research"


@dataclass(frozen=True)
class Agent:
    id: str
    name: str
    kind: str  # 'engine' (pure code) | 'persona' (LLM) | 'hybrid'
    route: str | None  # LiteLLM model_name; None = no LLM
    in_debate: bool = False
    persona: str = ""  # one-line system-prompt stance
    protected: bool = False  # the core: can change model, but can never be switched off or removed


AGENTS: dict[str, Agent] = {a.id: a for a in [
    Agent("A-00", "Master", "persona", ROUTINE, False,
          "Routes every free-text message to the cheapest correct handling and the right specialists.", True),
    Agent("A-01", "Operator", "engine", None, False, "", True),
    Agent("A-02", "Finance (CFO)", "persona", ROUTINE, True,
          "Rigid, disciplined and mathematical. Judge liquidity, burn rate and category caps."),
    Agent("A-03", "Health & Biohacker", "persona", ROUTINE, True,
          "Decisive and clinical. Judge sugar/caffeine/sodium against daily ceilings and sleep state."),
    Agent("A-04", "Tech & Lab Architect", "persona", ROUTINE, True,
          "Rational and sceptical. Judge hardware purchases, depreciation and gimmicks."),
    Agent("A-05", "Social Ambassador", "persona", ROUTINE, True,
          "Empathetic and adaptive. Judge whether social spending is reasonable."),
    Agent("A-06", "Mental Health", "persona", MENTAL, False,
          "Warm and non-judgmental. Ephemeral /private sessions only; never logged."),
    Agent("A-07", "The Planner", "persona", ROUTINE, True,
          "Visionary and patient. Judge impact on sinking funds and goal deadlines."),
    Agent("A-08", "The Reminder", "engine", ROUTINE),
    Agent("A-09", "Macro Strategist", "persona", MACRO),
    Agent("A-10", "The Arbiter", "hybrid", ARBITER, False, "", True),
    Agent("A-11", "Auditor / Reconciler", "engine", VISION),
    Agent("A-12", "Reporting Agent", "engine", ROUTINE),
    Agent("A-13", "Briefing Agent", "engine", ROUTINE),
]}

DEBATERS = [a for a in AGENTS.values() if a.in_debate]
