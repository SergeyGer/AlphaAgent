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
daphne, so VS Code has something to attach to and you decide what runs. **Nothing
serves until you start it.** That surprises people, and it is why a freshly opened
dev container shows "site can't be reached" on an apparently running stack.

Run **AlphaAgent: Run server (in this container)** from the task list, or type it:

```bash
python manage.py migrate          # once, or after pulling migrations
python manage.py seed_demo        # once: creates demo / demo-pass-123 and an API token
python manage.py runserver 0.0.0.0:8000
```

`0.0.0.0` is not decoration. The Django default binds `127.0.0.1`, which inside a
container is its own loopback — the forwarded port then connects to nothing and
the browser fails even though the server logs "Starting development server".

## Which port do I open? This is the confusing part

There are two routes to the same application, and they use different numbers:

| You are | Route | URL |
| --- | --- | --- |
| In VS Code, via a dev container | VS Code tunnels **container port 8000** to your machine | **<http://127.0.0.1:8000/>** |
| On the server itself | Docker publishes **container 8000 as server 8080** | <http://127.0.0.1:8080/> |

Both reach the same Django process. The numbers differ because they are two
different mechanisms, not two different servers.

**Through a dev container, use port 8000.** 8080 belongs to the *host* side of the
`8080:8000` mapping in `docker-compose.yml`; nothing binds 8080 inside the
container, which is why `devcontainer.json` does not forward it — forwarding it
would only ever tunnel a dead port.

Check the **PORTS** tab next to the terminal if you are unsure which one VS Code
forwarded; it lists the live ones and their labels.

For the full stack instead — the real ASGI server, the worker, the scheduler and
the MCP tool server — start it from the **host** terminal (not from inside the
container, which has no Docker):

```bash
docker compose up -d
```

Running both at once means two Django processes against one database, so use one
or the other.

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
| "Site can't be reached" on 8080 | Wrong port. Through a dev container the dashboard is on **8000**, not 8080. |
| "Site can't be reached" on 8000 | Nothing is serving. The container starts `sleep infinity`; run the server task. |
| Server logs "Starting development server" but the browser fails | You bound `127.0.0.1` instead of `0.0.0.0`; the forwarded port reaches nothing. |
