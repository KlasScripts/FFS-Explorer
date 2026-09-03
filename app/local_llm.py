"""local_llm.py — thin HTTP client for a locally-running OpenAI-compatible
chat server (LM Studio by default, but anything speaking the same
/v1/chat/completions shape works). Qt-free, stdlib-only (urllib) — this is
one optional feature, not worth a new third-party HTTP dependency for a
single POST + JSON response.

Nothing here ever touches evidence or the archive — this module only
talks to a local HTTP server the examiner chose to run, with text the
caller (ai_summary.py) already assembled.
"""

import concurrent.futures
import json
import urllib.error
import urllib.request


def call_chat(endpoint: str, api_key: str, model: str, prompt: str,
              timeout: float = 240.0, max_tokens: int = 2048) -> dict:
    """POST one chat-completion request. Returns {'text': str} on success
    or {'error': str} on any failure — never raises. A local model server
    being unreachable, misconfigured, or slow is an ordinary, expected
    condition for this optional feature (the examiner may not have it
    running), not a bug to propagate as an exception.

    *timeout* is enforced as a genuine WALL-CLOCK deadline (via a helper
    thread + future.result(timeout=...)), not just passed to urlopen()'s
    own `timeout=` parameter as before. Confirmed necessary by direct
    testing, not theoretical: a real reduce call against a local LM Studio
    server once ran for 5,217 SECONDS (87 minutes) despite urlopen's own
    timeout being set to 240s — LM Studio's server apparently sends
    occasional keep-alive bytes during a long generation, which resets
    urllib's per-read idle timer indefinitely without ever completing the
    response, so urlopen's timeout never actually fires even though the
    call is effectively hung. A wall-clock future.result() deadline can't
    be fooled by that; it simply stops waiting after *timeout* seconds
    regardless of what the socket does. The abandoned request thread may
    keep running in the background after this function returns an error
    (Python has no way to forcibly kill a blocked thread) — harmless here,
    since nothing in this module holds a lock or shared state the caller
    depends on; the pool itself is shut down without waiting for it
    (`shutdown(wait=False)`) so THIS call never blocks on that leftover
    thread either.

    *max_tokens* caps how much the SERVER itself will generate — a second,
    independent safeguard against the same failure mode: an unbounded
    local model that gets stuck in a repetitive/rambling generation can
    otherwise run for a very long time with no natural stop condition.
    2048 is generous for a multi-paragraph narrative (this project's own
    real reduce narratives run well under that) while still bounding a
    runaway generation to a finite amount of work."""
    payload = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.2,
        "max_tokens": max_tokens,
    }).encode("utf-8")
    req = urllib.request.Request(endpoint, data=payload, method="POST")
    req.add_header("Content-Type", "application/json")
    if api_key:
        req.add_header("Authorization", f"Bearer {api_key}")

    def _do_request():
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()

    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        raw = pool.submit(_do_request).result(timeout=timeout)
    except concurrent.futures.TimeoutError:
        return {"error": f"timed out after {timeout:.0f}s waiting for the model to respond"}
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8"))
            msg = detail.get("error", {}).get("message") or str(detail)
        except Exception:
            msg = exc.reason
        return {"error": f"HTTP {exc.code}: {msg}"}
    except Exception as exc:
        return {"error": str(exc)}
    finally:
        pool.shutdown(wait=False)

    try:
        body = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        return {"error": f"invalid JSON from {endpoint}: {exc}"}
    try:
        return {"text": body["choices"][0]["message"]["content"]}
    except (KeyError, IndexError, TypeError):
        return {"error": f"unexpected response shape from {endpoint}: {body}"}


def list_models(endpoint_base: str, api_key: str, timeout: float = 10.0) -> dict:
    """GET {endpoint_base}/models — lets a caller discover which model id
    is actually loaded right now instead of hardcoding one, since the
    loaded model can change between LM Studio sessions. *endpoint_base*
    is the API root (e.g. 'http://localhost:1234/v1'), not the full
    chat-completions URL."""
    url = endpoint_base.rstrip("/") + "/models"
    req = urllib.request.Request(url, method="GET")
    if api_key:
        req.add_header("Authorization", f"Bearer {api_key}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8"))
            msg = detail.get("error", {}).get("message") or str(detail)
        except Exception:
            msg = exc.reason
        return {"error": f"HTTP {exc.code}: {msg}"}
    except Exception as exc:
        return {"error": str(exc)}
    return {"models": [m.get("id") for m in body.get("data", [])]}
