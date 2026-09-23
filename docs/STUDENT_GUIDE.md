# Deploying your project — student guide

Your project runs on a server shared with other teams. That constraint explains most of the rules
below: a mistake in your container can take down someone else's project, so the platform enforces
limits and checks things before they run.

---

## 1. Get a readiness score first

Before anything is deployed, ask for an analysis in the dashboard. It clones your repo, works out
what you built, and gives you a score out of 100 with a list of specific problems.

**Blockers** must be fixed — the platform won't deploy with one outstanding. Everything else is
advice, ordered by how much trouble it will cause you.

The report also generates files for you: a Dockerfile matched to your stack, a `.dockerignore`, a
`.env.example`, and a deploy workflow. Copy them in. They're a starting point, not a finished
answer — read them.

---

## 2. Add the deploy workflow

Create `.github/workflows/deploy.yml`:

```yaml
name: Deploy
on:
  push:
    branches: [main]

jobs:
  deploy:
    uses: vnrvjiet-incubator/viljaops/.github/workflows/viljaops-deploy.yml@main
    with:
      app_port: 8000          # the port your app listens on INSIDE the container
      health_path: /health
      memory_mb: 512
    secrets:
      VILJAOPS_TOKEN: ${{ secrets.VILJAOPS_TOKEN }}
```

For a frontend, the API URL has to be set at **build** time:

```yaml
    with:
      app_port: 80
      build_args: |
        VITE_API_URL=https://your-team.apps.vnrvjiet.in/api
```

You never pick a host port. The platform assigns one and wires Nginx to it.

---

## 3. The five mistakes that cause most failures

### `localhost` won't mean what you think

Inside a container, `localhost` is *the container itself*. In a browser bundle, it's *the
visitor's own computer*. Neither is your server.

```python
DATABASE_URL = "postgresql://user:pass@localhost:5432/db"   # breaks in a container
DATABASE_URL = os.environ["DATABASE_URL"]                   # correct
```

```js
const API = "http://localhost:5000/api"        // every visitor calls their own machine
const API = import.meta.env.VITE_API_URL       // correct
```

### Your `.env` file is not on the server

It's git-ignored — correctly, because it holds passwords. So it never reaches the build. Register
the values with the platform instead, and commit a `.env.example` with the key names and dummy
values so reviewers know what your app needs.

### Dependencies you installed by hand aren't in the image

`pip install requests` on your laptop doesn't put it in `requirements.txt`. The container will
start, fail on `import`, and exit. Run `pip freeze > requirements.txt` (or commit your
`package-lock.json`) before you push.

### Your laptop has 16 GB; your container has 512 MB

Loading a whole CSV or an ML model into memory works locally and gets the container killed on the
server with exit code 137. Load models once at startup, never per request. Read large files in
chunks. Bound any cache.

### Order your Dockerfile so the cache works

```dockerfile
COPY requirements.txt .          # dependency layer, rarely changes
RUN pip install -r requirements.txt
COPY . .                         # your code, changes every commit
```

With `COPY . .` first, every single commit reinstalls every dependency. That's the difference
between a 15-second deploy and a 4-minute one that sometimes times out.

---

## 4. Add a health endpoint

```python
@app.get("/health")
def health():
    return {"status": "ok"}
```

Keep it cheap — no database calls. It's polled every 30 seconds, and it's how the platform tells
"running" apart from "running but broken". Without one, a hung app keeps receiving traffic and a
failed deploy can't be rolled back automatically.

---

## 5. When a deployment fails

Open the incident in the dashboard. You'll get:

- the root cause in one sentence, with a **confidence score** — below 50% means treat it as a
  hint, not an answer
- the exact log lines it used as evidence
- a plain-language explanation
- what to change, often with the patch

Confidence is shown honestly on purpose. If it's low, the system is telling you it isn't sure, and
a DevOps mentor will pick it up.

**If the same failure happens three times, stop and ask for help.** The platform flags exactly
that pattern to your mentor, because retrying rarely fixes a deterministic failure.

---

## What the platform can see

Deployment outcomes, CI results, container logs and resource usage for your project, and the
contents of your repository when you request an analysis.

It does not read your messages, and nothing scores you as a student. The mentor board flags
*projects that look blocked* so someone can offer help — the signal it weights most heavily is the
same deployment failing repeatedly. You can see your own project's risk history in the dashboard.

If a credential ever shows up in your repo, it's reported as a blocker. Delete-and-push is not
enough — git keeps old versions, so the credential has to be revoked and reissued.
