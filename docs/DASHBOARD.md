# Local dashboard

Katydid exposes a small HTTP control surface for a caller that already owns task storage and worker lifecycle. It is a trusted local-user tool. It has no account authentication and must not be placed behind a public listener, reverse proxy, tunnel, shared host, or externally reachable container port.

Create the server with:

```python
from katydid.dashboard import make_server

server = make_server(store, ["payments", "catalog"], enqueue, port=8765)
server.serve_forever()
```

`make_server(store, repositories, enqueue, host="127.0.0.1", port=8765)` returns a `ThreadingHTTPServer`. The caller starts it, runs workers separately, and calls `shutdown()` and `server_close()` during teardown. The host must be a literal loopback address or `localhost`; wildcard and external bindings are rejected.

The store provides:

```python
get_task(task_id: str) -> dict
list_tasks() -> list[dict]
events(task_id: str) -> list[dict]
control(task_id: str, action: str, instruction: str | None = None) -> dict
```

The enqueue callback has the signature `enqueue(repository: str) -> dict`. Repository names must come from the registered list passed to `make_server`.

## HTTP contract

- `GET /api/repositories` returns `{"repositories": [...]}`.
- `GET /api/tasks` returns `{"tasks": [...]}`.
- `GET /api/tasks/{id}` returns `{"task": {...}}`.
- `GET /api/tasks/{id}/events` returns `{"events": [...]}`.
- `POST /api/tasks` accepts `{"repository": "registered-name"}` and returns the task with status 201.
- `POST /api/tasks/{id}/control` accepts `pause`, `resume`, `cancel`, or `steer`. Steering also requires a nonblank `instruction` of at most 4,000 characters.

Every POST requires `Content-Type: application/json` and `X-Katydid-Request: dashboard`. Request bodies are capped at 64 KiB. Browser requests with a nonlocal Origin are rejected, and mutation Origins must exactly match the Host name and port. Host headers must resolve syntactically to `localhost` or a loopback IP. The server sends no CORS permission headers.

Static assets use a fixed allowlist, so URL paths cannot select arbitrary files. A restrictive content security policy blocks inline and remote scripts. The browser code renders repository, task, event, log, and evidence values through `textContent`; stored strings are never interpreted as markup.

Open `http://127.0.0.1:8765/` on the same machine. The page polls every four seconds, keeps errors in a nonblocking status notice, and wires dispatch, pause, resume, cancel, and steering controls directly to the API.
