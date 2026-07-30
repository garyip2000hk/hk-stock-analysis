#!/usr/bin/env python3
"""
FinTech Analysis Engine: 財技歸邊分析
Analyzes corporate actions + CCASS data to determine stock manipulation likelihood.
"""

import json
import os
from datetime import datetime

CACHE_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_FILE = os.path.join(CACHE_DIR, "corp_actions_cache.json")

# === MOTIVATION SCORING ===

def score_motivation(events):
    """
    Score corporate actions for share concentration intent.
    Higher score = more likely designed to concentrate shares.
    
    Scoring rules:
    - Rights issue with deep discount (>30%): +30
    - Consolidation + rights issue combo: +25  
    - CB with low conversion price: +20
    - General offer at deep discount: +25
    - Multiple actions within 6 months: +15
    - Bonus issue before major action: +10 (sweetener)
    """
    score = 0
    signals = []
    
    # Check for rights issues with deep discount
    rights_events = [e for e in events if '供股' in str(e.get('type', '')) or 'rights' in str(e.get('type_en', '')).lower()]
    for e in rights_events:
        price = e.get('price')
        ratio = e.get('ratio', '')
        try:
            price = float(price)
        except:
            price = None
        if price and price < 0.5:
            score += 30
            signals.append(f'深折讓供股(HK${price})')
        elif price:
            score += 15
            signals.append(f'供股 HK${price}')
        if '六' in str(ratio) or '四' in str(ratio):
            score += 10
            signals.append(f'大比例供股({ratio})')
    
    # Check for consolidation
    consol_events = [e for e in events if '合股' in str(e.get('type', '')) or 'consolidation' in str(e.get('type_en', '')).lower()]
    if consol_events:
        score += 15
        signals.append('曾進行合股')
        # Consolidation + rights issue = classic pattern
        if rights_events:
            score += 25
            signals.append('合股後供股(向下炒pattern)')
    
    # Check for CB with favorable terms for insiders
    cb_events = [e for e in events if 'CB' in str(e.get('type', '')) or '換股' in str(e.get('type', '')) or 'convertible' in str(e.get('type_en', '')).lower()]
    for e in cb_events:
        price = e.get('price')
        try:
            price = float(price)
        except:
            price = None
        if price and price < 1.0:
            score += 20
            signals.append(f'低換股價CB(HK${price})')
        elif price:
            score += 10
            signals.append(f'可換股債券(HK${price})')
    
    # Check for general offer at discount
    offer_events = [e for e in events if '要約' in str(e.get('type', '')) or '全購' in str(e.get('type', '')) or 'offer' in str(e.get('type_en', '')).lower()]
    for e in offer_events:
        price = e.get('price')
        try:
            price = float(price)
        except:
            price = None
        if price and price < 5.0:
            score += 25
            signals.append(f'低價要約(HK${price})')
        else:
            score += 10
            signals.append('全面要約')
    
    # Check for multiple actions in short period
    if len(events) >= 4:
        score += 15
        signals.append(f'多項財技活動({len(events)}項)')
    elif len(events) >= 2:
        score += 5
    
    # Check for bonus issue as sweetener
    bonus_events = [e for e in events if '紅股' in str(e.get('type', '')) or 'bonus' in str(e.get('type_en', '')).lower()]
    if bonus_events and len(events) >= 3:
        score += 10
        signals.append('送紅股+其他財技')
    
    # Check for winding-up petition (financial distress signal)
    winding_events = [e for e in events if '清盤' in str(e.get('type', '')) or 'winding' in str(e.get('type_en', '')).lower()]
    if winding_events:
        # If petition was withdrawn, it's more suspicious
        if any('撤回' in str(e.get('detail', '')) or 'withdrawn' in str(e.get('status', '')) for e in winding_events):
            score += 10
            signals.append('清盤呈請已撤回(可疑)')
    
    # Normalize to 0-100
    score = min(score, 100)
    
    return {
        'score': score,
        'level': 'high' if score >= 60 else 'medium' if score >= 30 else 'low',
        'signals': signals,
        'event_count': len(events),
    }


# === CCASS CONCENTRATION SCORING ===

def score_concentration(ccass_data):
    """
    Score CCASS concentration for share cornering indication.
    Higher score = more concentrated / share cornering.
    
    Scoring:
    - Top 5 > 60%: +35
    - Top 5 > 40%: +20
    - Top 10 > 70%: +30
    - Top 10 > 50%: +15
    - Top 20 > 80%: +25
    - Top 20 > 65%: +15
    """
    if not ccass_data or 'error' in ccass_data:
        return {'score': 0, 'level': 'unknown', 'signals': ['CCASS數據不可用']}
    
    conc = ccass_data.get('concentration', {})
    top5 = conc.get('top_5', 0)
    top10 = conc.get('top_10', 0)
    top20 = conc.get('top_20', 0)
    
    score = 0
    signals = []
    
    if top5 >= 60:
        score += 35
        signals.append(f'頭5大佔{top5}%(極高集中)')
    elif top5 >= 40:
        score += 20
        signals.append(f'頭5大佔{top5}%(高集中)')
    elif top5 >= 25:
        score += 10
        signals.append(f'頭5大佔{top5}%(中等集中)')
    else:
        signals.append(f'頭5大佔{top5}%(分散)')
    
    if top10 >= 70:
        score += 30
        signals.append(f'頭10大佔{top10}%(極高集中)')
    elif top10 >= 50:
        score += 15
        signals.append(f'頭10大佔{top10}%(高集中)')
    
    if top20 >= 80:
        score += 25
        signals.append(f'頭20大佔{top20}%(極高集中)')
    elif top20 >= 65:
        score += 15
        signals.append(f'頭20大佔{top20}%(高集中)')
    
    # Check if top holder dominates
    top_holders = ccass_data.get('top_holders', [])
    if top_holders and top_holders[0].get('percentage', 0) > 30:
        score += 20
        signals.append(f'單一券商主導({top_holders[0].get("percentage")}%)')
    
    score = min(score, 100)
    
    return {
        'score': score,
        'level': 'high' if score >= 60 else 'medium' if score >= 30 else 'low',
        'signals': signals,
        'concentration': conc,
        'participants': ccass_data.get('total_participants', 0),
    }


def combined_analysis(stock_code, events, ccass_data):
    """Combine motivation and concentration scoring for final analysis."""
    motivation = score_motivation(events)
    concentration = score_concentration(ccass_data)
    
    # Combined score: weighted average
    combined = int(motivation['score'] * 0.45 + concentration['score'] * 0.55)
    
    # Final assessment
    if combined >= 65:
        verdict = '🔴 高歸邊風險'
        verdict_detail = '財技動機強 + CCASS高度集中，股權歸邊機會高，具炒作潛力'
    elif combined >= 40:
        verdict = '🟡 中等歸邊風險'  
        verdict_detail = '部分歸邊信號，值得持續監察倉位變化'
    elif combined >= 20:
        verdict = '🟢 低歸邊風險'
        verdict_detail = '財技動機不明顯或CCASS分散，歸邊機會較低'
    else:
        verdict = '⚪ 無明顯歸邊信號'
        verdict_detail = '未檢測到財技歸邊相關信號'
    
    return {
        'stock_code': stock_code,
        'analysis_date': datetime.now().strftime('%Y-%m-%d'),
        'combined_score': combined,
        'verdict': verdict,
        'verdict_detail': verdict_detail,
        'motivation': motivation,
        'concentration': concentration,
    }


# CLI
if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('stock_code')
    parser.add_argument('--ccass', help='CCASS JSON data as string')
    args = parser.parse_args()
    
    # Load events from cache
    events = []
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE) as f:
            cache = json.load(f)
        events = cache.get(args.stock_code, [])
    
    # Parse CCASS data if provided
    ccass_data = {}
    if args.ccass:
        ccass_data = json.loads(args.ccass)
    
    result = combined_analysis(args.stock_code, events, ccass_data)
    print(json.dumps(result, ensure_ascii=False, indent=2))
