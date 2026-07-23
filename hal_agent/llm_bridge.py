"""
Ponte LLM locale (opzionale).

Modello "outbound polling": l'agente CHIEDE al SaaS se ci sono job di generazione,
li esegue contro l'LLM locale (Ollama / LM Studio, API OpenAI-compatibile) e rimanda
il risultato. Così NON serve alcun tunnel in ingresso (niente NAT/firewall aperti).

Contratto lato SaaS (da implementare):
  GET  {server_url}{poll_path}      -> { "job": {id, prompt, model, max_tokens} } | { "job": null }
  POST {server_url}{result_path}    <- { "job_id": ..., "text": "..." }
"""
import logging
import httpx

from . import config as cfg
from . import telemetry

log = logging.getLogger("hal_agent.llm")


def _headers(conf):
    h = {"Content-Type": "application/json"}
    if conf.get("token"):
        h["Authorization"] = f"Bearer {conf['token']}"
    return h


def poll_and_run_once(conf: dict) -> bool:
    """Preleva un job dal SaaS, lo esegue in locale e invia il risultato. True se ha lavorato."""
    br = conf.get("llm_bridge", {})
    if not br.get("enabled"):
        return False
    server = conf["server_url"].rstrip("/")
    try:
        r = httpx.get(server + br.get("poll_path", "/api/agent/jobs"),
                      headers=_headers(conf), timeout=20)
        r.raise_for_status()
        job = (r.json() or {}).get("job")
    except Exception as e:
        log.debug("Nessun job / errore poll: %s", e)
        return False
    if not job:
        return False

    text = run_local_llm(br, job.get("prompt", ""), job.get("model"), job.get("max_tokens", 1024))
    detail = "" if text else "l'LLM locale non ha risposto"
    try:
        httpx.post(server + br.get("result_path", "/api/agent/jobs/result"),
                   headers=_headers(conf),
                   json={"job_id": job.get("id"), "text": text}, timeout=30)
        log.info("Job %s eseguito e restituito", job.get("id"))
    except Exception as e:
        log.error("Invio risultato job fallito: %s", e)
        detail = "risultato non consegnato: %s" % str(e)[:120]
    telemetry.record_bridge_job(job.get("id"), bool(text) and not detail, len(text or ""), detail)
    return True


def check_llm(bridge: dict):
    """
    Verifica se l'LLM (locale/tunnel/remoto) risponde: GET .../v1/models.
    Ritorna (ok: bool, detail: str). detail contiene l'errore se non ok.
    """
    endpoint = (bridge.get("endpoint") or "http://localhost:11434").rstrip("/")
    url = endpoint + ("/models" if endpoint.endswith("/v1") else "/v1/models")
    headers = {}
    key = (bridge.get("api_key") or "").strip()
    if key:
        headers["Authorization"] = "Bearer " + key
    try:
        r = httpx.get(url, headers=headers, timeout=8)
        if r.status_code == 200:
            return True, ""
        return False, f"HTTP {r.status_code}"
    except Exception as e:
        return False, str(e)[:120]


def run_local_llm(bridge: dict, prompt: str, model=None, max_tokens: int = 1024) -> str:
    """
    Chiama un endpoint OpenAI-compatibile: locale (Ollama :11434 / LM Studio :1234),
    tunnel (URL remoto) o provider remoto (es. DeepSeek). Usa api_key/model dalla config.
    """
    endpoint = (bridge.get("endpoint") or "http://localhost:11434").rstrip("/")
    # accetta sia base "…" sia "…/v1"
    url = endpoint + ("/chat/completions" if endpoint.endswith("/v1") else "/v1/chat/completions")

    mdl = model or bridge.get("model") or ("llama3" if "11434" in endpoint else "local-model")
    headers = {"Content-Type": "application/json"}
    key = (bridge.get("api_key") or "").strip()
    if key:
        headers["Authorization"] = "Bearer " + key

    body = {
        "model": mdl,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "stream": False,
    }
    try:
        r = httpx.post(url, json=body, headers=headers, timeout=300)
        r.raise_for_status()
        data = r.json()
        return data["choices"][0]["message"]["content"]
    except Exception as e:
        log.error("LLM bridge errore (%s): %s", url, e)
        return ""
