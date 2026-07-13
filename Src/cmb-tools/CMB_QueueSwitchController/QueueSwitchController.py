#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
QueueSwitchController - 叫號/取號系統控制器
"""

import asyncio
import websockets
import json
import threading
import time
import datetime
import os
import sys
import logging
import platform
from dataclasses import dataclass, field
from enum import Enum
from concurrent.futures import ThreadPoolExecutor


print("啟動 QueueSwitchController...")
print("EX: python QueueSwitchController.py --env trial")
os.chdir(os.path.dirname(os.path.abspath(__file__)))    # 設定工作目錄為腳本所在目錄


# # 日誌設定
# log_dir = (
#     # os.path.join(os.getenv("APPDATA", ""), "QueueSwitchController")
#     # if platform.system() == "Windows"
#     # else "."
#     "."
# )

# log_file = os.path.join(log_dir, "QueueSwitchController.log")

# if not os.path.exists(log_dir):
#     os.makedirs(log_dir)

# logging.basicConfig(
#     level=logging.INFO,
#     format="%(asctime)s [%(levelname)s] %(message)s",
#     handlers=[
#         logging.FileHandler(log_file, encoding="utf-8"),
#         logging.StreamHandler(sys.stdout),
#     ],
# )
# logger = logging.getLogger(__name__)

# 日誌設定
# === 新增：自訂 Formatter ===
class PaddingFormatter(logging.Formatter):
    def __init__(self, fmt=None, datefmt=None, style="%", pad_len=30):
        super().__init__(fmt=fmt, datefmt=datefmt, style=style)
        self.pad_len = pad_len

    def format(self, record):
        # 先讓原本的格式化跑完
        msg = super().format(record)
        # 在訊息尾端補 N 個空白
        return msg + (" " * self.pad_len)

# 日誌設定
log_dir = "."
log_file = os.path.join(log_dir, "QueueSwitchController.log")

if not os.path.exists(log_dir):
    os.makedirs(log_dir)

# 這裡不使用 basicConfig 的 handlers 參數，而是自行建立 handler，便於掛自訂 formatter
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# 檔案 handler（不加空白：一般建議檔案保持乾淨）
file_handler = logging.FileHandler(log_file, encoding="utf-8")
file_handler.setLevel(logging.INFO)
file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))

# 主控台 handler（加 30 個空白）
console_handler = logging.StreamHandler(sys.stdout)
console_handler.setLevel(logging.INFO)
console_handler.setFormatter(PaddingFormatter("%(asctime)s [%(levelname)s] %(message)s", pad_len=50))

# 先清掉既有 handlers（避免重覆添加）
logger.handlers.clear()
logger.addHandler(file_handler)
logger.addHandler(console_handler)


class ActionType(Enum):
    ON = "ON"
    OFF = "OFF"


@dataclass
class ScheduleItem:
    """排程項目"""

    time_str: str  # HH:MM
    hour: int
    minute: int
    action: ActionType
    remark: str = ""

    @property
    def time_tuple(self):
        return (self.hour, self.minute)


@dataclass
class VendorConfig:
    """Caller 設定"""

    caller_id: str
    name: str
    closed_dates: set = field(default_factory=set)
    schedules: list = field(default_factory=list)

    def is_closed(self, date_str):
        return date_str in self.closed_dates


class WebSocketSession:
    """WebSocket 會話（每次命令獨立連接）"""

    def __init__(self, caller_id, password, ws_url):
        self.caller_id = caller_id
        self.password = password
        self.ws_url = ws_url

    async def execute(self, action):
        """執行開關動作"""
        try:
            # SSL 設定
            ssl_context = None
            if self.ws_url.startswith("wss://"):
                import ssl

                ssl_context = ssl.create_default_context()

                # Windows 上關閉 SSL 驗證
                if platform.system() == "Windows":
                    ssl_context.check_hostname = False
                    ssl_context.verify_mode = ssl.CERT_NONE

            logger.info(f"[{self.caller_id}] 連接 WebSocket...")

            connect_timeout = 10
            read_timeout = 5

            async with websockets.connect(
                self.ws_url,
                ssl=ssl_context,
                ping_interval=None,
                close_timeout=1,
                open_timeout=connect_timeout,
            ) as ws:
                # 登入
                login_cmd = {
                    "action": "login",
                    "vendor_id": "tawe",
                    "caller_id": self.caller_id,
                    "password": self.password,
                    "uuid": f"login_{int(time.time())}",
                }

                logger.info(f"[{self.caller_id}] 發送登入命令")
                await ws.send(json.dumps(login_cmd))

                # 等待登入回應（使用 asyncio.wait_for 設定接收超時）
                try:
                    response = await asyncio.wait_for(ws.recv(), timeout=read_timeout)
                    logger.debug(f"[{self.caller_id}] 登入回應: {response[:100]}...")
                except asyncio.TimeoutError:
                    logger.error(f"[{self.caller_id}] 登入回應超時")
                    return False

                # 檢查登入是否成功
                if not self._check_success(response):
                    logger.error(f"[{self.caller_id}] 登入失敗")
                    return False

                # 執行開關
                switch_cmd = {
                    "action": "get_num_switch",
                    "vendor_id": "tawe",
                    "caller_id": self.caller_id,
                    "switch": "on" if action == ActionType.ON else "off",
                    "uuid": f"switch_{int(time.time())}",
                }

                action_str = "開啟" if action == ActionType.ON else "關閉"
                logger.info(f"[{self.caller_id}] 發送{action_str}命令")
                await ws.send(json.dumps(switch_cmd))

                # 等待回應
                try:
                    response = await asyncio.wait_for(ws.recv(), timeout=read_timeout)
                    logger.debug(f"[{self.caller_id}] 開關回應: {response[:100]}...")
                except asyncio.TimeoutError:
                    logger.error(f"[{self.caller_id}] 開關回應超時")
                    return False

                # 檢查開關是否成功
                success = self._check_success(response)
                if success:
                    # logger.info(f"[{self.caller_id}] {action_str}成功")
                    pass
                else:
                    logger.error(
                        f"[{self.caller_id}] {action_str}失敗，回應: {response[:100]}..."
                    )

                return success

        except websockets.exceptions.WebSocketException as e:
            logger.error(f"[{self.caller_id}] WebSocket 錯誤: {e}")
            return False
        except Exception as e:
            logger.error(f"[{self.caller_id}] 執行失敗: {e}")
            return False

    def _check_success(self, response):
        """檢查回應是否成功"""
        try:
            data = json.loads(response)
            return data.get("result", "").lower() == "ok"
        except:
            # 非 JSON 回應，使用字串判斷
            response_lower = response.lower()
            return "ok" in response_lower or "success" in response_lower

    def execute_sync(self, action):
        """同步執行（供執行緒池使用）"""
        try:
            # Windows 事件循環設定
            if platform.system() == "Windows":
                asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

            # 建立新的事件循環
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

            # 設置慢回調門檻 !!!@@@
            loop.slow_callback_duration = 0.5

            result = loop.run_until_complete(self.execute(action))
            loop.close()
            return result
        except Exception as e:
            logger.error(f"[{self.caller_id}] 同步執行錯誤: {e}")
            return False


class ConfigManager:
    """設定檔管理"""

    def __init__(self, config_path=None):
        self.config_path = config_path or "queue_switch_config.txt"
        self.callers = {}
        self.last_hash = ""
        self.lock = threading.RLock()
        self.load()

    def load(self):
        """載入設定檔"""
        if not os.path.exists(self.config_path):
            logger.error(f"設定檔不存在: {self.config_path}")
            return

        # 檢查檔案是否變更
        import hashlib

        with open(self.config_path, "rb") as f:
            current_hash = hashlib.md5(f.read()).hexdigest()
            if current_hash == self.last_hash:
                return

        logger.info("載入設定檔...")
        callers = {}
        current = None

        with open(self.config_path, "r", encoding="utf-8") as f:
            for line_num, line in enumerate(f, 1):
                line = line.strip()
                if not line or line.startswith("#"):
                    continue

                # Caller 區塊
                if line.startswith("[") and line.endswith("]"):
                    caller_id = line[1:-1].strip()
                    current = VendorConfig(caller_id=caller_id, name="")
                    callers[caller_id] = current
                    continue

                if not current:
                    continue

                # 名稱
                if line.startswith("NAME:"):
                    current.name = line[5:].strip()

                # 休業日期
                elif line.startswith("CLOSED:"):
                    dates = [d.strip() for d in line[7:].split(",") if d.strip()]
                    for date in dates:
                        try:
                            datetime.datetime.strptime(date, "%Y-%m-%d")
                            current.closed_dates.add(date)
                        except ValueError:
                            logger.warning(f"第{line_num}行: 日期格式錯誤: {date}")

                # 排程
                elif "," in line:
                    parts = [p.strip() for p in line.split(",")]
                    if len(parts) >= 2:
                        time_str, action_str = parts[0], parts[1].upper()
                        remark = parts[2] if len(parts) > 2 else ""

                        # 驗證時間格式
                        if ":" not in time_str:
                            logger.warning(f"第{line_num}行: 時間格式錯誤: {time_str}")
                            continue

                        try:
                            hour, minute = map(int, time_str.split(":"))
                            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                                logger.warning(
                                    f"第{line_num}行: 時間超出範圍: {time_str}"
                                )
                                continue
                        except ValueError:
                            logger.warning(f"第{line_num}行: 時間解析錯誤: {time_str}")
                            continue

                        if action_str not in ["ON", "OFF"]:
                            logger.warning(f"第{line_num}行: 動作錯誤: {action_str}")
                            continue

                        action = ActionType.ON if action_str == "ON" else ActionType.OFF

                        current.schedules.append(
                            ScheduleItem(
                                time_str=time_str,
                                hour=hour,
                                minute=minute,
                                action=action,
                                remark=remark,
                            )
                        )

        with self.lock:
            self.callers = callers
            self.last_hash = current_hash

        logger.info(f"載入完成，共 {len(callers)} 個 Caller")

    def get_today_schedules(self):
        """取得今日所有排程"""
        with self.lock:
            today = datetime.datetime.now().strftime("%Y-%m-%d")
            result = {}

            for caller_id, config in self.callers.items():
                if config.is_closed(today):
                    logger.debug(f"[{caller_id}] 今日休業，跳過")
                    continue

                schedules = [(s, s.action) for s in config.schedules]
                if schedules:
                    result[caller_id] = schedules

            return result

    def get_next_schedule(self):
        """取得下一個排程任務的時間和資訊"""
        now = datetime.datetime.now()
        today = now.strftime("%Y-%m-%d")
        current_time = (now.hour, now.minute)

        next_schedule = None
        min_diff = float("inf")

        with self.lock:
            for caller_id, config in self.callers.items():
                if config.is_closed(today):
                    continue

                for schedule in config.schedules:
                    schedule_time = schedule.time_tuple

                    # 計算時間差（分鐘）
                    schedule_minutes = schedule_time[0] * 60 + schedule_time[1]
                    current_minutes = current_time[0] * 60 + current_time[1]
                    diff = schedule_minutes - current_minutes

                    # 只考慮未來的排程
                    if diff > 0 and diff < min_diff:
                        min_diff = diff
                        next_schedule = {
                            "time": schedule.time_str,
                            "caller_id": caller_id,
                            "vendor_name": config.name,
                            "action": schedule.action.value,
                            "remark": schedule.remark,
                        }

        return next_schedule, min_diff


class TaskScheduler:
    """任務排程器"""

    def __init__(self, config_manager, ws_url):
        self.config = config_manager
        self.ws_url = ws_url
        self.thread_pool = ThreadPoolExecutor(
            max_workers=10, thread_name_prefix="QueueTask"
        )
        self.password = "YV7X+xUEsMckopbXpp5sey+eosV8HYIGxa/fOS69/SU="

    def execute_action(self, caller_id, schedule):
        """執行單一動作"""
        try:
            session = WebSocketSession(caller_id, self.password, self.ws_url)
            success = session.execute_sync(schedule.action)

            action_str = "開啟" if schedule.action == ActionType.ON else "關閉"
            remark = f" ({schedule.remark})" if schedule.remark else ""

            if success:
                logger.info(
                    f"[{caller_id}] {schedule.time_str} {action_str}成功{remark}                    \n"
                )
            else:
                logger.error(
                    f"[{caller_id}] {schedule.time_str} {action_str}失敗{remark}                    \n"
                )

        except Exception as e:
            logger.error(f"[{caller_id}] 任務執行異常: {e}")

    def check_schedules(self):
        """檢查並執行當前排程"""
        now = datetime.datetime.now()
        current = (now.hour, now.minute)

        logger.debug(f"檢查排程，當前時間: {now.hour:02d}:{now.minute:02d}")

        for caller_id, schedules in self.config.get_today_schedules().items():
            for schedule, action in schedules:
                if schedule.time_tuple == current:
                    print("\n")  # 換行顯示日誌
                    logger.info(
                        f"[{caller_id}] 執行排程: {schedule.time_str} {action.value}"
                    )
                    self.thread_pool.submit(self.execute_action, caller_id, schedule)


class QueueSwitchController:
    """主控制器"""

    def __init__(self, config_path=None, ws_url=None):
        if ws_url is None:
            env = os.environ.get("CMB_ENV", "trial")
            urls = {
                "local": "ws://localhost:38000",
                "live": "wss://cmb-caller-frontend-410240967190.asia-east1.run.app/",
                "trial": "wss://cmb-caller-frontend-306511771181.asia-east1.run.app/",
            }
            ws_url = urls.get(env, urls["trial"])

        self.config = ConfigManager(config_path)
        self.scheduler = TaskScheduler(self.config, ws_url)
        self.running = False
        self.last_display_time = 0
        self.display_interval = 1  # 每秒更新一次顯示

        # logger.info(f"連線至 '{env}': '{ws_url}'")
        logger.info(f"連線至 '{env}'")
        logger.info(f"控制器初始化完成")
        logger.info(f"WebSocket: {ws_url}")
        logger.info(f"設定檔: {self.config.config_path}")
        logger.info(f"作業系統: {platform.system()} {platform.release()}")

    def _display_status(self):
        """顯示狀態資訊（不換行）"""
        now = datetime.datetime.now()
        now_str = now.strftime("%H:%M:%S")

        # 取得下一個排程
        next_schedule, minutes_left = self.config.get_next_schedule()

        if next_schedule:
            # 計算目標時間的 datetime 物件
            target_time = datetime.datetime(
                now.year,
                now.month,
                now.day,
                int(next_schedule["time"].split(":")[0]),  # 小時
                int(next_schedule["time"].split(":")[1]),  # 分鐘
            )

            # 計算時間差
            time_delta = target_time - now

            if time_delta.total_seconds() > 0:
                # 取得時、分、秒
                total_seconds = int(time_delta.total_seconds() + 1)
                hours = total_seconds // 3600
                minutes = (total_seconds % 3600) // 60
                seconds = total_seconds % 60  # 加1秒避免顯示不準

                time_left_str = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
            else:
                # 如果已經過了排程時間
                time_left_str = "00:00:00"

            display_text = (
                f"\r現在時間: {now_str} | "
                f"下次執行: {next_schedule['time']} ({next_schedule['vendor_name']}, {next_schedule['caller_id']}) {next_schedule['action']} | "
                f"倒數: {time_left_str}   \r"
            )
        else:
            display_text = f"\r現在時間: {now_str} | 今日無更多排程   \r"

        sys.stdout.write(display_text)
        sys.stdout.flush()
        # print("\r", end="")  # 將游標移到本行最前面，不換行

    async def run(self):
        """主迴圈"""
        self.running = True
        logger.info("控制器開始執行")

        print("\n" + "=" * 80)
        print("QueueSwitchController 運行中...")
        print("按下 Ctrl+C 停止程式")
        print("=" * 80)

        last_minute = -1
        last_check = time.time()

        try:
            while self.running:
                now = datetime.datetime.now()
                current_time = time.time()

                # 顯示狀態資訊（每秒更新一次）
                if current_time - self.last_display_time >= self.display_interval:
                    self.last_display_time = current_time
                    self._display_status()

                # 每分鐘檢查排程
                if now.minute != last_minute:
                    last_minute = now.minute
                    self.scheduler.check_schedules()

                    # 每小時記錄狀態
                    if now.minute == 0:
                        logger.info(f"系統狀態: 時間={now.strftime('%H:%M:%S')}                              ")   # 空白填充清除殘留字元

                # 每30秒檢查設定檔
                if current_time - last_check >= 30:
                    last_check = current_time
                    self.config.load()

                # await asyncio.sleep(0.05)  # 縮短睡眠時間讓顯示更即時
                await asyncio.sleep(0.5)  # 縮短睡眠時間讓顯示更即時

        except Exception as e:
            # 清除狀態行
            sys.stdout.write("\r" + " " * 100 + "\r")
            sys.stdout.flush()
            logger.error(f"主迴圈錯誤: {e}")
            import traceback

            traceback.print_exc()
        finally:
            self.running = False
            # 清除狀態行
            sys.stdout.write("\r" + " " * 100 + "\r")
            sys.stdout.flush()
            print("\n控制器停止")
            logger.info("控制器停止")

    def start(self):
        """啟動控制器"""
        # Windows 事件循環設定
        if platform.system() == "Windows":
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

        # 設置偵錯模式
        os.environ["PYTHONASYNCIODEBUG"] = "0"  # 改為 0 減少調試訊息

        # 設置 websockets 日誌級別
        logging.getLogger("websockets").setLevel(logging.WARNING)
        logging.getLogger("asyncio").setLevel(logging.WARNING)

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        # 設置慢回調門檻 !!!@@@
        loop.slow_callback_duration = 0.5

        try:
            loop.run_until_complete(self.run())
        except KeyboardInterrupt:
            # 清除狀態行
            sys.stdout.write("\r" + " " * 100 + "\r")
            sys.stdout.flush()
            print("\n收到中斷訊號，正在關閉...")
            logger.info("收到中斷訊號")
        except Exception as e:
            # 清除狀態行
            sys.stdout.write("\r" + " " * 100 + "\r")
            sys.stdout.flush()
            logger.error(f"執行錯誤: {e}")
            import traceback

            traceback.print_exc()
        finally:
            self.running = False
            loop.close()


def create_example_config():
    """建立範例設定檔"""
    content = """# 叫號系統設定檔
# 格式說明:
# 1. [Caller ID] - 每個 Caller 區塊以此開始
# 2. NAME: Caller 名稱
# 3. CLOSED: 休業日期 (yyyy-mm-dd格式，多個用逗號分隔)
# 4. HH:MM, 動作(ON/OFF), 備註(選填)

[v0103]
NAME: 測試診所
CLOSED: 2026-12-25, 2026-12-31
09:00, ON, 開始營業
12:30, OFF, 午休
13:30, ON, 下午營業
17:00, OFF, 結束營業

[a0101]
NAME: 第一診所
CLOSED: 2026-01-10
08:30, ON, 早上開診
12:00, OFF, 午休
14:00, ON, 下午開診
18:00, OFF, 晚上休診
"""

    config_path = "queue_switch_config.txt"
    if not os.path.exists(config_path):
        with open(config_path, "w", encoding="utf-8") as f:
            f.write(content)
        print(f"已建立範例設定檔: {config_path}")
        print("請編輯設定檔後重新啟動程式")
    else:
        # print(f"設定檔已存在: {config_path}")
        pass


def main():
    print("=" * 50)
    print("QueueSwitchController")
    print(f"平台: {platform.system()} {platform.release()}")
    print("=" * 50)

    create_example_config()

    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--config", help="設定檔路徑")
    parser.add_argument("--url", help="WebSocket URL")
    parser.add_argument("--env", choices=["local", "trial", "live"], default="trial")
    parser.add_argument("--debug", action="store_true", help="啟用偵錯模式")

    args = parser.parse_args()
    os.environ["CMB_ENV"] = args.env

    if args.debug:
        logger.setLevel(logging.DEBUG)
        logging.getLogger("websockets").setLevel(logging.DEBUG)
    else:
        # 非調試模式減少日誌輸出
        logger.setLevel(logging.INFO)
        logging.getLogger("websockets").setLevel(logging.WARNING)
        logging.getLogger("asyncio").setLevel(logging.WARNING)

    controller = QueueSwitchController(config_path=args.config, ws_url=args.url)

    try:
        controller.start()
    except KeyboardInterrupt:
        print("\n程式結束")
    except Exception as e:
        logger.error(f"執行錯誤: {e}")
        import traceback

        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
