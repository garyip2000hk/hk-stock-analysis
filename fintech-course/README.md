# 港股財技入門課程（互動教學模組）

一個完整、獨立嘅 React 互動教學模組，教學生認識港股 10 大財技（供股、配售、要約、CB、回購、合股、拆股、送紅股、私有化、派息）嘅財技邏輯同對股權嘅影響。

同 `garysir.zo.space/fintech-course` 線上版同步。

## 功能

- **10 個財技 module**，每個包括：
  - 點解公司要咁做（財技邏輯）
  - 對股票嘅影響（好定壞、買入定避開、關鍵指標）
  - 實戰案例（用文字描述，方便改成真實港股案例）
  - **互動模擬器** — 學生自己輸入條件（例如供股折讓、1供幾、跟供率），即時見到：
    - 圓形儀錶：歸邊風險評分（0–100）
    - 股權圓環圖：大股東 vs 小股東「之前 → 之後」
    - 方向 badge（正面／負面）
    - 動態文字解讀
- **Quiz** — 5 條選擇題，考完顯示成績
- **學習進度** — 完成一個 module 會打勾
- 全部 SVG 圖表手寫，無需 chart library

## 檔案

| 檔案 | 用途 |
|---|---|
| `FinTechCourse.tsx` | 完整 React 組件（約 1500 行，default export `FintechCourse`）|

## 依賴

- React 18+
- `lucide-react`（圖示）
- Tailwind CSS（部分排版 class，例如 `text-balance`、間距 flex/grid）

模組內所有顏色都係用 CSS variables（`--page-accent` 等）inline style 定義，所以即使冇完整 Tailwind theme 都 render 到。

## 合併到 Web App

### Vite / React

```bash
cp fintech-course/FinTechCourse.tsx src/pages/
# 確保有 lucide-react
npm install lucide-react
```

```tsx
// router 入面
import FintechCourse from "./pages/FinTechCourse";

<Route path="/fintech-course" element={<FintechCourse />} />
```

如果個 app 冇用 Tailwind，可以加返基本 utility class，或者將模組入面少量 Tailwind class（主要係 `mt-*`、`space-y-*`、`text-balance`）改做 inline style。

### Next.js

擺喺 `app/fintech-course/page.tsx` 或者 `pages/fintech-course.tsx`，頂部加 `"use client";`（因為用咗 useState / useEffect）。

## 合併到手機 App

組件係標準 React hooks + SVG，有兩條路：

1. **React Native + WebView（最快）** — 直接載入 `https://garysir.zo.space/fintech-course`，一個 WebView screen 搞掂，更新課程內容唔使出 app update。
2. **React Native 原生移植** — 邏輯層（`MODULES` 數據、`simulate` functions、quiz state）可以原封不動搬過去；UI 層要將 `div/span/input` 換做 `View/Text/TextInput`，SVG 圖表改用 `react-native-svg`。所有計算邏輯（風險評分、股權變化、解讀文字）都係純函數，可以直接重用。

## 數據來源

課程內容根據本 repo 嘅財技分析系統（`corp_scanner.py` 10 大財技分類、`signal_weights.py` 評分邏輯）設計，模擬器嘅評分方向同正式分析系統一致。
