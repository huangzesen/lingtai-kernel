---
name: stream-progress-behavior-tests
behavior_version: 1
labt_version: 2
contract: CONTRACT.md
anatomy: ANATOMY.md
related_files:
  - src/lingtai/kernel/stream_progress/CONTRACT.md
  - src/lingtai/kernel/stream_progress/ANATOMY.md
  - src/lingtai/kernel/stream_progress/__init__.py
  - src/lingtai/adapters/stream_progress.py
  - tests/test_stream_progress.py
maintenance: |
  Created with the stream-progress vertical slice (2026-08-26). Keep this file
  reciprocal with CONTRACT.md and ANATOMY.md (tridirectional loop): when a
  stream-progress behavior clause changes, update the guarding LABT here in
  the same change.
---
# Stream Progress Behavior Tests

Self-contained agent behavior tasks guarding the observable behavior clauses of
`src/lingtai/kernel/stream_progress/CONTRACT.md` (memory-only lifecycle,
fail-open publication, loopback-only read API with deterministic discovery).
Pinned pytest commands must run from the repo root with the project's Python.

## Behavior SP001 — a streaming response is observable as RAM-only character progress on loopback, and never as text or a file

- **id**: SP001
- **title**: a streaming response is observable as RAM-only character progress on loopback, and never as text or a file
- **guards**: `stream-progress` § Behavior
- **runner**: any LingTai agent with `shell` and `file` access to this repository
- **prerequisites**: a clean checkout of `<repo>`; python on PATH with the project installed (`.venv`); no other process listening on this machine's loopback candidates for the agent id `labt-sp001`
- **estimate**: ≈ 10 minutes

### Steps
1. From `<repo>`, run `python -m pytest tests/test_stream_progress.py tests/test_streaming.py -q` and capture the outcome.
2. Write `<scratch>/sp001.py` with the following content and run it with the project's Python:

```python
import json, urllib.request, hashlib
from lingtai.adapters.stream_progress import LoopbackStreamProgressPublisher
from lingtai.kernel.stream_progress import candidate_ports

agent_id = "labt-sp001"
seed = int.from_bytes(hashlib.sha256(b"lingtai.stream-progress/v1\x00" + agent_id.encode()).digest()[:2], "big")
assert candidate_ports(agent_id) == [41000 + ((seed + i * 7919) % 20000) for i in range(8)]

pub = LoopbackStreamProgressPublisher(agent_id)
assert pub.start() is True and pub.port in candidate_ports(agent_id)

def read():
    with urllib.request.urlopen(f"http://127.0.0.1:{pub.port}/v1/stream-progress", timeout=1) as r:
        assert r.headers["Cache-Control"] == "no-store"
        return json.loads(r.read())

s0 = read(); assert s0["schema"] == "lingtai.stream-progress/v1" and s0["agent_id"] == agent_id
assert set(s0) == {"schema", "agent_id", "generation", "active", "streamed_chars", "updated_unix_ms", "pid"}
gen = pub.begin(); pub.add_chars(gen, len("héllo wörld")); s1 = read()
assert s1["active"] is True and s1["streamed_chars"] == 11 and s1["generation"] == s0["generation"] + 1 == gen
pub.add_chars(gen - 1, 99); pub.end(gen - 1); s1b = read()  # stale generation: ignored
assert s1b["active"] is True and s1b["streamed_chars"] == 11
pub.end(gen); s2 = read(); assert s2["active"] is False and s2["streamed_chars"] == 0
pub.close(); print("SP001 OK", pub.candidates)
```
3. While step 2's publisher is alive (add `input()` before `pub.close()` if needed), run `curl -s -o /dev/null -w '%{http_code}' -X POST http://127.0.0.1:<port>/v1/stream-progress` and `curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:<port>/other`; also check `git status --short` and `ls -a <scratch>` for any new file the publisher could have written.

### Expected evidence
- [ ] Step 1: the stream-progress and streaming suites pass, pinning the three-operation generation-bound Port shape, begin-before-wait, the count-only `on_output_chars` seam (every provider output fragment adds its delivered length, bound to the returned generation; terminal echoes counted once; legacy `on_chunk` untouched), `finally`-clear of that generation, old-generation callbacks ignored after a newer begin, fail-open, default-on sources (factory never called for explicit `streaming: false`), and the loopback API.
- [ ] Step 2: the script prints `SP001 OK` — bound port is a discovery candidate, body carries exactly the seven v1 fields with no text, `Cache-Control: no-store`, `begin`/`add_chars`/`end` are read back live, and a stale-generation `add_chars`/`end` leaves the active snapshot untouched.
- [ ] Step 3: POST answers `405`, an unknown path answers `404`, and no new file appears anywhere (progress is RAM-only).

### Pass / Fail
Pass when the suite passes, the script prints `SP001 OK`, 405/404 are observed, and no file was written. Fail on any text field in the body, any non-loopback bind, any file written, or an agent/LLM call that fails because of progress publication; record the evidence trail in the task report.
