/**
 * HKEX 期權鏈日報 → Google Sheet 自動入數
 *
 * 數據源：Google Drive 資料夾「HKEX 期權鏈日報」入面每日上傳嘅 CSV
 *        （由 Zo 每日 21:00 HKT 上傳：每日總覽 / 期權鏈_精選）
 * 工作表：每日總覽（append-only 歷史）、期權鏈_當日（最新一個交易日）、說明
 *
 * 安裝：見同資料夾 README.md（撳一次 setup() 就得）
 */

var FOLDER_ID = '15WEwSFqEaQdAzFH3Gq4wjJHqc-Dt8rVR';
var SS_ID = '1p8I9uljTfRnFhljm7X7dNDU4JyTSIPkNlZ4qTP9XjvE';
var TAB_OVERVIEW = '每日總覽';
var TAB_CHAIN = '期權鏈_當日';
var TAB_NOTE = '說明';

/** 呢啲欄位一律保留做文字（股票代號要保留前導 0；日期唔好俾 Sheet 自動改格式） */
var TEXT_COLS = {
  '日期': 1, '股票代號': 1, '代表到期日': 1, '到期日': 1,
  'HKATS': 1, '名稱': 1, '貴平': 1, '價內外': 1, '類型': 1, 'ATM': 1
};

/* ─────────────────────────── 安裝 ─────────────────────────── */

/**
 * 一鍵安裝：授權 → 建 tab → 即時回填全部歷史 → 掛每小時 trigger。
 * 撳一次就完成，之後唔使再理。
 */
function setup() {
  var ss = SpreadsheetApp.openById(SS_ID);
  ensureTabs_(ss);
  installTriggers_();
  var res = dailyUpdate();
  Logger.log('[chain-sheet] setup 完成：%s', JSON.stringify(res));
  return res;
}

/** 掛 hourly trigger（冪等：已有就唔會重複掛）。
 *  用「每小時」而唔係「每日 21:30」，因為 Apps Script trigger 時間跟 script
 *  timezone，唔確定係咪 HKT；冪等設計下每小時跑一次冇副作用。 */
function installTriggers_() {
  var all = ScriptApp.getProjectTriggers();
  for (var i = 0; i < all.length; i++) {
    if (all[i].getHandlerFunction() === 'dailyUpdate') return all[i].getUniqueId();
  }
  var t = ScriptApp.newTrigger('dailyUpdate').timeBased().everyHours(1).create();
  Logger.log('[chain-sheet] 已掛 trigger %s', t.getUniqueId());
  return t.getUniqueId();
}

/** 移除 trigger（想停用自動更新就用呢個）。 */
function uninstall() {
  var all = ScriptApp.getProjectTriggers();
  for (var i = 0; i < all.length; i++) {
    if (all[i].getHandlerFunction() === 'dailyUpdate') ScriptApp.deleteTrigger(all[i]);
  }
  Logger.log('[chain-sheet] 已移除自動 trigger');
}

/** 手動即時跑一次。 */
function runNow() {
  return dailyUpdate();
}

/* ─────────────────────────── 主流程 ─────────────────────────── */

function dailyUpdate() {
  var lock = LockService.getScriptLock();
  if (!lock.tryLock(30000)) {
    Logger.log('[chain-sheet] 上一次執行未完成，跳過。');
    return { skipped: true, reason: 'locked' };
  }
  try {
    return dailyUpdate_(SpreadsheetApp.openById(SS_ID));
  } finally {
    lock.releaseLock();
  }
}

function dailyUpdate_(ss) {
  ensureTabs_(ss);

  var folder;
  try {
    folder = DriveApp.getFolderById(FOLDER_ID);
  } catch (e) {
    return writeStatus_(ss, '❌ 揾唔到 Drive 資料夾（id ' + FOLDER_ID + '）：' + e.message, null, null);
  }

  var files = collect_(folder);
  var ov = appendOverview_(ss, files.overview);
  var ch = refreshChain_(ss, files.chain);

  var latest = files.overview.length ? files.overview[files.overview.length - 1].date
    : (files.chain.length ? files.chain[files.chain.length - 1].date : null);
  var msg;
  if (!files.overview.length && !files.chain.length) {
    msg = '⚠️ Drive 資料夾入面未見 CSV';
  } else if (ov.added.length || (ch && ch.changed)) {
    msg = '✅ 已更新';
  } else {
    msg = '— 無新數據';
  }
  var detail = [];
  if (ov.added.length) detail.push('總覽新增：' + ov.added.join('、'));
  if (ch && ch.changed) detail.push('期權鏈更新：' + ch.date + '（' + ch.rows + ' 行）');
  if (ch && !ch.changed && ch.date) detail.push('期權鏈已是最新：' + ch.date);

  return writeStatus_(ss, msg, latest, detail.join('　|　'));
}

/* ─────────────────────────── 每日總覽 ─────────────────────────── */

function appendOverview_(ss, files) {
  var sh = ss.getSheetByName(TAB_OVERVIEW);
  if (!files.length) return { added: [] };

  var newest = files[files.length - 1].date;
  var lastRow = sh.getLastRow();

  var have = {};
  var maxDate = '';
  if (lastRow > 1) {
    var col = sh.getRange(2, 1, lastRow - 1, 1).getValues();
    for (var i = 0; i < col.length; i++) {
      var d = String(col[i][0] || '').slice(0, 10);
      if (d) { have[d] = 1; if (d > maxDate) maxDate = d; }
    }
  }
  // 快路徑：Sheet 已有最新一日 → 唔使開任何 CSV
  if (maxDate === newest) return { added: [] };

  var pending = [];
  for (var k = 0; k < files.length; k++) {
    if (!have[files[k].date]) pending.push(files[k]);
  }
  if (!pending.length) return { added: [] };

  var added = [], nCols = 0;
  for (var m = 0; m < pending.length; m++) {
    var rows = csvRows_(pending[m].file);
    if (rows.length < 2) continue;
    var header = rows[0];
    if (sh.getLastRow() < 1) writeHeader_(sh, header);
    var data = normalize_(header, rows.slice(1));
    if (!data.length) continue;
    writeRows_(sh, data, header.length);
    nCols = header.length;
    added.push(pending[m].date + '（' + data.length + ' 行）');
  }
  if (added.length) finish_(sh, nCols);
  return { added: added };
}

/* ─────────────────────────── 期權鏈_當日 ─────────────────────────── */

function refreshChain_(ss, files) {
  if (!files.length) return { changed: false };
  var rec = files[files.length - 1];
  var props = PropertiesService.getScriptProperties();
  var done = props.getProperty('chainDate');
  if (done === rec.date) return { changed: false, date: rec.date };

  var rows = csvRows_(rec.file);
  if (rows.length < 2) return { changed: false };

  var sh = ss.getSheetByName(TAB_CHAIN);
  var header = rows[0];
  var data = normalize_(header, rows.slice(1));

  removeFilter_(sh);
  sh.clear();
  writeHeader_(sh, header);
  writeRows_(sh, data, header.length);
  finish_(sh, header.length);
  props.setProperty('chainDate', rec.date);
  return { changed: true, date: rec.date, rows: data.length };
}

/* ─────────────────────────── 工具 ─────────────────────────── */

function ensureTabs_(ss) {
  var ov = ss.getSheetByName(TAB_OVERVIEW) || ss.insertSheet(TAB_OVERVIEW);
  var ch = ss.getSheetByName(TAB_CHAIN) || ss.insertSheet(TAB_CHAIN);
  var note = ss.getSheetByName(TAB_NOTE) || ss.insertSheet(TAB_NOTE);
  ss.setActiveSheet(ov);

  // 清走 API 建表時留低嘅空白預設 tab
  var sheets = ss.getSheets();
  for (var i = 0; i < sheets.length; i++) {
    var s = sheets[i];
    if ([TAB_OVERVIEW, TAB_CHAIN, TAB_NOTE].indexOf(s.getName()) >= 0) continue;
    if (s.getLastRow() <= 1 && s.getLastColumn() <= 1) ss.deleteSheet(s);
  }

  if (note.getLastRow() < 3) {
    note.clear();
    note.getRange(1, 1, NOTE_TEXT.length, 2).setValues(NOTE_TEXT);
    note.getRange(1, 1).setFontSize(14).setFontWeight('bold');
    note.setColumnWidth(1, 170);
    note.setColumnWidth(2, 720);
    for (var j = 0; j < NOTE_TEXT.length; j++) {
      if (String(NOTE_TEXT[j][0] || '').indexOf('■') === 0) {
        note.getRange(j + 1, 1).setFontWeight('bold');
      }
    }
  }
}

function writeStatus_(ss, msg, latestDate, detail) {
  var note = ss.getSheetByName(TAB_NOTE);
  var now = new Date();
  var tz = 'Asia/Hong_Kong';
  var stamp = Utilities.formatDate(now, tz, 'yyyy-MM-dd HH:mm') + ' HKT';
  if (note) {
    note.getRange(3, 2).setValue(stamp);
    note.getRange(4, 2).setValue(latestDate || '—');
    note.getRange(5, 2).setValue([msg, detail].filter(Boolean).join('　'));
  }
  Logger.log('[chain-sheet] %s | 最新數據日 %s | %s', stamp, latestDate || '-', [msg, detail].filter(Boolean).join(' | '));
  return { status: msg, latestDate: latestDate, detail: detail || '', checkedAt: stamp };
}

function writeHeader_(sh, header) {
  var r = sh.getRange(1, 1, 1, header.length);
  r.setValues([header])
    .setFontWeight('bold')
    .setFontColor('#ffffff')
    .setBackground('#1f3864');
  sh.setFrozenRows(1);
}

/** 寫完數據之後：篩選器蓋住成個數據區 + 自動欄寬（大表跳過欄寬，太慢）。 */
function finish_(sh, nCols) {
  var lastRow = sh.getLastRow();
  if (lastRow < 1 || !nCols) return;
  removeFilter_(sh);
  try { sh.getRange(1, 1, lastRow, nCols).createFilter(); } catch (e) {}
  if (lastRow <= 3000) {
    try { sh.autoResizeColumns(1, nCols); } catch (e) {}
  }
}

function removeFilter_(sh) {
  try {
    var f = sh.getFilter();
    if (f) f.remove();
  } catch (e) {}
}

function writeRows_(sh, rows, nCols) {
  var BATCH = 2000;
  for (var i = 0; i < rows.length; i += BATCH) {
    var chunk = rows.slice(i, i + BATCH);
    var start = sh.getLastRow() + 1;
    var need = start + chunk.length - 1;
    if (need > sh.getMaxRows()) sh.insertRowsAfter(sh.getMaxRows(), need - sh.getMaxRows());
    sh.getRange(start, 1, chunk.length, nCols).setValues(chunk);
  }
  SpreadsheetApp.flush();
}

function collect_(folder) {
  var ov = [], ch = [];
  var it = folder.getFiles();
  while (it.hasNext()) {
    var f = it.next();
    var name = f.getName();
    var m;
    if ((m = name.match(/^每日總覽_(\d{4}-\d{2}-\d{2})\.csv$/))) {
      ov.push({ date: m[1], file: f });
    } else if ((m = name.match(/^期權鏈_精選_(\d{4}-\d{2}-\d{2})\.csv$/))) {
      ch.push({ date: m[1], file: f });
    }
  }
  ov.sort(function (a, b) { return a.date < b.date ? -1 : 1; });
  ch.sort(function (a, b) { return a.date < b.date ? -1 : 1; });
  return { overview: ov, chain: ch };
}

function csvRows_(file) {
  var txt = file.getBlob().getDataAsString('UTF-8');
  if (txt.charCodeAt(0) === 0xFEFF) txt = txt.slice(1);   // 去 UTF-8 BOM
  return Utilities.parseCsv(txt);
}

/** CSV 全部係字串 → 數字欄轉 number（先可以排序／篩選），文字欄保留字串。 */
function normalize_(header, rows) {
  var isText = [];
  for (var c = 0; c < header.length; c++) isText.push(!!TEXT_COLS[String(header[c]).trim()]);
  var out = [];
  for (var i = 0; i < rows.length; i++) {
    var src = rows[i], row = [];
    var empty = true;
    for (var j = 0; j < header.length; j++) {
      var raw = (j < src.length && src[j] !== undefined && src[j] !== null) ? String(src[j]) : '';
      var v = raw;
      if (!isText[j] && raw !== '') {
        var t = raw.replace(/,/g, '');
        if (/^-?\d+(\.\d+)?$/.test(t)) v = Number(t);
      }
      if (v !== '') empty = false;
      row.push(v);
    }
    if (!empty) out.push(row);
  }
  return out;
}

/* ─────────────────────────── 說明 tab 內容 ─────────────────────────── */

var NOTE_TEXT = [
  ['HKEX 期權鏈日報（每日自動更新）', ''],
  ['', ''],
  ['最後檢查', ''],
  ['最後數據日期', ''],
  ['本次更新', ''],
  ['', ''],
  ['■ 數據來源', ''],
  ['港交所官方《Stock Options Daily Market Report》，每個交易日收市後刊登，由 Zo 每晚自動抓取再入表。', ''],
  ['價格係交易所結算價，唔係即市買賣價；IV 由官方日報計算（已用乾淨版 ATM IV，唔係污染過嗰份）。', ''],
  ['', ''],
  ['■ 兩個工作表', ''],
  ['每日總覽', '148 隻期權標的 × 每個交易日一行，append-only 累積歷史：代表月 ATM IV、IV 排名／百分位、IV/HV20 貴平、25D 偏斜、成交／未平倉／PCR。'],
  ['期權鏈_當日', '最新一個交易日嘅逐個行使價期權鏈（ATM ±8 個行使價 × 最近兩個到期月，約 6,700 行）：結算價、IV、成交、未平倉、Bid/Ask、Delta、價內外。每日整表換新。'],
  ['', ''],
  ['■ 欄位解釋', ''],
  ['ATM_IV%', '最貼近現價、剩餘 15–75 日嘅行使價嘅隱含波幅（Call/Put 平均）。'],
  ['IV排名%', '過去一年 IV 區間位置：(今日 IV − 一年最低) ÷ (一年最高 − 一年最低) × 100。越高＝相對自己歷史越貴。'],
  ['IV百分位%', '過去一年有幾多 % 嘅交易日 IV 低過今日。'],
  ['貴平', 'IV ÷ HV20（過去 20 日實際波幅）：≥1.3「貴」、≤0.9「平」，中間「中性」。'],
  ['成交PCR / 未平倉PCR', 'Put ÷ Call。>1 代表 Put 那邊較活躍。'],
  ['25D偏縮', '25-delta Put IV 減 25-delta Call IV（正值＝市場較擔心下跌）。'],
  ['價內外', '行使價相對現價：價內／平值／價外。'],
  ['', ''],
  ['■ 更新頻率', ''],
  ['每小時自動檢查一次；有新交易日先入數（冪等，唔會重複）。週末／公眾假期冇新數據屬正常。', ''],
  ['', ''],
  ['■ 免責聲明', ''],
  ['本表只係市場數據整理，唔構成任何投資建議。期權買賣涉及風險，虧損可以遠超本金。', '']
];
