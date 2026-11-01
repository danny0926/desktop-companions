二乃像素桌寵

完整的繁體中文啟動、互動、設定、語音安裝、隱私與架構說明請見儲存庫根目錄 README.md。

無模型啟動：
  python -m desktop_pet --no-voice --chat

雙擊根目錄 Start Nino.cmd 亦可啟動（預設不載入語音）。
可選語音請先執行 python tools/setup_voice.py --check，依 README 設定個人模型後使用啟動器 -Voice。

本目錄保存完整外殼、行為、畫面觀察、角色 agent、語音 facade、測試與兩套素材。所有私人執行資料保留於忽略的 outputs/，不隨儲存庫散布。

Markdown／手動圖片：聊天工具列可插入圖片、粗體、斜體並預覽；選用套件安裝 python -m pip install -r desktop_pet/requirements-chat.txt。文字與圖片可交錯，只有圖片也能傳送；本機附件依角色保存，最多4張／每張原檔12MB，網址圖片需先下載再選取。詳細語法與限制見根目錄 README.md。

對話與記憶：聊天標題或右鍵選單可開啟。重新啟動會恢復最近30筆紀錄；較舊內容在歷史頁以每頁20筆翻閱。重要記憶需明確新增，也可選取刪除。SQLite 依角色與對話 ID 隔離，紀錄與圖片只保存在被 Git 忽略的本機 outputs；完整說明見根目錄 README.md。
