import { useEffect, useState } from "react";
import {
  BookOpen,
  TrendingDown,
  TrendingUp,
  AlertTriangle,
  DollarSign,
  Repeat,
  Split,
  Gift,
  Building2,
  Banknote,
  Shield,
  ChevronDown,
  ChevronUp,
  ArrowRight,
  Lightbulb,
  Target,
  Zap,
  FlaskConical,
} from "lucide-react";

const theme = {
  background: "#0a0a0f",
  foreground: "#e8e4dc",
  card: "rgba(18, 18, 28, 0.92)",
  cardBorder: "rgba(255, 255, 255, 0.06)",
  muted: "#8a8780",
  accent: "#d4a853",
  accentMuted: "rgba(212, 168, 83, 0.12)",
  red: "#e05555",
  redMuted: "rgba(224, 85, 85, 0.12)",
  green: "#4caf7d",
  greenMuted: "rgba(76, 175, 125, 0.12)",
  yellow: "#d4a853",
  yellowMuted: "rgba(212, 168, 83, 0.12)",
  blue: "#6b9bd2",
  blueMuted: "rgba(107, 155, 210, 0.12)",
};

const modules = [
  {
    id: "rights_issue",
    icon: TrendingDown,
    title: "供股 (Rights Issue)",
    signal: "🔴 高風險財技",
    signalColor: "red",
    summary: "公司向現有股東按比例發行新股，通常以折讓價進行。係港股最常見嘅向下炒工具。",
    choyLogic: {
      title: "財技邏輯",
      points: [
        "大股東利用供股去攤薄唔跟供嘅小股東，壓低股價後再喺低位收集平貨",
        "折讓越大（>30%）、供股比例越大（如 1 供 4），向下炒訊號越強",
        "如果供股前先合股令股價「表面」上升，然後再大折讓供股 → 經典向下炒 pattern",
        "供股集資用途含糊（「一般營運資金」）係紅色旗幟",
      ],
    },
    impact: {
      title: "對股票嘅影響",
      points: [
        "短期：股價通常受壓，尤其供股權交易期間",
        "中期：CCASS 集中度可能上升（大股東收集散戶沽出嘅供股權）",
        "長期：如果供股後有實質業務發展，可能翻身；否則只係攤薄遊戲",
      ],
      direction: "bearish",
    },
    example: "經典案例：細價股先 10 合 1，然後 1 供 4，供股價較市價折讓 60%。唔跟供嘅小股東持股被大幅攤薄。",
    scoreHint: "FinTech Score: 深折讓供股 +30 分 | 大比例供股 +10 分",
  },
  {
    id: "placing",
    icon: Target,
    title: "配售 / 配股 (Placing)",
    signal: "🔴 注意歸邊",
    signalColor: "red",
    summary: "公司向特定投資者發行新股，唔係向全體股東。關鍵係睇「配俾邊個」同「折讓幾多」。",
    choyLogic: {
      title: "財技邏輯",
      points: [
        "如果配售對象係「自己人」或關連人士 → 歸邊信號，股權進一步集中",
        "大折讓配售（>20%）→ 可能係向下炒前奏，壓低股價等自己人接平貨",
        "「先舊後新」(Top-up Placing)：大股東先沽自己貨，再配新股補返，係減持信號",
        "配售後股價若迅速反彈 → 可能係「貨已歸邊」嘅確認信號",
      ],
    },
    impact: {
      title: "對股票嘅影響",
      points: [
        "短期：配售價通常低於市價，股價可能向配售價靠攏",
        "中期：睇配售對象——如果係長線基金接貨係正面，如果係「人頭」就係歸邊",
        "長期：多次頻繁配售係極危險信號，代表公司不斷印股票吸水",
      ],
      direction: "bearish",
    },
    example: "某細價股一年內進行 3 次配售，每次折讓 15-20%，配售對象為 BVI 公司。股價由 $2 跌至 $0.3。",
    scoreHint: "FinTech Score: 大折讓配售 +20 分 | 多次配售 +15 分",
  },
  {
    id: "general_offer",
    icon: Building2,
    title: "要約 / 全購 (General Offer)",
    signal: "🟡 視乎價格",
    signalColor: "yellow",
    summary: "大股東向所有股東提出收購要約，可能係全面收購或強制性現金要約。關鍵係要約價 vs 資產淨值。",
    choyLogic: {
      title: "財技邏輯",
      points: [
        "低價要約（<$5 或低於 NAV）→ 大股東想賤價吞埋剩餘股份",
        "高溢價要約（>50%溢價）→ 可能有「寶藏」未被市場發現",
        "強制性現金要約（觸發收購守則 30% 門檻）→ 大股東被動出手，可能唔想但被迫",
        "要約後若保留上市地位 → 股權可能極度集中（大股東 >75%）",
      ],
    },
    impact: {
      title: "對股票嘅影響",
      points: [
        "短期：股價通常升至接近要約價",
        "中期：如果要約失敗，股價可能跌返原位或更低",
        "長期：成功全購後可能私有化，小股東被迫離場",
      ],
      direction: "neutral",
    },
    example: "大股東提出 $2.5 全購，較市價溢價 30%，但公司 NAV 約 $6。小股東若接受等於半價賤賣資產。",
    scoreHint: "FinTech Score: 低價要約 +25 分 | 全面要約 +10 分",
  },
  {
    id: "cb",
    icon: Repeat,
    title: "可換股債券 (CB)",
    signal: "🟡 睇換股價",
    signalColor: "yellow",
    summary: "公司發行債券，持有人可喺特定條件下轉換為股票。低換股價嘅 CB 係潛在嘅攤薄炸彈。",
    choyLogic: {
      title: "財技邏輯",
      points: [
        "換股價越低 → CB 持有人（通常係大股東或關連方）越有動機壓低股價去換股",
        "低於市價嘅換股價代表 CB 持有人可低價入股，攤薄現有股東",
        "CB 到期前可能有壓價動機——壓低股價令換股更划算",
        "如果 CB 條款複雜（強制轉換、重訂換股價）→ 要極度小心",
      ],
    },
    impact: {
      title: "對股票嘅影響",
      points: [
        "短期：CB 公布後股價通常受壓",
        "中期：換股期間大量新股流出，造成持續性沽壓",
        "長期：如果 CB 換晒做股票，股本大幅膨脹，EPS 被攤薄",
      ],
      direction: "bearish",
    },
    example: "公司發行 $5000 萬 CB，換股價 $0.5（較市價折讓 40%）。CB 到期前股價被壓至 $0.3，持有人換股後即賺。",
    scoreHint: "FinTech Score: 低換股價 CB +20 分",
  },
  {
    id: "buyback",
    icon: TrendingUp,
    title: "回購 (Buyback)",
    signal: "🟢 正面信號",
    signalColor: "green",
    summary: "公司用現金喺市場回購自己嘅股票。通常代表管理層覺得股價被低估，願意用真金白銀支持。",
    choyLogic: {
      title: "財技邏輯",
      points: [
        "真金白銀回購 = 管理層對公司有信心，願意用公司錢投票",
        "持續性回購（每日回購而非一次過）比單次大型回購更有說服力",
        "如果回購量少、只係做樣 → 可能只係想制造虛假需求，托住股價",
        "回購後註銷股份 → EPS 提升，對長線股東有利",
      ],
    },
    impact: {
      title: "對股票嘅影響",
      points: [
        "短期：回購行動本身提供買盤支持，股價通常穩定或上升",
        "中期：流通股減少，莊家較難操控股價",
        "長期：持續回購 + 業務增長 = 最佳組合",
        "但要留意：如果公司借錢回購，可能係財技而非實力",
      ],
      direction: "bullish",
    },
    example: "某科技公司連續 20 個交易日回購，每日回購金額約 $500 萬。股價由 $15 升至 $22，漲幅 47%。",
    scoreHint: "回購係正面信號，唔會加分到 FinTech 歸邊分數。",
  },
  {
    id: "consolidation",
    icon: AlertTriangle,
    title: "合股 (Share Consolidation)",
    signal: "🔴 高風險警告",
    signalColor: "red",
    summary: "將多股合併為一股（如 10 合 1），股價表面上升但公司價值不變。幾乎必定係向下炒嘅第一步。",
    choyLogic: {
      title: "財技邏輯",
      points: [
        "合股本身唔改變公司價值，只係數字遊戲——10 張 $1 紙變 1 張 $10 紙",
        "合股後股價「上升」，然後再大折讓供股/配股 → 經典向下炒套路",
        "合股目的通常係：避免股價低於 $0.01 被交易所要求停牌/除牌",
        "合股 + 供股組合 = FinTech Score 直接 +25 分，係最危險嘅財技組合",
      ],
    },
    impact: {
      title: "對股票嘅影響",
      points: [
        "短期：合股後股價表面上升，但持股市值不變",
        "中期：合股後通常伴隨其他財技（供股/配售），股價往往下跌",
        "長期：歷史數據顯示，大部分合股股票一年後股價低於合股前水平",
      ],
      direction: "bearish",
    },
    example: "典型套路：10 合 1（股價由 $0.05 → $0.50），然後 1 供 5 大折讓供股（供股價 $0.10）。唔跟供嘅小股東被徹底攤薄。",
    scoreHint: "FinTech Score: 合股 +15 分 | 合股+供股組合 +25 分（疊加）",
  },
  {
    id: "split",
    icon: Split,
    title: "拆股 (Share Split)",
    signal: "🟢 一般正面",
    signalColor: "green",
    summary: "將一股拆成多股（如 1 拆 5），股價按比例下降，入場費降低。通常係股價已大升嘅公司先用。",
    choyLogic: {
      title: "財技邏輯",
      points: [
        "拆股令每手入場費下降，增加股票流動性，方便散戶參與",
        "通常係股價已經大幅上升嘅公司先會拆股（如由 $500 拆到 $100）",
        "拆股本身唔改變公司基本面，但反映管理層對前景有信心",
        "細價股拆股好罕見——如果係細價股拆股，要問「點解要拆？」",
      ],
    },
    impact: {
      title: "對股票嘅影響",
      points: [
        "短期：拆股後流動性增加，股價通常正面反應",
        "中期：更多散戶參與，股東基礎擴大，莊家較難操控",
        "長期：視乎公司基本面，拆股本身唔影響長期價值",
      ],
      direction: "bullish",
    },
    example: "某科技龍頭股 1 拆 5，拆股前股價 $500，拆股後 $100。散戶入場門檻大降，成交量明顯上升。",
    scoreHint: "拆股係中性/正面，唔會加分到 FinTech 歸邊分數。",
  },
  {
    id: "bonus_issue",
    icon: Gift,
    title: "送紅股 (Bonus Issue)",
    signal: "🟡 小心糖衣毒藥",
    signalColor: "yellow",
    summary: "公司向股東免費派送額外股份（如 10 送 1 紅股）。單獨睇係中性，但配合其他財技就要極度小心。",
    choyLogic: {
      title: "財技邏輯",
      points: [
        "送紅股只係數字遊戲——公司唔需要付出現金，只係將股本重組",
        "通常用作「甜頭」，吸引股東支持另一項財技（如供股）",
        "「糖衣毒藥」pattern：送紅股 → 股價短暫上升 → 然後宣布供股/配售",
        "如果送紅股比例異常地高（如 1 送 5）→ 要問「點解要咁慷慨？」",
      ],
    },
    impact: {
      title: "對股票嘅影響",
      points: [
        "短期：股價可能因「免費送股」概念短暫上升",
        "中期：如果伴隨其他財技宣布，股價通常回落",
        "長期：紅股本身唔影響公司價值，股價會按比例自動調整",
      ],
      direction: "neutral",
    },
    example: "公司宣布 10 送 1 紅股，股價短暫上升 5%。兩星期後宣布 1 供 3 大折讓供股。紅股 = 氹你入局嘅甜頭。",
    scoreHint: "FinTech Score: 紅股+其他財技 +10 分 | 多項財技組合 +15 分",
  },
  {
    id: "privatization",
    icon: Shield,
    title: "私有化 (Privatization)",
    signal: "🟡 睇出價",
    signalColor: "yellow",
    summary: "大股東用現金買晒所有股份，將公司由上市公司變為私人公司。出價 vs 資產淨值係唯一關鍵。",
    choyLogic: {
      title: "財技邏輯",
      points: [
        "大股東提出私有化，通常係因為覺得公司價值被市場低估",
        "要約價 vs 每股資產淨值(NAV)嘅折讓/溢價係核心指標",
        "如果出價低於 NAV → 大股東想賤價吞晒小股東嘅資產",
        "如果出價高溢價（>50%）→ 大股東可能發現咗市場未發現嘅價值",
        "私有化失敗後股價通常大跌——因為市場發現有人想賤價收購",
      ],
    },
    impact: {
      title: "對股票嘅影響",
      points: [
        "短期：股價升至接近要約價水平",
        "中期：私有化過程中股價會跟要約價掛鉤",
        "長期：成功私有化後股份被註銷；失敗則股價可能大跌",
        "要留意：私有化需要 75% 獨立股東同意 + <10% 反對",
      ],
      direction: "neutral",
    },
    example: "大股東提出 $3.0 私有化（較市價溢價 25%），但公司 NAV 約 $5.5。獨立股東否決後，股價跌回 $2.2。",
    scoreHint: "私有化唔直接加分到 FinTech Score，但要評估出價是否合理。",
  },
  {
    id: "dividend",
    icon: Banknote,
    title: "派息 (Dividend)",
    signal: "🟢 正面信號",
    signalColor: "green",
    summary: "公司將盈利以現金分派俾股東。真金白銀派息係最誠實嘅信號——公司有現金流先派到息。",
    choyLogic: {
      title: "財技邏輯",
      points: [
        "現金派息 = 公司有真實現金流，唔係紙上富貴",
        "穩定派息歷史 >3 年代表公司有持續盈利能力",
        "突然大增派息要小心——可能係想推高股價趁機出貨",
        "派息比率 >80% 可能不可持續；<20% 可能太慳",
        "特別股息（一次性）vs 常規股息——前者可能係賣資產後派錢",
      ],
    },
    impact: {
      title: "對股票嘅影響",
      points: [
        "短期：除淨日前後股價會按股息額調整（除淨效應）",
        "中期：穩定派息吸引長線投資者，股價通常較穩定",
        "長期：持續增加派息嘅公司通常股價表現優於大市",
        "但要留意：借錢派息係紅色旗幟——財技而非實力",
      ],
      direction: "bullish",
    },
    example: "某公用股連續 10 年每年增加派息 5-10%。股價 10 年間由 $30 升至 $80，總回報（連股息）超過 200%。",
    scoreHint: "派息係基本面正面信號，唔會加分到 FinTech 歸邊分數。",
  },
];

const signalColors: Record<string, { bg: string; text: string; border: string }> = {
  red: { bg: theme.redMuted, text: theme.red, border: "rgba(224,85,85,0.3)" },
  green: { bg: theme.greenMuted, text: theme.green, border: "rgba(76,175,125,0.3)" },
  yellow: { bg: theme.yellowMuted, text: theme.yellow, border: "rgba(212,168,83,0.3)" },
};

// ============ Interactive Simulator ============

type SimInput =
  | { key: string; label: string; kind: "slider"; min: number; max: number; step: number; unit: string; def: number }
  | { key: string; label: string; kind: "select"; options: { value: string; label: string }[]; def: string }
  | { key: string; label: string; kind: "toggle"; def: boolean };

type SimValues = Record<string, number | string | boolean>;

interface SimResult {
  score: number;
  direction: "bullish" | "bearish" | "neutral";
  majorBefore: number;
  majorAfter: number;
  minorityNote: string;
  bullets: string[];
}

interface SimConfig {
  inputs: SimInput[];
  calc: (v: SimValues) => SimResult;
}

const simConfigs: Record<string, SimConfig> = {
  rights_issue: {
    inputs: [
      { key: "discount", label: "供股價折讓幅度", kind: "slider", min: 0, max: 80, step: 5, unit: "%", def: 40 },
      { key: "ratio", label: "供股比例", kind: "select", options: [
        { value: "0.25", label: "4 供 1" },
        { value: "0.5", label: "2 供 1" },
        { value: "1", label: "1 供 1" },
        { value: "2", label: "1 供 2" },
        { value: "4", label: "1 供 4" },
      ], def: "1" },
      { key: "takeup", label: "小股東跟供率", kind: "slider", min: 0, max: 100, step: 10, unit: "%", def: 30 },
      { key: "consolidated", label: "供股前先合股", kind: "toggle", def: false },
    ],
    calc: (v) => {
      const discount = Number(v.discount);
      const ratio = Number(v.ratio);
      const takeup = Number(v.takeup) / 100;
      const cons = Boolean(v.consolidated);
      let score = 10;
      if (discount >= 30) score += 30; else if (discount >= 15) score += 15;
      if (ratio >= 2) score += 15; else if (ratio >= 1) score += 10;
      if (cons) score += 25;
      if (takeup <= 0.3) score += 10;
      score = Math.min(100, score);
      const majorBefore = 40;
      const majorNew = majorBefore * (1 + ratio);
      const minorityNew = 60 * (1 - takeup) + 60 * takeup * (1 + ratio);
      const total = majorNew + minorityNew;
      const majorAfter = Math.round((majorNew / total) * 100);
      const bullets: string[] = [];
      bullets.push(
        discount >= 30
          ? `折讓 ${discount}% 屬深折讓，唔跟供嘅小股東權益即刻被攤薄`
          : `折讓 ${discount}% 尚算溫和，但仍要留意集資用途`
      );
      if (takeup <= 0.3) bullets.push(`跟供率只有 ${Number(v.takeup)}%：大部分小股東唔跟，股權快速向大股東集中`);
      else if (takeup <= 0.6) bullets.push(`跟供率 ${Number(v.takeup)}%：部分小股東被攤薄`);
      else bullets.push(`跟供率 ${Number(v.takeup)}% 較高：攤薄效應相對細`);
      if (cons) bullets.push("先合股後供股：經典向下炒組合，FinTech Score 額外 +25");
      if (discount >= 30 && takeup <= 0.3) bullets.push("大股東可以用極低價收集小股東放棄嘅供股權，完成歸邊");
      return {
        score, direction: "bearish", majorBefore, majorAfter,
        minorityNote: `小股東持股由 ${100 - majorBefore}% → ${100 - majorAfter}%`,
        bullets,
      };
    },
  },
  placing: {
    inputs: [
      { key: "discount", label: "配售價折讓幅度", kind: "slider", min: 0, max: 50, step: 5, unit: "%", def: 20 },
      { key: "insider", label: "配售對象", kind: "select", options: [
        { value: "independent", label: "獨立第三方" },
        { value: "unknown", label: "背景不明 (BVI 公司)" },
        { value: "insider", label: "大股東關連人士" },
      ], def: "unknown" },
      { key: "times", label: "一年內配售次數", kind: "slider", min: 1, max: 6, step: 1, unit: " 次", def: 1 },
      { key: "size", label: "配售規模（佔已發行股本）", kind: "slider", min: 5, max: 50, step: 5, unit: "%", def: 20 },
    ],
    calc: (v) => {
      const discount = Number(v.discount);
      const insider = String(v.insider);
      const times = Number(v.times);
      const size = Number(v.size);
      let score = 5;
      if (discount >= 20) score += 20; else if (discount >= 10) score += 10;
      if (insider === "insider") score += 30; else if (insider === "unknown") score += 15;
      if (times >= 3) score += 15; else if (times === 2) score += 8;
      if (size >= 30) score += 10;
      score = Math.min(100, score);
      const majorBefore = 40;
      const newSharesToMajor = insider === "insider" ? size : insider === "unknown" ? size * 0.7 : size * 0.1;
      const totalAfter = 100 + size;
      const majorAfter = Math.round(((majorBefore + newSharesToMajor) / totalAfter) * 100);
      const bullets: string[] = [];
      if (insider === "insider") bullets.push("配售俾大股東關連人士：變相由大股東低價增持，歸邊訊號極強");
      else if (insider === "unknown") bullets.push("配售俾背景不明嘅 BVI 公司：要查 CCASS 睇貨最終去咗邊度");
      else bullets.push("配售俾真正獨立第三方：歸邊風險較低，但仍要留意折讓");
      if (discount >= 20) bullets.push(`折讓 ${discount}% 屬大折讓配售：接貨方即賺，疑似壓價益自己人`);
      if (times >= 3) bullets.push(`一年配 ${times} 次：公司不斷印股票吸水，股價長期受壓`);
      if (size >= 30) bullets.push(`一次過配 ${size}% 股本：攤薄效應大，小股東權益即時縮水`);
      return {
        score, direction: "bearish", majorBefore, majorAfter,
        minorityNote: `小股東持股由 ${100 - majorBefore}% → ${100 - majorAfter}%`,
        bullets,
      };
    },
  },
  general_offer: {
    inputs: [
      { key: "offerVsPrice", label: "要約價 vs 市價溢價", kind: "slider", min: -30, max: 80, step: 5, unit: "%", def: 20 },
      { key: "navDiscount", label: "要約價 vs 每股 NAV 折讓", kind: "slider", min: 0, max: 80, step: 5, unit: "%", def: 50 },
      { key: "type", label: "要約類型", kind: "select", options: [
        { value: "mandatory", label: "強制性要約（觸發 30% 門檻）" },
        { value: "voluntary", label: "自願全面要約" },
        { value: "partial", label: "部分要約" },
      ], def: "voluntary" },
      { key: "keepListing", label: "要約後保留上市地位", kind: "toggle", def: true },
    ],
    calc: (v) => {
      const premium = Number(v.offerVsPrice);
      const navDisc = Number(v.navDiscount);
      const type = String(v.type);
      const keepListing = Boolean(v.keepListing);
      let score = 10;
      if (navDisc >= 50) score += 25; else if (navDisc >= 30) score += 15;
      if (premium <= 10) score += 15;
      if (type === "mandatory") score += 10;
      if (keepListing) score += 10;
      score = Math.min(100, score);
      const majorBefore = 40;
      const acceptRate = premium >= 30 ? 0.6 : premium >= 15 ? 0.4 : 0.2;
      const majorAfter = Math.round(majorBefore + 60 * acceptRate);
      const bullets: string[] = [];
      if (navDisc >= 50) bullets.push(`要約價較 NAV 折讓 ${navDisc}%：大股東想賤價買走小股東手上嘅資產`);
      else if (navDisc >= 30) bullets.push(`要約價較 NAV 折讓 ${navDisc}%：作價偏低，小股東唔划算`);
      else bullets.push(`要約價接近 NAV（折讓 ${navDisc}%）：作價相對公道`);
      if (premium >= 30) bullets.push(`較市價溢價 ${premium}%：短期股價會向要約價靠攏，有套利空間`);
      else if (premium < 10) bullets.push(`溢價只有 ${premium}%：大股東冇誠意，股價可能唔升`);
      if (type === "mandatory") bullets.push("強制性要約：大股東係被規則逼出手，唔一定想收購");
      if (keepListing && majorAfter > 75) bullets.push(`要約後大股東持股 ${majorAfter}% 超過 75%：公眾持股量不足，可能面臨除牌風險`);
      return {
        score, direction: "neutral", majorBefore, majorAfter,
        minorityNote: `小股東持股由 ${100 - majorBefore}% → ${100 - majorAfter}%`,
        bullets,
      };
    },
  },
  cb: {
    inputs: [
      { key: "convDiscount", label: "換股價 vs 現價折讓", kind: "slider", min: -20, max: 60, step: 5, unit: "%", def: 30 },
      { key: "size", label: "CB 規模（全數換股佔現有股本）", kind: "slider", min: 5, max: 100, step: 5, unit: "%", def: 30 },
      { key: "holder", label: "CB 持有人", kind: "select", options: [
        { value: "independent", label: "獨立投資者" },
        { value: "insider", label: "大股東/關連方" },
      ], def: "insider" },
      { key: "resetClause", label: "設有換股價重訂條款", kind: "toggle", def: false },
    ],
    calc: (v) => {
      const convDisc = Number(v.convDiscount);
      const size = Number(v.size);
      const holder = String(v.holder);
      const reset = Boolean(v.resetClause);
      let score = 5;
      if (convDisc >= 30) score += 20; else if (convDisc >= 15) score += 10;
      if (convDisc < 0) score -= 5;
      if (size >= 50) score += 25; else if (size >= 25) score += 15;
      if (holder === "insider") score += 20;
      if (reset) score += 15;
      score = Math.min(100, Math.max(0, score));
      const majorBefore = 40;
      const newToMajor = holder === "insider" ? size : 0;
      const majorAfter = Math.round(((majorBefore + newToMajor) / (100 + size)) * 100);
      const bullets: string[] = [];
      if (convDisc >= 30) bullets.push(`換股價折讓 ${convDisc}%：持有人有強烈動機壓低股價後低位換股`);
      else if (convDisc < 0) bullets.push(`換股價高於現價 ${-convDisc}%：持有人冇即時換股動機，相對安全`);
      else bullets.push(`換股價折讓 ${convDisc}%：持有人有壓價誘因，留意股價走勢`);
      if (size >= 50) bullets.push(`全數換股會令股本膨脹 ${size}%：EPS 嚴重攤薄，屬潛在攤薄炸彈`);
      if (holder === "insider") bullets.push("CB 由大股東/關連方持有：等於預先低價攞貨，完成後股權歸邊");
      if (reset) bullets.push("設有重訂條款：股價跌得越多換股價越低，形成向下螺旋");
      return {
        score, direction: "bearish", majorBefore, majorAfter,
        minorityNote: `小股東持股由 ${100 - majorBefore}% → ${100 - majorAfter}%`,
        bullets,
      };
    },
  },
  buyback: {
    inputs: [
      { key: "dailyPct", label: "每日回購金額佔日均成交", kind: "slider", min: 1, max: 50, step: 1, unit: "%", def: 10 },
      { key: "days", label: "連續回購日數", kind: "slider", min: 1, max: 40, step: 1, unit: " 日", def: 10 },
      { key: "funding", label: "回購資金來源", kind: "select", options: [
        { value: "cash", label: "自有現金" },
        { value: "debt", label: "借貸" },
      ], def: "cash" },
      { key: "cancel", label: "回購後註銷股份", kind: "toggle", def: true },
    ],
    calc: (v) => {
      const dailyPct = Number(v.dailyPct);
      const days = Number(v.days);
      const funding = String(v.funding);
      const cancel = Boolean(v.cancel);
      let score = 0;
      score += Math.min(30, days);
      if (dailyPct >= 20) score += 20; else if (dailyPct >= 10) score += 12; else score += 5;
      if (funding === "debt") score -= 15;
      if (cancel) score += 15;
      score = Math.min(100, Math.max(0, score));
      const majorBefore = 40;
      const totalBought = Math.min(20, (dailyPct / 100) * days * 2);
      const totalAfter = cancel ? 100 - totalBought : 100;
      const majorAfter = Math.round((majorBefore / totalAfter) * 100);
      const bullets: string[] = [];
      if (days >= 15) bullets.push(`連續 ${days} 日回購：持續性買盤代表管理層真正睇好，唔係做樣`);
      else if (days < 5) bullets.push(`只回購咗 ${days} 日：可能只係托價做樣，說服力有限`);
      if (funding === "debt") bullets.push("⚠️ 借錢回購：可能係財技操作，要小心公司真實財政狀況");
      else bullets.push("用自有現金回購：公司真係有錢，信號較可信");
      if (cancel) bullets.push(`回購後註銷：總股數減少，每股 EPS 提升，長線股東直接受惠`);
      else bullets.push("回購後唔註銷（庫存股）：日後可以再賣返出嚟，利好打折扣");
      if (dailyPct >= 20) bullets.push(`每日回購佔成交 ${dailyPct}%：對股價有實質支持作用`);
      return {
        score, direction: "bullish", majorBefore, majorAfter,
        minorityNote: `小股東持股由 ${100 - majorBefore}% → ${100 - majorAfter}%（被動上升）`,
        bullets,
      };
    },
  },
  consolidation: {
    inputs: [
      { key: "ratio", label: "合股比例", kind: "select", options: [
        { value: "2", label: "2 合 1" },
        { value: "5", label: "5 合 1" },
        { value: "10", label: "10 合 1" },
        { value: "20", label: "20 合 1" },
        { value: "50", label: "50 合 1" },
      ], def: "10" },
      { key: "priceBefore", label: "合股前股價", kind: "slider", min: 0.01, max: 0.5, step: 0.01, unit: " 元", def: 0.05 },
      { key: "followUp", label: "合股後有冇後續財技", kind: "select", options: [
        { value: "none", label: "冇後續動作" },
        { value: "rights", label: "隨後宣布供股" },
        { value: "placing", label: "隨後宣布配售" },
      ], def: "rights" },
    ],
    calc: (v) => {
      const ratio = Number(v.ratio);
      const price = Number(v.priceBefore);
      const followUp = String(v.followUp);
      let score = 15;
      if (ratio >= 10) score += 15; else if (ratio >= 5) score += 8;
      if (price <= 0.05) score += 15; else if (price <= 0.1) score += 8;
      if (followUp === "rights") score += 30; else if (followUp === "placing") score += 25;
      score = Math.min(100, score);
      const majorBefore = 40;
      const majorAfter = followUp === "rights" ? majorBefore + 12 : followUp === "placing" ? majorBefore + 8 : majorBefore;
      const bullets: string[] = [];
      bullets.push(`合股本身唔改變股權比例：大股東 ${majorBefore}% 持股，合股前後完全一樣`);
      bullets.push(`${ratio} 合 1 後股價「上升」至約 $${(price * ratio).toFixed(2)}：純粹數字遊戲，公司價值不變`);
      if (followUp === "rights") bullets.push("⚠️ 合股後隨即供股：經典向下炒套路完成，小股東將被大幅攤薄");
      else if (followUp === "placing") bullets.push("⚠️ 合股後隨即配售：股價表面「高咗」，配股折讓空間更大");
      else bullets.push("暫時冇後續財技：但仍要密切留意未來 3-6 個月公告");
      if (price <= 0.05) bullets.push(`合股前股價 $${price} 接近 $0.01 下限：合股係為咗避免被除牌`);
      return {
        score, direction: "bearish", majorBefore, majorAfter,
        minorityNote: `小股東持股由 ${100 - majorBefore}% → ${100 - majorAfter}%`,
        bullets,
      };
    },
  },
  split: {
    inputs: [
      { key: "ratio", label: "拆股比例", kind: "select", options: [
        { value: "2", label: "1 拆 2" },
        { value: "5", label: "1 拆 5" },
        { value: "10", label: "1 拆 10" },
      ], def: "5" },
      { key: "priceBefore", label: "拆股前股價", kind: "slider", min: 10, max: 1000, step: 10, unit: " 元", def: 500 },
      { key: "reason", label: "拆股原因", kind: "select", options: [
        { value: "liquidity", label: "增加流通量" },
        { value: "unknown", label: "原因不明（細價股拆股）" },
      ], def: "liquidity" },
    ],
    calc: (v) => {
      const ratio = Number(v.ratio);
      const price = Number(v.priceBefore);
      const reason = String(v.reason);
      let score = 10;
      if (reason === "liquidity" && price >= 100) score += 30;
      if (reason === "unknown") score += 10;
      score = Math.min(100, score);
      const majorBefore = 40;
      const majorAfter = 40;
      const bullets: string[] = [];
      bullets.push(`拆股唔改變任何股權比例：大股東 ${majorBefore}% 完全不變`);
      bullets.push(`拆股後股價變為約 $${(price / ratio).toFixed(2)}，入場費大降，散戶更容易參與`);
      if (reason === "liquidity" && price >= 100) bullets.push("高價股拆細係正常操作：增加流通量，通常係管理層對前景有信心嘅表現");
      if (reason === "unknown") bullets.push("⚠️ 細價股無啦啦拆股好罕見：要問「點解要拆？」可能係配合其他財技");
      if (price < 100 && reason === "liquidity") bullets.push("股價本身唔算高都拆股：留意係咪想制造「平貨」假象吸引散戶");
      return {
        score, direction: "bullish", majorBefore, majorAfter,
        minorityNote: `小股東持股維持 ${100 - majorBefore}%（不變）`,
        bullets,
      };
    },
  },
  bonus_issue: {
    inputs: [
      { key: "ratio", label: "送股比例", kind: "select", options: [
        { value: "0.1", label: "10 送 1" },
        { value: "0.3", label: "10 送 3" },
        { value: "0.5", label: "10 送 5" },
        { value: "1", label: "1 送 1" },
        { value: "5", label: "1 送 5" },
      ], def: "0.1" },
      { key: "followUp", label: "之後有冇其他財技", kind: "select", options: [
        { value: "none", label: "冇後續動作" },
        { value: "rights", label: "兩星期後宣布供股" },
        { value: "placing", label: "一個月後宣布配售" },
      ], def: "rights" },
      { key: "cashAlternative", label: "公司有能力派現金但選擇送股", kind: "toggle", def: false },
    ],
    calc: (v) => {
      const ratio = Number(v.ratio);
      const followUp = String(v.followUp);
      const cashAlt = Boolean(v.cashAlternative);
      let score = 5;
      if (ratio >= 1) score += 15; else if (ratio >= 0.5) score += 8;
      if (followUp === "rights") score += 35; else if (followUp === "placing") score += 30;
      if (cashAlt) score += 10;
      score = Math.min(100, score);
      const majorBefore = 40;
      const majorAfter = followUp === "rights" ? majorBefore + 10 : followUp === "placing" ? majorBefore + 6 : majorBefore;
      const bullets: string[] = [];
      bullets.push(`送紅股本身唔改變股權比例：人人按持股比例送股，大股東 ${majorBefore}% 不變`);
      bullets.push("送股唔涉及現金：公司只係將儲備轉做股本，係純粹嘅數字遊戲");
      if (followUp === "rights") bullets.push("🍬☠️ 糖衣毒藥確認：紅股引你入局，供股先係真正目的——唔跟供就被攤薄");
      else if (followUp === "placing") bullets.push("🍬☠️ 糖衣毒藥確認：紅股推升股價後高位配售，大股東順利套現");
      else bullets.push("暫時冇後續財技：但仍要留意未來 1-2 個月公告");
      if (ratio >= 1) bullets.push(`${ratio === 1 ? "1 送 1" : "1 送 5"} 比例異常高：「點解要咁慷慨？」通常背後有動機`);
      if (cashAlt) bullets.push("有錢唔派息改送股：公司想保留現金，可能係資金緊張嘅訊號");
      return {
        score, direction: "neutral", majorBefore, majorAfter,
        minorityNote: `小股東持股由 ${100 - majorBefore}% → ${100 - majorAfter}%`,
        bullets,
      };
    },
  },
  privatization: {
    inputs: [
      { key: "navDiscount", label: "出價 vs 每股 NAV 折讓", kind: "slider", min: 0, max: 80, step: 5, unit: "%", def: 45 },
      { key: "premium", label: "出價 vs 市價溢價", kind: "slider", min: 0, max: 100, step: 5, unit: "%", def: 25 },
      { key: "majorStake", label: "大股東現有持股", kind: "slider", min: 30, max: 75, step: 5, unit: "%", def: 60 },
      { key: "sweetener", label: "附加特別股息甜頭", kind: "toggle", def: false },
    ],
    calc: (v) => {
      const navDisc = Number(v.navDiscount);
      const premium = Number(v.premium);
      const majorStake = Number(v.majorStake);
      const sweetener = Boolean(v.sweetener);
      let score = 10;
      if (navDisc >= 50) score += 30; else if (navDisc >= 30) score += 18;
      if (premium <= 15) score += 15;
      if (majorStake >= 60) score += 10;
      if (sweetener) score += 5;
      score = Math.min(100, score);
      const majorBefore = majorStake;
      const passRate = premium >= 40 ? 0.9 : premium >= 25 ? 0.7 : premium >= 15 ? 0.5 : 0.3;
      const majorAfter = Math.round(majorBefore + (100 - majorBefore) * passRate);
      const bullets: string[] = [];
      if (navDisc >= 50) bullets.push(`出價較 NAV 折讓 ${navDisc}%：大股東想半價買走公司資產，小股東應考慮否決`);
      else if (navDisc >= 30) bullets.push(`出價較 NAV 折讓 ${navDisc}%：作價偏低，但市况差時可能係唯一出路`);
      else bullets.push(`出價接近 NAV（折讓 ${navDisc}%）：作價公道，小股東利益有基本保障`);
      if (premium >= 40) bullets.push(`溢價 ${premium}% 相當吸引：通過機會大，套現離場係合理選擇`);
      else if (premium < 15) bullets.push(`溢價只有 ${premium}%：冇誠意，私有化好可能被否決`);
      if (sweetener) bullets.push("附加特別股息：係想氹獨立股東支持嘅甜頭，要計清楚總回報");
      bullets.push(`門檻：需要 75% 獨立股東同意 + 反對票 <10%。目前模擬通過率約 ${Math.round(passRate * 100)}%`);
      return {
        score, direction: "neutral", majorBefore, majorAfter,
        minorityNote: majorAfter >= 100 ? "私有化成功：小股東全部離場" : `小股東持股由 ${100 - majorBefore}% → ${100 - majorAfter}%`,
        bullets,
      };
    },
  },
  dividend: {
    inputs: [
      { key: "payoutRatio", label: "派息比率（佔盈利）", kind: "slider", min: 0, max: 150, step: 5, unit: "%", def: 50 },
      { key: "years", label: "連續派息年數", kind: "slider", min: 0, max: 15, step: 1, unit: " 年", def: 5 },
      { key: "funding", label: "派息資金來源", kind: "select", options: [
        { value: "earnings", label: "正常盈利" },
        { value: "debt", label: "借錢派息" },
        { value: "asset", label: "賣資產特別息" },
      ], def: "earnings" },
      { key: "yieldPct", label: "股息率", kind: "slider", min: 0, max: 20, step: 1, unit: "%", def: 5 },
    ],
    calc: (v) => {
      const payout = Number(v.payoutRatio);
      const years = Number(v.years);
      const funding = String(v.funding);
      const yieldPct = Number(v.yieldPct);
      let score = 10;
      if (years >= 5) score += 25; else if (years >= 3) score += 15;
      if (payout >= 30 && payout <= 70) score += 20;
      if (payout > 100) score -= 20;
      if (funding === "debt") score -= 30;
      if (funding === "asset") score += 5;
      if (yieldPct >= 4 && yieldPct <= 10) score += 15;
      if (yieldPct > 12) score -= 10;
      score = Math.min(100, Math.max(0, score));
      const majorBefore = 40;
      const majorAfter = 40;
      const bullets: string[] = [];
      if (years >= 5) bullets.push(`連續 ${years} 年派息：長期紀錄代表公司有真實、可持續嘅現金流`);
      else if (years === 0) bullets.push("第一次派息：要查清楚係咪想推高股價配合其他動作");
      else bullets.push(`連續 ${years} 年派息：紀錄尚短，繼續觀察可持續性`);
      if (payout > 100) bullets.push(`⚠️ 派息比率 ${payout}% 超過盈利：派得比賺得多，唔可持續`);
      else if (payout >= 30 && payout <= 70) bullets.push(`派息比率 ${payout}% 健康：保留足夠資金發展，又肯回饋股東`);
      if (funding === "debt") bullets.push("🚩 借錢派息係紅色旗幟：公司冇真現金流，派息只係財技唔係實力");
      if (funding === "asset") bullets.push("賣資產派特別息：一次性事件，唔好當係常規派息嚟估值");
      if (yieldPct > 12) bullets.push(`股息率 ${yieldPct}% 異常高：通常係股價暴跌造成，高息陷阱要小心`);
      return {
        score, direction: "bullish", majorBefore, majorAfter,
        minorityNote: "派息唔影響股權比例，只係現金分派",
        bullets,
      };
    },
  },
};

function gaugeColor(score: number): string {
  if (score >= 70) return theme.red;
  if (score >= 40) return theme.yellow;
  return theme.green;
}

function scoreLabel(score: number): string {
  if (score >= 70) return "高危";
  if (score >= 40) return "警惕";
  return "低風險";
}

function Gauge({ score, size = 160 }: { score: number; size?: number }) {
  const cx = 100;
  const cy = 100;
  const r = 80;
  const startAngle = -180;
  const endAngle = 0;
  const scoreAngle = startAngle + (score / 100) * 180;

  const polar = (angle: number, radius: number) => ({
    x: cx + radius * Math.cos((angle * Math.PI) / 180),
    y: cy + radius * Math.sin((angle * Math.PI) / 180),
  });

  const arc = (a0: number, a1: number, radius: number) => {
    const p0 = polar(a0, radius);
    const p1 = polar(a1, radius);
    const large = a1 - a0 > 180 ? 1 : 0;
    return `M ${p0.x} ${p0.y} A ${radius} ${radius} 0 ${large} 1 ${p1.x} ${p1.y}`;
  };

  const needleTip = polar(scoreAngle, r - 22);
  const color = gaugeColor(score);

  return (
    <svg viewBox="0 0 200 118" style={{ width: size, height: "auto" }}>
      <path d={arc(-180, -108, r)} stroke={theme.green} strokeWidth={16} fill="none" strokeLinecap="round" opacity={0.9} />
      <path d={arc(-108, -36, r)} stroke={theme.yellow} strokeWidth={16} fill="none" strokeLinecap="round" opacity={0.9} />
      <path d={arc(-36, 0, r)} stroke={theme.red} strokeWidth={16} fill="none" strokeLinecap="round" opacity={0.9} />
      <path d={arc(-180, scoreAngle, r)} stroke={color} strokeWidth={16} fill="none" strokeLinecap="round" opacity={0.35} />
      <line x1={cx} y1={cy} x2={needleTip.x} y2={needleTip.y} stroke={color} strokeWidth={4} strokeLinecap="round" />
      <circle cx={cx} cy={cy} r={7} fill={color} />
      <text x={cx} y={cy - 24} textAnchor="middle" fill={color} fontSize={30} fontWeight={800}>
        {score}
      </text>
      <text x={cx} y={cy - 6} textAnchor="middle" fill={theme.muted} fontSize={12}>
        {scoreLabel(score)}
      </text>
      <text x={14} y={cy + 14} fill={theme.green} fontSize={10}>0</text>
      <text x={172} y={cy + 14} fill={theme.red} fontSize={10}>100</text>
    </svg>
  );
}

function OwnershipDonut({ before, after, size = 150 }: { before: number; after: number; size?: number }) {
  const r = 60;
  const stroke = 26;
  const circ = 2 * Math.PI * r;

  const ring = (pct: number, color: string, offset: number) => (
    <circle
      cx={75} cy={75} r={r} fill="none"
      stroke={color} strokeWidth={stroke}
      strokeDasharray={`${(pct / 100) * circ} ${circ}`}
      strokeDashoffset={-offset * circ}
      transform="rotate(-90 75 75)"
      strokeLinecap="butt"
    />
  );

  return (
    <div className="flex items-center gap-5">
      <div className="text-center">
        <svg viewBox="0 0 150 150" style={{ width: size * 0.52, height: "auto" }}>
          <circle cx={75} cy={75} r={r} fill="none" stroke="rgba(255,255,255,0.06)" strokeWidth={stroke} />
          {ring(before, theme.accent, 0)}
          {ring(100 - before, "rgba(255,255,255,0.15)", before / 100)}
          <text x={75} y={70} textAnchor="middle" fill={theme.foreground} fontSize={20} fontWeight={700}>{before}%</text>
          <text x={75} y={88} textAnchor="middle" fill={theme.muted} fontSize={10}>大股東</text>
        </svg>
        <div className="mt-1 text-xs text-[var(--page-muted)]">之前</div>
      </div>
      <ArrowRight className="size-5 shrink-0 text-[var(--page-accent)]" />
      <div className="text-center">
        <svg viewBox="0 0 150 150" style={{ width: size * 0.52, height: "auto" }}>
          <circle cx={75} cy={75} r={r} fill="none" stroke="rgba(255,255,255,0.06)" strokeWidth={stroke} />
          {ring(after, after > before ? theme.red : after < before ? theme.green : theme.accent, 0)}
          {ring(100 - after, "rgba(255,255,255,0.15)", after / 100)}
          <text x={75} y={70} textAnchor="middle" fill={after > before ? theme.red : after < before ? theme.green : theme.foreground} fontSize={20} fontWeight={700}>
            {after}%
          </text>
          <text x={75} y={88} textAnchor="middle" fill={theme.muted} fontSize={10}>大股東</text>
        </svg>
        <div className="mt-1 text-xs text-[var(--page-muted)]">之後</div>
      </div>
    </div>
  );
}

function ModuleSimulator({ moduleId }: { moduleId: string }) {
  const cfg = simConfigs[moduleId];
  const [values, setValues] = useState<SimValues>(() => {
    const init: SimValues = {};
    if (cfg) for (const inp of cfg.inputs) init[inp.key] = inp.def;
    return init;
  });

  useEffect(() => {
    const init: SimValues = {};
    if (cfg) for (const inp of cfg.inputs) init[inp.key] = inp.def;
    setValues(init);
  }, [moduleId]);

  if (!cfg) return null;

  const result = cfg.calc(values);
  const dirInfo =
    result.direction === "bullish"
      ? { label: "方向：正面", color: theme.green, bg: theme.greenMuted }
      : result.direction === "bearish"
        ? { label: "方向：負面", color: theme.red, bg: theme.redMuted }
        : { label: "方向：中性", color: theme.yellow, bg: theme.yellowMuted };

  return (
    <div
      className="mt-6 rounded-2xl p-5"
      style={{ backgroundColor: "rgba(212,168,83,0.05)", border: `1px solid rgba(212,168,83,0.25)` }}
    >
      <div className="mb-4 flex items-center gap-2">
        <FlaskConical className="size-5 text-[var(--page-accent)]" />
        <h3 className="text-base font-bold text-[var(--page-accent)]">互動模擬器：試吓唔同條件</h3>
      </div>

      <div className="grid gap-6 lg:grid-cols-2">
        {/* Inputs */}
        <div className="space-y-4">
          {cfg.inputs.map((inp) => (
            <div key={inp.key}>
              {inp.kind === "slider" && (
                <div>
                  <div className="mb-1 flex items-center justify-between text-sm">
                    <span>{inp.label}</span>
                    <span className="rounded-md bg-[var(--page-accent)]/15 px-2 py-0.5 font-mono text-xs font-semibold text-[var(--page-accent)]">
                      {String(values[inp.key])}
                      {inp.unit}
                    </span>
                  </div>
                  <input
                    type="range"
                    min={inp.min}
                    max={inp.max}
                    step={inp.step}
                    value={Number(values[inp.key])}
                    onChange={(e) => setValues((prev) => ({ ...prev, [inp.key]: Number(e.target.value) }))}
                    className="w-full accent-[#d4a853]"
                  />
                </div>
              )}
              {inp.kind === "select" && (
                <div>
                  <div className="mb-1.5 text-sm">{inp.label}</div>
                  <div className="flex flex-wrap gap-1.5">
                    {inp.options.map((opt) => {
                      const active = values[inp.key] === opt.value;
                      return (
                        <button
                          key={opt.value}
                          onClick={() => setValues((prev) => ({ ...prev, [inp.key]: opt.value }))}
                          className="rounded-lg px-2.5 py-1.5 text-xs font-medium transition-all"
                          style={{
                            backgroundColor: active ? theme.accentMuted : "rgba(255,255,255,0.04)",
                            color: active ? theme.accent : theme.muted,
                            border: `1px solid ${active ? "rgba(212,168,83,0.4)" : "rgba(255,255,255,0.08)"}`,
                          }}
                        >
                          {opt.label}
                        </button>
                      );
                    })}
                  </div>
                </div>
              )}
              {inp.kind === "toggle" && (
                <button
                  onClick={() => setValues((prev) => ({ ...prev, [inp.key]: !prev[inp.key] }))}
                  className="flex w-full items-center justify-between rounded-lg px-3 py-2 text-sm transition-all"
                  style={{
                    backgroundColor: values[inp.key] ? theme.accentMuted : "rgba(255,255,255,0.04)",
                    border: `1px solid ${values[inp.key] ? "rgba(212,168,83,0.4)" : "rgba(255,255,255,0.08)"}`,
                  }}
                >
                  <span style={{ color: values[inp.key] ? theme.accent : theme.muted }}>{inp.label}</span>
                  <span
                    className="relative inline-flex h-5 w-9 items-center rounded-full transition-colors"
                    style={{ backgroundColor: values[inp.key] ? theme.accent : "rgba(255,255,255,0.15)" }}
                  >
                    <span
                      className="inline-block size-3.5 rounded-full bg-white transition-transform"
                      style={{ transform: values[inp.key] ? "translateX(18px)" : "translateX(3px)" }}
                    />
                  </span>
                </button>
              )}
            </div>
          ))}
        </div>

        {/* Charts */}
        <div className="flex flex-col items-center gap-4">
          <div className="flex flex-wrap items-center justify-center gap-5">
            <div className="text-center">
              <Gauge score={result.score} size={170} />
              <div className="mt-1 text-xs text-[var(--page-muted)]">歸邊風險評分</div>
            </div>
            <div className="text-center">
              <OwnershipDonut before={result.majorBefore} after={result.majorAfter} />
              <div className="mt-1 text-xs text-[var(--page-muted)]">股權結構變化</div>
            </div>
          </div>
          <span
            className="rounded-full px-3 py-1 text-xs font-semibold"
            style={{ backgroundColor: dirInfo.bg, color: dirInfo.color }}
          >
            {dirInfo.label}
          </span>
        </div>
      </div>

      {/* Explanation */}
      <div className="mt-5 rounded-xl p-4" style={{ backgroundColor: "rgba(0,0,0,0.25)" }}>
        <div className="mb-2 flex items-center gap-2 text-xs font-semibold text-[var(--page-accent)]">
          <Lightbulb className="size-4" /> 模擬結果解讀
        </div>
        <ul className="space-y-1.5">
          {result.bullets.map((b, i) => (
            <li key={i} className="flex gap-2 text-xs leading-relaxed text-[var(--page-muted)]">
              <span className="mt-1.5 block size-1 shrink-0 rounded-full bg-[var(--page-accent)]" />
              <span>{b}</span>
            </li>
          ))}
          <li className="flex gap-2 text-xs leading-relaxed" style={{ color: result.majorAfter > result.majorBefore ? theme.red : theme.green }}>
            <span className="mt-1.5 block size-1 shrink-0 rounded-full" style={{ backgroundColor: result.majorAfter > result.majorBefore ? theme.red : theme.green }} />
            <span>{result.minorityNote}</span>
          </li>
        </ul>
      </div>
    </div>
  );
}

export default function FinTechCourse() {
  const [activeModule, setActiveModule] = useState(0);
  const [expandedSections, setExpandedSections] = useState<Record<string, boolean>>({});
  const [quizMode, setQuizMode] = useState(false);
  const [quizStep, setQuizStep] = useState(0);
  const [quizScore, setQuizScore] = useState(0);
  const [quizAnswers, setQuizAnswers] = useState<Record<number, string>>({});

  const module = modules[activeModule];
  const sc = signalColors[module.signalColor];

  const toggleSection = (key: string) => {
    setExpandedSections((prev) => ({ ...prev, [key]: !prev[key] }));
  };

  const quizQuestions = [
    {
      q: "以下邊種組合係經典「向下炒」pattern？",
      options: ["拆股 + 派息", "合股 + 大折讓供股", "回購 + 送紅股", "私有化 + 派息"],
      answer: 1,
      explain: "合股令股價表面上升，然後大折讓供股攤薄小股東——係財技最經典嘅向下炒套路。",
    },
    {
      q: "公司宣布送紅股後，你最應該警惕乜嘢？",
      options: ["股價會升", "可能會伴隨供股或配售宣布", "公司會回購", "股息會增加"],
      answer: 1,
      explain: "紅股好多時係「糖衣毒藥」——甜頭嚟嘅，真正嘅財技（供股/配售）通常緊接住嚟。",
    },
    {
      q: "CB（可換股債券）換股價越低，代表乜嘢？",
      options: ["公司越安全", "CB 持有人越有動機壓低股價", "股價一定會升", "利息成本越低"],
      answer: 1,
      explain: "換股價越低，CB 持有人換股時越著數，所以佢哋有動機壓低股價令換股更划算。",
    },
    {
      q: "以下邊個係真正嘅正面信號？",
      options: ["公司宣布 10 合 1", "公司用現金持續回購", "公司大折讓配售", "公司發行低換股價 CB"],
      answer: 1,
      explain: "真金白銀回購代表管理層對公司有信心。其他三個都係財技歸邊嘅危險信號。",
    },
    {
      q: "FinTech 歸邊分析中，邊個組合會加最多分（最高風險）？",
      options: ["派息 + 回購", "拆股 + 送紅股", "合股後供股 + 深折讓", "私有化 + 派特別息"],
      answer: 2,
      explain: "合股後供股（+25）+ 深折讓供股（+30）可以加到 55 分以上，係最高風險級別。",
    },
  ];

  const handleQuizAnswer = (idx: number) => {
    const correct = idx === quizQuestions[quizStep].answer;
    if (correct) setQuizScore((s) => s + 1);
    setQuizAnswers((prev) => ({ ...prev, [quizStep]: String(idx) }));
  };

  const nextQuiz = () => {
    if (quizStep < quizQuestions.length - 1) {
      setQuizStep((s) => s + 1);
    }
  };

  const resetQuiz = () => {
    setQuizStep(0);
    setQuizScore(0);
    setQuizAnswers({});
  };

  return (
    <div
      style={
        {
          "--page-bg": theme.background,
          "--page-fg": theme.foreground,
          "--page-card": theme.card,
          "--page-muted": theme.muted,
          "--page-accent": theme.accent,
          "--page-red": theme.red,
          "--page-green": theme.green,
          "--page-yellow": theme.yellow,
        } as React.CSSProperties
      }
      className="min-h-screen bg-[var(--page-bg)] text-[var(--page-fg)]"
    >
      <div className="mx-auto max-w-6xl px-4 py-10">
        {/* Header */}
        <header className="mb-10 text-center">
          <div className="mx-auto mb-3 flex size-14 items-center justify-center rounded-2xl bg-[var(--page-accent)]/10">
            <BookOpen className="size-7 text-[var(--page-accent)]" />
          </div>
          <h1 className="text-3xl font-bold tracking-tight">港股財技入門課程</h1>
          <p className="mt-2 text-[var(--page-muted)]">
            10 大財技分類 · 財技邏輯 · 實戰案例 · 學完先用 App
          </p>
        </header>

        {/* Module Navigation */}
        <nav className="mb-8 flex flex-wrap gap-2">
          {modules.map((m, i) => {
            const Icon = m.icon;
            const isActive = i === activeModule;
            const scNav = signalColors[m.signalColor];
            return (
              <button
                key={m.id}
                onClick={() => {
                  setActiveModule(i);
                  setQuizMode(false);
                }}
                className={`flex items-center gap-2 rounded-xl px-4 py-2.5 text-sm font-medium transition-all ${
                  isActive
                    ? "bg-[var(--page-accent)]/15 text-[var(--page-accent)] ring-1 ring-[var(--page-accent)]/30"
                    : "bg-[var(--page-card)] text-[var(--page-muted)] hover:text-[var(--page-fg)]"
                }`}
                style={
                  isActive
                    ? { borderColor: "rgba(212,168,83,0.3)" }
                    : { border: "1px solid rgba(255,255,255,0.06)" }
                }
              >
                <Icon className="size-4" />
                <span className="hidden sm:inline">{m.title.split("(")[0].trim()}</span>
                <span className="sm:hidden">{m.title.split(" ")[0]}</span>
              </button>
            );
          })}
        </nav>

        {/* Quiz Toggle */}
        <div className="mb-6 flex justify-end">
          <button
            onClick={() => {
              setQuizMode(!quizMode);
              resetQuiz();
            }}
            className="flex items-center gap-2 rounded-xl border px-4 py-2 text-sm font-medium transition-all"
            style={{
              borderColor: quizMode ? "rgba(212,168,83,0.4)" : "rgba(255,255,255,0.08)",
              backgroundColor: quizMode ? "rgba(212,168,83,0.1)" : "transparent",
              color: quizMode ? theme.accent : theme.muted,
            }}
          >
            <Zap className="size-4" />
            {quizMode ? "返回課程" : "挑戰測驗"}
          </button>
        </div>

        {/* Quiz Mode */}
        {quizMode ? (
          <div
            className="rounded-2xl p-6 sm:p-8"
            style={{ backgroundColor: theme.card, border: `1px solid ${theme.cardBorder}` }}
          >
            {quizStep < quizQuestions.length ? (
              <div>
                <div className="mb-2 flex items-center justify-between text-sm text-[var(--page-muted)]">
                  <span>
                    問題 {quizStep + 1} / {quizQuestions.length}
                  </span>
                  <span>
                    得分: {quizScore}/{quizStep}
                  </span>
                </div>
                <div className="mb-1 h-1.5 rounded-full bg-white/5">
                  <div
                    className="h-full rounded-full bg-[var(--page-accent)] transition-all duration-500"
                    style={{ width: `${((quizStep) / quizQuestions.length) * 100}%` }}
                  />
                </div>
                <h3 className="mt-6 text-xl font-semibold">{quizQuestions[quizStep].q}</h3>
                <div className="mt-5 space-y-3">
                  {quizQuestions[quizStep].options.map((opt, i) => {
                    const answered = quizAnswers[quizStep] !== undefined;
                    const isSelected = quizAnswers[quizStep] === String(i);
                    const isCorrect = i === quizQuestions[quizStep].answer;
                    let btnStyle: React.CSSProperties = {
                      border: "1px solid rgba(255,255,255,0.08)",
                      backgroundColor: "transparent",
                    };
                    if (answered && isCorrect) {
                      btnStyle = {
                        border: `1px solid ${theme.green}`,
                        backgroundColor: theme.greenMuted,
                      };
                    } else if (answered && isSelected && !isCorrect) {
                      btnStyle = {
                        border: `1px solid ${theme.red}`,
                        backgroundColor: theme.redMuted,
                      };
                    } else if (!answered) {
                      btnStyle = {
                        border: "1px solid rgba(255,255,255,0.08)",
                        backgroundColor: "transparent",
                      };
                    }
                    return (
                      <button
                        key={i}
                        onClick={() => !answered && handleQuizAnswer(i)}
                        disabled={answered}
                        className="w-full rounded-xl px-5 py-3.5 text-left text-sm transition-all hover:border-white/20 disabled:cursor-default"
                        style={btnStyle}
                      >
                        {String.fromCharCode(65 + i)}. {opt}
                        {answered && isCorrect && " ✓"}
                        {answered && isSelected && !isCorrect && " ✗"}
                      </button>
                    );
                  })}
                </div>
                {quizAnswers[quizStep] !== undefined && (
                  <div
                    className="mt-5 rounded-xl p-4 text-sm"
                    style={{ backgroundColor: "rgba(212,168,83,0.08)" }}
                  >
                    <Lightbulb className="mb-1 inline size-4 text-[var(--page-accent)]" />{" "}
                    <span className="text-[var(--page-accent)]">解釋：</span>
                    {quizQuestions[quizStep].explain}
                  </div>
                )}
                {quizAnswers[quizStep] !== undefined && (
                  <button
                    onClick={nextQuiz}
                    className="mt-5 flex items-center gap-2 rounded-xl bg-[var(--page-accent)] px-5 py-2.5 text-sm font-semibold text-black transition-all hover:opacity-90"
                  >
                    下一題 <ArrowRight className="size-4" />
                  </button>
                )}
              </div>
            ) : (
              <div className="text-center">
                <div className="mx-auto mb-4 flex size-16 items-center justify-center rounded-full bg-[var(--page-accent)]/15">
                  <TrophyIcon className="size-8 text-[var(--page-accent)]" />
                </div>
                <h3 className="text-2xl font-bold">
                  {quizScore === quizQuestions.length
                    ? "🏆 滿分！"
                    : quizScore >= 3
                      ? "👍 做得好好！"
                      : "📚 繼續努力！"}
                </h3>
                <p className="mt-2 text-[var(--page-muted)]">
                  你答啱 {quizScore}/{quizQuestions.length} 題
                </p>
                <div className="mt-4 flex justify-center gap-3">
                  <button
                    onClick={resetQuiz}
                    className="rounded-xl bg-[var(--page-accent)] px-5 py-2.5 text-sm font-semibold text-black transition-all hover:opacity-90"
                  >
                    再試一次
                  </button>
                  <button
                    onClick={() => setQuizMode(false)}
                    className="rounded-xl border px-5 py-2.5 text-sm font-medium transition-all"
                    style={{ borderColor: "rgba(255,255,255,0.15)", color: theme.muted }}
                  >
                    返回課程
                  </button>
                </div>
              </div>
            )}
          </div>
        ) : (
          /* Module Content */
          <div
            className="rounded-2xl p-6 sm:p-8"
            style={{ backgroundColor: theme.card, border: `1px solid ${theme.cardBorder}` }}
          >
            {/* Module Header */}
            <div className="mb-6 flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
              <div className="flex items-start gap-4">
                <div
                  className="flex size-12 items-center justify-center rounded-xl"
                  style={{ backgroundColor: sc.bg }}
                >
                  <module.icon className="size-6" style={{ color: sc.text }} />
                </div>
                <div>
                  <h2 className="text-2xl font-bold">{module.title}</h2>
                  <span
                    className="mt-1 inline-block rounded-full px-3 py-0.5 text-xs font-medium"
                    style={{
                      backgroundColor: sc.bg,
                      color: sc.text,
                      border: `1px solid ${sc.border}`,
                    }}
                  >
                    {module.signal}
                  </span>
                </div>
              </div>
            </div>

            {/* Summary */}
            <p className="mb-8 leading-relaxed text-[var(--page-muted)]">{module.summary}</p>

            {/* 財技邏輯 */}
            <SectionBlock
              title={module.choyLogic.title}
              icon={<Target className="size-5" />}
              accentColor={theme.accent}
              accentBg={theme.accentMuted}
              expanded={expandedSections["choy"] !== false}
              onToggle={() => toggleSection("choy")}
            >
              <ul className="space-y-3">
                {module.choyLogic.points.map((p, i) => (
                  <li key={i} className="flex gap-3 text-sm leading-relaxed">
                    <span
                      className="mt-1.5 block size-1.5 shrink-0 rounded-full"
                      style={{ backgroundColor: theme.accent }}
                    />
                    <span>{p}</span>
                  </li>
                ))}
              </ul>
            </SectionBlock>

            {/* Impact */}
            <SectionBlock
              title={module.impact.title}
              icon={
                module.impact.direction === "bullish" ? (
                  <TrendingUp className="size-5" />
                ) : module.impact.direction === "bearish" ? (
                  <TrendingDown className="size-5" />
                ) : (
                  <AlertTriangle className="size-5" />
                )
              }
              accentColor={
                module.impact.direction === "bullish"
                  ? theme.green
                  : module.impact.direction === "bearish"
                    ? theme.red
                    : theme.yellow
              }
              accentBg={
                module.impact.direction === "bullish"
                  ? theme.greenMuted
                  : module.impact.direction === "bearish"
                    ? theme.redMuted
                    : theme.yellowMuted
              }
              expanded={expandedSections["impact"] !== false}
              onToggle={() => toggleSection("impact")}
              badge={
                <span
                  className="rounded-full px-2 py-0.5 text-xs font-medium"
                  style={{
                    backgroundColor:
                      module.impact.direction === "bullish"
                        ? theme.greenMuted
                        : module.impact.direction === "bearish"
                          ? theme.redMuted
                          : theme.yellowMuted,
                    color:
                      module.impact.direction === "bullish"
                        ? theme.green
                        : module.impact.direction === "bearish"
                          ? theme.red
                          : theme.yellow,
                  }}
                >
                  {module.impact.direction === "bullish"
                    ? "看好"
                    : module.impact.direction === "bearish"
                      ? "看淡"
                      : "中性"}
                </span>
              }
            >
              <ul className="space-y-3">
                {module.impact.points.map((p, i) => (
                  <li key={i} className="flex gap-3 text-sm leading-relaxed">
                    <span
                      className="mt-1.5 block size-1.5 shrink-0 rounded-full"
                      style={{
                        backgroundColor:
                          module.impact.direction === "bullish"
                            ? theme.green
                            : module.impact.direction === "bearish"
                              ? theme.red
                              : theme.yellow,
                      }}
                    />
                    <span>{p}</span>
                  </li>
                ))}
              </ul>
            </SectionBlock>

            {/* Example */}
            <SectionBlock
              title="真實案例"
              icon={<BookOpen className="size-5" />}
              accentColor={theme.blue}
              accentBg={theme.blueMuted}
              expanded={expandedSections["example"] !== false}
              onToggle={() => toggleSection("example")}
            >
              <p className="text-sm leading-relaxed">{module.example}</p>
            </SectionBlock>

            {/* Score Hint */}
            <SectionBlock
              title="FinTech 評分提示"
              icon={<DollarSign className="size-5" />}
              accentColor={theme.accent}
              accentBg={theme.accentMuted}
              expanded={expandedSections["score"] !== false}
              onToggle={() => toggleSection("score")}
            >
              <p className="text-sm leading-relaxed text-[var(--page-muted)]">{module.scoreHint}</p>
            </SectionBlock>

            {/* Interactive Simulator */}
            <ModuleSimulator key={module.id} moduleId={module.id} />

            {/* Module Navigation Bottom */}
            <div className="mt-8 flex justify-between border-t pt-6" style={{ borderColor: "rgba(255,255,255,0.06)" }}>
              <button
                onClick={() => setActiveModule((prev) => (prev > 0 ? prev - 1 : modules.length - 1))}
                className="flex items-center gap-2 rounded-xl px-4 py-2.5 text-sm font-medium text-[var(--page-muted)] transition-all hover:text-[var(--page-fg)]"
                style={{ border: "1px solid rgba(255,255,255,0.08)" }}
              >
                ← 上一課
              </button>
              <span className="text-sm text-[var(--page-muted)]">
                {activeModule + 1} / {modules.length}
              </span>
              <button
                onClick={() => setActiveModule((prev) => (prev < modules.length - 1 ? prev + 1 : 0))}
                className="flex items-center gap-2 rounded-xl px-4 py-2.5 text-sm font-medium text-[var(--page-muted)] transition-all hover:text-[var(--page-fg)]"
                style={{ border: "1px solid rgba(255,255,255,0.08)" }}
              >
                下一課 →
              </button>
            </div>
          </div>
        )}

        {/* Footer */}
        <footer className="mt-12 text-center text-xs text-[var(--page-muted)]">
          <p>港股財技入門課程 · Powered by 財技分析系統</p>
          <p className="mt-1">學完之後即可使用財技分析 App 進行實戰分析</p>
        </footer>
      </div>
    </div>
  );
}

function SectionBlock({
  title,
  icon,
  accentColor,
  accentBg,
  expanded,
  onToggle,
  children,
  badge,
}: {
  title: string;
  icon: React.ReactNode;
  accentColor: string;
  accentBg: string;
  expanded: boolean;
  onToggle: () => void;
  children: React.ReactNode;
  badge?: React.ReactNode;
}) {
  return (
    <div className="mb-4">
      <button
        onClick={onToggle}
        className="flex w-full items-center justify-between rounded-xl px-4 py-3.5 text-left transition-all hover:bg-white/[0.02]"
        style={{ backgroundColor: expanded ? accentBg : "transparent" }}
      >
        <div className="flex items-center gap-3">
          <span style={{ color: accentColor }}>{icon}</span>
          <span className="text-sm font-semibold" style={{ color: expanded ? accentColor : "inherit" }}>
            {title}
          </span>
          {badge}
        </div>
        {expanded ? (
          <ChevronUp className="size-4 text-[var(--page-muted)]" />
        ) : (
          <ChevronDown className="size-4 text-[var(--page-muted)]" />
        )}
      </button>
      {expanded && <div className="px-4 pb-3 pt-1">{children}</div>}
    </div>
  );
}

function TrophyIcon({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M6 9H4.5a2.5 2.5 0 0 1 0-5C7 4 6 9 6 9z" />
      <path d="M18 9h1.5a2.5 2.5 0 0 0 0-5C17 4 18 9 18 9z" />
      <path d="M4 22h16" />
      <path d="M10 14.66V17c0 .55-.47.98-.97 1.21C7.85 18.75 7 20.24 7 22" />
      <path d="M14 14.66V17c0 .55.47.98.97 1.21C16.15 18.75 17 20.24 17 22" />
      <path d="M18 2H6v7a6 6 0 0 0 12 0V2Z" />
    </svg>
  );
}
