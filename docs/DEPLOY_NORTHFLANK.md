# Deploy ASTF on Northflank (free Sandbox)

Northflank Sandbox includes always-on compute (no sleeping) with limited free resources — a good fit for a paper trading worker.

## What you need
- Northflank account (Sandbox is fine)
- This repo connected via GitHub: `ssidhar1/polymarket-adaptive-bot`
- **No private key required for paper mode**

## Steps

1. Log in at [https://app.northflank.com](https://app.northflank.com)
2. Create a **Project** (or use an existing one)
3. **Create Service** → deploy from **Git repository**
4. Connect GitHub if needed and select `polymarket-adaptive-bot`
5. Build settings:
   - Build type: **Dockerfile**
   - Dockerfile path: `/Dockerfile`
   - Build context: `/`
6. Runtime:
   - This is a **worker** (no public HTTP port required)
   - Command is already in the Dockerfile: `python -m src.main --mode paper`
7. Resources: smallest plan that fits free Sandbox (e.g. ~0.1–0.5 vCPU, 256–512 MB RAM)
8. Environment variables (optional):
   - `LOG_LEVEL=INFO`
   - `DEFAULT_MODE=paper`
9. Deploy and open **Logs** — you should see `ASTF PAPER MODE` and cycle lines

## Persistence tip
Ephemeral disks lose `trades.db` / `runtime_state.json` on redeploy.
For longer paper runs, attach a small **persistent volume** mounted at `/app/data` and later point the bot paths there (optional improvement).

## Security
- Do **not** put a wallet private key in env until you intentionally unlock live mode after paper edge is proven.
- Paper mode needs no secrets.

## Local alternative (no Northflank)
```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp config.example.yaml config.yaml
python -m src.main --mode paper
```
