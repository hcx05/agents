"""Offline demo: full Manager -> Worker -> Manager -> User flow on the fake CLI (no quota used).
    python tests/demo.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests.rig import Rig, M, W, answer, dispatch, final  # noqa: E402

r = Rig({
    "manager": [M([dispatch("w1", "t1", "請檢查 logs 並整理異常。")], say="我先分析專案結構。"),
                M([answer("w1", "t1", "請先檢查 logs/ 資料夾。")]),
                M([final("任務完成，以下是整理結果……")])],
    "w1": [W("question", "t1", "我需要確認 logs 的存放位置。"), W("done", "t1", "已完成，發現三項異常。")],
})
print("Me> 幫我整理這個專案")
r.say("幫我整理這個專案")
print("\n".join(r.chat_lines()))
print(f"\nfinal state: {r.engine.state.value}; session dir: {r.store.dir}")
r.close()
