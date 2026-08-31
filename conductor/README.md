# Conductor definitions

Run `python3 conductor/definitions/generate_definitions.py` to regenerate the task and workflow JSON. The single workflow sequences the 15 pipeline stages. Each SIMPLE task receives paths, and `conductor/workers/stage_worker.py` reconstructs state from the run directory instead of accepting a mutable pipeline object.

Conductor supplies durable retries and recovery. The Python stage boundary also has the config's bounded retry count for one invocation. The generated files contain no timestamps and are byte-stable across repeated generation.

With a reachable Conductor API:

```bash
python3 -m conductor.client register --url http://localhost:8080
python3 -m conductor.worker --url http://localhost:8080
python3 -m conductor.client launch --url http://localhost:8080 --story STORY --config CONFIG --run-dir RUN_DIR
python3 -m conductor.client monitor --url http://localhost:8080 --workflow-id WORKFLOW_ID
```

The worker is a normal Conductor SIMPLE-task worker. It polls, acknowledges one task, reconstructs the stage from the payload and run artifacts, and reports completion or failure. Conductor remains the durable state owner.
