"""Phase A e2e 冒烟：从 .env 读 peer token，逐项验证 v1.0 兼容层。"""
import json
import urllib.request

BASE = "http://127.0.0.1:8310"
peers = json.loads([l for l in open(".env")
                    if l.startswith("LAS_A2A_PEERS=")][0].split("=", 1)[1])
TOK = {m["worker"]: t for t, m in peers.items()}


def call(method, params, token=None, legacy=False):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization" if not legacy else "X-Agent-Token"] = (
            f"Bearer {token}" if not legacy else token)
    req = urllib.request.Request(
        BASE + "/a2a", headers=headers,
        data=json.dumps({"jsonrpc": "2.0", "id": "e1",
                         "method": method, "params": params}).encode())
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def get(path, token=None):
    req = urllib.request.Request(BASE + path)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, {}


def send_v1(text, token, **metadata):
    return call("SendMessage", {"message": {
        "role": "user",
        "parts": [{"text": text, "mediaType": "text/plain"}],
        "metadata": metadata}}, token)


print("1. 无 token →", get("/.well-known/agent-card.json")[0], "(期望 401)")
s, card = get("/.well-known/agent-card.json", TOK["codex"])
print("2. card →", s, card["supportedInterfaces"])
s, r = send_v1("列出当前 agents 表里的在线 worker 清单", TOK["codex"])
t = r["result"]["task"]
print(f"3. SendMessage codex → state={t['status']['state']} id={t['id']} "
      f"assigned={t['metadata']['assigned_to']}")
s, r = send_v1("查询任务", TOK["codex"], agent="kimi")
print("4. 伪造 metadata.agent=kimi →", r["error"]["code"], "(期望 -32602)")
s, r = send_v1("检索 agentHub 项目的设计文档要点", TOK["kimi"])
t2 = r["result"]["task"]
print(f"5. SendMessage kimi → state={t2['status']['state']} "
      f"assigned={t2['metadata']['assigned_to']}")
# 写任务（避开常驻授权 pattern「创建文件」，用「写入」触发 ask）→ input-required
s, r = send_v1("在 ~/AgentWorkspace 写入文件 a2a-phaseA-smoke.md，内容一行冒烟记录",
               TOK["codex"])
t3 = r["result"]["task"]
print(f"6. 写任务 → state={t3['status']['state']} (期望 input-required)")
s, r = send_v1("批准", TOK["codex"], taskId=t3["id"])
print("7. compat 自然语言审批 →", r["error"]["code"], "(期望 -32602 拒绝)")
s, r = call("tasks/approve", {"id": t3["id"]}, TOK["codex"])
print(f"8. tasks/approve → state={r['result']['status']['state']} "
      f"(期望 submitted/working)")
s, r = call("tasks/approve", {"id": t3["id"]}, TOK["codex"])
print("9. 重复 approve →", r["error"]["code"], r["error"]["message"][:40])
s, r = call("tasks/get", {"id": t3["id"]}, TOK["kimi"])
print(f"10. kimi peer tasks/get 跨 peer 查询 → state="
      f"{r['result']['status']['state']}")
