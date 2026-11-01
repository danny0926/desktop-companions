# 來源、素材與權利範圍

本儲存庫由既有 TTS 工作區的桌寵程式獨立匯出，保留完整外殼、角色 agent、行為、觀察、語音 facade、測試與兩套素材。`runtime/irodori_lowmem.py` 是原工作區的自有低記憶體整合程式；匯出只調整路徑、固定 SilentCipher 本機來源與移除桌寵不使用的故事 CLI。這裡沒有代替原作者或權利人宣告一個涵蓋全部內容的開源授權。

## 角色與生成圖像

中野二乃與《五等分の花嫁》的角色、名稱及相關權利仍屬各權利人（春場ねぎ、講談社及相關製作／授權方）。這是非官方生成同人美術與獨立成人後續場景，不代表官方授權、合作或背書；私人儲存庫也不改變原有權利範圍。

`desktop_pet/assets/nino/` 與 `nino-pixel-v2/` 是原專案以內建 image generation 工具產生、裁切與技術規整的圖像。v2 為 96×112 邏輯像素、51 影格，左向走路鏡射右向，表情與小動作使用生成的臉部／上身加固定站姿下身。未下載或重用第三方 sprite 像素。各目錄 `generation.json` 保留生成方式、處理步驟、限制與來源；v2 的 `prompts/` 保留文字提示。原始桌面截圖、私人參考圖片、原始工具輸出、瀏覽器全文與個人機器路徑已排除。

官方 [TBS 角色介紹](https://www.tbs.co.jp/anime/5hanayome/2nd/character/chara02.html) 僅用於角色識別與短篇背景事實。v1 的風格參考含使用者提供的桌寵圖庫畫面，未複製圖中角色像素，也不散布那張畫面。其他網路圖像／Shimeji／遊戲素材只是調查候選，不因列出來源就獲得再利用授權。

## 角色背景文字與研究

`config/desktop_pet_character.json` 的 6 條官方資料與 12 條社群資料均為短篇獨立改寫，保留每條 `source_id`。來源為 [TBS](https://www.tbs.co.jp/anime/5hanayome/2nd/character/chara02.html) 與 [萌娘百科〈中野二乃〉](https://zh.moegirl.org.cn/中野二乃)，原工作區記錄的閱讀日期是 2026-11-01；社群內容不等同官方定論。文章與原作台詞並未整篇複製。角色口語範例是原創，成人場景／關係距離是應用創作設定。

`docs/research/` 保存先前選型與素材來源調查，包含其他專案的作者、網址與當時辨識的授權資訊。這些專案未打包於本儲存庫；歷史的候選架構、效能或 readiness 文字不代表目前架構或新環境驗證結果。來源條款仍應以原始頁面為準。

## 選擇性語音依賴

| 元件 | 固定來源 | 說明 |
| --- | --- | --- |
| Irodori-TTS | [Aratako/Irodori-TTS](https://github.com/Aratako/Irodori-TTS/tree/59be938c99d48b5653ca5aa3fdd0cddaa56940e5) | 上游程式宣告 MIT；透過本機 bootstrap 取得，未整包入庫 |
| 基礎模型 | [Irodori-TTS-v4.1-Small](https://huggingface.co/Aratako/Irodori-TTS-v4.1-Small) | revision 與 SHA-256 見 lock，模型頁條款獨立適用 |
| Codec | [Semantic-DACVAE-Japanese-32dim](https://huggingface.co/Aratako/Semantic-DACVAE-Japanese-32dim) | 固定 revision，未散布權重 |
| DACVAE 程式 | [facebookresearch/dacvae](https://github.com/facebookresearch/dacvae/tree/414c20785fc3a28373073ea8ef7a1316eeeaca6e) | 使用固定上游依賴 commit，條款以該來源為準 |
| SilentCipher | [SesameAILabs/silentcipher](https://github.com/SesameAILabs/silentcipher/tree/d46d7d0893a583d8968ab3a6626e2289faec9152)、[sony/silentcipher 模型](https://huggingface.co/sony/silentcipher) | 官方浮水印仍為語音完成的必要條件；程式與權重各自有條款 |
| Codex CLI | [openai/codex](https://github.com/openai/codex) | 外部安裝、外部登入，不附帶憑證或訂閱權利 |

PyTorch、Transformers、PEFT、Accelerate、SoundFile 等 Python 依賴由固定上游 `uv.lock`／保留的 requirements 匯出描述，不把 site-packages 放進 Git。使用者自己的 adapter、reference latent、資料集、聲音與原始音訊的權利需自行確認；本儲存庫不提供它們，也沒有授予模仿特定人物或角色聲音的權利。
