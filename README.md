# 二乃像素桌寵

Windows 原生桌寵：像素角色在多螢幕桌面散步、回應摸頭與文字動作，透過已登入的 Codex CLI 訂閱進行日文對話，搭配繁體中文字幕。可另行設定本機 Irodori 語音；新 clone 不需要模型、CUDA、PyTorch 或其他 Python 套件即可開啟無語音介面。

![二乃像素待機](desktop_pet/assets/nino-pixel-v3/frames/idle.png)

這是獨立、非官方的成人後續創作場景。程式、角色背景摘要與生成美術各有不同的來源與權利範圍，見 [來源與權利說明](THIRD_PARTY_NOTICES.md)。模型、個人對話與原始參考音訊不在儲存庫內。

## 立即啟動

需要 Windows 10/11 與 Python 3.12（安裝時包含 Tcl/Tk）。在儲存庫根目錄執行：

```powershell
python -m desktop_pet --no-voice --chat
```

也可雙擊 `Start Nino.cmd`，或執行下列啟動器；啟動器預設關閉語音，會優先採用本目錄 `.venv/Scripts/python.exe`，再使用 PATH 的 Python。

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File tools/start_nino_pet.ps1 -Chat
```

介面可以在未登入 Codex 時啟動；實際對話需要官方 Codex CLI 與既有 ChatGPT 訂閱登入。自行安裝官方 CLI，執行 `codex login` 選擇 ChatGPT，並以 `codex login status` 確認登入方式。程式拒絕 API key 登入，不從訂閱擷取 token，也不改用付費 API 或本機／自架 LLM。模型預設 `gpt-6-luna`，可在角色設定的 `provider.model` 指定帳號實際可用的模型。CLI 必須支援本程式使用的 `--ignore-user-config`、`--ephemeral`、`--output-schema` 與功能停用旗標；不支援時會顯示真實錯誤。

## 已保留的功能

- 預設像素 v3 有 **79 個影格與 32 組動畫**，包含四方向四相位散步、坐姿、表情、抱胸、歪頭、坐下、坐姿搖晃、起立、被抱起與落地；像素 v2（51 格）與 Q 版 v1 仍可選。動畫依 manifest 播放，缺少動作的外觀會回退站姿。
- 跨相鄰螢幕移動、每螢幕 DPI、工作區與可見路線限制；拖曳、輸入、播放、鎖定與全螢幕時暫停移動。全螢幕保持自動語音安靜；使用者主動發起的對話仍可播放。滑鼠靠近會暫停，方便點選。
- 點／來回摸頭即時反應；點身體開聊天、拖曳身體移動、Shift+拖曳不限區域。右鍵與托盤包含互動、自訂動作、穿透、設定與退出。`Ctrl+Alt+N` 叫回聊天並解除穿透。
- 中文或日文文字輸入；`Enter` 傳送、`Shift+Enter` 換行、`Esc` 停止。`（摸頭）`、`*輕拍肩膀*`、`/action 自訂動作` 走獨立動作語意，不把旁白讀成語音。
- 有來源標記的 **18 條角色背景摘要**：6 條官方資料、12 條社群資料。日文台詞、繁中字幕與演出欄位分開；明確記憶寫入與對話交付狀態分開。
- 安靜／偶爾／活潑主動頻率；閒置返回時輪換開場意圖，依實際聽過的內容延續話題，避免近期重複。不將閒置推論成離席或工作完成。
- 聊天視窗可切換坐在原地／起來走走並保存偏好。原生拖曳攜抱使用真實指標位移、抓握與阻尼安定，不循環播放任意搬運影格。
- 開啟設定、切換坐姿或外觀等展示操作不會取消正在播放的非畫面主動語音。持續閱讀可累積到 300 秒後才進入閒置暫停；全螢幕仍禁止自動語音。
- 可選目前前景視窗或其所在螢幕的單張畫面觀察，使用 Codex 讀圖；畫面 PNG 只供該次請求，完成／取消後刪除。觀察有 60 秒新鮮度與畫面切換檢查，手動操作優先。
- 語音逐句合成、第一句完成即播放、下一句接續；停止／新輸入取消舊回合。回覆旁可重播當次原始 WAV、收藏原音；缺少音檔與片段不會假裝完整重播。重播不重新合成，也不再寫一則記憶。「重試語音」則沿用原台詞重新合成。

設定、聊天與收藏都從右鍵選單進入。關閉聊天只會收起視窗，完整退出請用「退出二乃」。麥克風／ASR 尚未提供。對嘴是實際 PCM 音量驅動的兩張嘴形，語音是完整句 WAV 接續，不是音素對嘴或原生音訊串流。

## 可選的本機語音

語音仍採單一自有子程序、CUDA 原生 BF16 與低記憶體載入。需求包含相容 NVIDIA GPU／驅動、足夠 RAM／提交餘量，以及官方 Irodori、codec、SilentCipher、tokenizer、相容 LoRA 與 reference latent。資源不足會保留文字並回報等待或失敗；程式不關閉其他應用。

版本固定於 [voice_artifacts.lock.json](config/voice_artifacts.lock.json)：Irodori 上游 commit `59be938c99d48b5653ca5aa3fdd0cddaa56940e5`、基礎模型 revision `2b28324dc263ed5e6638b3cf3dd94c82ead07b4b`、codec revision `47376ee24834d7a05a48ebabfe3cde29b3c5e214`、SilentCipher revision `a1c4d021905e0dc5b24be5f68db5fc4dba410ee1`。上游原始碼與模型只在本機選擇性取得，不加入 Git。

先檢查目前缺少什麼。這個指令只讀檔案與 Git revision，不匯入 ML 或載入模型；缺檔回傳 exit code 2。

```powershell
python tools/setup_voice.py --check
```

在全新機器設定時，明確執行下列選擇性下載與安裝步驟。需要 Git 與 `uv`；Python 3.11 用於與原語音環境一致的獨立環境。`uv sync` 使用固定上游的 `uv.lock`，安裝 CUDA 12.8 套件與其依賴，不是啟動桌寵的必要步驟。

```powershell
python tools/setup_voice.py --fetch-upstream
$env:UV_PROJECT_ENVIRONMENT = Join-Path (Get-Location) '.venv-irodori-gpu'
uv sync --project .vendor/irodori-tts --frozen --extra cu128 --no-dev --python 3.11
& ./.venv-irodori-gpu/Scripts/python.exe tools/setup_voice.py --download
```

`--fetch-upstream`／`--download` 結束仍會檢查所有檔案，因此尚未提供個人 adapter／reference 時回傳 2 是預期結果。下載的模型約 3 GB，相關授權以各上游模型頁為準。原環境的依賴鎖定匯出也保留於 `runtime/irodori-gpu-requirements.txt` 供稽核，不代表需要安裝整套環境才能顯示介面。

自行提供有權使用、與固定基礎模型相容的 PEFT adapter 目錄（含 `adapter_config.json` 與 `adapter_model.safetensors`）以及 deterministic reference latent。reference 是 Irodori 預先編碼的 `.pt`，不是任意 WAV；製作流程見訓練工作台與上游說明。以下命令只記錄本機檔案位置與計算 SHA-256，不複製聲音、資料集或權重：

```powershell
python tools/setup_voice.py --adapter-dir 'E:/my-voice/final-step-500' --reference-latent 'E:/my-voice/reference_latent.pt'
python tools/setup_voice.py --check --verify-hashes
powershell -NoProfile -ExecutionPolicy Bypass -File tools/start_nino_pet.ps1 -Chat -Voice
```

這會建立被 Git 忽略的 `config/desktop_pet_voice.local.json`，以最上層欄位覆蓋共用設定；相對路徑一律以本儲存庫根目錄解析。也可手動複製 [本機設定範例](config/desktop_pet_voice.local.example.json)。原二乃 final-step-500 adapter 的 SHA-256 是 `69ccbe7abf83735725f11bef586902fa25c52397229032102ab2984031ab0235`；reference 是 `f85b55bfcac97027c84707c1c6c7487509073cbb04b6f36d60e8254fe3fb876d`。這些是來源辨識資訊，並未附帶該檔案或給予聲音／角色使用授權。其他相容 adapter 可以透過設定指定自己的雜湊；實驗性的 frozen 模式僅接受原 final500 權重與設定，預設使用 PEFT 低記憶體模式。

如果同一台機器已有原 TTS 工作區，可選擇重用既有安裝與權重。這是明確指定的外部路徑，不是程式內建對另一個儲存庫的依賴；不會把其記憶、對話或音訊紀錄帶進新儲存庫。

```powershell
python tools/setup_voice.py --reuse-workspace 'E:/existing/TTS' --check
```

品質模式預設 40 步，官方 6 步 Sway 為實驗選項。原工作區的效能結果不能當成新機器的延遲保證；本次匯出沒有安裝重型環境或重新執行語音推論。

## 程式與設定

| 路徑 | 責任 |
| --- | --- |
| `desktop_pet/app.py`、`shell/` | Tk／Win32 外殼、托盤、互動、WAV 播放、收藏 |
| `desktop_pet/behavior/` | 多螢幕拓樸、移動規劃、閒置動作、主動事件與節流 |
| `desktop_pet/observation/` | 有界畫面擷取與觀察新鮮度 |
| `desktop_pet/agent/` | Codex 訂閱橋接、角色提示、結構化演出、SQLite 對話／明確記憶 |
| `desktop_pet/voice/` | 標準函式庫 facade、受控佇列、唯一自有 GPU worker、取消與資源門檻 |
| `runtime/irodori_lowmem.py` | 從原工作台保留的自有低記憶體推論整合；非整包上游複本 |
| `config/desktop_pet_*.json` | 外殼、行為、角色與語音設定 |
| `desktop_pet/assets/` | 兩套 PNG atlas、逐格圖、manifest 與清理後的生成來源 |
| `docs/research/` | 歷史選型／素材來源研究；不是現版本測試結果 |

對話路徑為 UI → ConversationController → AgentService → 官方 `codex exec` → 結構化日文／字幕／演出；可選語音再經 VoiceService → 自有 Irodori worker → 有官方浮水印的 WAV → Windows 播放。`runtime/` 的小量自有程式與 [tts-workbench](https://github.com/danny0926/tts-workbench) 有共同來源，各儲存庫可獨立運作；訓練、資料處理與評聽工具位於工作台。

## 本機資料與隱私

`outputs/`、模型、虛擬環境與 `*.local.json` 都被忽略。外殼偏好／未送出的草稿在 `outputs/pet-shell-v1/state/window.json`，最後回覆與語音收藏也在該 state 目錄。對話與明確儲存的記憶在 `outputs/pet-agent-v1/state/` 的 SQLite；操作／回覆事件與合成結果也僅保留在本機 outputs。收藏是原 WAV 的位元相同副本；取消收藏不會自動刪除檔案。

角色設定預設允許偶爾看目前視窗，可在「看畫面與小動作…」關閉，或把 `desktop_pet_app.json` 的 `screen_observation_enabled` 設為 `false`。此功能會把當次畫面交給 Codex 服務判讀；畫面中的指令視為不可信資料，不能改變角色任務或工具限制。不連續擷取、不進行本機 OCR，PNG 不寫入事件紀錄。顯式 `--screenshot` 開發參數會在指定路徑保留 PNG，請不要加入 Git。

Codex 每次請求使用獨立暫存目錄、read-only sandbox、ephemeral 模式與結構化 schema，關閉工具／外掛／代理功能；只沿用官方 CLI 的 ChatGPT 登入，不讀取或匯出憑證。程式會移除子程序環境中的 API key；登入／CLI 版本或訂閱額度失敗會顯示失敗，不自動切換服務。

## 開發檢查

不啟動 GUI、Codex 或 ML 的基本入口檢查：

```powershell
python -m desktop_pet --help
python -m desktop_pet.voice --help
python tools/setup_voice.py --check
```

以改動範圍選擇既有 `unittest` 模組。`voice/test_loading_contracts.py` 與 `voice/test_adapter_loading.py` 需要選擇性 ML 依賴／個人模型；未設定時會略過。`--smoke` 會啟動實際 GUI，僅在沒有需要保留的桌寵執行個體、且有互動桌面時主動執行。此次匯出未重跑 GUI、實際對話或語音。

## 視線與主動對話

- 角色會依滑鼠位置微微移動眼神，水平與垂直各最多 1 個邏輯像素；站立、坐姿及說話時都適用。拖曳按住或角色旋轉時暫停；不支援的閉眼與背面姿勢維持原樣。
- 主動搭話可把畫面當作聊天線索，日文台詞會直接對你說話，不會旁白描述介面；每次回覆是完整短句，最多 36 個日文字元，並顯示中文字幕。全螢幕時自動靜音。
- 對話模型預設為 `gpt-6-luna`。已確認一次真實模型請求能產生完整短句和中文字幕；單一樣本不代表整體品質，畫面觸發時的對話自然度仍待觀察。
