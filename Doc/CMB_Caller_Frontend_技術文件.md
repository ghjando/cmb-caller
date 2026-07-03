# CMB Caller Frontend 伺服器技術文件

**作者**: Roy Ching
**更新日期**: 2026-07-02

---

## 目錄

1. [系統架構](#1-系統架構)
2. [技術堆疊](#2-技術堆疊)
3. [WebSocket 通訊](#3-websocket-通訊)
4. [API 指令](#4-api-指令)
5. [訊息流程](#5-訊息流程)
6. [錯誤處理](#6-錯誤處理)
7. [LINE 通知](#7-line-通知)
8. [部署設定](#8-部署設定)

---

## 1. 系統架構

### 1.1 系統架構圖

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                        CMB Caller Frontend Server                          │
│                      (Python FastAPI + WebSocket)                          │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  ┌──────────────────────────────────────────────────────────────────┐       │
│  │  單一實例協調機制 (GCP Pub/Sub)                                    │       │
│  │  • 新實例啟動後廣播 STOP_SERVER 訊息                               │       │
│  │  • 舊實例收到後主動停止接受新連線 (accepting_connections=False)     │       │
│  │  • 舊實例關閉既有連線，將服務轉移給新實例                          │       │
│  └──────────────────────────────────────────────────────────────────┘       │
│                                                                             │
│         ▲ 誰能處理連線                                                    │
│         │                                                                 │
│  ┌──────┴───────┐                                                      │
│  │              │                                                        │
│  │    處理中    │  ◄── 只有「未收到 STOP_SERVER」的實例                   │
│  │   (Active)   │                                                        │
│  └──────┬───────┘                                                      │
│         │                                                                   │
│         ▼                                                                   │
│  ┌─────────────────────────────────────────────────────────────┐              │
│  │                  業務邏輯處理                                      │              │
│  │  • 登入驗證 (auth)                                           │              │
│  │  • 資料同步 (sync)                                            │              │
│  │  • 轉發訊息到 Main Server                                     │              │
│  └─────────────────────────────────────────────────────────────┘              │
│                                                                             │
└────────────┬──────────────────┬──────────────────┬───────────────────────┘
             │                  │                  │
             │ WebSocket         │ WebSocket        │ WebSocket
             ▼                  ▼                  ▼
┌─────────────────┐    ┌─────────────────┐    ┌─────────────────┐
│    Caller       │    │   網頁前端      │    │  CMB Main       │
│   (ESP32)       │    │   (Browser)     │    │   Server       │
│                 │    │                 │    │                │
│ • 顯示號碼      │    │ • 操作叫號      │    │ • 業務邏輯     │
│ • 接收叫號      │    │ • 查看狀態      │    │ • 資料管理     │
│ • 實體機器      │    │ • 店家/管理員   │    │ • 中央主機     │
└─────────────────┘    └─────────────────┘    └─────────────────┘



                                                 │
                                                 │ 訊息轉發
                                                 ▼
                                          ┌─────────────────┐
                                          │   LINE Bot      │
                                          │   (通知服務)    │
                                          └─────────────────┘
```
					  

### 1.2 各元件角色

| 元件 | 角色 |
|------|------|
| **Caller（ESP32）   | 叫號硬體，顯示號碼、接收叫號、處理業務邏輯 |
| **Frontend Server   | WebSocket 代理 + 簡單業務邏輯（登入驗證、資料同步、資料廣播）|
| **CMB Main Server   | 叫號系統後端中央伺服器（負責真正的業務邏輯）|
| **GCP Pub/Sub       | 跨實例協調（新實例廣播 STOP_SERVER，舊實例主動讓出服務）|
| **LINE Bot          | 錯誤/事件通知 |
| **網頁前端（Browser）| 店家/管理員操作介面，WebSocket 客戶端 |

### 1.3 連線架構（clients 資料結構）

```
clients = {
    "v0001": {
        "caller_num": 123,           # 目前的叫號
        "caller_name": "店名",        # 店家名稱
        "connections": {
            ws1: { "ws_type": "caller" },   # Caller 硬體
            ws2: { "ws_type": "web" },       # 網頁操作者
            ws3: { "ws_type": "visitor" }   # 顧客/訪客
        },
        "login_time": datetime,
        "disconnect_time": None
    }
}
```

### 1.4 單一實例鎖機制
```
當多個 Frontend Server 實例啟動時：
1. 新實例啟動後廣播 STOP_SERVER 訊息
2. 舊實例收到後主動停止接受新連線 (accepting_connections=False)
3. 舊實例關閉既有連線，將服務轉移給新實例
```

---

## 2. 軟體

### 2.1 模組

| 模組         | 版本     | 用途          |
|--------------|---------|---------------|
| Python       | 3.x     | 程式語言      |
| FastAPI      | -       | Web API 框架  |
| WebSocket    | -       | 即時通訊      |
| uvicorn      | -       | ASGI 伺服器   |
| Google Cloud | Pub/Sub | 跨實例通訊    |
| LINE Bot API | -       | LINE 通知     |
| psutil       | -       | 系統監控      |


---

## 3. WebSocket 通訊

### 3.1 端點

```
主端點: ws://host:port/
替代端點: ws://host:port/ws
```

### 3.2 FastAPI 端點

```
# WebSocket 端點
@fastapi_app.websocket("/")
async def websocket_endpoint(websocket: WebSocket):
    await frontend_server.handle_websocket_connection(websocket)

# 健康檢查
GET /health
Response: {
    "status": "healthy",
    "uptime_seconds": xxx,
    "active_connections": 10
}

# 重新啟動（內部）
GET /restart

# 顯示所有連線資料
GET /show_all_back_data

# 詳細狀態
GET /status
```

---

## 4. API 指令

### 4.1 指令分類

```
# Caller 連線類型
ws_type 是整數位元遮罩（bitmask），定義如下：
數值	說明
1 (0x01)	H/W Caller（實體叫號機）
2 (0x02)	SOFT CMB Caller（軟體 / 網頁店家端）
4 (0x04)	user_get_num（訪客 / 顧客取號）
8 (0x08)	WiFi 設定程式（Setup WiFi）
且可合併使用，例如 0x2 + 0x4 表示同時發送給「軟體 Caller」與「訪客」。

# 指令需要處理的動作
CALLER_CSV_COMMANDS_TO_PROCESS = {"send", "auth", "get_num_info", "info", "get"}

# 需要等待回覆的動作（需與 CMB Main Server 確認）
client_wait_reply_actions_check = {
    "user_get_num",        # 顧客取號
    "get_num_status",       # 取號狀態
    "get_num_info",         # 取號資訊
    "web_cancel_get_num",   # 網頁取消取號
    "remove_number",         # 移除號碼
    "booking_data",         # 預約資料
    "web_reset_caller",     # 重置叫號
    "group_login",          # 群組登入
}

# Main Server 主動通知的動作
servsr_replay_active_actions_check = {
    "get_num_switch",    # 切換取號模式
    "new_get_num",       # 新取號通知
    "reset_caller",      # 重置叫號
    "cancel_get_num",    # 取消取號
    "reserve_number",    # 保留號碼
    "login",             # 登入
    "set_params",        # 設定參數
    "set_time_period",   # 設定時段
    "call_number",       # 叫號
}

# 需回覆 OK 的動作
servsr_active_actions_replay_ok_check = {
    "new_get_num",
    "reset_caller",
    "cancel_get_num",
    "reserve_number",
}
```

### 4.2 訊息格式

**CSV 格式（舊格式，Caller 使用）**
```
# 格式
"{caller_id},{action},{data}"

# 範例
v0001,auth,liM3yMfrMIAWHmFVvGQ1RA3BmdCTx2/hHdFbzv7ulcQ=
v0001,info,'SSID:CMB00000 ; RSSI:-45dBm ; Ver:HW3_20250901'
v0001,get
v0001,send,123
```

**JSON 格式（新格式）**
```
// login
{
    "action": "login",
    "caller_id": "v0001",
    "password": "xxx",
    "hardware": "HW3_20250901"
}

// user_get_num（顧客取號）
{
    "action": "user_get_num",
    "caller_id": "v0001",
    "user_id": "U123456",
    "wait_time_avg": 5
}

// get_num_info
{
    "action": "get_num_info",
    "caller_id": "v0001"
}

// send（叫號）
{
    "action": "send",
    "caller_id": "v0001",
    "call_num": 123
}

// web_cancel_get_num（取消取號）
{
    "action": "web_cancel_get_num",
    "caller_id": "v0001",
    "get_num_item_id": "xxx"
}

// remove_number（移除號碼）
{
    "action": "remove_number",
    "caller_id": "v0001",
    "call_num": 123
}

// reset_caller（重置叫號）
{
    "action": "reset_caller",
    "caller_id": "v0001"
}

// set_params（參數設定）
{
    "action": "set_params",
    "caller_id": "v0001",
    "params": {
        "xxx": "xxx"
    }
}
```

### 4.3 回覆格式

```
// 成功回覆
{
    "action": "xxx",
    "result": "OK",
    "caller_id": "v0001",
    "data": {...}
}

// 失敗回覆
{
    "action": "xxx",
    "result": "Fail",
    "error_code": "006",
    "error_message": "illegal caller_id",
    "caller_id": "v0001"
}
```

### 4.4 錯誤碼

錯誤碼請參照 "caller server 通訊協定 (2.8.2版).txt" (或更新版本)

以下為少部分為相容舊程式所使用.

| 錯誤碼 | 說明 |
|--------|------|
| 001 | JSON 格式錯誤
| 002 | payload 為空 
| 003 | WiFi 連線失敗
| 004 | 認證失敗
| 005 | 連線錯誤 / GCP 連線問題
| 006 | 非法的 caller_id 
| 007 | 缺少必要參數（KeyError）
| 008 | 請求超時（asyncio.TimeoutError）
| 009 | 文字錯誤 / 其它
| 999 | 系統內部錯誤

---

## 5. 訊息流程

### 5.1 Caller 登入流程

```
Caller            Caller Frontend         CMB Main Server
  │                     │                      │
  │───── auth ─────────►│                      │
  │                     │───── auth ──────────►│  是 web caller 才送至 Main Server
  │                     │◄──── OK ─────────────│
  │◄──── OK ────────────│                      │
  │                     │                      │
  │───── info ─────────►│                      │  實體 Caller 才有傳
  │◄──── OK ────────────│                      │
  │                     │                      │  
  │◄─ wifi_get_status ──│                      │  Frontend 收到 info 主動發出詢問
  │── wifi_get_status ─►│                      │  Frontend 會廣播至 web caller
  │                     │                      │
  │───── get ──────────►│                      │
  │                     │───── get_num_info ──►│
  │                     │◄──── get_num_info ───│
  │◄──── update ────────│                      │
  │                     │                      │
```

### 5.2 叫號流程

```
Caller              Frontend               CMB Main
  │                     │                      │
  │───── send,123 ─────►│                      │
  │                     │───── call_number ───►│
  │                     │◄──── call_number─────│
  │◄─ OK,id,num,update ─│                      │  (廣播)
  │                     │                      │
```

### 5.3 顧客取號流程

```
顧客手機         Frontend               CMB Main
   │                     │                       │
   │─── user_get_num ───►│                       │
   │                     │─── user_get_num ─────►│
   │                     │◄── user_get_num ──────│
   │◄── user_get_num ────│                       │  (廣播)
   │                     │                       │
```

---

## 6. 錯誤處理

### 6.1 重試機制

```
# 最大重試次數
max_retries = 6

# 重試延遲
RETRY_DELAY = 1  # 秒

# 等待回覆超時
server_timeout = 2 # 秒

* 實際值以原始碼為準

# 處理流程
async def handle_json_cmd_with_reply(caller_id, json_data, websocket):
    for attempt in range(MAX_RETRIES):
        try:
            # 發送至 CMB Main Server
            await send_to_main_server(json_data)

            # 等待回覆
            reply = await wait_for_reply(timeout=server_timeout)

            if reply:
                await websocket.send_json(reply)
                return

        except TimeoutError:
            if attempt < MAX_RETRIES - 1:
                await asyncio.sleep(RETRY_DELAY)
                continue
            raise
```

### 6.2 斷線處理

```
# 記錄斷線時間
async def remove_connection(self, caller_id, websocket):
    if caller_id in self.clients:
        del self.clients[caller_id]["connections"][websocket]

        # 如果沒有任何連線了，記錄斷線時間
        if not self.clients[caller_id]["connections"]:
            self.clients[caller_id]["disconnect_time"] = datetime.now()

# 重連時重新認證
# 2026/06/05: 斷線重連需要重新認證(auth)，並在 auth 後更新目前號碼資料
```

### 6.3 連線監控

```
class ConnectionMonitor:
    async def check_health(self):
        # 檢查所有連線狀態
        # 記錄不健康的連線
        pass

    async def record_connect(self):
        # 記錄連線時間
        pass

    async def record_disconnect(self, reason: str):
        # 記錄斷線時間和原因
        pass
```

---

## 7. LINE 通知

### 7.1 LINE Bot 設定

```
class LineNotifier:
    def __init__(self):
        self.line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)

    async def send_message(self, message: str):
        """發送 LINE 通知"""
        for user in recipients:
            try:
                message_text = template.format(status=status)
                self.line_bot_api.push_message(
                    user["id"], TextSendMessage(text=message_text)
                )
                print(f"✅ 已發送給 {user['name']}")
                result = True
```

### 7.2 通知時機

```
# 連線失敗通知
if connection_failed:
    line_notifier.send_message(
        f"⚠️ Caller 連線失敗\n"
        f"Caller ID: {caller_id}\n"
        f"時間: {datetime.now()}"
    )

# 其他重要事件...
```

---

## 8. 部署設定

### 8.1 本地測試

```
# 安裝依賴
pip install fastapi uvicorn websockets==13.1
pip install linebot
pip install google-cloud-pubsub
pip install psutil

```

### 8.2 GCP 部署

```
# 建構映像
於 WSL
./gcloud_deploy.sh live

./gcloud_deploy.sh [trial|live]
```

---

## 附錄

### A. 版本歷史

| 日期       | 變更內容 |
|------------|----------|
| 2026-06-05 | 修復斷線重連同步問題 |

### B. 關鍵檔案

| 檔案 | 用途 |
|------|------|
| `cmb-caller-frontend.py` | 主程式 |

### C. 連接埠對照

| 服務 | 預設埠 |
|------|--------|
| HTTP | 38000 |
| WebSocket | 38000 |
| Health Check | /health |

---

**文件結束**
