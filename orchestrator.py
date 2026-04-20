"""
RenewTech Orchestrator — multi-agent health monitor and self-healer
Monitors NEU (Node.js) and Hermes (Python gateway), cross-checks each other,
auto-heals via Railway API, and notifies via Telegram.
"""
import asyncio
import datetime
import json
import os
import httpx

# ── Config ──────────────────────────────────────────────────────────────────
BRIDGE_SECRET      = os.environ.get("AGENT_BRIDGE_SECRET", "renewtech-bridge-2026")
RAILWAY_TOKEN      = os.environ.get("RAILWAY_TOKEN", "")
PROJECT_ID         = os.environ.get("RAILWAY_PROJECT_ID", "8009c5a8-c89b-42a8-a216-4c5d026c52d0")
ENV_ID             = os.environ.get("RAILWAY_ENV_ID", "5f09857e-f42b-40ba-a83a-6c7c4f2ba2c1")
NEU_SERVICE_ID     = os.environ.get("NEU_SERVICE_ID", "")
HERMES_SERVICE_ID  = os.environ.get("HERMES_SERVICE_ID", "fc8a7e36-e9e3-41b5-b952-3c3a723da688")
NEU_URL            = os.environ.get("NEU_INTERNAL_URL", os.environ.get("NEU_SERVICE_URL", ""))
HERMES_URL         = os.environ.get("HERMES_INTERNAL_URL", os.environ.get("HERMES_SERVICE_URL", ""))
TELEGRAM_TOKEN     = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_ADMIN_CHAT_ID", "8071235400")
CHECK_INTERVAL     = int(os.environ.get("CHECK_INTERVAL_SECONDS", "60"))
FAIL_THRESHOLD     = int(os.environ.get("FAIL_THRESHOLD", "3"))

RAILWAY_API        = "https://backboard.railway.app/graphql/v2"
BRIDGE_HEADERS     = {"X-Agent-Secret": BRIDGE_SECRET, "Content-Type": "application/json"}

# ── State ────────────────────────────────────────────────────────────────────
failures: dict[str, int] = {"NEU": 0, "HERMES": 0}
last_status: dict[str, str] = {"NEU": "unknown", "HERMES": "unknown"}
heal_in_progress: dict[str, bool] = {"NEU": False, "HERMES": False}


# ── Telegram notifications ───────────────────────────────────────────────────
async def notify(client: httpx.AsyncClient, text: str) -> None:
    if not TELEGRAM_TOKEN:
        print(f"[NOTIFY] {text}", flush=True)
        return
    try:
        await client.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"},
            timeout=10,
        )
    except Exception as e:
        print(f"[NOTIFY-ERR] {e}", flush=True)


# ── Railway redeploy (auto-heal) ─────────────────────────────────────────────
async def railway_redeploy(client: httpx.AsyncClient, service_id: str, service_name: str) -> bool:
    if not RAILWAY_TOKEN or not service_id:
        print(f"[HEAL] No RAILWAY_TOKEN or service_id for {service_name}, skipping redeploy", flush=True)
        return False
    query = """
    mutation serviceInstanceRedeploy($serviceId: String!, $environmentId: String!) {
      serviceInstanceRedeploy(serviceId: $serviceId, environmentId: $environmentId)
    }
    """
    try:
        r = await client.post(
            RAILWAY_API,
            headers={"Authorization": f"Bearer {RAILWAY_TOKEN}", "Content-Type": "application/json"},
            json={"query": query, "variables": {"serviceId": service_id, "environmentId": ENV_ID}},
            timeout=15,
        )
        data = r.json()
        if data.get("data", {}).get("serviceInstanceRedeploy"):
            print(f"[HEAL] Redeployed {service_name}", flush=True)
            return True
        print(f"[HEAL] Redeploy response for {service_name}: {data}", flush=True)
        return False
    except Exception as e:
        print(f"[HEAL-ERR] {service_name}: {e}", flush=True)
        return False


# ── Health check helpers ─────────────────────────────────────────────────────
async def check_agent(client: httpx.AsyncClient, name: str, base_url: str) -> dict:
    if not base_url:
        return {"alive": False, "error": f"{name}_URL not configured"}
    try:
        r = await client.get(
            f"{base_url}/agent/health",
            headers=BRIDGE_HEADERS,
            timeout=10,
        )
        if r.status_code == 200:
            return {"alive": True, "data": r.json()}
        return {"alive": False, "status_code": r.status_code}
    except Exception as e:
        return {"alive": False, "error": str(e)}


async def cross_check(client: httpx.AsyncClient, from_agent: str, target_url: str) -> dict:
    """Ask one agent to ping the other via its /agent/ping-* endpoint."""
    if not target_url:
        return {"ok": False, "error": "URL not configured"}
    endpoint = f"{target_url}/agent/ping-hermes" if from_agent == "NEU" else f"{target_url}/agent/ping-neu"
    try:
        r = await client.get(endpoint, headers=BRIDGE_HEADERS, timeout=15)
        return r.json() if r.status_code == 200 else {"ok": False, "status_code": r.status_code}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ── Main monitoring loop ─────────────────────────────────────────────────────
async def monitor_loop() -> None:
    async with httpx.AsyncClient() as client:
        await notify(client, "Orchestrator started — monitoring NEU and Hermes")

        while True:
            ts = datetime.datetime.utcnow().isoformat()
            print(f"[{ts}] Running health checks...", flush=True)

            neu_result    = await check_agent(client, "NEU", NEU_URL)
            hermes_result = await check_agent(client, "HERMES", HERMES_URL)

            for name, result, svc_id in [
                ("NEU", neu_result, NEU_SERVICE_ID),
                ("HERMES", hermes_result, HERMES_SERVICE_ID),
            ]:
                alive = result.get("alive", False)
                prev  = last_status[name]

                if alive:
                    if failures[name] > 0 or prev != "ok":
                        await notify(client, f"[Orchestrator] {name} recovered after {failures[name]} failures")
                    failures[name] = 0
                    last_status[name] = "ok"
                    heal_in_progress[name] = False
                    print(f"[{name}] OK", flush=True)
                else:
                    failures[name] += 1
                    last_status[name] = "down"
                    print(f"[{name}] FAIL #{failures[name]} — {result.get('error', result)}", flush=True)

                    if failures[name] == 1:
                        await notify(client, f"[Orchestrator] WARNING: {name} health check failed — {result.get('error','')}")

                    if failures[name] >= FAIL_THRESHOLD and not heal_in_progress[name]:
                        heal_in_progress[name] = True
                        await notify(client, f"[Orchestrator] AUTO-HEAL: redeploying {name} after {failures[name]} consecutive failures")
                        healed = await railway_redeploy(client, svc_id, name)
                        if not healed:
                            await notify(client, f"[Orchestrator] WARN: could not redeploy {name} — check RAILWAY_TOKEN and service ID")

            # Cross-check: ask NEU to ping Hermes
            if neu_result.get("alive") and HERMES_URL:
                cross = await cross_check(client, "NEU", NEU_URL)
                if not cross.get("hermes_alive", cross.get("ok", False)):
                    print(f"[CROSS] NEU->Hermes ping failed: {cross}", flush=True)

            await asyncio.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    asyncio.run(monitor_loop())
