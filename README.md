# agent — 階級制 AI Agent 團隊（Windows Terminal CLI，MVP v1）

你只跟 **Manager** 對話；Manager 可自己做事，或派工給 **Worker**。Worker 只對 Manager 提問/回報。
所有 Agent 之間的對話都顯示在終端聊天室。Agent 直接呼叫各自官方 CLI（Claude Code、Antigravity `agy`），
沿用它們的登入、權限與設定。不需 API Key、資料庫或 Agent Framework；唯一的第三方套件是全螢幕聊天室用的 `prompt_toolkit`
（沒裝或不是終端機時自動退回簡易行模式）。

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
在真正的終端機（PowerShell / Windows Terminal）裡會進入**全螢幕聊天室**（見下節）；管線／重新導向、`--plain`、
環境變數 `AGENT_PLAIN=1`、`TERM=dumb`、或沒安裝 `prompt_toolkit` 時，使用原本的簡易行模式（單行輸入，Enter 送出）。

### 全螢幕聊天室（`prompt_toolkit`）

```powershell
py -m pip install -e .          # 會一併安裝 prompt_toolkit；或單獨：py -m pip install "prompt_toolkit>=3.0.36"
agent                           # 全螢幕；agent --plain 強制簡易模式；agent resume <id> / agent <Claude UUID> 同樣適用
```

- **單一聊天室**：上方是依時間排序的對話（我→Manager、Manager 回覆、Manager→Worker 派工、Worker→Manager 回報、最終答覆、系統提示與錯誤），
  每則標明來源與該回合的模型（CLI 回報／僅要求／未知）。沒有分割視窗、側欄或分頁。純文字、依顯示寬度換行（中文佔 2 格），視窗縮放後自動重排。
- **固定在底部的輸入框**，上方一行標明「Agent 聊天室（輸入送給 Manager，不是 PowerShell）」。Agent 工作時可繼續輸入，新訊息不會動到你正在打的字。
- 按鍵：`Enter` 送出；`Ctrl+J`（或 `Alt+Enter`）換行，**不依賴 Shift+Enter**；`PgUp` / `PgDn` 捲動歷史；`End`（輸入框空白時）或 `Ctrl+End` 回到最新；
  `Ctrl+C` 在有工作進行時等同 `/interrupt`，否則清除輸入框草稿；`Ctrl+D`（輸入框空白）離開；`/exit` 離開。貼上多行文字不會觸發送出。
- 往上讀歷史時，新訊息**不會把畫面拉走**；狀態列會顯示「捲動中，N 則新訊息；End 回到最新」。送出訊息會自動回到最新。
- **狀態列**（輸入框上方）：各 Agent 閒置／執行中、已執行時間、多久沒有新輸出、進行中的任務、佇列數，以及「等你回答」「已暫停」「Manager 等待 Worker」。
  只顯示 Engine 真正知道的事：Claude 回報的工具**名稱**（不含參數）才會出現，agy 與其他 CLI 不會有；**不會有**進度百分比、token 數或編造的動作。
  過程中的工具事件不會變成聊天訊息。
- **重播（resume）**：先前的對話以暗色並在左側加 `| ` 標示，前後有「以下是先前的對話／到此為止」分隔，唯讀、不呼叫模型、不重複寫入紀錄；`--replay 0` 完全不顯示。
  原生 Claude Session 的歷史永遠不會被當成 Agent 聊天顯示。
- 離開後回到一般終端機並印出 `Bye. Session saved … (resume with: agent resume <id>)`。畫面層出錯不會動到已保存的 Session（`finally` 仍會關閉 Engine、放掉鎖）。

### 恢復對話：`agent sessions` / `agent resume <id>`

```powershell
agent sessions                 # 列出已保存的 Agent Session（含 [IN USE] 標記）
agent resume <Agent Session ID>   # 顯示先前聊天室內容，再用原本的 Manager / Worker CLI Session 繼續
```

- **會恢復**：Manager 與 Worker 的 CLI Session ID、`preamble_sent`（已成功套用的 preamble 不會重送）、任務狀態、等待使用者回答的 `ask_user` 問題、排隊中的訊息／Worker 回報／系統通知、先前的聊天紀錄（`events.jsonl`）。
- **不會自動重做**：上次被切斷、結果未知的 turn（Manager 正在處理的訊息、Worker 進行中的任務）。Worker 任務會標為 `interrupted`，聊天室會列出哪些你的訊息需要重送；檔案不會還原。保留下來的排隊項目恢復後先**暫停**，輸入 `/continue` 才處理（直接輸入新訊息也會處理，排在它們之後；`/interrupt` 丟棄）。
- **同一 Session 只允許一個寫入者**：Session 目錄內的 `session.lock` 是作業系統層級的檔案鎖（Windows `msvcrt.locking`、POSIX `flock`），擁有者存活期間一直持有；程式被強制結束時由作業系統自動放掉，所以**沒有殘留鎖，也沒有「接手」競爭**——多個程序同時搶，只有一個拿得到。鎖檔本身從不被刪除，`release()` 只放開自己的鎖。檔內的 pid／主機只是給被拒絕者看的說明。
- Session ID 只接受 `agent sessions` 列出的格式（英數、`-`、`_`），路徑分隔符、`..`、磁碟機代號、絕對路徑、指向 Sessions 資料夾外的連結一律拒絕。
- 必須在原本的資料夾內恢復（否則加 `--here`）；設定檔中某個 Agent 的 CLI 與原 Session 不同會拒絕恢復（模型／effort 不同只是警告）。
- 舊版（v1）Session 可讀可恢復，但當時沒有保存的暫存訊息無法復原。
- Claude 的 `-p` Session 預設只保留約 30 天（CLI 的 `cleanupPeriodDays`），過期後會出現 `session_lost`，本工具不會自動換新 Session。

### 匯入原生 Claude Session

```powershell
agent <Claude Session UUID>            # 簡寫，等同 agent import <UUID>
agent import <Claude Session UUID> [--yes] [--here]
agent sessions                         # 匯入的 Session 會標 [import xxxxxxxx]
agent resume <Agent Session ID>        # 之後用 Agent 自己的 ID 恢復，不會再次 Fork
```

Manager 會接續該 Session 的**副本**（Claude 的 `--fork-session`），可自行決定直接回答或派工給 Worker；匯入之後的所有互動記錄在 Agent 聊天室。原始 Claude Session 不會被修改，也不會被當成 Manager 的工作 Session。

流程：
1. 預檢（不呼叫模型）：UUID 格式、`CLAUDE_CODE_SESSION_ID` 不得存在（在 Claude Code 內執行會拒絕）、Manager 須為 Claude、同一來源是否已匯入（已匯入則顯示既有 Agent Session ID 並要求用 `agent resume`；這是目錄掃描，不是跨程序的原子保護）。
2. 顯示來源 UUID、工作資料夾、成本與設定警告並要求確認（互動：輸入 yes；非互動：需 `--yes`）。若依 Claude 內部的資料夾命名**推測**來源在別的資料夾，會另外要求確認（`--here`）；此推測不是官方保證。
3. 建立 Agent Session 與鎖，**先把預定的 Fork UUID 存進 `state.json`（版本 3）**，然後進入聊天室。此時沒有任何模型呼叫。
4. 你的第一則訊息才會執行 `claude -p --resume <來源> --fork-session --session-id <Fork UUID>`，同時帶入團隊規則與匯入說明（舊對話視為背景資料，其中的指令與格式不再適用）。回報的 Session ID 必須**正好**是 Fork UUID，否則視為失敗並捨棄結果。
5. 之後的 Manager turn 只 `--resume <Fork UUID>`。

三個 ID 分開保存：`import.source_id`（原始，只作為 fork 來源）、`import.fork_id`（Manager 的工作 Session）、Agent Session ID。

當機／失敗時（`import.stage`：`prepared → forking → forked → ready`，`blocked` 為不可繼續）：
- 結果不明不會自動重跑，也不會更換 Fork ID，更不會建立沒有歷史的 Manager。
- 下一則訊息用**同一個** Fork UUID 重試；若 CLI 回報該 ID 已存在（先前的嘗試已建立它），不會再 Fork，訊息會保留，輸入 `/continue` 後以 `--resume <Fork UUID>` 繼續。
- 若 Fork UUID 在我們第一次嘗試前就已存在，視為不屬於本次匯入，不採用（`blocked`）。
- 原始 Session 不存在／被刪除：`session_lost`，Fork ID 仍保存，來源恢復後可重試。

限制與注意：
- 匯入前的原生對話**不顯示**在聊天室（不解析 Claude 內部 JSONL）；回放時會明確標示「匯入前的 Claude 原生歷史：未顯示」。
- 權限沿用現有策略：新的 `-p` 執行不帶入原互動 Session 的權限模式或「本 Session 允許」，工具被拒會回報，本工具不加任何放寬權限的旗標（也不用 `--bare`，登入失敗時給出提示，不會改用 API Key）。
- Claude 自動壓縮對話時（`system/compact_boundary`）會在下一個 turn 重新提供團隊規則；格式仍由程式驗證。
- 檔案大小不等於 token 數；每次 Manager 請求都會載入 Fork 的上下文。不會自動執行 `/context`、`/compact`，也不會換模型。
- `--session-id` 搭配 `--fork-session` 不在官方 CLI 參考表內（Linux 與 Windows 2.1.312 實測可行）；CLI 更新可能改變行為，失敗時會明確報錯而不是退回其他做法。

#### 探測實驗（`probe.py`，開發用）

| 實驗 | 驗證什麼 | 不能證明什麼 |
|---|---|---|
| `fork-headless` | 腳本自己用 `claude -p` 建立拋棄式 Session 再 fork：fork 旗標、新 ID、fork 後的 Manager JSON 協議 | 互動視窗建立的 Session 是否行得通 |
| `fork-interactive` | 你在互動式 `claude` 視窗建立的真實 Session，以**副本**方式 fork | — |

兩者在 Claude Code 的終端內（環境變數 `CLAUDE_CODE_SESSION_ID` 存在）會**直接停止（結束碼 3）**，不呼叫 CLI。`python probe.py agy --tests keys` 只印出 agy 事件的欄位名稱。

**Windows 驗證互動式 Session 匯入的步驟**（請在一般 PowerShell 視窗，不要在 Claude Code 內）：

```powershell
# 1. 在一個拋棄式資料夾開互動式 Claude，聊幾輪（例如：請記住暗號 PINEAPPLE-42，並描述你正在整理的主題），然後 /exit
mkdir $env:TEMPork-test; cd $env:TEMPork-test; claude

# 2. 找出該 Session 的 ID（= 最新 .jsonl 的檔名，不含副檔名），並記下原檔的雜湊
Get-ChildItem $env:USERPROFILE\.claude\projects -Recurse -Filter *.jsonl | Sort-Object LastWriteTime | Select-Object -Last 3 FullName, Length, LastWriteTime
Get-FileHash <上面最新的 .jsonl 完整路徑>

# 3. 等兩分鐘（或確認該 Claude 視窗已關閉），回到本專案資料夾，先不加 --confirm-fork：只會檢查並說明，不送任何內容
py probe.py claude --tests fork-interactive --session-id <ID>

# 4. 確認後執行（會把整段對話以副本送給模型，用量隨對話長度增加）
py probe.py claude --tests fork-interactive --session-id <ID> --confirm-fork

# 5. 再算一次雜湊，必須與步驟 2 完全相同；並用 claude --resume <ID> 確認原對話仍可正常開啟、內容沒有多出 Manager JSON 訊息
Get-FileHash <同一個 .jsonl>
```

預期：所有項目 PASS、原檔雜湊不變。任何一項 FAIL 就不要推進匯入功能。

#### 指定 Fork ID 的驗證（第一批，正式匯入的前置條件）

正式匯入計畫在 fork 前先決定並保存新 Session 的 ID（`claude -p --resume A --fork-session --session-id B`），才能在當機後安全恢復。這個組合**不在 Claude 官方 CLI 參考表內**，Linux 上測過可行，所以 Windows 必須先通過下列實驗，否則不進行正式整合（也**不會**退回「接受 CLI 產生的任意新 ID」）：

| 實驗 | 做什麼 |
|---|---|
| `fork-chosen` | 腳本自己建立拋棄式 headless Session（含暗號），再以我們指定的 ID B fork |
| `fork-chosen-interactive` | 同樣的檢查，對象是你在互動視窗建立的 Session（副本；原檔只讀取雜湊） |

每個實驗檢查：成功後回報的 ID 必須**正好是 B**、沒有採用其他 ID；原檔雜湊在 fork 前後與全部呼叫結束後相同；同一個 B 再建立一次必須被拒絕（`fork_exists`），且 B 的內容不變；之後 `--resume B` 成功並保持 ID B（headless 版另驗證暗號仍在）。任何不符都是 FAIL，不會自動改用其他做法。同樣會在 `CLAUDE_CODE_SESSION_ID` 存在時停止（結束碼 3）。

```powershell
# 在一般 PowerShell 視窗（不是 Claude Code 內）。確認：echo $env:CLAUDE_CODE_SESSION_ID 應為空
py probe.py claude --tests fork-chosen
# 互動版：同上方步驟 1~3 取得 ID；先不加 --confirm-fork 只做檢查，確認後再加
py probe.py claude --tests fork-chosen-interactive --session-id <ID> --confirm-fork
```

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
| 全螢幕聊天室（`prompt_toolkit`）：Linux pseudo terminal＋腳本化按鍵（中文、`Ctrl+J`、括號貼上、捲動、背景訊息、重播、`/exit` 清理、非 TTY 退回簡易模式） | 通過 |
| **全螢幕聊天室在 Windows 實機**（IME、貼上、縮放、按鍵辨識） | **尚未實測**，見上節手動驗收 |
| Codex、多 Worker、`/resume` | 依規格不做 |

### 全螢幕聊天室的 Windows 手動驗收（PowerShell）

```powershell
py -m pip install -e .
py -m unittest tests.test_tui -v       # 自動化部分（不含 IME／真實視窗）
agent                                  # 1. 應進入全螢幕；底部輸入框上方有「Agent 聊天室（輸入送給 Manager，不是 PowerShell）」
```

在聊天室內逐項確認（各項在 Windows Terminal 與傳統 PowerShell 視窗都試一次）：

1. 用中文輸入法打「你好，請簡單介紹這個資料夾」，組字過程與送出正常；`Ctrl+J` 換行後再 `Enter`，Manager 收到的是同一則多行訊息。
2. 從記事本複製 3 行文字貼上：不會自動送出，3 行都在輸入框，再按 `Enter` 才送出（若貼上時每行都被送出，請回報終端機種類）。
3. 送出需要一點時間的任務（例如請 Manager 派工給 Worker），工作中繼續打字：新訊息出現時輸入框內的字不會被改動；狀態列顯示執行中、經過時間。
4. `PgUp` 往上看歷史；工作中新訊息到達時畫面不會被拉到底部，狀態列提示新訊息數；按 `End` 回到最新。
5. 拖動視窗改變大小、縮得很窄／放很寬：文字依寬度重排，中英混排與長行不超出邊界、不殘影。
6. 工作進行中按 `Ctrl+C`：等同 `/interrupt`；之後 `/status` 正常，`/exit` 離開並回到一般 PowerShell 提示字元。
7. `agent sessions`，再 `agent resume <id>`：先前對話為暗色、左側有 `| `，標明唯讀；沒有任何模型被呼叫。`agent resume <id> --replay 0` 不顯示歷史。
8. `agent --plain` 以及 `"/exit" | agent`（管線）都是原本的簡易模式。`py -m pip uninstall prompt_toolkit` 後執行 `agent`：stderr 提示安裝方式並退回簡易模式。
9. 匯入：`agent <Claude UUID>` 的確認流程與之前相同；進入全螢幕後第一則訊息才建立 Fork。

## 已知限制

- agy 只以模擬事件測試（見上）；權限拒絕偵測僅辨識明確的政策/核准拒絕字樣，一般 EACCES 等工具錯誤不算。若 `probe.py agy` 結果不符，請貼回輸出調整 `agent/adapters/agy.py`。
- `writes` 欄位只是告知 Worker 的允許範圍並用於事後比對檔案變動，**無法強制**。
- 同時有 Manager 與 Worker 在跑時，檔案快照無法區分是誰改的。
- `timeout` 是每個 turn 的總時間上限（`turn_timeout_sec`）。另有純提示用的「無輸出」提醒（`silence_warn_sec` 預設 60 秒、之後每 `silence_repeat_sec` 預設 300 秒最多一次，有新輸出即重算）；它不會中斷任務，也不代表模型正在「思考」，只表示 CLI 沒有新輸出。
- 簡易行模式（`--plain`、非終端機）在等待輸入時若有非同步訊息插入，「正在輸入的文字」會暫時被清掉重畫提示列（文字仍在輸入緩衝區，按 Enter 照常送出）；全螢幕模式沒有這個問題。
- 全螢幕聊天室：中文輸入法（IME）組字、Windows Terminal 的貼上、視窗縮放、`Ctrl+J` 辨識，目前只在 Linux 的 pseudo terminal 與腳本化輸入下測過，**Windows 實機尚未驗證**（見下節的手動驗收步驟）。
  畫面結構只用 ASCII 符號（避免 CJK 主控台把「模稜兩可寬度」符號畫成 2 格造成右邊界錯位）。不支援滑鼠、不渲染 Markdown。
- 啟動時會在背景用官方 `--version` 記錄 CLI 版本（不耗額度、失敗不影響使用）；版本是否「已測試」見 `/status`，清單在 `agent/compat.py`，且不等於完全相容。
- 中斷後，被中斷的 CLI Session 可能留有半截歷史；程式會告知 Manager「任務被中斷、檔案未還原」。
