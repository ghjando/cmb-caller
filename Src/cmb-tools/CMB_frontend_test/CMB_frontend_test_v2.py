#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
CMB Frontend WebSocket 測試客戶端 v2.1
=====================================

【功能說明】
- 連線到 CMB 叫號機 WebSocket 伺服器
- 支援三種環境:local(本地)、trial(測試)、live(正式)
- 自動執行叫號、查詢、ping 等命令序列
- 類比多客戶端同時連線情境

【使用方法】
    python CMB_frontend_test_v2.py [local|trial|live]

【範例】
    python CMB_frontend_test_v2.py trial   # 連接測試環境
    python CMB_frontend_test_v2.py live     # 連接正式環境
    python CMB_frontend_test_v2.py          # 預設使用 trial

【更新日誌】
    v2.1 (2026/04/14)
    - 修復:重連後自動重新登入
    - 修復:同時只有一個監聽協程(舊的會先取消)
    - 修復:解析錯誤訊息時不再當掉
    - 新增:連線後自動執行認證

    v2.0 (2026/04/14)
    - 重構程式架構,新增說明註解
    - 加入主動關閉機制(Ctrl+C 可優雅退出)
    - 統一重連邏輯至 ReconnectionManager
    - 新增連線逾時保護
    - 移除全域變數,改用類別封裝
    - 密碼抽出至頂部方便修改

    v1.0 (2025/03/20)
    - 初始版本
"""

import asyncio
import websockets
import random
import signal
import sys
from datetime import datetime
import json
import threading
import time

# ============================================================================
# 【配置區】修改這裡即可變更設定
# ============================================================================

# CMB 叫號機密碼(支援多種格式切換)
PASSWORDS = {
    "soft_cmb": "YV7X+xUEsMckopbXpp5sey+eosV8HYIGxa/fOS69/SU=",   # SOFT CMB Caller
    # "cmb_caller": "liM3yMfrMIAWHmFVvGQ1RA3BmdCTx2/hHdFbzv7ulcQ=",   # CMB Caller
    # "user_get_num": "user_get_num",   # user_get_num
    # "fail": "Fail_Password",   # Fail
}

# 使用的密碼類型
ACTIVE_PASSWORD = PASSWORDS["soft_cmb"]

# WebSocket 伺服器 URL(支援三種環境)
SERVER_URLS = {
    "local": "ws://localhost:38000",
    "trial": "wss://cmb-caller-frontend-306511771181.asia-east1.run.app/",
    "live": "wss://cmb-caller-frontend-410240967190.asia-east1.run.app/",
    "cust": "wss://cmb-caller-frontend-cust-488726723787.asia-east1.run.app/",
}

# 預設伺服器
DEFAULT_SERVER = "trial"

# 測試參數
NUM_CLIENTS = 1              # 同時連線的客戶端數量
MESSAGE_INTERVAL = 30       # 發送訊息的間隔(秒)
CLIENT_ID = "z0002"          # 客戶端 ID
# CLIENT_ID = "a0109"          # 客戶端 ID



# ============================================================================
# Jupyter 環境相容性處理
# ============================================================================

try:
    from IPython import get_ipython
    if get_ipython() is not None:
        import nest_asyncio
        nest_asyncio.apply()
        print("✓ nest_asyncio 已啟用 (Jupyter 環境)", flush=True)
    else:
        print("- nest_asyncio 未啟用 (非 Jupyter 環境)", flush=True)
except ImportError:
    print("- IPython 未安裝", flush=True)

# ============================================================================
# 通用工具函式
# ============================================================================

def pretty_json(s: str) -> str:
    """將 JSON 字串格式化輸出(單行版,方便檢視)"""
    try:
        return json.dumps(json.loads(s), ensure_ascii=False, separators=(',', ':'))
    except Exception:
        return s


def get_timestamp() -> str:
    """取得帶毫秒的時間戳記"""
    # return datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-4]
    return datetime.now().strftime('%H:%M:%S.%f')[:-3]

# ============================================================================
# 重連管理器
# ============================================================================

class ReconnectionManager:
    def __init__(self, max_delay: int = 300, protection_threshold: int = 3):
        self.max_delay = max_delay
        self.protection_threshold = protection_threshold
        self.reconnect_history: list[float] = []
        self.attempt_count = 0

    def should_wait(self) -> bool:
        now = time.time()
        self.reconnect_history = [t for t in self.reconnect_history if now - t < 60]
        return len(self.reconnect_history) >= self.protection_threshold

    def get_delay(self) -> float:
        delay = min(2 ** self.attempt_count, self.max_delay)
        return max(1, delay)

    async def wait_before_reconnect(self, seconds: int = 30):
        print(f"⚠ 檢測到頻繁重連,等待 {seconds} 秒")
        for i in range(seconds, 0, -1):
            await asyncio.sleep(1)
            print(f"\r  等待倒數:{i:2d} 秒  ", end="", flush=True)
        print()

    def record_success(self):
        self.attempt_count = 0
        self.reconnect_history.append(time.time())

    def record_failure(self):
        self.attempt_count += 1

# ============================================================================
# WebSocket 客戶端類別
# ============================================================================

class WebSocketClient:
    def __init__(self, ws_url: str, client_id: str):
        self.ws_url = ws_url
        self.client_id = client_id
        self.ws = None
        self.is_connected = False
        self.should_stop = False
        self.is_authenticated = False  # 新增:認證狀態標記

        self.reconnect_manager = ReconnectionManager()
        self.listen_task = None
        self._print_lock = asyncio.Lock()
        self.current_number = 0

    async def connect(self):
        """連線到 WebSocket 伺服器"""
        while not self.should_stop:
            try:
                # 取消舊的監聽任務
                if self.listen_task and not self.listen_task.done():
                    self.listen_task.cancel()
                    try:
                        await self.listen_task
                    except asyncio.CancelledError:
                        pass

                self.ws = await asyncio.wait_for(
                    websockets.connect(self.ws_url),
                    timeout=30
                )

                async with self._print_lock:
                    print(f"[{get_timestamp()}] ✓ WebSocket 已連線")

                self.is_connected = True
                self.is_authenticated = False
                self.reconnect_manager.record_success()

                # 先認證,等完成後才開始監聽
                print(f"[{get_timestamp()}] 自動登入中...")
                await self.authenticate_json()

                # 啟動監聽任務(只啟動一個,不要同時等)
                self.listen_task = asyncio.create_task(self._listen())

                # 等待監聽任務完成(而不是另外開一個等待)
                await self.listen_task

            except asyncio.TimeoutError:
                async with self._print_lock:
                    print(f"[{get_timestamp()}] ✗ 連線逾時(30秒)")
                self.reconnect_manager.record_failure()

            except asyncio.CancelledError:
                print(f"[{get_timestamp()}] 連線任務被取消")
                break

            except websockets.exceptions.ConnectionClosed:
                async with self._print_lock:
                    print(f"[{get_timestamp()}] ✗ 連線被關閉")

            except Exception as e:
                async with self._print_lock:
                    print(f"[{get_timestamp()}] ✗ 連線錯誤: {e}")
                self.reconnect_manager.record_failure()

            if self.should_stop:
                break

            self.is_connected = False
            self.is_authenticated = False

            if self.reconnect_manager.should_wait():
                await self.reconnect_manager.wait_before_reconnect()

            delay = self.reconnect_manager.get_delay()
            async with self._print_lock:
                print(f"[{get_timestamp()}] 等待 {delay:.1f} 秒後重連...")

            await asyncio.sleep(delay)

        async with self._print_lock:
            print(f"[{get_timestamp()}] 連線任務結束")


    # async def connect(self):
    #     """連線到 WebSocket 伺服器"""
    #     while not self.should_stop:
    #         try:
    #             # 取消舊的監聽任務
    #             if self.listen_task and not self.listen_task.done():
    #                 self.listen_task.cancel()
    #                 try:
    #                     await self.listen_task
    #                 except asyncio.CancelledError:
    #                     pass

    #             self.ws = await asyncio.wait_for(
    #                 websockets.connect(self.ws_url),
    #                 timeout=30
    #             )

    #             async with self._print_lock:
    #                 print(f"[{get_timestamp()}] ✓ WebSocket 已連線")

    #             self.is_connected = True
    #             self.is_authenticated = False  # 重置認證狀態
    #             self.reconnect_manager.record_success()

    #             # 連線後自動認證
    #             print(f"[{get_timestamp()}] 自動登入中...")
    #             await self.authenticate_json()

    #             # 啟動監聽任務
    #             self.listen_task = asyncio.create_task(self._listen())

    #             # 等待連線斷開
    #             await self._wait_until_disconnected()

    #         except asyncio.TimeoutError:
    #             async with self._print_lock:
    #                 print(f"[{get_timestamp()}] ✗ 連線逾時(30秒)")
    #             self.reconnect_manager.record_failure()

    #         except asyncio.CancelledError:
    #             print(f"[{get_timestamp()}] 連線任務被取消")
    #             break

    #         except websockets.exceptions.ConnectionClosed:
    #             async with self._print_lock:
    #                 print(f"[{get_timestamp()}] ✗ 連線被關閉")

    #         except Exception as e:
    #             async with self._print_lock:
    #                 print(f"[{get_timestamp()}] ✗ 連線錯誤: {e}")
    #             self.reconnect_manager.record_failure()

    #         if self.should_stop:
    #             break

    #         self.is_connected = False
    #         self.is_authenticated = False

    #         if self.reconnect_manager.should_wait():
    #             await self.reconnect_manager.wait_before_reconnect()

    #         delay = self.reconnect_manager.get_delay()
    #         async with self._print_lock:
    #             print(f"[{get_timestamp()}] 等待 {delay:.1f} 秒後重連...")

    #         await asyncio.sleep(delay)

    #     async with self._print_lock:
    #         print(f"[{get_timestamp()}] 連線任務結束")

    async def _wait_until_disconnected(self):
        """等待連線斷開"""
        try:
            async for message in self.ws:
                await self._handle_message(message)
        except websockets.exceptions.ConnectionClosed:
            pass
        except asyncio.CancelledError:
            if self.ws:
                await self.ws.close()
            raise

    async def disconnect(self):
        """斷開連線"""
        self.should_stop = True
        if self.listen_task and not self.listen_task.done():
            self.listen_task.cancel()
        if self.ws:
            await self.ws.close()
        self.is_connected = False
        self.is_authenticated = False
        print(f"[{get_timestamp()}] 已斷開連線")

    async def _listen(self):
        """監聽並處理伺服器回應"""
        try:
            async for message in self.ws:
                await self._handle_message(message)
        except websockets.exceptions.ConnectionClosed:
            print(f"[{get_timestamp()}] 連線已斷開")
        except asyncio.CancelledError:
            print(f"[{get_timestamp()}] 監聽任務被取消")
            if self.ws:
                await self.ws.close()
        except Exception as e:
            print(f"[{get_timestamp()}] 監聽錯誤: {e}")

    async def _handle_message(self, message: str):
        """處理接收到的訊息"""
        async with self._print_lock:
            print(f"[{get_timestamp()}] 接收 <<< : {pretty_json(message)}")

        # 嘗試解析為 JSON
        try:
            data = json.loads(message)

            # 檢查登入回應
            if data.get("action") == "login" and data.get("result") == "OK":
                self.is_authenticated = True
                print(f"[{get_timestamp()}] ✓ 登入成功")
                return

            # 檢查是否為錯誤訊息
            if data.get("result", "").startswith("Fail") and "not logged in" in str(data.get("result", "")):
                self.is_authenticated = False
                print(f"[{get_timestamp()}] ⚠️ 認證失敗或未登入")
                return

        except json.JSONDecodeError:
            pass

        # 解析 comma 格式訊息（安全處理）
        parts = message.split(',')
        if len(parts) >= 2:
            action = parts[-1]
            try:
                if action == 'get':
                    self.current_number = int(parts[-2])        # !!!@@@
                    print(f"G current_number={self.current_number}", flush=True)
                    pass
                elif action == 'update':
                    self.current_number = int(parts[-2])
                    print(f"U current_number={self.current_number}", flush=True)
            except (ValueError, IndexError):
                pass

    # async def _handle_message(self, message: str):
    #     """處理接收到的訊息"""
    #     async with self._print_lock:
    #         print(f"[{get_timestamp()}] 接收 <<< : {pretty_json(message)}")

    #     # 檢查是否為錯誤訊息
    #     if message.startswith("Fail") or "not logged in" in message:
    #         self.is_authenticated = False
    #         print(f"[{get_timestamp()}] ⚠ 認證失敗或未登入,需要重新連線")
    #         return

    #     # 嘗試解析為 JSON
    #     try:
    #         data = json.loads(message)
    #         if data.get("result", "").startswith("Fail") and "not logged in" in str(data.get("result", "")):
    #             self.is_authenticated = False
    #             print(f"[{get_timestamp()}] ⚠ 認證失敗或未登入")
    #     except json.JSONDecodeError:
    #         pass

    #     # 解析 comma 格式訊息(安全處理)
    #     parts = message.split(',')
    #     if len(parts) >= 2:
    #         action = parts[-1]
    #         try:
    #             if action == 'get':
    #                 self.current_number = int(parts[-2])
    #             elif action == 'update':
    #                 self.current_number = int(parts[-2])
    #         except (ValueError, IndexError):
    #             # 忽略無法解析的訊息
    #             pass

    async def send_message(self, message: str):
        """發送訊息"""
        # 檢查認證狀態
        if not self.is_authenticated:
            print(f"[{get_timestamp()}] ⚠ 未認證,先執行登入...")
            await self.authenticate_json()

        # 等待連線就緒
        while not self.is_connected:
            print(f"[{get_timestamp()}] 等待連線中...")
            await asyncio.sleep(1)

        try:
            await self.ws.send(message)
            async with self._print_lock:

                print("")
                # 如果有 call_num,特別標示
                if "call_num" in message:
                    data = json.loads(message)
                    print(f"[{get_timestamp()}] --- call_num: {data['call_num']} ---")
                print(f"[{get_timestamp()}] 發送 >>> : {message}")

        except websockets.exceptions.ConnectionClosed:
            async with self._print_lock:
                print(f"[{get_timestamp()}] ✗ 發送時連線已關閉")
            self.is_connected = False
            self.is_authenticated = False

        except Exception as e:
            async with self._print_lock:
                print(f"[{get_timestamp()}] ✗ 發送失敗: {e}")
            self.is_connected = False
            self.is_authenticated = False

    async def send_json(self, data: dict):
        """發送 JSON 格式訊息"""
        # 檢查認證狀態
        if not self.is_authenticated:
            print(f"[{get_timestamp()}] ⚠ 未認證,先執行登入...")
            await self.authenticate_json()

        message = json.dumps(data)
        await self.send_message(message)

    async def authenticate(self):
        """發送舊格式認證"""
        message = f'{self.client_id},AUTH,{ACTIVE_PASSWORD}'
        await self.send_message(message)
        await asyncio.sleep(1)

    async def authenticate_json(self):
        """發送新格式認證（不等待回應，由 _listen 處理）"""
        data = {
            "action": "login",
            "vendor_id": "tawe",
            "caller_id": self.client_id,
            "password": ACTIVE_PASSWORD,
            "uuid": hex(id(self))
        }
        message = json.dumps(data)

        while not self.is_connected:
            print(f"[{get_timestamp()}] 等待連線中...")
            await asyncio.sleep(1)

        try:
            await self.ws.send(message)
            async with self._print_lock:
                print(f"\n[{get_timestamp()}] 發送 >>> : {message}")
            # 不在這裡 recv，讓 _listen() 接收並設定 is_authenticated
        except Exception as e:
            async with self._print_lock:
                print(f"[{get_timestamp()}] ✗ 認證發送失敗: {e}")
            self.is_authenticated = False

    # async def authenticate_json(self):
    #     """發送新格式認證（JSON）並等待結果"""
    #     data = {
    #         "action": "login",
    #         "vendor_id": "tawe",
    #         "caller_id": self.client_id,
    #         "password": ACTIVE_PASSWORD,
    #         "uuid": hex(id(self))
    #     }
    #     message = json.dumps(data)
        
    #     # 直接發送，不重複檢查認證狀態
    #     while not self.is_connected:
    #         print(f"[{get_timestamp()}] 等待連線中...")
    #         await asyncio.sleep(1)
        
    #     try:
    #         await self.ws.send(message)
    #         async with self._print_lock:
    #             print(f"\n[{get_timestamp()}] 發送 >>> : {message}")
            
    #         # 自己接收回應（而不是讓 _listen 搶走）
    #         try:
    #             response = await asyncio.wait_for(self.ws.recv(), timeout=5)
    #             async with self._print_lock:
    #                 print(f"[{get_timestamp()}] 接收 <<< : {pretty_json(response)}")
    #             # 檢查是否成功
    #             try:
    #                 data = json.loads(response)
    #                 if data.get("result") == "OK":
    #                     self.is_authenticated = True
    #                     print(f"[{get_timestamp()}] ✓ 登入成功")
    #                 else:
    #                     self.is_authenticated = False
    #                     print(f"[{get_timestamp()}] ✗ 登入失敗: {data.get('result')}")
    #             except json.JSONDecodeError:
    #                 self.is_authenticated = False
    #                 print(f"[{get_timestamp()}] ✗ 回應無法解析")
    #         except asyncio.TimeoutError:
    #             print(f"[{get_timestamp()}] ✗ 登入回應逾時")
    #             self.is_authenticated = False
    #         except Exception as e:
    #             print(f"[{get_timestamp()}] ✗ 接收回應錯誤: {e}")
    #             self.is_authenticated = False
                
    #     except Exception as e:
    #         async with self._print_lock:
    #             print(f"[{get_timestamp()}] ✗ 認證發送失敗: {e}")
    #         self.is_authenticated = False

    def generate_next_value(self) -> int:
        """產生下一個號碼(遞增並循環 1-999)"""
        self.current_number += 1
        if self.current_number > 999:
            self.current_number = 1
        return self.current_number

# ============================================================================
# 測試輔助函式
# ============================================================================

def wait_with_timeout(timeout: int = 60):
    """倒數計時等待(可按 Enter 提前繼續)"""
    event = threading.Event()

    def wait_for_enter():
        input()
        event.set()

    thread = threading.Thread(target=wait_for_enter, daemon=True)
    thread.start()

    print(f"程式暫停中,按 Enter 鍵繼續,或等待 {timeout} 秒自動繼續...")

    for remaining in range(timeout, 0, -1):
        if event.is_set():
            break
        print(f"\r  剩餘時間:{remaining:2d} 秒  ", end="", flush=True)
        time.sleep(1)

    print("\n繼續執行程式")

# ============================================================================
# 測試案例
# ============================================================================

async def test_auth_and_commands(client: WebSocketClient):
    """測試案例:認證 + 基本命令"""
    print("\n" + "="*60)
    print("開始執行測試案例:認證 + 基本命令")
    print("="*60 + "\n")

    await asyncio.sleep(5)

    if not client.is_connected:
        print("✗ 未連線,取消測試")
        return

    print("【1/7】發送 JSON 格式登入")
    await client.authenticate_json()
    wait_with_timeout(3)

    print("\n【2/7】查詢取號狀態")
    await client.send_json({
        "action": "get_num_status",
        "vendor_id": "tawe",
        "caller_id": client.client_id,
        "uuid": hex(id(client))
    })
    await asyncio.sleep(1)

    print("\n【3/7】發送 ping")
    await client.send_message(f'{client.client_id},ping,123')
    await asyncio.sleep(1)

    print("\n【4/7】查詢號碼")
    await client.send_message(f'{client.client_id},get')
    await asyncio.sleep(2)
    wait_with_timeout(3)

    print("\n【5/7】叫號:1")
    await client.send_message(f'{client.client_id},SEND,1')
    await asyncio.sleep(3)

    print("\n【6/7】叫號:0")
    await client.send_message(f'{client.client_id},SEND,0')
    await asyncio.sleep(1)

    print("\n【7/7】再次查詢號碼")
    await client.send_message(f'{client.client_id},get')
    await asyncio.sleep(1)

    await client.send_message(f'{client.client_id},SEND,88888')  # 測試異常號碼
    await asyncio.sleep(3)

    await client.send_message(f'{client.client_id},get')
    await asyncio.sleep(1)

    wait_with_timeout(3)

    print("\n【額外】最後 ping")
    await client.send_message(f'{client.client_id},ping')
    await asyncio.sleep(1)

    print("\n" + "="*60)
    print("測試案例執行完畢")
    print("="*60 + "\n")

async def test_loop_simulation(client: WebSocketClient, message_interval: int):
    """測試案例:迴圈模擬客戶端行為"""
    print("\n" + "="*60)
    print("開始執行:迴圈模擬測試(按 Ctrl+C 停止)")
    print("="*60 + "\n")

    await asyncio.sleep(5)
    next_value = 0

    while True:
        if not client.is_connected:
            print("✗ 連線已斷開,等待重連...")
            await asyncio.sleep(5)
            continue

        print("\n" + "-"*40)
        print(f"迴圈測試 #{next_value + 1}")
        print("-"*40)

        # 1. 發送 booking_data
        await client.send_json({
            "action": "booking_data",
            "vendor_id": "tawe",
            "caller_id": client.client_id,
            "switch": "off",
            "uuid": hex(id(client))
        })
        await asyncio.sleep(1)

        # 2. ping
        await client.send_message(f'{client.client_id},ping,{next_value}')
        await asyncio.sleep(1)

        # 3. get
        await client.send_message(f'{client.client_id},get')
        await asyncio.sleep(1)

        # 4. call_number
        next_value = client.generate_next_value()
        await client.send_json({
            # "action": "call_number",
            "vendor_id": "tawe",
            "caller_id": client.client_id,
            "call_num": str(next_value),
            "change": True,
            "last_update": 0,
            "counter_name": "櫃台",
            "counter_num": "一號",
            "uuid": "JSON_CALL_1"
        })
        await asyncio.sleep(1)

        # 5. ping
        await client.send_message(f'{client.client_id},ping,{next_value}')
        await asyncio.sleep(1)

        # 6. get_num_info
        await client.send_message(f'{client.client_id},get_num_info')
        await asyncio.sleep(1)

        # 7. get_num_info (JSON)
        await client.send_json({
            "action": "get_num_info",
            "vendor_id": "tawe",
            "caller_id": client.client_id,
            "user_id": "ASD7QEZ3XCT5FG",
            "uuid": hex(id(client))
        })
        await asyncio.sleep(1)

        # 8. get_num_status
        await client.send_json({
            "action": "get_num_status",
            "vendor_id": "tawe",
            "caller_id": client.client_id,
            "uuid": hex(id(client))
        })
        await asyncio.sleep(1)

        # 9. get
        await client.send_message(f'{client.client_id},get')
        await asyncio.sleep(1)

        # 10. call_number
        next_value = client.generate_next_value()
        await client.send_json({
            "action": "call_number",
            "vendor_id": "tawe",
            "caller_id": client.client_id,
            "call_num": str(next_value),
            "change": True,
            "last_update": 0,
            "counter_name": "櫃台",
            "counter_num": "一號",
            "uuid": "JSON_CALL_2"
        })
        await asyncio.sleep(1)

        interval = message_interval * (1 + random.uniform(-0.1, 0.1))
        print(f"\n等待 {interval:.1f} 秒後執行下一輪...")
        await asyncio.sleep(interval)

# ============================================================================
# 主程式
# ============================================================================

async def create_clients(ws_url: str, num_clients: int) -> list:
    """建立多個 WebSocket 客戶端"""
    print(f"準備連線至: {ws_url}")

    clients = []
    for i in range(num_clients):
        client = WebSocketClient(ws_url, CLIENT_ID)
        clients.append(client)
        asyncio.create_task(client.connect())
        await asyncio.sleep(0.01)

    return clients

async def main():
    global DEFAULT_SERVER, SERVER_URLS

    if len(sys.argv) > 1:
        param = sys.argv[1].lower()
        if param in SERVER_URLS:
            DEFAULT_SERVER = param
            print(f"從命令列取得參數: '{param}'")
        else:
            print(f"無效參數 '{param}',使用預設值: '{DEFAULT_SERVER}'")
    else:
        print(f"未提供參數,使用預設值: '{DEFAULT_SERVER}'")

    ws_url = SERVER_URLS[DEFAULT_SERVER]
    print(f"目標伺服器: {ws_url}\n")

    clients = await create_clients(ws_url, NUM_CLIENTS)

    print("等待連線建立...")
    await asyncio.sleep(2)

    for client in clients:
        await test_auth_and_commands(client)

    await test_loop_simulation(clients[0], MESSAGE_INTERVAL)

def signal_handler(signum, frame):
    """處理 Ctrl+C"""
    print("\n\n收到 Ctrl+C,正在停止所有客戶端...")
    for client in getattr(main, 'clients', []):
        asyncio.create_task(client.disconnect())
    sys.exit(0)

if __name__ == "__main__":
    signal.signal(signal.SIGINT, signal_handler)

    print("="*60)
    print("CMB Frontend WebSocket 測試客戶端 v2.1")
    print("="*60)
    print(f"支援環境: {list(SERVER_URLS.keys())}")
    print(f"使用方式: python {sys.argv[0]} [local|trial|live|cust]")
    print("按 Ctrl+C 可優雅停止")
    print("="*60 + "\n")

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n程式已停止")
