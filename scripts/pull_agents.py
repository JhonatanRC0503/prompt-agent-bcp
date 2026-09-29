"""Re-sincroniza las instrucciones de los agentes desde Microsoft Foundry hacia el repo.

El código es la fuente de verdad (`main.py` + `instructions.md` + `agent.yaml`); esto solo
sirve para recuperar cambios hechos directamente en el portal de Foundry.

Uso:
    python scripts/pull_agents.py               # todos los agentes del proyecto
    python scripts/pull_agents.py sectorial     # solo los indicados
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from azure.ai.projects import AIProjectClient
from azure.identity import DefaultAzureCredential
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[1]
AGENTS_DIR = REPO_ROOT / "agents"


def main() -> None:
    load_dotenv(REPO_ROOT / ".env")
    endpoint = os.environ.get("FOUNDRY_PROJECT_ENDPOINT")
    if not endpoint:
        sys.exit("Falta FOUNDRY_PROJECT_ENDPOINT (defínelo en .env en la raíz del repo).")

    wanted = set(sys.argv[1:])

    with DefaultAzureCredential() as credential:
        client = AIProjectClient(endpoint=endpoint, credential=credential)
        for agent in client.agents.list():
            data = agent.as_dict()
            name = data["name"]
            if wanted and name not in wanted:
                continue

            latest = data["versions"]["latest"]
            definition = latest["definition"]
            target = AGENTS_DIR / name / "instructions.md"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(definition.get("instructions", "").rstrip() + "\n", encoding="utf-8")

            print(f"{name} v{latest['version']} -> {target.relative_to(REPO_ROOT)}")
            remote_config = {k: v for k, v in definition.items() if k != "instructions"}
            print("  modelo/herramientas en Foundry (revísalo contra agent.yaml y main.py):")
            print("  " + json.dumps(remote_config, ensure_ascii=False, indent=2).replace("\n", "\n  "))


if __name__ == "__main__":
    main()
