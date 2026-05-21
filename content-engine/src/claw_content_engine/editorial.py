from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass

from .models import SignalItem


@dataclass
class EditorialResult:
    takeaways: list[str]
    metadata: dict


class EditorialClient:
    """Provider-neutral OpenAI-compatible editorial client.

    GLM is the preferred v1 path, but the same class can call any
    OpenAI-compatible chat-completions endpoint.
    """

    def __init__(self) -> None:
        self.provider = (os.environ.get("EDITORIAL_PROVIDER") or "glm").lower()
        self.api_key = (
            os.environ.get("EDITORIAL_API_KEY")
            or os.environ.get("GLM_API_KEY")
            or os.environ.get("OPENAI_API_KEY")
            or os.environ.get("OPENROUTER_API_KEY")
            or ""
        )
        self.base_url = (
            os.environ.get("EDITORIAL_BASE_URL")
            or os.environ.get("GLM_BASE_URL")
            or "https://open.bigmodel.cn/api/paas/v4"
        ).rstrip("/")
        self.model = (
            os.environ.get("EDITORIAL_MODEL")
            or os.environ.get("GLM_MODEL")
            or "glm-4-plus"
        )

    def available(self) -> bool:
        return self.provider not in {"none", "off"} and bool(self.api_key)

    def generate_takeaways(self, sections: dict[str, list[SignalItem]]) -> EditorialResult:
        if not self.available():
            return EditorialResult(
                takeaways=heuristic_takeaways(sections),
                metadata={"provider": "deterministic", "used_llm": False},
            )

        prompt = _takeaway_prompt(sections)
        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You write buyer-facing strategy notes for Claw Consulting. "
                        "Be specific, sober, and useful to CTOs, founders, and operators. "
                        "Return only JSON."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.4,
        }
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=45) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            content = data["choices"][0]["message"]["content"]
            parsed = json.loads(_strip_fences(content))
            takeaways = [str(x).strip() for x in parsed.get("takeaways", []) if str(x).strip()]
            if not takeaways:
                takeaways = heuristic_takeaways(sections)
            return EditorialResult(
                takeaways=takeaways[:5],
                metadata={
                    "provider": self.provider,
                    "base_url": self.base_url,
                    "model": self.model,
                    "used_llm": True,
                },
            )
        except (urllib.error.URLError, KeyError, json.JSONDecodeError, TimeoutError) as exc:
            return EditorialResult(
                takeaways=heuristic_takeaways(sections),
                metadata={
                    "provider": self.provider,
                    "base_url": self.base_url,
                    "model": self.model,
                    "used_llm": False,
                    "error": str(exc)[:300],
                },
            )


def _strip_fences(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`")
        stripped = stripped.removeprefix("json").strip()
    return stripped


def _takeaway_prompt(sections: dict[str, list[SignalItem]]) -> str:
    compact = {
        key: [
            {"title": item.title, "summary": item.summary, "url": item.url}
            for item in items[:8]
        ]
        for key, items in sections.items()
    }
    return (
        "Given these weekly signals, write 3-5 concise consulting takeaways. "
        "Each takeaway must say what a buyer/operator should do, evaluate, or watch. "
        "Do not invent metrics. Return JSON as {\"takeaways\": [\"...\"]}.\n\n"
        f"{json.dumps(compact, indent=2)}"
    )


def heuristic_takeaways(sections: dict[str, list[SignalItem]]) -> list[str]:
    takeaways: list[str] = []
    if sections.get("agent_ops"):
        top = sections["agent_ops"][0]
        takeaways.append(
            f"Review agent operating assumptions against {top.title}; releases are only useful when they change workflow, governance, or runtime behavior."
        )
    if sections.get("model_moves"):
        top = sections["model_moves"][0]
        takeaways.append(
            f"Track {top.title} as a model-strategy input, but test it against cost, latency, licensing, and deployment constraints before changing defaults."
        )
    if sections.get("watchlist"):
        top = sections["watchlist"][0]
        takeaways.append(
            f"Treat {top.title} as a reminder to keep an agent risk register: exposed tools, auth paths, package provenance, and rollback owners."
        )
    takeaways.append(
        "Use the week's strongest signals to pick one pilot assumption to validate, not to chase every new framework or model release."
    )
    return takeaways[:5]
