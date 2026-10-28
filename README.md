# agent — 階級制 AI Agent 團隊（Windows Terminal CLI，MVP v1）

你只跟 **Manager** 對話；Manager 可自己做事，或派工給 **Worker**。Worker 只對 Manager 提問/回報。
所有 Agent 之間的對話都顯示在終端聊天室。Agent 直接呼叫各自官方 CLI（Claude Code、Antigravity `agy`），
沿用它們的登入、權限與設定。只用 Python 標準函式庫，不需 API Key、資料庫或 Agent Framework。

```
Me> 幫我整理這個專案
Manager> 我先分析專案結構。
Manager> @w1 請檢查 logs 並整理異常。
w1> @Manager 我需要確認 logs 的存放位置。
Manager> @w1 請先檢查 logs/ 資料夾。
w1> @Manager 已完成，發現三項異常。
Manager> @Me 任務完成，以下是整理結果……
```

## 安裝（Windows PowerShell，Python 3.10+）

```powershell
cd <此專案資料夾>
py -m pip install -e .
agent --help
```

若 `agent` 找不到，把 `py -m site --user-base` 底下的 `Scripts` 資料夾加入 PATH，或改用 `py -m agent`。
也可不安裝直接執行：`py -m agent`（在專案資料夾內）。

## 使用

```powershell
agent config          # 建立（若不存在）並顯示 %APPDATA%\agent\config.json 的路徑與檢查結果
agent config --show   # 另外印出內容
agent                 # 進入聊天室（以目前資料夾為 Agent 的工作資料夾）
```

聊天室指令：`/status`（狀態、各 Agent Session ID、未完成任務）、`/interrupt`（中斷正在跑的 turn；
不假裝成功、不還原檔案，之後可繼續輸入）、`/exit`（清理子程序、存檔、離開）。
Agent 工作時你可以繼續輸入；Manager 忙碌時訊息會排隊。每次 `agent` 都建立新對話。
輸入僅支援單行（Enter 送出）。Agent 輸出會在你輸入一半時插入，這時提示字元會重畫、已打的字仍在輸入緩衝區。

### 恢復對話：`agent sessions` / `agent resume <id>`

```powershell
agent sessions                 # 列出已保存的 Agent Session（含 [IN USE] 標記）
agent resume <Agent Session ID>   # 顯示先前聊天室內容，再用原本的 Manager / Worker CLI Session 繼續
```

- **會恢復**：Manager 與 Worker 的 CLI Session ID、`preamble_sent`（已成功套用的 preamble 不會重送）、任務狀態、等待使用者回答的 `ask_user` 問題、排隊中的訊息／Worker 回報／系統通知、先前的聊天紀錄（`events.jsonl`）。
- **不會自動重做**：上次被切斷、結果未知的 turn（Manager 正在處理的訊息、Worker 進行中的任務）。Worker 任務會標為 `interrupted`，聊天室會列出哪些你的訊息需要重送；檔案不會還原。保留下來的排隊項目恢復後先**暫停**，輸入 `/continue` 才處理（直接輸入新訊息也會處理，排在它們之後；`/interrupt` 丟棄）。
- **同一 Session 只允許一個寫入者**：Session 目錄內有 `session.lock`（pid + 心跳）。擁有者還活著就拒絕，絕不覆蓋；擁有者已消失（當機）才接手殘留鎖。若鎖看起來卡住但程序仍在，訊息會告訴你要手動刪哪個檔案。
- 必須在原本的資料夾內恢復（否則加 `--here`）；設定檔中某個 Agent 的 CLI 與原 Session 不同會拒絕恢復（模型／effort 不同只是警告）。
- 舊版（v1）Session 可讀可恢復，但當時沒有保存的暫存訊息無法復原。
- Claude 的 `-p` Session 預設只保留約 30 天（CLI 的 `cleanupPeriodDays`），過期後會出現 `session_lost`，本工具不會自動換新 Session。

### 實驗：原生 Claude Session 匯入（尚未成為正式功能）

底層已備妥 `--resume <id> --fork-session` 的安全規則（fork 後 Session ID 必須是新的、原 Session 不能被當成工作 Session、一般 resume 仍拒絕 ID 變更），但**沒有** `agent <Claude Session ID>` 這個指令。是否可行用拋棄式 Session 驗證：`python probe.py claude --tests fork`（約 3 次小額度呼叫，只用腳本自己建立的 Session）。`python probe.py agy --tests keys` 只印出 agy 事件的欄位名稱，用來確認它有沒有模型欄位。

### 每則回覆的模型

每個 Agent 回覆前會在聊天室標一行 `Manager · claude · <模型>（CLI 回報）`，只在該 Agent 的模型變動時再出現。三種意義不同：**CLI 回報**（CLI 在這個 turn 的事件裡明說用了什麼）／**要求使用 X（CLI 未確認）**（我們傳了 `--model`，但 CLI 沒回報）／**模型未知**。要求的模型不會被當成實際模型；每個 turn 各自記錄（`events.jsonl` 的 `chat.turn`），之後改設定不會改寫歷史。agy 目前沒有已確認的模型欄位，所以通常顯示「要求使用」或「未知」。

### 設定檔 `%APPDATA%\agent\config.json`

預設內容即規格中的設定（manager=`claude`、`w1`=`agy`）。`model`/`effort` 為 `null` 時沿用 CLI 預設；
程式不寫死任何模型名稱。Manager 派工時可指定該 Worker 的 `model`/`effort`，未指定則用設定檔值。
模型或 effort 無效時 CLI 會報錯，本程式原樣回報給 Manager/你，**不會**偷偷換模型。

可選欄位（不在預設檔內）：
- `agents.<id>.command`：CLI 執行檔完整路徑（PATH 找不到時用）。
- `agents.<id>.options`：`extra_args`（額外 CLI 參數陣列）、agy 的 `stdin_mode`（`auto|always|never`）、`use_json_schema`。
- `snapshot`：`{"enabled": true, "max_files": 20000, "max_seconds": 5}`。
- 第一版固定 **1 個 Manager + 1 個 Worker**；Manager 與 Worker 可用同一種 CLI，各自有獨立 Session。

## 權限

程式**不會**加入 `--dangerously-skip-permissions`、`--permission-mode`、`--allowedTools`、`--sandbox`、`--bare`，
也不修改官方 CLI 的設定。headless 模式下 CLI 拒絕的工具呼叫會被偵測（Claude：`permission_denials`；
agy：stderr 與 `step_update.tool_info.error` 中的明確拒絕文字，啟發式），並以 `blocked` 證據回報給 Manager——即使 Worker 自稱 done 也會改為 blocked。
Manager 再告訴你需要怎麼授權。Manager 被指示在刪除/覆寫/部署/推送/付款前先 `ask_user`，
這是**應用層規則**，無法攔截 CLI 內部所有工具呼叫。

## 儲存與隱私

`%APPDATA%\agent\sessions\<時間>-<id>\`：
- `state.json`（原子寫入）：各 Agent Session ID、任務、狀態、等待使用者的問題。
- `events.jsonl`：聊天、狀態轉換、turn 結果、錯誤、檔案變動摘要。
- `raw\<agent>.jsonl`：CLI 原始輸出（每行最多 2000 字、每檔最多 2 MB，並遮蔽常見金鑰/token/環境變數中的秘密）。

遮蔽是盡力而為，**raw log 仍可能含有你的提示、檔案內容與路徑**，請把這個資料夾視為敏感資料。
`AGENT_HOME` 環境變數可改變資料夾位置。

## 運作方式（重點）

- Manager 每個 turn 回傳 `{"say", "actions":[dispatch|answer|ask_user|final]}`，Worker 回傳
  `{"status": done|question|blocked|failed, "task_id", "text", "files_touched"}`。
  Claude 以 `--json-schema` 取得 `structured_output`；agy 以提示內嵌 JSON 並由本程式驗證。
  Schema 驗證 + 程式層驗證（收件人、Task ID、狀態、`final` 不得在任務未結束時送出、不得 dispatch 給自己、
  `ask_user`/`final` 必須單獨）。不合法時最多要求修正一次，仍不合法則該 turn 失敗並回到可操作狀態。
- `ask_user`：顯示問題 → 暫停（Worker 回報暫存）→ 你的下一則輸入作為答案送回**同一個 Manager Session**。
- 狀態機：`idle running waiting_worker waiting_manager waiting_user done blocked failed interrupted timeout`，
  合法轉換表在 `orchestrator.py`，非法轉換會記錄為 `illegal_transition` 事件（測試斷言其為零）。
- Session：Claude 首輪用我們產生的 `--session-id`，之後 `--resume`；agy 從事件取得 conversation id 後存檔，
  之後 `--conversation`。無法續談（`session_lost`）會明確回報，不會悄悄開新對話。
- 子程序：不使用 `shell=True`；prompt 走 stdin（Claude）或 `-p`/stdin streaming（agy，兩者不混用）；
  `.cmd/.bat` 啟動器拒絕含 `" % ^ & | < > 換行` 的參數；`.ps1` 以 `powershell -File` 啟動。
  Windows 以 `CREATE_NEW_PROCESS_GROUP` + `CTRL_BREAK_EVENT`，3 秒後仍未結束才對**本程式啟動的** PID 執行 `taskkill /PID <pid> /T /F`。
- 檔案變動：每個 turn 前後做標準函式庫快照（略過 `.git`、`node_modules`、`.venv` 等、不跟隨符號連結、
  有數量/時間上限，不完整會標示）。只表示「執行期間有變動」，不保證是該 Agent 造成；不會還原或刪除任何檔案。

## 測試

```powershell
py -m unittest discover -s tests -t .        # Fake CLI：完整 Manager→Worker→Manager→User 流程，不耗額度
py tests\demo.py                              # 離線示範聊天室
py probe.py claude                            # 真實 CLI 探測（耗少量額度，在暫存資料夾）
py probe.py agy
```

`probe.py` 會逐項輸出 PASS/FAIL：stream-json、JSON Schema、stdin、續談、無效模型/effort、權限拒絕偵測、
Ctrl+Break/taskkill 清理（agy 另測 `--conversation`/`--json-schema` 是否能與 stdin streaming 搭配）。
結果寫到 `%TEMP%\agent-probe-<cli>.json`。

### 驗證狀態（誠實版）

| 項目 | 狀態 |
|---|---|
| Fake CLI 全流程測試（Linux） | 通過 |
| 真實 Claude Code 2.1.312（Linux）：stream-json+`--json-schema`、stdin prompt、`--session-id`/`--resume`、無效模型/effort 偵測、權限拒絕偵測、中斷、完整聊天室（Manager+Worker 皆 Claude）| 通過 |
| **Windows 原生**（subprocess、`.cmd` shim、Ctrl+Break/taskkill、主控台 UTF-8/ANSI） | **尚未實測**，程式已依 Windows 行為撰寫，請跑 `probe.py` |
| **agy 1.4.9** 官方 `init→step_update→result` 解析、`--effort` 傳遞、`--conversation` 一致性防護 | 僅以官方格式的模擬事件與 `tests/fake_agy.py` 測試；**未用真實 agy 驗證** |
| agy `--conversation` ＋ stdin streaming、`--json-schema`、soft-denial 實際文字、Windows 行為 | **尚未實測**，請跑 `py probe.py agy` |
| Codex、多 Worker、`/resume` | 依規格不做 |

## 已知限制

- agy 只以模擬事件測試（見上）；權限拒絕偵測僅辨識明確的政策/核准拒絕字樣，一般 EACCES 等工具錯誤不算。若 `probe.py agy` 結果不符，請貼回輸出調整 `agent/adapters/agy.py`。
- `writes` 欄位只是告知 Worker 的允許範圍並用於事後比對檔案變動，**無法強制**。
- 同時有 Manager 與 Worker 在跑時，檔案快照無法區分是誰改的。
- `timeout` 是每個 turn 的總時間上限（`turn_timeout_sec`）。另有純提示用的「無輸出」提醒（`silence_warn_sec` 預設 60 秒、之後每 `silence_repeat_sec` 預設 300 秒最多一次，有新輸出即重算）；它不會中斷任務，也不代表模型正在「思考」，只表示 CLI 沒有新輸出。
- 聊天室在等待輸入時若有非同步訊息（開始執行、提醒）插入，終端機的「正在輸入的文字」會暫時被清掉重畫提示列（文字仍在輸入緩衝區，按 Enter 照常送出）。標準函式庫的一般行輸入無法做到完整保留；完整方案（自製行編輯器或 prompt_toolkit）列為後續工作。
- 啟動時會在背景用官方 `--version` 記錄 CLI 版本（不耗額度、失敗不影響使用）；版本是否「已測試」見 `/status`，清單在 `agent/compat.py`，且不等於完全相容。
- 中斷後，被中斷的 CLI Session 可能留有半截歷史；程式會告知 Manager「任務被中斷、檔案未還原」。
