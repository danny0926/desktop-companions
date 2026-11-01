# 可選語音 runtime

此目錄保留原 TTS 工作台的自有 `irodori_lowmem.py`，供 `desktop_pet.voice.worker` 呼叫。模型建構與分塊載入是本地整合，採樣、tokenizer、codec、PEFT 與 SilentCipher 仍使用固定官方上游。桌寵不需要工作台的資料處理、訓練或故事生成程式。

`voice_artifacts.py` 是不載入 ML 的檔案檢查。`configure_paths()` 只在自有語音子程序設定模型位置；Irodori 上游不被修改。SilentCipher 明確讀取固定 revision 的本機 44.1 kHz 權重，不以隱含 `main` 快取選模型。

`irodori-gpu-requirements.txt` 是原工作區從固定 Irodori `uv.lock` 匯出的 CUDA 12.8 依賴，含 pinned Git 依賴與套件雜湊，保留供環境追溯。建議安裝方式是根目錄 README 的 `uv sync --frozen`；本次匯出沒有重新安裝或跑推論。

此副本與 [tts-workbench](https://github.com/danny0926/tts-workbench) 共用來源而各自獨立維護。修改載入契約時需比對兩邊，不能依靠另一個 checkout 出現在磁碟。上游版本與模型 revision 見 `config/voice_artifacts.lock.json`；個人位置由忽略的 `desktop_pet_voice.local.json` 管理。
