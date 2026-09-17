"""Backend services package.

Keep package import lightweight so pure services (valuation, ledger adapters,
etc.) can be used and tested without importing the optional LLM stack.
"""

__all__ = ["AgentService"]


def __getattr__(name):
    if name == "AgentService":
        from .agent_service import AgentService
        return AgentService
    raise AttributeError(name)
