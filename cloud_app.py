# -*- coding: utf-8 -*-
"""Cloud web wrapper for the lottery auto-update project.

This file intentionally keeps the original crawler/statistics logic in
scripts/lottery_auto_update_full.py unchanged. It adds a small FastAPI layer
for cloud deployment, manual/cron updates, and HTML dashboard serving.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, Dict

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parent
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import lottery_auto_update_full as core  # noqa: E402

APP_NAME = os.getenv("APP_NAME", "彩球雲端版")
DEFAULT_MODE = os.getenv("LOTTERY_UPDATE_MODE", "daily").lower()
AUTO_UPDATE_ON_START = os.getenv("AUTO_UPDATE_ON_START", "0").strip() in {"1", "true", "yes", "y"}
# 1 = 網頁上的「更新資料」按鈕可直接觸發更新；0 = 改回需要 UPDATE_TOKEN。
PUBLIC_WEB_UPDATE = os.getenv("PUBLIC_WEB_UPDATE", "1").strip().lower() in {"1", "true", "yes", "y", "on"}

app = FastAPI(title=APP_NAME, version="1.0.0")

for folder_name in ("output", "data", "logs"):
    path = ROOT / folder_name
    path.mkdir(parents=True, exist_ok=True)
    app.mount(f"/{folder_name}", StaticFiles(directory=str(path)), name=folder_name)

_update_lock = threading.Lock()
_update_state: Dict[str, Any] = {
    "running": False,
    "last_started_at": None,
    "last_finished_at": None,
    "last_mode": None,
    "last_ok": None,
    "last_error": None,
}


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _allowed_mode(mode: str | None) -> str:
    mode = (mode or DEFAULT_MODE or "daily").lower().strip()
    if mode not in {"daily", "weekly", "full"}:
        raise HTTPException(status_code=400, detail="mode must be daily, weekly, or full")
    return mode


def _check_token(request: Request) -> None:
    expected = os.getenv("UPDATE_TOKEN", "").strip()
    if not expected:
        return
    received = request.headers.get("x-update-token") or request.query_params.get("token") or ""
    if received.strip() != expected:
        raise HTTPException(status_code=401, detail="missing or invalid update token")


def _read_status_file() -> Dict[str, Any]:
    status_path = core.OUTPUT_DIR / "lottery_auto_update_status.json"
    if not status_path.exists():
        return {}
    try:
        return json.loads(status_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"status_read_error": str(exc)}


def _dashboard_path() -> Path:
    return core.OUTPUT_DIR / "lottery_final_dashboard.html"


def _cloud_overlay() -> str:
    return """
<style id="lottery-cloud-overlay-style">
#lottery-cloud-toolbar,
#lottery-cloud-toolbar *{box-sizing:border-box!important}
#lottery-cloud-toolbar{position:fixed!important;right:16px!important;bottom:16px!important;z-index:2147483000!important;display:flex!important;gap:8px!important;align-items:center!important;flex-wrap:wrap!important;max-width:min(640px,calc(100vw - 32px))!important;padding:10px 12px!important;border-radius:18px!important;background:rgba(15,23,42,.92)!important;color:#fff!important;border:1px solid rgba(255,255,255,.18)!important;box-shadow:0 16px 48px rgba(2,6,23,.35)!important;font-family:'Microsoft JhengHei',Arial,sans-serif!important;font-size:13px!important;line-height:1.3!important;backdrop-filter:blur(10px)!important}
#lottery-cloud-toolbar strong{font-weight:900!important;color:#fef3c7!important}
#lottery-cloud-toolbar button,#lottery-cloud-toolbar a{appearance:none!important;border:0!important;border-radius:999px!important;padding:9px 13px!important;font-weight:900!important;text-decoration:none!important;cursor:pointer!important;background:#fbbf24!important;color:#111827!important;font-size:14px!important;line-height:1!important}
#lottery-cloud-toolbar a.secondary{background:#e5e7eb!important;color:#111827!important}
#lottery-cloud-toolbar button:disabled{opacity:.6!important;cursor:not-allowed!important}
#lottery-cloud-status{min-width:110px!important;color:#d1fae5!important}
@media (max-width:640px){#lottery-cloud-toolbar{left:10px!important;right:10px!important;bottom:10px!important;justify-content:center!important}.lottery-cloud-label{display:none!important}}
</style>
<div id="lottery-cloud-toolbar" role="region" aria-label="cloud controls">
  <span class="lottery-cloud-label"><strong>雲端版</strong>｜<span id="lottery-cloud-status">讀取中</span></span>
  <button type="button" onclick="lotteryCloudUpdate('weekly')">更新資料</button>
  <a class="secondary" href="/api/status" target="_blank" rel="noopener">狀態</a>
</div>
<script id="lottery-cloud-overlay-script">
(function(){
  const statusEl = document.getElementById('lottery-cloud-status');
  async function refreshStatus(){
    try{
      const res = await fetch('/api/status', {cache:'no-store'});
      const data = await res.json();
      if(data.update && data.update.running){ statusEl.textContent = '更新中…'; return true; }
      const generated = data.generated_at || (data.file_status && data.file_status.dashboard_mtime) || '尚未更新';
      statusEl.textContent = generated;
      return false;
    }catch(e){ statusEl.textContent = '離線'; return false; }
  }
  async function waitDone(){
    const running = await refreshStatus();
    if(running){ setTimeout(waitDone, 3500); return; }
    statusEl.textContent = '完成，重新載入…';
    location.href='/?t=' + Date.now();
  }
  window.lotteryCloudUpdate = async function(mode){
    const buttons = document.querySelectorAll('#lottery-cloud-toolbar button,.cloud-actions button');
    buttons.forEach(b => b.disabled = true);
    statusEl.textContent = '送出更新…';
    try{
      let res = await fetch('/api/web-update?mode=' + encodeURIComponent(mode || 'weekly'), {method:'POST', cache:'no-store'});
      if(res.status === 401){
        const token = prompt('請輸入 UPDATE_TOKEN') || '';
        res = await fetch('/api/update?mode=' + encodeURIComponent(mode || 'weekly') + '&token=' + encodeURIComponent(token), {method:'POST', cache:'no-store'});
      }
      const text = await res.text();
      if(!res.ok){ statusEl.textContent = '失敗 ' + res.status; alert(text); buttons.forEach(b => b.disabled = false); return; }
      statusEl.textContent = '已開始更新…';
      setTimeout(waitDone, 2500);
    }catch(e){
      statusEl.textContent = '更新失敗';
      alert(String(e));
      buttons.forEach(b => b.disabled = false);
    }
  };
  refreshStatus();
  setInterval(refreshStatus, 10000);
})();
</script>
"""


def _inject_overlay(html: str) -> str:
    overlay = _cloud_overlay()
    if "lottery-cloud-overlay-script" in html:
        return html
    lower = html.lower()
    idx = lower.rfind("</body>")
    if idx >= 0:
        return html[:idx] + overlay + html[idx:]
    return html + overlay


def _empty_dashboard_html() -> str:
    return f"""<!doctype html>
<html lang="zh-Hant"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{APP_NAME}</title>
<style>
:root{{--bg:#eef2f7;--card:#fffdf7;--text:#0f172a;--muted:#64748b;--line:#dbe4ee;--accent:#fbbf24}}
*{{box-sizing:border-box}}body{{margin:0;background:linear-gradient(135deg,#eef2f7,#fff7ed);color:var(--text);font-family:'Microsoft JhengHei',Arial,sans-serif}}.wrap{{max-width:980px;margin:0 auto;padding:24px}}.card{{background:var(--card);border:1px solid var(--line);border-radius:24px;padding:22px;margin:16px 0;box-shadow:0 18px 50px rgba(15,23,42,.08)}}h1{{margin:0 0 8px;font-size:32px}}p{{line-height:1.8;color:var(--muted)}}button,a.btn{{border:0;border-radius:999px;background:var(--accent);color:#111827;font-weight:900;padding:12px 18px;text-decoration:none;cursor:pointer;display:inline-block;margin:6px 6px 6px 0}}code{{background:#f1f5f9;border-radius:8px;padding:2px 6px}}.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px}}.mini{{padding:14px;border-radius:18px;background:#f8fafc;border:1px solid #e2e8f0}}</style>
</head><body><main class="wrap"><section class="card"><h1>{APP_NAME}</h1><p>目前還沒有產生雲端 Dashboard。按下「更新資料」後，系統會抓台彩資料、合併 CSV、輸出 <code>output/lottery_final_dashboard.html</code>，完成後會自動重新整理。</p><button onclick="lotteryCloudUpdate('weekly')">更新資料</button><a class="btn" href="/api/status" target="_blank" rel="noopener">查看狀態 JSON</a></section><section class="card"><h2>雲端端點</h2><div class="grid"><div class="mini"><b>/</b><br>Dashboard</div><div class="mini"><b>/api/web-update</b><br>網頁按鈕更新</div><div class="mini"><b>/api/update-sync</b><br>同步更新，適合 cron</div><div class="mini"><b>/output</b><br>輸出檔案目錄</div></div></section></main>{_cloud_overlay()}</body></html>"""


def _run_update(mode: str) -> Dict[str, Any]:
    if not _update_lock.acquire(blocking=False):
        return {"ok": False, "running": True, "message": "update already running", "update": dict(_update_state)}
    try:
        _update_state.update({
            "running": True,
            "last_started_at": _now(),
            "last_finished_at": None,
            "last_mode": mode,
            "last_ok": None,
            "last_error": None,
        })
        code = core.main(mode=mode, open_dashboard=False)
        _update_state.update({
            "running": False,
            "last_finished_at": _now(),
            "last_ok": code == 0,
            "last_error": None,
        })
        return {"ok": code == 0, "running": False, "exit_code": code, "update": dict(_update_state)}
    except Exception as exc:
        err = traceback.format_exc()
        core.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        (core.OUTPUT_DIR / "lottery_error.txt").write_text(err, encoding="utf-8")
        _update_state.update({
            "running": False,
            "last_finished_at": _now(),
            "last_ok": False,
            "last_error": str(exc),
        })
        return {"ok": False, "running": False, "error": str(exc), "traceback": err, "update": dict(_update_state)}
    finally:
        _update_lock.release()


def _background_update(mode: str) -> None:
    _run_update(mode)


@app.on_event("startup")
def startup() -> None:
    if AUTO_UPDATE_ON_START and not _dashboard_path().exists():
        thread = threading.Thread(target=_background_update, args=(DEFAULT_MODE,), daemon=True)
        thread.start()


@app.get("/", response_class=HTMLResponse)
def home() -> HTMLResponse:
    path = _dashboard_path()
    if path.exists():
        html = path.read_text(encoding="utf-8", errors="replace")
        return HTMLResponse(_inject_overlay(html), headers={"Cache-Control": "no-store"})
    return HTMLResponse(_empty_dashboard_html(), headers={"Cache-Control": "no-store"})


@app.get("/health", response_class=PlainTextResponse)
def health() -> str:
    return "ok"


@app.get("/api/status")
def api_status() -> JSONResponse:
    dashboard = _dashboard_path()
    error_file = core.OUTPUT_DIR / "lottery_error.txt"
    status = _read_status_file()
    status.update({
        "app": APP_NAME,
        "update": dict(_update_state),
        "file_status": {
            "dashboard_exists": dashboard.exists(),
            "dashboard_mtime": datetime.fromtimestamp(dashboard.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S") if dashboard.exists() else None,
            "error_exists": error_file.exists(),
        },
        "paths": {
            "dashboard": "/output/lottery_final_dashboard.html",
            "status_json": "/output/lottery_auto_update_status.json",
            "today_picks": "/output/lottery_today_picks.csv",
            "confidence_539": "/output/539_confidence_rank.csv",
        },
    })
    return JSONResponse(status, headers={"Cache-Control": "no-store"})


@app.api_route("/api/web-update", methods=["GET", "POST"])
def api_web_update(request: Request, background_tasks: BackgroundTasks, mode: str | None = None) -> JSONResponse:
    """One-click update endpoint for the web button.

    PUBLIC_WEB_UPDATE=1 lets the page trigger updates without typing the token.
    Set PUBLIC_WEB_UPDATE=0 in Render if you want to protect this endpoint too.
    """
    if not PUBLIC_WEB_UPDATE:
        _check_token(request)
    selected_mode = _allowed_mode(mode or "weekly")
    if _update_state.get("running"):
        return JSONResponse({"ok": False, "running": True, "message": "update already running", "update": dict(_update_state)}, status_code=202)
    background_tasks.add_task(_background_update, selected_mode)
    _update_state.update({
        "running": True,
        "last_started_at": _now(),
        "last_mode": selected_mode,
        "last_ok": None,
        "last_error": None,
    })
    return JSONResponse({"ok": True, "running": True, "message": "web update started", "mode": selected_mode}, status_code=202)


@app.api_route("/api/update", methods=["GET", "POST"])
def api_update(request: Request, background_tasks: BackgroundTasks, mode: str | None = None) -> JSONResponse:
    _check_token(request)
    selected_mode = _allowed_mode(mode)
    if _update_state.get("running"):
        return JSONResponse({"ok": False, "running": True, "message": "update already running", "update": dict(_update_state)}, status_code=202)
    background_tasks.add_task(_background_update, selected_mode)
    _update_state.update({
        "running": True,
        "last_started_at": _now(),
        "last_mode": selected_mode,
        "last_ok": None,
        "last_error": None,
    })
    return JSONResponse({"ok": True, "running": True, "message": "update started", "mode": selected_mode}, status_code=202)


@app.api_route("/api/update-sync", methods=["GET", "POST"])
def api_update_sync(request: Request, mode: str | None = None) -> JSONResponse:
    _check_token(request)
    selected_mode = _allowed_mode(mode)
    result = _run_update(selected_mode)
    status_code = 200 if result.get("ok") else (202 if result.get("running") else 500)
    return JSONResponse(result, status_code=status_code, headers={"Cache-Control": "no-store"})


@app.get("/api/error", response_class=PlainTextResponse)
def api_error() -> str:
    path = core.OUTPUT_DIR / "lottery_error.txt"
    if not path.exists():
        return "no error file"
    return path.read_text(encoding="utf-8", errors="replace")
