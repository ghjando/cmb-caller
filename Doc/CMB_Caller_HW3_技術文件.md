# CMB Caller HW3 韌體技術文件

**版本**: HW3_20250901
**作者**: Roy Ching
**更新日期**: 2026-07-02

---


## 目錄

1. [系統架構](#1-系統架構)
2. [通訊協定](#2-通訊協定)
3. [狀態機](#3-狀態機)
4. [API 指令](#4-api-指令)
5. [錯誤處理](#5-錯誤處理)
6. [設定檔案](#6-設定檔案)
7. [開發指南](#7-開發指南)

---


## 1. 系統架構

### 1.1 硬體架構

```
┌─────────────────────────────────────┐
│           ESP32 微控制器             │
├─────────────────────────────────────┤
│  GPIO                   功能        │
│  ─────────────────────────────────  │
│  33 (LED_RED)          紅色 LED    │
│  32 (LED_GREEN)        綠色 LED    │
│  2  (LED_BLUE)         藍色 LED    │
│  0  (BUTTON_PIN)       模式切換按鍵 │
│  16 (RX2)              UART2 接收  │
│  17 (TX2)              UART2 發送  │
└─────────────────────────────────────┘
```

### 1.2 網路架構

```
┌──────────────┐      WiFi       ┌──────────────────┐
│  CMB Caller  │◄───────────────►│  WebSocket Server │
│   (ESP32)   │    WSS://       │ (Frontend Server) │
└──────────────┘                 └──────────────────┘
        │
        │ AP Mode (設定用)
        ▼
┌──────────────┐
│  網頁設定介面 │
│ http://IP    │
└──────────────┘
```

### 1.3 系統流程圖

```
開機
  │
  ▼
STATE_INIT
  │
  ▼
WiFi 連線 ──超時──► STATE_ERROR
  │
  │ 成功
  ▼
STATE_WIFI_CONNECTED
  │
  ▼
WebSocket 連線 ──超時──► STATE_ERROR
  │
  │ 成功
  ▼
STATE_WEBSOCKET_CONNECTED ───► 正常運行
  │
  ▼
STATE_AP_STA (設定模式) ───► STATE_AP_STA_C (已連線)
```

---


## 2. 通訊協定

### 2.1 訊息格式

**Device → Server**（純文字格式，用逗號分隔）

```
{Caller_Number},{action},{data}

// 範例：
v0001,auth,liM3yMfrMIAWHmFVvGQ1RA3BmdCTx2/hHdFbzv7ulcQ=
v0001,info,'SSID:CMB00000 ; RSSI:-46dBm ; BSSID:50:64:2B:32:1A:3A ; IP:192.168.31.101 ......
v0001,get
v0001,send,123
```

**Server → Device**

```
OK,{Caller_Number},{number},{action}

// 範例：
OK,v0001,456,update
OK,v0001,789,get
```

### 2.2 Action 類型

| Action       | 方向          | 說明                 | 範例                          |
|-------------|---------------|-------------------------|------------------------------|
| `auth`      | Device→Server | 認證，連線時自動發送     | `v0001,auth,{password}`     |
| `info`      | Device→Server | 設備資訊，連線時自動發送 | `v0001,info,'SSID:...'`     |
| `get`       | Device→Server | 查詢目前顯示號碼        | `v0001,get`                |
| `send,{num}`| Device→Server | 發送叫號（範圍 000-999）| `v0001,send,123`           |
| `ping`      | Device→Server | 心跳檢測                | `v0001,ping,123`           |
| `pong`      | Device←Server | 心跳檢測回覆            | `pong`                      |

### 2.3 訊息流程圖

```
┌─────────────┐                      ┌─────────────┐
│   Device    │                      │   Caller     │
│  (ESP32)    │                      │   Server    │
└──────┬──────┘                      └──────┬──────┘
       │                                    │
       │────── {id},auth ──────────────────►│
       │                                    │
       │◄───── OK,,auth ────────────────────│
       │                                    │
       │────── {id},info ──────────────────►│
       │                                    │
       │◄───── OK,{id},{num},info ──────────│
       │                                    │
       │◄───── wifi_get_status    ──────────│  Server 收到 info 後，主動發送.
       │                                    │
       │────── wifi_get_status ────────────►│
       │                                    │
       │────── {id},get ───────────────────►│
       │                                    │
       │◄───── OK,{id},{num},get ───────────│
       │                                    │
       │      (顯示 num)                    │
       │                                    │
       │────── {id},send,{num} ────────────►│
       │                                    │
       │◄───── OK,{id},{num},update ────────│
       │                                    │
       │                                    │
       │                                    │
       │────── {id},ping,{num} ────────────►│  心跳：每 30 秒一次 
       │                                    │
       │◄───── pong ────────────────────────│
       │                                    │
```

### 2.4 Caller 與 Frontend Server 連線互動流程 (簡述)

```
Caller (ESP32)                              Frontend Server
     │                                             │
     │2◄────────── {id},auth ────────────────────►1│  登入驗證
     │2◄────────── {id},info ────────────────────►1│  Caller 資訊 & WiFi 狀態
     │1◄────────── wifi_get_status ──────────────►2│  查詢 WiFi 資訊
     │2◄────────── {id},get ─────────────────────►1│  同步號碼
     │                                             │
     │──────────── {id},send,{num} ───────────────►│  發送叫號
     │◄─────────── OK,{id},{num},update ───────────│  確認接收
     │                                             │
     │◄───────── 接收廣播訊息 ──────────────────────│  廣播
```

**流程說明：**

1. **連線建立**：Caller 連線到 Frontend Server，發送 `auth` 進行驗證
2. **號碼同步**：詢問現在號碼，Frontend Server 回傳目前的叫號資料（`get`）
3. **叫號作業**：Caller 發送 `send,{num}`，Frontend 回傳 `OK` 確認
4. **訊息轉發**：Frontend Server 負責轉發訊息到其他客戶端（Caller、網頁、訪客）

---


## 3. 狀態機

### 3.1 狀態列舉

```c
enum SystemState {
  STATE_INIT,                  //  0: 初始狀態
  STATE_WIFI_CONNECTING,       //  1: WiFi 連線中
  STATE_WIFI_CONNECTED,        //  2: WiFi 已連線
  STATE_WEBSOCKET_CONNECTING,  //  3: WebSocket 連線中
  STATE_WEBSOCKET_CONNECTED,   //  4: WebSocket 已連線（正常運行）
  STATE_ERROR,                 //  5: 錯誤狀態
  STATE_DEMO,                  //  6: Demo 模式（獨立運行）
  STATE_TRANS,                 //  7: 資料傳輸中（短暫狀態）
  STATE_AP_STA,               //  8: AP+STA 模式（設定模式，未連線）
  STATE_AP_STA_C,             //  9: AP+STA 模式（設定模式，已連線）
  STATE_NUMBER_ERROR,          // 10: 數字掃描硬體錯誤
  STATE_RESTORE,               // 11: 回復舊值（轉場用）
  STATE_COUNT                  // 12: 狀態總數
};
```

### 3.2 LED 顯示對照

```
┌──────────────────────────┬────────┬────────┬────────┬─────────────────────┐
│ 狀態                     │  紅燈  │  綠燈  │  藍燈  │ 說明                │
├──────────────────────────┼────────┼────────┼────────┼─────────────────────┤
│ STATE_INIT               │   ON   │  OFF   │  OFF   │                     │
│ STATE_WIFI_CONNECTING    │ 閃爍   │  OFF   │  OFF   │                     │
│ STATE_WIFI_CONNECTED     │   ON   │  OFF   │  OFF   │                     │
│ STATE_WEBSOCKET_...      │ 慢閃   │ 慢閃   │  OFF   │                     │
│ STATE_WEBSOCKET_...      │  OFF   │   ON   │  OFF   │ ◄ 正常運行          │
│ STATE_ERROR              │  OFF   │  OFF   │  OFF   │                     │
│ STATE_DEMO               │  OFF   │ 慢閃   │  OFF   │ ◄ Demo 模式         │
│ STATE_TRANS              │ 快閃   │   ON   │  OFF   │ ◄ 傳輸中（短暫）    │
│ STATE_AP_STA             │ 快閃   │ 慢閃   │  OFF   │ ◄ 設定模式          │
│ STATE_AP_STA_C           │  OFF   │ 慢閃   │  OFF   │ ◄ 設定模式（已連線）│
│ STATE_NUMBER_ERROR       │   ON   │  OFF   │  OFF   │ ◄ 硬體錯誤          │
│ STATE_RESTORE            │ (沿用前一狀態)              │ ◄ 轉場用            │
└──────────────────────────┴────────┴────────┴────────┴─────────────────────┘
```

**閃爍說明：**
- 快閃：亮 100ms / 滅 100ms（紅燈）
- 慢閃：亮 500ms / 滅 500ms（綠燈）或 亮 1900ms / 滅 100ms（Demo）
- LED 皆為「低電位點亮」

### 3.3 狀態轉移範例

```c
// 狀態轉移函數
void updateSystemState(SystemState newState) {
    if (status.state != newState) {
        SystemState oldState = status.state;
        status.state = newState;
        status.lastStateChange = millis();

        Serial.printf("狀態變更: %d -> %d\n", oldState, newState);

        // 根據新狀態更新 LED
        updateLEDs(newState);
    }
}

// 使用範例
void loop() {
    if (WiFi.status() != WL_CONNECTED) {
        updateSystemState(STATE_WIFI_CONNECTING);  // 開始連線
    } else if (!webSocketClient.isConnected()) {
        updateSystemState(STATE_WEBSOCKET_CONNECTING);  // 開始連線
    } else {
        updateSystemState(STATE_WEBSOCKET_CONNECTED);  // 正常運行
    }
}
```

---


## 4. API 指令

### 4.1 WiFi 相關指令（JSON 格式）

**Client → Device**

| Action               | 說明         | 範例                                                    |
|---------------------|------------|--------------------------------------------------------|
| `wifi_get_status`   | 取得 WiFi 狀態 | `{"action":"wifi_get_status","caller_id":"v0001"}`  |
| `wifi_scan_list`    | 掃描可用網路  | `{"action":"wifi_scan_list","caller_id":"v0001"}`   |
| `wifi_get_profiles` | 取得已儲存的網路| `{"action":"wifi_get_profiles","caller_id":"v0001"}`|
| `wifi_add_profile`  | 新增 WiFi 熱點| `{"action":"wifi_add_profile","caller_id":"v0001","data":{"ssid":"MyWiFi","password":"12345678"}}` |
| `wifi_delete_profile`| 刪除 WiFi 熱點| `{"action":"wifi_delete_profile","caller_id":"v0001","data":{"ssid":"MyWiFi"}}` |

**Device → Client（wifi_get_status 回應範例）**

```json
{
  "action": "wifi_get_status",
  "caller_id": "v0001",
  "uuid": "xxx-xxx",
  "result": "OK",
  "data": {
    "wifi_connected": true,
    "current_ssid": "CMB00000",
    "password": "88888888",
    "ip_address": "192.168.1.100",
    "rssi": -45
  }
}
```

**Device → Client（wifi_scan_list 回應範例）**

```json
{
  "action": "wifi_scan_list",
  "caller_id": "v0001",
  "uuid": "xxx-xxx",
  "result": "OK",
  "data": {
    "networks": [
      { "ssid": "CMB00000", "bssid": "AA:BB:CC:DD:EE:FF", "rssi": -45, "channel": 6, "encryption": "WPA2" },
      { "ssid": "MyWiFi",   "bssid": "11:22:33:44:55:66", "rssi": -60, "channel": 1, "encryption": "WPA" }
    ]
  }
}
```

**Device → Client（wifi_add_profile / wifi_delete_profile 回應範例）**

```json
// 成功
{ "action": "wifi_add_profile", "caller_id": "v0001", "uuid": "xxx", "result": "OK" }

// 失敗
{ "action": "wifi_add_profile", "caller_id": "v0001", "uuid": "xxx", "result": "Fail, 005:invalid ssid" }
```

### 4.2 叫號相關

```c
// 發送叫號流程
void sendWebSocketMessage(int value) {
    String message = "";
    check_auth();  // 確保已認證
    message = Caller_Number + ",send," + String(value);

    bool success = webSocketClient_sendTXT(message);

    if (success) {
        waitingResponse = true;  // 等待 OK 回應
        sendTime = millis();
    } else {
        // 發送失敗，放回 buffer 等待重試
        retryValue = value;
        retryMode = true;
    }
}
```

---


## 5. 錯誤處理

### 5.1 斷線重連機制

```c
// 檢查回應超時
void checkResponse() {
    if (waitingResponse) {
        if (millis() - sendTime >= retryTimeout * 2000) {
            waitingResponse = false;
            retryMode = true;  // 啟動重試

            Serial.println("回應超時，啟動重試機制");

            // 連續超時，嘗試重新連線
            timeoutCount++;
            if (timeoutCount >= 1) {
                webSocketClient.disconnect();
                delay(500);
                setupWebSocket();  // 重新連線
                timeoutCount = 0;
            }
        }
    }
}
```

### 5.2 資料緩衝機制

```c
// 循環緩衝區結構
#define NUM_BUFFER_SIZE 60
int num_buffer[NUM_BUFFER_SIZE];
int num_head = 0;
int num_tail = 0;

// 緩衝區操作
void buffer_push(int value) {
    num_buffer[num_head] = value;
    num_head = (num_head + 1) % NUM_BUFFER_SIZE;
    if (num_head == num_tail) {
        // Buffer 已滿，覆寫舊資料
        num_tail = (num_tail + 1) % NUM_BUFFER_SIZE;
    }
}

bool buffer_pop(int& value) {
    if (num_head == num_tail) return false;  // Buffer 空
    value = num_buffer[num_tail];
    num_tail = (num_tail + 1) % NUM_BUFFER_SIZE;
    return true;
}
```

### 5.3 緩衝發送流程

```c
void sendBufferedData() {
    if (WiFi.status() == WL_CONNECTED &&
        webSocketClient.isConnected() &&
        !waitingResponse) {

        int value;
        if (retryMode) {
            // 重試模式
            sendWebSocketMessage(retryValue);
            retryMode = false;
        } else if (buffer_pop(value)) {
            // 一般模式
            sendWebSocketMessage(value);
        }

        // Buffer 空了，查詢雲端最新號碼同步
        if (buffer_use && buffer_size() == 0) {
            buffer_use = false;
            String message = Caller_Number + ",get";
            webSocketClient_sendTXT(message);  // 同步
        }
    }
}
```

### 5.4 錯誤代碼

| 代碼  | 說明                                 |
|-------|------------------------------------|
| `001` | JSON 格式錯誤（deserializeJson 失敗）    |
| `002` | payload 為空（收到空白訊息）             |
| `003` | 不支援的指令（action 未知）              |
| `004` | 認證失敗（僅供參考，實際由 Server 決定）    |
| `005` | 無效的 SSID（長度為 0 或超過 31 字元）   |
| `006` | 無效的密碼（長度超過 63 字元）            |
| `008` | 找不到指定的網路（credential not found）  |
| `009` | 系統內部錯誤                           |

---


## 6. 設定檔案

### 6.1 credentials.h 結構

```c
// ============================================
// 情境A：本地測試模式 (LOCAL_TEST)
// ============================================
#ifdef LOCAL_TEST

// WiFi 網路設定（預設網路清單）
WiFiNetwork defaultNetworks[] = {
    { "CMBzxxxx", "88888888", true, 0, false },
    { "CMB00000", "88888888", true, 0, false }
};

// WebSocket 伺服器設定（陣列形式，支援多台備援）
WebSocketServerConfig servers[] = {
    // 測試伺服器（WSS）
    { "cmb-caller-frontend-306511771181.asia-east1.run.app", 443, true }
};

#define SERVER_COUNT (sizeof(servers) / sizeof(servers[0]))

#else
// ============================================
// 情境B：正式上線模式
// ============================================

WiFiNetwork defaultNetworks[] = {
    { "CMBzxxxx", "88888888", true, 0, false },
    { "CMB00000", "88888888", true, 0, false },
    { "CMBz8888", "88888888", true, 0, false },
    { "CMBz6666", "88888888", true, 0, false }
};

WebSocketServerConfig servers[] = {
    // 正式伺服器（WSS，port 固定為 443）
    { "cmb-caller-frontend-410240967190.asia-east1.run.app", 443, true }
};

#define SERVER_COUNT (sizeof(servers) / sizeof(servers[0]))

#endif
```

**補充說明：**
- `port` 固定為 `443`（WSS 標準埠），若使用非 SSL 則為 `80`
- 系統會依序嘗試 `servers[]` 陣列中的伺服器，直到成功連線為止
- `defaultNetworks[]` 為出廠預設網路，用戶可透過 Web 或遠端指令新增

### 6.2 認證密碼

```c
// 認證密碼（Base64 編碼）
const char auth_password[] PROGMEM =
    "liM3yMfrMIAWHmFVvGQ1RA3BmdCTx2/hHdFbzv7ulcQ=";
```

---


## 7. 開發指南

### 7.1 新增 WiFi 熱點

```c
// 方法1：直接在 credentials.h 新增
WiFiNetwork defaultNetworks[] = {
    { "現有網路1", "密碼1", true, 0, false },
    { "新網路2",   "密碼2", true, 0, false },  // 新增這行
};

// 方法2：透過程式碼動態新增（透過 Web API）
void addWiFiNetwork(const char* ssid, const char* password) {
    // 透過 Web API: POST /add_http 或 /add_manual
    // 或透過 WebSocket 指令: wifi_add_profile
}
```

### 7.2 切換伺服器

```c
// 在 credentials.h 修改 servers[] 陣列
WebSocketServerConfig servers[] = {
    // 改這裡的網址
    { "新伺服器網址.asia-east1.run.app", 443, true }
};
```

### 7.3 調整參數

```c
// 常見調整參數位置

// WiFi 連線超時（預設 7 秒）
const long WIFI_TIMEOUT = 7000;

// 狀態更新間隔（預設 500 毫秒）
const long STATE_UPDATE_INTERVAL = 500;

// Ping 間隔（預設 30 秒）
const long PING_INTERVAL = 30000;

// Demo 模式更新間隔（30-90 秒隨機）
const unsigned long MIN_INTERVAL = 30000;
const unsigned long MAX_INTERVAL = 90000;

// 叫號範圍（000-999）
const int MIN_VALUE = 1;
const int MAX_VALUE = 999;
```

### 7.4 OTA 遠端更新

程式已內建 ArduinoOTA 功能，可透過網路更新：

```c
void setup() {
    // ... 其他初始化 ...

    // OTA 初始化
    ArduinoOTA.setHostname(Caller_Number.c_str());
    ArduinoOTA.begin();
}

void loop() {
    // ... 其他任務 ...

    // OTA 處理
    ArduinoOTA.handle();
}
```

### 7.5 Debug 技巧

```c
// 序列埠監控（baud rate: 115200）

// 觀察訊息傳輸
Serial.println("接收: " + message);

// 觀察狀態變更
Serial.printf("狀態變更: %d -> %d\n", oldState, newState);

// 觀察 Buffer 狀態
Serial.printf("Buffer 大小: %d\n", buffer_size());

// 觀察 WiFi 強度
Serial.printf("RSSI: %d dBm\n", WiFi.RSSI());
```

---


## 附錄

### A. 版本歷史

| 日期       | 變更內容                                                                 |
|-----------|-----------------------------------------------------------------------|
| 2025-04-24| 只支援 HW 3.0，支援藍牙傳輸 (ESP32 * 3)                                     |
| 2025-06-12| 增加遠端及 STA 設定 WiFi 連線功能；自動連線依順序掃描結果連線；修正 Websocket 斷線 WiFi 也重連之問題；修正雲端傳入後又上傳雲端的問題；加入使用按鍵開啟 STA 功能 |
| 2025-06-13| 先設 WIFI_AP_STA 再換 WIFI_STA，避免 reboot 問題                           |
| 2025-06-16| WiFi 設定改 WIFI_AP_STA 固定不變換，直接 enable/disable AP；增加狀態 STATE_RESTORE |
| 2025-06-17| 增加 login 時 get 同步號碼；cldUR2Send 改 0~999；開機 WiFi 設 WIFI_AP_STA   |
| 2025-06-26| 支援近端 WiFi 設定；參數 device_id 改 caller_id                             |
| 2025-06-27| wifi_get_status 增加回覆 password 參數；支援遠端 WiFi 設定                  |
| 2025-07-07| 加快斷線重連速度；顯示斷線時間                                             |
| 2025-09-01| login 時比對最後兩組號碼以避免重複叫號                                      |

**目前版本**: HW3_20250901

### B. 接腳對照表

| GPIO  | 功能          | 說明                      |
|-------|-------------|--------------------------|
| 33    | LED_RED     | 紅色 LED（低電位點亮）        |
| 32    | LED_GREEN   | 綠色 LED（低電位點亮）        |
| 2     | LED_BLUE    | 藍色 LED（低電位點亮）        |
| 0     | BUTTON_PIN  | 模式切換按鍵（Demo Mode）    |
| 16    | RX2         | UART2 接收（與 LED 子板通訊） |
| 17    | TX2         | UART2 發送（與 LED 子板通訊） |

### C. 通訊埠設定

| 設定        | 數值                              |
|------------|----------------------------------|
| Baud Rate  | 115200（序列監控）/ 9600（UART2）  |
| Data Bits  | 8                                |
| Parity     | None                             |
| Stop Bits  | 1                                |

---

**文件結束**
