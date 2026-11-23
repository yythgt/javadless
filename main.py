
import asyncio
import aiohttp
from aiohttp import web, WSMsgType
import os
import sys
import json
import uuid
import shutil
import tarfile
import signal
import platform
import logging
from pathlib import Path
from datetime import datetime, timezone
from urllib.parse import urlencode, quote

# ==========================================
# CONFIG
# ==========================================

CONFIG = {
    "DOMAIN": "node17.botkeep.cloud",
    "PORT": 57344,

    "MAX_USERS": 100,
    "INITIAL_USERS": 1,

    "INTERNAL_PORT": 18080,
    "SINGBOX_VERSION": "1.11.15",

    "RESTART_BASE_DELAY": 2,
    "RESTART_MAX_DELAY": 30,
    "STOP_TIMEOUT": 5
}

WS_PATH = (
    "/@BlueKnight_Net--@BlueKnight_Net--"
    "@BlueKnight_Net--@BlueKnight_Net--ws"
)

# ==========================================
# PATHS
# ==========================================

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "bk-data"
BIN_DIR = DATA_DIR / "bin"

USERS_FILE = DATA_DIR / "users.json"
CONFIG_FILE = DATA_DIR / "sing-box.json"
BIN_FILE = BIN_DIR / "sing-box"

# ==========================================
# LOGGING
# ==========================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [BlueKnight] %(levelname)s: %(message)s"
)

logger = logging.getLogger("BlueKnight")


def log(*args):
    logger.info(" ".join(map(str, args)))


def log_error(message, error):
    logger.error("%s %s", message, error, exc_info=error)


# ==========================================
# GLOBAL STATE
# ==========================================

users = []

singbox = None
server_runner = None
client_session = None

shutting_down = False
restarting = False

restart_task = None
restart_attempts = 0

mutation_lock = asyncio.Lock()
restart_lock = asyncio.Lock()
shutdown_lock = asyncio.Lock()

shutdown_event = asyncio.Event()


# ==========================================
# HELPERS
# ==========================================

def now_iso():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def prepare_directories():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    BIN_DIR.mkdir(parents=True, exist_ok=True)


def escape_html(value):
    import html
    return html.escape(str(value), quote=True)


def atomic_write(file_path, content):
    file_path = Path(file_path)

    temp = file_path.with_name(
        file_path.name + f".{os.getpid()}.tmp"
    )

    temp.write_text(content, encoding="utf-8")
    os.replace(temp, file_path)


def serialize_user(user):
    return {
        "id": user["id"],
        "name": user["name"],
        "uuid": user["uuid"],
        "createdAt": user["createdAt"]
    }


# ==========================================
# USERS
# ==========================================

def save_users():
    atomic_write(
        USERS_FILE,
        json.dumps(users, indent=2, ensure_ascii=False)
    )


def load_users():
    global users

    if USERS_FILE.exists():
        try:
            users = json.loads(
                USERS_FILE.read_text(encoding="utf-8")
            )
        except Exception as error:
            log_error("Users file error:", error)
            users = []

    if not isinstance(users, list):
        users = []

    users = [
        user for user in users
        if (
            isinstance(user, dict)
            and isinstance(user.get("id"), str)
            and isinstance(user.get("uuid"), str)
        )
    ]

    while len(users) < CONFIG["INITIAL_USERS"]:
        number = len(users) + 1

        users.append({
            "id": str(uuid.uuid4()),
            "name": f"User {number}",
            "uuid": str(uuid.uuid4()),
            "createdAt": now_iso()
        })

    users = users[:CONFIG["MAX_USERS"]]

    save_users()


# ==========================================
# DOWNLOAD
# ==========================================

async def download_file(url, destination, redirects=0):
    if redirects > 5:
        raise RuntimeError("Too many redirects")

    timeout = aiohttp.ClientTimeout(total=120)

    async with client_session.get(
        url,
        headers={"User-Agent": "BlueKnight-Botkeep"},
        timeout=timeout,
        allow_redirects=True,
        max_redirects=5
    ) as response:

        if response.status != 200:
            raise RuntimeError(
                f"Download failed: HTTP {response.status}"
            )

        with open(destination, "wb") as output:
            async for chunk in response.content.iter_chunked(65536):
                output.write(chunk)


# ==========================================
# INSTALL SING-BOX
# ==========================================

async def install_singbox():
    if sys.platform != "linux":
        raise RuntimeError("Linux required")

    if BIN_FILE.exists():
        try:
            result = await asyncio.create_subprocess_exec(
                str(BIN_FILE),
                "version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )

            await asyncio.wait_for(result.communicate(), timeout=10)

            if result.returncode == 0:
                log("sing-box already installed")
                return

        except Exception:
            pass

        log("Existing sing-box binary is invalid")
        BIN_FILE.unlink(missing_ok=True)

    machine = platform.machine().lower()

    if machine in ("x86_64", "amd64"):
        arch = "amd64"
    elif machine in ("aarch64", "arm64"):
        arch = "arm64"
    else:
        raise RuntimeError(
            f"Unsupported architecture: {machine}"
        )

    version = CONFIG["SINGBOX_VERSION"]

    archive_name = (
        f"sing-box-{version}-linux-{arch}.tar.gz"
    )

    url = (
        f"https://github.com/SagerNet/sing-box/releases/"
        f"download/v{version}/{archive_name}"
    )

    archive = BIN_DIR / archive_name
    extract_dir = BIN_DIR / "extract"

    log("Downloading sing-box...")

    await download_file(url, archive)

    extract_dir.mkdir(parents=True, exist_ok=True)

    try:
        with tarfile.open(archive, "r:gz") as tar:
            root = extract_dir.resolve()

            for member in tar.getmembers():
                target = (extract_dir / member.name).resolve()

                if not target.is_relative_to(root):
                    raise RuntimeError("Unsafe archive path")

            tar.extractall(extract_dir)

        binary = None

        for folder in extract_dir.iterdir():
            candidate = folder / "sing-box"

            if candidate.is_file():
                binary = candidate
                break

        if binary is None:
            raise RuntimeError("sing-box binary not found")

        shutil.copy2(binary, BIN_FILE)
        BIN_FILE.chmod(0o755)

    finally:
        archive.unlink(missing_ok=True)
        shutil.rmtree(extract_dir, ignore_errors=True)

    log("sing-box installed")


# ==========================================
# SING-BOX CONFIG
# ==========================================

def build_singbox_config():
    return {
        "log": {
            "level": "warn",
            "timestamp": True
        },

        "inbounds": [
            {
                "type": "vless",
                "tag": "vless-ws",

                "listen": "127.0.0.1",
                "listen_port": CONFIG["INTERNAL_PORT"],

                "users": [
                    {
                        "name": user["name"],
                        "uuid": user["uuid"]
                    }
                    for user in users
                ],

                "tls": {
                    "enabled": False
                },

                "transport": {
                    "type": "ws",
                    "path": WS_PATH
                }
            }
        ],

        "outbounds": [
            {
                "type": "direct",
                "tag": "direct"
            }
        ],

        "route": {
            "final": "direct"
        }
    }


def write_singbox_config():
    atomic_write(
        CONFIG_FILE,
        json.dumps(
            build_singbox_config(),
            indent=2,
            ensure_ascii=False
        )
    )


# ==========================================
# SING-BOX PROCESS MANAGEMENT
# ==========================================

def is_singbox_running():
    return (
        singbox is not None
        and singbox.returncode is None
    )


async def check_singbox_config():
    process = await asyncio.create_subprocess_exec(
        str(BIN_FILE),
        "check",
        "-c",
        str(CONFIG_FILE),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )

    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(),
            timeout=15
        )
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        raise RuntimeError("sing-box config check timeout")

    if process.returncode != 0:
        raise RuntimeError(
            "Invalid sing-box config: "
            + (stderr or stdout).decode(errors="replace")
        )


async def start_singbox():
    global singbox

    if shutting_down:
        raise RuntimeError("Application is shutting down")

    if is_singbox_running():
        log("sing-box is already running")
        return

    write_singbox_config()

    await check_singbox_config()

    process = await asyncio.create_subprocess_exec(
        str(BIN_FILE),
        "run",
        "-c",
        str(CONFIG_FILE),
        stdout=None,
        stderr=None
    )

    singbox = process

    log("sing-box started, PID:", process.pid)

    asyncio.create_task(
        monitor_singbox(process)
    )


async def monitor_singbox(process):
    global singbox

    code = await process.wait()

    log("sing-box exited:", code)

    if singbox is process:
        singbox = None

    if shutting_down or restarting:
        return

    log("Unexpected sing-box exit detected")

    schedule_singbox_restart()


# ==========================================
# AUTOMATIC RESTART
# ==========================================

def schedule_singbox_restart():
    global restart_task, restart_attempts

    if shutting_down or restarting:
        return

    if restart_task is not None and not restart_task.done():
        return

    delay = min(
        CONFIG["RESTART_MAX_DELAY"],
        CONFIG["RESTART_BASE_DELAY"] *
        (2 ** restart_attempts)
    )

    restart_attempts += 1

    log(
        f"sing-box restart scheduled in {delay}s",
        f"attempt {restart_attempts}"
    )

    restart_task = asyncio.create_task(
        automatic_restart(delay)
    )


async def automatic_restart(delay):
    global restart_attempts, restart_task

    try:
        await asyncio.sleep(delay)

        if shutting_down or is_singbox_running():
            return

        await start_singbox()
        restart_attempts = 0

    except Exception as error:
        log_error("Automatic restart failed:", error)

        if not shutting_down:
            schedule_singbox_restart()

    finally:
        restart_task = None


# ==========================================
# SAFE PROCESS STOP
# ==========================================

async def stop_singbox():
    global singbox

    process = singbox

    if process is None or process.returncode is not None:
        if singbox is process:
            singbox = None
        return

    log("Stopping sing-box...")

    try:
        process.terminate()
    except ProcessLookupError:
        singbox = None
        return

    try:
        await asyncio.wait_for(
            process.wait(),
            timeout=CONFIG["STOP_TIMEOUT"]
        )

    except asyncio.TimeoutError:
        log("sing-box did not stop, sending SIGKILL")

        try:
            process.kill()
        except ProcessLookupError:
            pass

        try:
            await asyncio.wait_for(
                process.wait(),
                timeout=3
            )
        except asyncio.TimeoutError:
            raise RuntimeError(
                "sing-box could not be stopped safely"
            )

    log("Previous sing-box stopped")

    if singbox is process:
        singbox = None


# ==========================================
# SAFE RESTART
# ==========================================

async def restart_singbox():
    global restarting, restart_attempts, restart_task

    if shutting_down:
        raise RuntimeError("Application is shutting down")

    async with restart_lock:
        if restarting:
            raise RuntimeError("Restart already in progress")

        restarting = True

        try:
            if restart_task and not restart_task.done():
                restart_task.cancel()

                try:
                    await restart_task
                except asyncio.CancelledError:
                    pass

                restart_task = None

            await stop_singbox()

            if shutting_down:
                return

            await start_singbox()

            restart_attempts = 0

            log("sing-box restarted successfully")

        finally:
            restarting = False


# ==========================================
# VLESS LINK
# ==========================================

def make_link(user):
    params = {
        "encryption": "none",
        "security": "none",
        "type": "ws",
        "host": CONFIG["DOMAIN"],
        "path": WS_PATH
    }

    return (
        f"vless://{user['uuid']}@"
        f"{CONFIG['DOMAIN']}:{CONFIG['PORT']}?"
        f"{urlencode(params)}"
        f"#{quote(user['name'])}"
    )


# ==========================================
# DASHBOARD
# ==========================================

def dashboard(request):
    rows = []

    for user in users:
        user_id = escape_html(user["id"])

        rows.append(f"""
        <tr>
          <td>
            <strong>{escape_html(user["name"])}</strong>
            <div class="small">
              {escape_html(user.get("createdAt", ""))}
            </div>
          </td>

          <td class="config-cell">
            <textarea
              id="config-{user_id}"
              readonly
              rows="4"
              spellcheck="false"
            >{escape_html(make_link(user))}</textarea>

            <button onclick="copyLink('{user_id}')">
              Copy VLESS
            </button>
          </td>

          <td>
            <button class="delete"
              onclick="deleteUser('{user_id}')">
              Delete
            </button>
          </td>
        </tr>
        """)

    rows_html = "".join(rows) or """
        <tr>
          <td colspan="3">No users found</td>
        </tr>
    """

    running = "Running" if is_singbox_running() else "Stopped"

    return web.Response(text=f"""
    <!DOCTYPE html>
    <html lang="en">
    <head>
      <meta charset="UTF-8">
      <meta name="viewport" content="width=device-width,initial-scale=1">

      <title>Blue Knight Panel</title>

      <style>
        * {{ box-sizing: border-box; }}

        body {{
          margin: 0;
          padding: 20px;
          background: #10131b;
          color: white;
          font-family: Arial, sans-serif;
        }}

        .container {{
          max-width: 1000px;
          margin: auto;
        }}

        .card {{
          background: #202635;
          padding: 20px;
          margin: 15px 0;
          border-radius: 12px;
          overflow: hidden;
        }}

        input, button {{
          padding: 11px;
          border: 0;
          border-radius: 7px;
        }}

        input {{
          background: #111722;
          color: white;
          border: 1px solid #39445b;
        }}

        button {{
          background: #287dce;
          color: white;
          cursor: pointer;
          margin: 4px 2px;
        }}

        button.delete {{ background: #bd3548; }}

        textarea {{
          display: block;
          width: 100%;
          min-height: 100px;
          margin: 8px 0;
          padding: 10px;
          background: #111722;
          color: #65dca5;
          border: 1px solid #39445b;
          border-radius: 7px;
          resize: vertical;
          overflow-wrap: anywhere;
          word-break: break-all;
          font-size: 12px;
          line-height: 1.6;
        }}

        table {{
          width: 100%;
          border-collapse: collapse;
        }}

        th, td {{
          padding: 12px 8px;
          text-align: left;
          vertical-align: top;
          border-bottom: 1px solid #39445b;
        }}

        .top {{
          display: flex;
          justify-content: space-between;
          align-items: center;
        }}

        .muted, .small {{
          color: #aab3c4;
          font-size: 12px;
        }}

        #username {{ width: min(250px, 65%); }}

        @media(max-width: 650px) {{
          body {{ padding: 10px; }}
          th, td {{ padding: 8px 4px; font-size: 12px; }}
          textarea {{ min-width: 180px; font-size: 11px; }}
          .card {{ padding: 12px; }}
        }}
      </style>
    </head>

    <body>
      <div class="container">

        <div class="top">
          <h2>Blue Knight</h2>
          <span class="muted">VLESS Management</span>
        </div>

        <div class="card">
          <h3>Server Information</h3>

          <p class="muted">
            Domain: {escape_html(CONFIG["DOMAIN"])}
          </p>

          <p class="muted">
            Port: {CONFIG["PORT"]}
          </p>

          <p class="muted">
            Users: {len(users)}/{CONFIG["MAX_USERS"]}
          </p>

          <p class="muted">
            Protocol: VLESS + WebSocket
          </p>

          <p class="muted">
            sing-box: {running}
          </p>
        </div>

        <div class="card">
          <h3>Add User</h3>

          <form id="addForm">
            <input
              id="username"
              placeholder="Username"
              maxlength="40"
              required
            >

            <button type="submit">Add User</button>
          </form>
        </div>

        <div class="card">
          <h3>Users and VLESS Configurations</h3>

          <table>
            <thead>
              <tr>
                <th>Name</th>
                <th>VLESS Configuration</th>
                <th>Action</th>
              </tr>
            </thead>

            <tbody id="usersTable">
              {rows_html}
            </tbody>
          </table>
        </div>

      </div>

      <script>
        async function api(url, method, body) {{
          const response = await fetch(url, {{
            method,
            headers: {{
              'Content-Type': 'application/json'
            }},
            body: body ? JSON.stringify(body) : undefined
          }});

          const data = await response.json();

          if (!response.ok) {{
            throw new Error(data.error || 'Request failed');
          }}

          return data;
        }}

        async function copyLink(id) {{
          const textarea = document.getElementById('config-' + id);

          if (!textarea) {{
            alert('Configuration not found');
            return;
          }}

          try {{
            await navigator.clipboard.writeText(textarea.value);
            alert('VLESS copied successfully');
          }} catch (error) {{
            textarea.focus();
            textarea.select();

            const copied = document.execCommand('copy');

            if (!copied) {{
              alert('Copy failed. Select and copy manually.');
              return;
            }}

            alert('VLESS copied successfully');
          }}
        }}

        async function deleteUser(id) {{
          if (!confirm('Delete this user?')) return;

          try {{
            await api(
              '/api/users/' + encodeURIComponent(id),
              'DELETE'
            );

            location.reload();
          }} catch (error) {{
            alert(error.message);
          }}
        }}

        document.getElementById('addForm')
          .addEventListener('submit', async event => {{
            event.preventDefault();

            const input = document.getElementById('username');
            const name = input.value.trim();

            if (!name) return;

            try {{
              await api('/api/users', 'POST', {{ name }});
              location.reload();
            }} catch (error) {{
              alert(error.message);
            }}
          }});
      </script>
    </body>
    </html>
    """, content_type="text/html")


# ==========================================
# API STATUS
# ==========================================

async def api_status(request):
    return web.json_response({
        "success": True,
        "domain": CONFIG["DOMAIN"],
        "port": CONFIG["PORT"],
        "users": len(users),
        "maxUsers": CONFIG["MAX_USERS"],
        "singbox": is_singbox_running(),
        "restarting": restarting,
        "shuttingDown": shutting_down,
        "restartAttempts": restart_attempts
    })


# ==========================================
# API USERS
# ==========================================

async def api_get_users(request):
    return web.json_response({
        "success": True,
        "users": [
            {
                "id": user["id"],
                "name": user["name"],
                "createdAt": user["createdAt"],
                "link": make_link(user)
            }
            for user in users
        ]
    })


async def api_add_user(request):
    try:
        body = await request.json()

        name = str(body.get("name") or "").strip()

        if not name or len(name) > 40:
            return web.json_response({
                "error": "Name must contain 1 to 40 characters"
            }, status=400)

        async with mutation_lock:
            if len(users) >= CONFIG["MAX_USERS"]:
                raise RuntimeError("Maximum users reached")

            user = {
                "id": str(uuid.uuid4()),
                "name": name,
                "uuid": str(uuid.uuid4()),
                "createdAt": now_iso()
            }

            users.append(user)

            try:
                save_users()
                await restart_singbox()

            except Exception:
                users.remove(user)
                save_users()
                raise

        return web.json_response({
            "success": True,
            "user": {
                "id": user["id"],
                "name": user["name"]
            },
            "link": make_link(user)
        })

    except Exception as error:
        log_error("Add user error:", error)

        return web.json_response({
            "error": str(error) or "Could not create user"
        }, status=500)


async def api_delete_user(request):
    try:
        user_id = request.match_info["id"]

        async with mutation_lock:
            index = next(
                (
                    i for i, user in enumerate(users)
                    if user["id"] == user_id
                ),
                -1
            )

            if index == -1:
                raise LookupError("User not found")

            removed = users[index]

            users.pop(index)

            try:
                save_users()
                await restart_singbox()

            except Exception:
                users.insert(index, removed)
                save_users()
                raise

        return web.json_response({
            "success": True,
            "message": "User deleted"
        })

    except LookupError as error:
        return web.json_response({
            "error": str(error)
        }, status=404)

    except Exception as error:
        log_error("Delete user error:", error)

        return web.json_response({
            "error": str(error) or "Could not delete user"
        }, status=500)


async def api_user_link(request):
    user_id = request.match_info["id"]

    user = next(
        (item for item in users if item["id"] == user_id),
        None
    )

    if user is None:
        return web.json_response({
            "error": "User not found"
        }, status=404)

    return web.json_response({
        "success": True,
        "link": make_link(user)
    })


# ==========================================
# HEALTH
# ==========================================

async def health(request):
    return web.json_response({
        "status": "ok",
        "panel": "Blue Knight",
        "singbox": is_singbox_running(),
        "restarting": restarting,
        "uptime": int(asyncio.get_running_loop().time())
    })


# ==========================================
# WEBSOCKET PROXY
# ==========================================

async def websocket_proxy(request):
    if request.path != WS_PATH:
        raise web.HTTPNotFound()

    if not is_singbox_running():
        log("WebSocket rejected: sing-box is unavailable")
        raise web.HTTPServiceUnavailable(
            text="WebSocket proxy unavailable"
        )

    downstream = web.WebSocketResponse(autoping=True)
    await downstream.prepare(request)

    upstream = None

    try:
        upstream = await client_session.ws_connect(
            f"http://127.0.0.1:{CONFIG['INTERNAL_PORT']}"
            f"{request.rel_url}",
            autoping=True,
            heartbeat=30
        )

        async def client_to_upstream():
            async for message in downstream:
                if message.type == WSMsgType.TEXT:
                    await upstream.send_str(message.data)

                elif message.type == WSMsgType.BINARY:
                    await upstream.send_bytes(message.data)

                elif message.type == WSMsgType.PING:
                    await upstream.ping(message.data)

                elif message.type == WSMsgType.PONG:
                    await upstream.pong(message.data)

                elif message.type in (
                    WSMsgType.CLOSE,
                    WSMsgType.CLOSED,
                    WSMsgType.ERROR
                ):
                    break

        async def upstream_to_client():
            async for message in upstream:
                if message.type == WSMsgType.TEXT:
                    await downstream.send_str(message.data)

                elif message.type == WSMsgType.BINARY:
                    await downstream.send_bytes(message.data)

                elif message.type == WSMsgType.PING:
                    await downstream.ping(message.data)

                elif message.type == WSMsgType.PONG:
                    await downstream.pong(message.data)

                elif message.type in (
                    WSMsgType.CLOSE,
                    WSMsgType.CLOSED,
                    WSMsgType.ERROR
                ):
                    break

        tasks = [
            asyncio.create_task(client_to_upstream()),
            asyncio.create_task(upstream_to_client())
        ]

        done, pending = await asyncio.wait(
            tasks,
            return_when=asyncio.FIRST_COMPLETED
        )

        for task in pending:
            task.cancel()

        await asyncio.gather(*pending, return_exceptions=True)

        for task in done:
            if task.exception():
                raise task.exception()

    except asyncio.CancelledError:
        raise

    except Exception as error:
        log_error("WebSocket proxy error:", error)

    finally:
        if upstream is not None and not upstream.closed:
            await upstream.close()

        if not downstream.closed:
            await downstream.close()

    return downstream


# ==========================================
# APPLICATION
# ==========================================

def create_app():
    app = web.Application(client_max_size=32 * 1024)

    app.router.add_get("/", dashboard)

    app.router.add_get("/api/status", api_status)

    app.router.add_get("/api/users", api_get_users)
    app.router.add_post("/api/users", api_add_user)

    app.router.add_delete(
        "/api/users/{id}",
        api_delete_user
    )

    app.router.add_get(
        "/api/users/{id}/link",
        api_user_link
    )

    app.router.add_get("/health", health)

    app.router.add_get(WS_PATH, websocket_proxy)

    return app


# ==========================================
# SHUTDOWN
# ==========================================

async def shutdown():
    global shutting_down, client_session

    async with shutdown_lock:
        if shutting_down:
            return

        shutting_down = True
        shutdown_event.set()

        log("Shutting down...")

        if restart_task and not restart_task.done():
            restart_task.cancel()

            try:
                await restart_task
            except asyncio.CancelledError:
                pass

        if server_runner:
            await server_runner.cleanup()

        try:
            await stop_singbox()
        except Exception as error:
            log_error("sing-box shutdown error:", error)

        if client_session and not client_session.closed:
            await client_session.close()

        log("Shutdown completed")


# ==========================================
# MAIN
# ==========================================

async def main():
    global client_session, server_runner

    try:
        prepare_directories()
        load_users()

        client_session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=None)
        )

        log("Starting Blue Knight...")
        log("Domain:", CONFIG["DOMAIN"])
        log("Port:", CONFIG["PORT"])
        log("Python:", sys.version.split()[0])
        log("Platform:", sys.platform, platform.machine())

        await install_singbox()

        if shutting_down:
            return

        await start_singbox()

        app = create_app()

        server_runner = web.AppRunner(app)

        await server_runner.setup()

        site = web.TCPSite(
            server_runner,
            "0.0.0.0",
            CONFIG["PORT"]
        )

        await site.start()

        log("================================")
        log("Blue Knight Panel started")
        log("Domain:", CONFIG["DOMAIN"])
        log("Port:", CONFIG["PORT"])
        log("Users:", len(users))
        log(
            "Panel:",
            f"http://{CONFIG['DOMAIN']}:{CONFIG['PORT']}/"
        )
        log("WebSocket path:", WS_PATH)
        log("================================")

        await shutdown_event.wait()

    except Exception as error:
        log_error("Startup error:", error)

        await shutdown()

        raise

    finally:
        if not shutting_down:
            await shutdown()


# ==========================================
# SIGNAL HANDLING
# ==========================================

if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(
                sig,
                lambda: asyncio.create_task(shutdown())
            )
        except NotImplementedError:
            pass

    try:
        loop.run_until_complete(main())

    except KeyboardInterrupt:
        pass

    except Exception as error:
        log_error("Application stopped:", error)
        sys.exit(1)

    finally:
        pending = asyncio.all_tasks(loop)

        for task in pending:
            task.cancel()

        if pending:
            loop.run_until_complete(
                asyncio.gather(*pending, return_exceptions=True)
            )

        loop.close()
