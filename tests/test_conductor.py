from __future__ import annotations

import hashlib
import json
import subprocess
import unittest
from pathlib import Path

from insomnia_media_pipeline.contracts import STAGE_CONTRACTS


ROOT = Path(__file__).resolve().parents[1]
GENERATOR = ROOT / "conductor" / "definitions" / "generate_definitions.py"
TASKS = ROOT / "conductor" / "definitions" / "task-definitions.json"
WORKFLOW = ROOT / "conductor" / "definitions" / "workflows" / "insomnia_media_pipeline_v1.json"


class ConductorDefinitionTests(unittest.TestCase):
    def generate(self) -> None:
        result = subprocess.run(
            ["python3", str(GENERATOR)],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr)

    def test_generated_definition_is_complete_simplified_and_byte_stable(self) -> None:
        self.generate()
        first = {path: path.read_bytes() for path in (TASKS, WORKFLOW)}
        self.generate()
        second = {path: path.read_bytes() for path in (TASKS, WORKFLOW)}
        self.assertEqual(
            {path: hashlib.sha256(data).hexdigest() for path, data in first.items()},
            {path: hashlib.sha256(data).hexdigest() for path, data in second.items()},
        )

        tasks = json.loads(TASKS.read_text(encoding="utf-8"))
        workflow = json.loads(WORKFLOW.read_text(encoding="utf-8"))
        expected = [contract.name for contract in STAGE_CONTRACTS]
        self.assertEqual(15, len(tasks))
        self.assertEqual(expected, [task["name"].removeprefix("insomnia_media_pipeline_") for task in tasks])
        self.assertEqual(expected, [task["inputParameters"]["payload"]["stage"] for task in workflow["tasks"]])
        self.assertEqual(["storyPath", "configPath", "runDir"], workflow["inputParameters"])
        self.assertTrue(all(task["type"] == "SIMPLE" for task in workflow["tasks"]))
        self.assertEqual("finalize", workflow["tasks"][-1]["inputParameters"]["payload"]["stage"])


if __name__ == "__main__":
    unittest.main()
