# Working in a Dev Container

Open this folder in VS Code and run **Dev Containers: Reopen in Container** from
the command palette (or accept the prompt VS Code shows when it notices this
directory). VS Code builds the project's own compose stack and attaches to it.

Nothing here is a second, parallel environment. The container is the `web` service
of [`docker-compose.yml`](../docker-compose.yml) with a thin override, so the
editor, the Django server, the Celery worker, PostgreSQL and Redis share one
network and one `.env` — the same arrangement as `docker compose up`.

## Starting the application

The container is deliberately kept alive with `sleep infinity` rather than running
daphne, so VS Code has something to attach to and you decide what runs. From the
integrated terminal:

```bash
python manage.py migrate          # once, or after pulling migrations
python manage.py seed_demo        # once: creates demo / demo-pass-123 and an API token
python manage.py runserver 0.0.0.0:8000
```

`runserver` reloads on edit, which is the point of developing inside the
container. The dashboard is then on <http://127.0.0.1:8080/> (port 8000 inside the
container is published as 8080 — see `ALPHA_WEB_PORT`).

For the full stack instead — the real ASGI server, the worker, the scheduler and
the MCP tool server — start it from the **host** terminal:

```bash
docker compose up -d
```

Both at once will fight over the database connection, so use one or the other.

## Frontend

The container has no Node runtime; `frontend/` is built by its own stage of the
multi-stage [`Dockerfile`](../Dockerfile) during `docker compose build`. To work on
the React app, install Node in the container and use the normal commands:

```bash
cd frontend && npm ci && npm run dev
```

## Closing down

**Closing the VS Code window stops the stack.** That is configured on purpose
(`"shutdownAction": "stopCompose"`) — a devcontainer that kept running after you
closed it would hold the ports and recreate exactly the conflict this project
tries to avoid.

To release everything, including the network:

```bash
docker compose down
```

Volumes are never removed by any of these, so your database survives.

## Nothing starts by itself

No service in either compose file sets a restart policy, so **rebooting the
machine does not bring this stack back**. Starting it is always deliberate.

## If the container will not start

| Symptom | Cause |
| --- | --- |
| `port is already allocated` | Another project holds 8000/8080/5432/6379. `docker compose down` in that project, or set `ALPHA_WEB_PORT` in `.env`. |
| `env file .env not found` | Run `make env` (or copy `.env.example` to `.env`) before opening the container. |
| Python tools missing in the terminal | `postCreateCommand` installs `requirements-dev.txt`; run **Dev Containers: Rebuild Container** if it was interrupted. |
| The editor shows the host's files | The bind mount failed — check that `.devcontainer/docker-compose.extend.yml` mounts `.` and not `..`. |
