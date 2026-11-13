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
   - `ASTF_DATA_DIR=/app/data` (default in image)
   - `PAPER_RESET_BANKROLL=true` once if you want a clean $1000 paper start
9. Deploy and open **Logs** — you should see `ASTF PAPER MODE` and cycle lines

## Persistent volume (strongly recommended)

Without a volume, every redeploy/restart can wipe:
- `runtime_state.json` (bankroll, learner, open positions)
- `trades.db` (history + timing stats)

**Setup:**
1. Northflank → service → **Volumes** (or Storage)
2. Create a small volume (1 GB is plenty)
3. Mount path: **`/app/data`**
4. Redeploy

The bot writes state under `ASTF_DATA_DIR` (default `/app/data`).

## Security
- Do **not** put a wallet private key in env until you intentionally unlock live mode after paper edge is proven.
- Paper mode needs no secrets.

## Local alternative (no Northflank)
```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp config.example.yaml config.yaml
mkdir -p data
export ASTF_DATA_DIR=./data
python -m src.main --mode paper
```
