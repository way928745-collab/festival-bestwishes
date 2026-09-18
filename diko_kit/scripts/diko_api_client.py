#!/usr/bin/env python3
"""
diko-use 底座客户端（DIKO · www.dikoai.cn）——通用框架版，不含任何个人凭据。

两层设计：
1) 认证层（与业务无关）：
   - login()                 登入拿 access + refresh
   - refresh_access()        每次调用前换新 access（refresh 超 24h 自动重登）
   - get_profile()           查询用户信息 / 剩余 DI 币
   - format_balance_line()   余额展示一句话
2) 任务层（业务可配置）：
   - submit_task() / get_task_status() / wait_task_finish()
   - extract_result_urls() / download_file()

凭据只读写本技能 scripts/config/diko_api.env（发布版全空；运行时交互写入）。

用法（其他技能引用）：
    import sys
    sys.path.insert(0, "<diko-use目录>/scripts")
    import diko_api_client as diko
    access = diko.refresh_access()
    task_id = diko.submit_task(access, app_id="41", node_data={...})
"""
import os
import time
from pathlib import Path
from datetime import datetime
import requests
from dotenv import load_dotenv, set_key, dotenv_values

# 框架版：凭据只读写本技能 scripts/config/diko_api.env（与脚本同级的 config 目录）
CONFIG_DIR = os.path.join(os.path.dirname(os.path.realpath(__file__)), "config")
CONFIG_PATH = os.path.join(CONFIG_DIR, "diko_api.env")

load_dotenv(CONFIG_PATH)
BASE_URL = (os.getenv("DIKO_BASE_URL") or "https://www.dikoai.cn").rstrip("/")

POLL_INTERVAL_SEC = int(os.getenv("POLL_INTERVAL_SEC", "10"))          # 音频/视频任务轮询间隔
POLL_TIMEOUT_SEC = int(os.getenv("POLL_TIMEOUT_SEC", "600"))           # 总超时
REFRESH_MAX_AGE_HOURS = int(os.getenv("REFRESH_MAX_AGE_HOURS", "24"))  # refresh 超该时长 → 重新登入

LANGS = ["中文", "英文", "日文", "韩文", "德文", "法文", "俄文", "葡萄牙文", "西班牙文", "意大利文"]

# env 变量名
KEY_BASE_URL = "DIKO_BASE_URL"
KEY_USERNAME = "DIKO_USERNAME"
KEY_PASSWORD = "DIKO_PASSWORD"
KEY_REFRESH = "DIKO_REFRESH"
KEY_REFRESH_TIME = "DIKO_REFRESH_TIME"      # 登入/刷新成功时间（ISO 字符串）


# ───────────────── 配置读写 ─────────────────
def load_credentials() -> tuple:
    """从 env 读取 (username, password)。缺字段抛错并提示走注册/登入引导。"""
    vals = dotenv_values(CONFIG_PATH)
    username = (vals.get(KEY_USERNAME) or "").strip()
    password = (vals.get(KEY_PASSWORD) or "").strip()
    if not username or not password:
        raise RuntimeError(
            "尚未配置 DIKO 用户名/密码。请在 config/diko_api.env 写入：\n"
            f"{KEY_USERNAME}=你的用户名\n{KEY_PASSWORD}=你的密码\n"
            "（首次使用：交互询问用户名密码并保存后重试；无账号先走 diko-use 注册模块或到 www.dikoai.cn 注册）"
        )
    return username, password


def save_credentials(username: str, password: str) -> str:
    """交互确认后保存账号到 env。返回 env 路径。"""
    os.makedirs(CONFIG_DIR, exist_ok=True)
    set_key(CONFIG_PATH, KEY_USERNAME, username)
    set_key(CONFIG_PATH, KEY_PASSWORD, password)
    return CONFIG_PATH


def _set_env(key: str, value: str):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    set_key(CONFIG_PATH, key, value)


# ───────────────── 认证：登入 & 刷新 ─────────────────
def _form_post(path: str, data: dict, timeout: int = 60):
    """通用 form-data POST，返回 json。"""
    resp = requests.post(BASE_URL + path, data=data, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def _get_json(path: str, headers: dict, timeout: int = 60):
    """通用 GET，返回 json。"""
    resp = requests.get(BASE_URL + path, headers=headers, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def login(username: str, password: str, save: bool = True) -> dict:
    """登入拿 access + refresh。save=True 时把 refresh 与时间缓存到 env；
    save=False 为一次性登入（如注册成功后的余额展示），不写入任何凭据文件。"""
    data = _form_post("/api/membership/jwt/create/", {"username": username, "password": password})
    access = data.get("access")
    refresh = data.get("refresh")
    if not access or not refresh:
        raise RuntimeError(f"登入响应缺少 access/refresh: {data}")
    if save:
        _set_env(KEY_REFRESH, refresh)
        _set_env(KEY_REFRESH_TIME, datetime.now().isoformat())
    return {"access": access, "refresh": refresh, "user_info": data.get("user_info")}


def _need_relogin() -> bool:
    """refresh 是否已超过 REFRESH_MAX_AGE_HOURS 需重新登入。"""
    vals = dotenv_values(CONFIG_PATH)
    t_str = (vals.get(KEY_REFRESH_TIME) or "").strip()
    if not t_str:
        return True
    try:
        t = datetime.fromisoformat(t_str)
        return (datetime.now() - t).total_seconds() > REFRESH_MAX_AGE_HOURS * 3600
    except ValueError:
        return True


def refresh_access() -> str:
    """
    用 refresh 换新 access（>REFRESH_MAX_AGE_HOURS 则重新登入）。返回可用 access。
    所有任务提交前都应调用本函数拿最新的 access。
    """
    vals = dotenv_values(CONFIG_PATH)
    username = (vals.get(KEY_USERNAME) or "").strip()
    password = (vals.get(KEY_PASSWORD) or "").strip()

    if _need_relogin():
        if not username or not password:
            raise RuntimeError("refresh 已过期，且未配置用户名/密码，无法重新登入。请走登入/注册引导。")
        info = login(username, password)
        return info["access"]

    refresh = (vals.get(KEY_REFRESH) or "").strip()
    if not refresh:
        if not username or not password:
            raise RuntimeError("未找到 refresh，且未配置用户名/密码。请走登入/注册引导。")
        info = login(username, password)
        return info["access"]

    try:
        data = _form_post("/api/membership/jwt/refresh/", {"refresh": refresh})
        access = data.get("access")
        if not access:
            raise RuntimeError(f"刷新响应缺少 access: {data}")
        _set_env(KEY_REFRESH_TIME, datetime.now().isoformat())
        return access
    except Exception as e:
        raise RuntimeError(f"刷新 access 失败：{e}")


def _headers(access: str) -> dict:
    return {"Authorization": f"Bearer {access}", "Content-Type": "application/json"}


# ───────────────── 余额/等级 profile（通用） ─────────────────
def get_profile(access: str) -> dict:
    """
    GET /api/membership/profile/ 取用户信息（整个响应返回）。
    调用方自行取 username / vip_type_display / vip_points / permanent_di_points 等字段。
    """
    data = _get_json("/api/membership/profile/", _headers(access))
    objs = data if isinstance(data, list) else [data]
    obj = next((o for o in objs if isinstance(o, dict)), {})
    return obj


def format_balance_line(profile: dict) -> str:
    """余额展示一句话：名称 + 等级 + 剩余 DI 币。
    vip_points=月会员积分，permanent_di_points=永久积分。"""
    username = profile.get("username") or "这位用户"
    vip = profile.get("vip_type_display") or "会员"
    monthly = profile.get("vip_points")
    permanent = profile.get("permanent_di_points")
    monthly_txt = f"{monthly:,}" if isinstance(monthly, (int, float)) else str(monthly or "0")
    perm_txt = f"{permanent:,}" if isinstance(permanent, (int, float)) else str(permanent or "0")
    return (f"✨ {username} · {vip}，当前 DI 币余量：月积分 {monthly_txt} + 永久积分 {perm_txt}")


# ───────────────── z_model 异步任务层（业务可配置） ─────────────────
def submit_task(access: str, app_id: str, node_data: dict) -> str:
    """
    提交 z_model 异步任务，返回 task_id。
    app_id：应用 id（人声"摄影师-声音设计"=41；其他任务传自己的 app_id）
    node_data：应用节点数据（键值由应用决定，调用方按需传入）
    """
    payload = {"app_id": str(app_id), "node_data": node_data}
    resp = requests.post(BASE_URL + "/api/z_model/tasks/", headers=_headers(access), json=payload, timeout=60)
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"提交任务失败: {data}")
    return data["data"]["task_id"]


def get_task_status(task_id: str, access: str, app_id: str = "41") -> dict:
    """轮询一次任务状态，返回 data dict。"""
    params = {"task_id": task_id, "app_id": str(app_id)}
    resp = requests.get(BASE_URL + "/api/z_model/tasks/", headers=_headers(access), params=params, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"查询任务状态失败: {data}")
    return data["data"]


def wait_task_finish(task_id: str, access: str, app_id: str = "41", progress_cb=None) -> dict:
    """
    每 POLL_INTERVAL_SEC 秒轮询任务直到 completed/failed。
    progress_cb: 可选回调 progress_cb(progress:int, status:str)。
    """
    start = time.time()
    while True:
        d = get_task_status(task_id, access, app_id)
        status = d.get("status")
        progress = d.get("progress") or 0
        if progress_cb:
            progress_cb(int(progress), status or "")
        if status in ("completed", "failed", "error"):
            return d
        if time.time() - start > POLL_TIMEOUT_SEC:
            raise TimeoutError(f"任务 {task_id} 轮询超时")
        time.sleep(POLL_INTERVAL_SEC)


def extract_result_urls(result_data: dict) -> list:
    """从完成的任务 data 中提取结果 URL 列表（音频/图片等）。"""
    urls = result_data.get("result_list") or []
    if not urls and result_data.get("result_urls"):
        urls = [result_data["result_urls"]]
    return urls


def download_file(url: str, save_path: str) -> str:
    """下载文件到本地。"""
    r = requests.get(url, timeout=180)
    r.raise_for_status()
    os.makedirs(os.path.dirname(os.path.abspath(save_path)) or ".", exist_ok=True)
    with open(save_path, "wb") as f:
        f.write(r.content)
    return save_path
