"""Laya Multilingual router as an MCP server (stdio) for Hermes.

Exposes convaiinnovations/laya-multilingual (322M, mmBERT-based, 100+ languages,
non-autoregressive "System 1" decision model) as Hermes MCP tools.

Design:
  - The checkpoint is LAZILY loaded on the first predict call so tools/list stays fast
    and `hermes mcp test` returns quickly even before the model is cached.
  - One flexible tool: laya_predict(state, questions_json). questions_json is the
    typed-question spec map from the laya README, e.g.
        {"department": {"type": "choice", "instructions": "...",
                        "criteria": {"billing": "invoices, payments", ...}},
         "refund_requested": {"type": "noul", "instructions": "..."}}
  - USE_TF=0 is set defensively: the README warns that laya.load() can hang when
    transformers probes for TensorFlow at import (manifested if TF is installed).
  - The model is a decision/router layer, NOT a generator: it returns typed answers
    (with probabilities) in a single forward pass. No free-text output.
"""
import json
import sys
import threading


def _unbuffered():
    """Force line-buffered, write-through stdout/stderr.

    When Hermes spawns this server, stdout is a pipe (not a tty); Python defaults to
    block buffering there, which would break MCP JSON-RPC framing over stdio.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(line_buffering=True, write_through=True)
        except Exception:
            pass


_unbuffered()

from mcp.server.fastmcp import FastMCP  # noqa: E402

MODEL_ID = "convaiinnovations/laya-multilingual"

_agent = None
_load_lock = threading.Lock()
mcp = FastMCP("laya-router")


def _get_agent():
    global _agent
    if _agent is None:
        # One thread only: a second caller importing transformers at the same instant
        # as the warmup thread hits a lazy-import race in transformers 5.x
        # (ImportError: AutoTokenizer). Block here until the load finishes.
        with _load_lock:
            if _agent is None:
                import os
                os.environ.setdefault("USE_TF", "0")
                import laya
                _agent = laya.load(MODEL_ID)
    return _agent


def _start_warmup():
    """Load the checkpoint in a background daemon thread as soon as the server starts.

    Why: the MCP client (Hermes) applies a short per-request timeout, but a cold
    torch import + model load takes 60-130s on this machine. If the first tool call
    had to do the load inline, it would exceed that timeout and the connection would
    drop ("Connection closed"). Warming up off the event loop keeps tools/list fast
    and makes the first real predict hit a hot model (<1s).
    """
    t = threading.Thread(target=_get_agent, name="laya-warmup", daemon=True)
    t.start()


_start_warmup()


@mcp.tool()
def laya_predict(state: str, questions_json: str) -> str:
    """Run one forward pass of Laya Multilingual (System-1 decision/router, 100+ languages).

    Args:
        state: the input to decide on — a text, email, ticket, or JSON document as a string.
        questions_json: a JSON OBJECT mapping question names to their spec, e.g.
            "department": {"type": "choice", "instructions": "Which team should handle this?",
                           "criteria": {"billing": "invoices, payments, refunds",
                                        "technical": "bugs and outages"}},
            "refund_requested": {"type": "noul", "instructions": "Does the sender ask for money back?"}
          Supported question types follow the laya library (choice, noul, bool, labels, ...).
          See the convaiinnovations/laya-multilingual README for the full schema.

    Returns:
        JSON string of the raw result, e.g.
        {"answers": {"department": {"choice": "billing"},
                     "refund_requested": {"noul": true}},
         ...probability fields...}

    Use this as a cheap routing/decision layer BEFORE an LLM: classify/triage the input,
    then pass the structured decision plus the original text to the LLM for the actual work.
    """
    try:
        qs = json.loads(questions_json)
        if not isinstance(qs, dict):
            return json.dumps({"error": "questions_json must be a JSON object"})
    except Exception as e:
        return json.dumps({"error": "questions_json parse error: %s" % e})

    try:
        agent = _get_agent()
        stanje = {"body": state} if isinstance(state, str) else state
        result = agent.predict(stanje, qs)
        return json.dumps(result, ensure_ascii=False, default=str)
    except Exception as e:
        import traceback
        traceback.print_exc()
        return json.dumps({"error": "predict failed: %s" % e})


def main():
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
